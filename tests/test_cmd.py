from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from polarum import cli, cmd
from polarum.actions import ActionResult
from polarum.config import Node, Settings, UpdateSettings
from polarum.events import EventLog
from polarum.health import HealthResult
from polarum.preflight import Preflight
from polarum.updates import AptPlan, NodeReport, PackageChange


def mknode(name: str, order: int | None = None, canary: bool = False) -> Node:
    return Node(name=name, ip="192.168.1.11", mac="AA:BB:CC:DD:EE:01",
                ssh_host="192.168.1.11", update_order=order, canary=canary)


NODES = [mknode("node-a", 2), mknode("node-b", 1), mknode("node-c")]
SETTINGS = Settings(relay="relay-host", update=UpdateSettings())


class CmdCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.events = EventLog.open(Path(self.tmp.name) / "events.log")
        self.out = io.StringIO()
        self.err = io.StringIO()
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(contextlib.redirect_stdout(self.out))
        stack.enter_context(contextlib.redirect_stderr(self.err))

    def tty(self, value: bool):
        return mock.patch("sys.stdin.isatty", return_value=value)

    def log(self) -> str:
        return self.events.path.read_text()


class StatusTests(CmdCase):
    def test_status_table(self) -> None:
        results = {"node-a": HealthResult("ONLINE", 1.5), "node-b": HealthResult("OFFLINE", None),
                   "node-c": HealthResult("BOOTING", 2.0)}
        with mock.patch("polarum.cmd.check_node", side_effect=lambda n: results[n.name]):
            self.assertEqual(cmd.cmd_status(SETTINGS, NODES), 0)
        text = self.out.getvalue()
        self.assertIn("node-a", text)
        self.assertIn("ONLINE", text)
        self.assertIn("1.5 ms", text)
        self.assertIn("OFFLINE", text)


class WakeTests(CmdCase):
    def wake(self, name="node-b", yes=False, health="OFFLINE", answer="y", tty=True):
        with self.tty(tty), \
                mock.patch("polarum.cmd.check_node", return_value=HealthResult(health, None)), \
                mock.patch("builtins.input", return_value=answer) as ask, \
                mock.patch("polarum.cmd.run_action",
                           return_value=ActionResult(True, "magic packet sent")) as act:
            code = cmd.cmd_wake(SETTINGS, NODES, self.events, name, yes)
        return code, ask, act

    def test_asks_for_confirmation(self) -> None:
        code, ask, act = self.wake(answer="y")
        self.assertEqual(code, 0)
        ask.assert_called_once()
        act.assert_called_once()
        self.assertIn("ok: magic packet sent", self.log())

    def test_declined_sends_nothing(self) -> None:
        code, _, act = self.wake(answer="n")
        self.assertEqual(code, 1)
        act.assert_not_called()

    def test_yes_skips_question(self) -> None:
        code, ask, act = self.wake(yes=True, tty=False)
        self.assertEqual(code, 0)
        ask.assert_not_called()
        act.assert_called_once()

    def test_no_tty_without_yes_refuses(self) -> None:
        code, ask, act = self.wake(tty=False)
        self.assertEqual(code, 1)
        act.assert_not_called()
        self.assertIn("--yes", self.err.getvalue())

    def test_already_running_is_a_noop(self) -> None:
        code, _, act = self.wake(health="ONLINE", yes=True)
        self.assertEqual(code, 0)
        act.assert_not_called()

    def test_unknown_node_and_failure(self) -> None:
        code, _, act = self.wake(name="nope", yes=True)
        self.assertEqual(code, 2)
        act.assert_not_called()
        with self.tty(True), mock.patch("polarum.cmd.check_node", return_value=HealthResult("OFFLINE", None)), \
                mock.patch("polarum.cmd.run_action", return_value=ActionResult(False, "no key")):
            self.assertEqual(cmd.cmd_wake(SETTINGS, NODES, self.events, "NODE-B", True), 1)
        self.assertIn("FAILED: no key", self.log())


class OffTests(CmdCase):
    def off(self, answer="proceed", health="ONLINE", tty=True, pf=None):
        pf = pf or Preflight(uptime="up 1 day", running_vms=2)
        with self.tty(tty), \
                mock.patch("polarum.cmd.check_node", return_value=HealthResult(health, None)), \
                mock.patch("polarum.cmd.run_preflight", return_value=pf) as pre, \
                mock.patch("builtins.input", return_value=answer), \
                mock.patch("polarum.cmd.run_action",
                           return_value=ActionResult(True, "poweroff command accepted")) as act:
            code = cmd.cmd_off(SETTINGS, NODES, self.events, "node-a")
        return code, pre, act

    def test_typed_proceed_required(self) -> None:
        code, pre, act = self.off()
        self.assertEqual(code, 0)
        act.assert_called_once_with(SETTINGS, NODES[0], "poweroff")
        self.assertIn("VMs running", self.out.getvalue())     # pre-flight is shown first
        self.assertIn("poweroff | started (cli)", self.log())

    def test_anything_else_cancels(self) -> None:
        for answer in ("y", "yes", "PROCEED", "proceed ", ""):
            code, _, act = self.off(answer=answer)
            self.assertEqual(code, 1, answer)
            act.assert_not_called()

    def test_no_terminal_refuses_before_any_ssh(self) -> None:
        code, pre, act = self.off(tty=False)
        self.assertEqual(code, 1)
        pre.assert_not_called()
        act.assert_not_called()

    def test_unreachable_node_refuses(self) -> None:
        code, pre, act = self.off(health="OFFLINE")
        self.assertEqual(code, 1)
        act.assert_not_called()

    def test_preflight_failure_fails_closed(self) -> None:
        code, _, act = self.off(pf=Preflight(error="Connection refused"))
        self.assertEqual(code, 1)
        act.assert_not_called()

    def test_eof_at_prompt_cancels(self) -> None:
        with self.tty(True), \
                mock.patch("polarum.cmd.check_node", return_value=HealthResult("ONLINE", None)), \
                mock.patch("polarum.cmd.run_preflight", return_value=Preflight()), \
                mock.patch("builtins.input", side_effect=EOFError), \
                mock.patch("polarum.cmd.run_action") as act:
            self.assertEqual(cmd.cmd_off(SETTINGS, NODES, self.events, "node-a"), 1)
        act.assert_not_called()


class CheckTests(CmdCase):
    def reports(self, mapping):
        return mock.patch("polarum.cmd.check_node_updates",
                          side_effect=lambda n: mapping[n.name])

    def test_check_all_uses_update_order_and_skips_others(self) -> None:
        def clean(n):
            return NodeReport(n, plan=AptPlan(), preflight=Preflight(quorate=True))

        seen: list[str] = []

        def fake(node):
            seen.append(node.name)
            return clean(node.name)
        with mock.patch("polarum.cmd.check_node_updates", side_effect=fake):
            code = cmd.cmd_check(SETTINGS, NODES, self.events, "all")
        self.assertEqual(code, 0)
        self.assertEqual(sorted(seen), ["node-a", "node-b"])        # node-c has no update_order
        self.assertLess(self.out.getvalue().index("node-b"), self.out.getvalue().index("== node-a"))
        self.assertIn("Nothing was installed", self.out.getvalue())

    def test_blocked_and_error_exit_codes(self) -> None:
        foreign = NodeReport("node-b", plan=AptPlan(installs=(
            PackageChange("x", "1", "2", ("Weird",)),)))
        with self.reports({"node-b": foreign}):
            self.assertEqual(cmd.cmd_check(SETTINGS, NODES, self.events, "node-b"), 3)
        with self.reports({"node-b": NodeReport("node-b", error="boom")}):
            self.assertEqual(cmd.cmd_check(SETTINGS, NODES, self.events, "node-b"), 1)
        self.assertIn("update-check | FAILED: boom", self.log())

    def test_unknown_target_and_no_update_order(self) -> None:
        self.assertEqual(cmd.cmd_check(SETTINGS, NODES, self.events, "nope"), 2)
        self.assertEqual(cmd.cmd_check(SETTINGS, [mknode("only")], self.events, "all"), 1)

    def test_no_install_path_exists(self) -> None:
        import inspect
        source = inspect.getsource(cmd)
        for word in ("UpdateRun", "start_unit", "dist-upgrade -y"):
            self.assertNotIn(word, source)


class ParserTests(unittest.TestCase):
    def test_yes_only_exists_on_wake(self) -> None:
        self.assertTrue(cli.parse_args(["wake", "node-a", "--yes"]).yes)
        self.assertTrue(cli.parse_args(["wake", "-y", "node-a"]).yes)
        for argv in (["off", "node-a", "--yes"], ["check", "all", "--yes"], ["status", "--yes"],
                     ["--yes", "off", "node-a"]):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
                cli.parse_args(argv)
            self.assertEqual(ctx.exception.code, 2)

    def test_no_command_means_tui(self) -> None:
        self.assertIsNone(cli.parse_args([]).command)
        self.assertEqual(cli.parse_args(["check", "all"]).target, "all")


if __name__ == "__main__":
    unittest.main()
