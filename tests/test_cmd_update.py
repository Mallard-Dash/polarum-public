from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from polarum import cli, cmd, cmd_update
from polarum.config import Node, Settings, UpdateSettings
from polarum.events import EventLog
from polarum.health import HealthResult
from polarum.preflight import Preflight
from polarum.updates import AptPlan, NodeReport, PackageChange, Removal

from tests.test_runner import FakeOps


def mknode(name: str, ip: str, order: int | None = None, canary: bool = False) -> Node:
    return Node(name=name, ip=ip, mac="AA:BB:CC:DD:EE:01", ssh_host=ip,
                update_order=order, canary=canary)


NODES = [mknode("node-a", "192.168.1.11", 2), mknode("node-b", "192.168.1.12", 1),
         mknode("node-c", "192.168.1.13")]
SETTINGS = Settings(relay="relay-host", update=UpdateSettings())


def plan(origin: str = "Debian", removals: int = 0) -> AptPlan:
    return AptPlan(
        installs=(PackageChange("pkg", "1.0", "2.0", (origin,)),),
        removals=tuple(Removal(f"gone{i}") for i in range(removals)))


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.events = EventLog.open(Path(self.tmp.name) / "events.log")
        self.out, self.err = io.StringIO(), io.StringIO()
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(contextlib.redirect_stdout(self.out))
        stack.enter_context(contextlib.redirect_stderr(self.err))
        stack.enter_context(mock.patch("sys.stdin.isatty", return_value=True))
        stack.enter_context(mock.patch(
            "polarum.cmd_update.PROGRESS_POLL", 0.01))
        self.health = HealthResult("ONLINE", 1.0)
        stack.enter_context(mock.patch(
            "polarum.cmd_update.check_node", side_effect=lambda n: self.health))
        self.plans: dict[str, AptPlan] = {}
        stack.enter_context(mock.patch(
            "polarum.cmd_update.check_node_updates",
            side_effect=lambda n: NodeReport(
                n.name, plan=self.plans.get(n.name, plan()),
                preflight=Preflight(quorate=True))))
        self.ops = FakeOps()

    def run_update(self, target, answer: str | None) -> int:
        for n in NODES:
            self.ops.rechecks[n.name] = NodeReport(
                n.name, plan=self.plans.get(n.name, plan()))
        with mock.patch("builtins.input", side_effect=[answer] if answer is not None
                        else EOFError):
            return cmd_update.cmd_update(SETTINGS, NODES, self.events, target, ops=self.ops)


class UpdateTests(Case):
    def test_single_node_by_name_after_typed_proceed(self) -> None:
        self.assertEqual(self.run_update("NODE-A", "proceed"), 0)
        self.assertEqual(self.ops.names("start_unit"), ["node-a"])
        self.assertIn("node-a", self.out.getvalue())

    def test_single_node_by_ip(self) -> None:
        self.assertEqual(self.run_update("192.168.1.13", "proceed"), 0)
        self.assertEqual(self.ops.names("start_unit"), ["node-c"])

    def test_all_runs_ordered_nodes_only(self) -> None:
        self.assertEqual(self.run_update(None, "proceed"), 0)
        self.assertEqual(self.ops.names("start_unit"), ["node-b", "node-a"])

    def test_wrong_phrase_installs_nothing(self) -> None:
        for answer in ("yes", "PROCEED", "override", "", None):
            with self.subTest(answer=answer):
                self.ops.calls.clear()
                self.assertEqual(self.run_update("node-a", answer), 1)
                self.assertEqual(self.ops.names("start_unit"), [])

    def test_blocked_review_needs_override(self) -> None:
        self.plans["node-a"] = plan(origin="Evil")
        self.assertEqual(self.run_update("node-a", "proceed"), 1)
        self.assertEqual(self.ops.names("start_unit"), [])
        self.assertEqual(self.run_update("node-a", "override"), 0)
        self.assertEqual(self.ops.names("start_unit"), ["node-a"])

    def test_requires_terminal(self) -> None:
        with mock.patch("sys.stdin.isatty", return_value=False):
            self.assertEqual(self.run_update("node-a", "proceed"), 1)
        self.assertEqual(self.ops.names("start_unit"), [])

    def test_node_not_online_refused(self) -> None:
        self.health = HealthResult("OFFLINE", None)
        self.assertEqual(self.run_update("node-a", "proceed"), 1)
        self.assertIn("not ONLINE", self.err.getvalue())

    def test_unknown_target(self) -> None:
        self.assertEqual(self.run_update("nope", "proceed"), 2)

    def test_nothing_to_update_does_not_ask(self) -> None:
        self.plans = {n.name: AptPlan() for n in NODES}
        self.assertEqual(self.run_update("node-a", None), 0)
        self.assertIn("Nothing to update", self.out.getvalue())

    def test_failure_exit_code_and_summary(self) -> None:
        self.ops.polls["node-b"] = ["done:100"]
        self.assertEqual(self.run_update(None, "proceed"), 1)
        text = self.out.getvalue()
        self.assertIn("FAILED", text)
        self.assertIn("SKIPPED", text)
        self.assertEqual(self.ops.names("start_unit"), ["node-b"])

    def test_check_failure_blocks_install(self) -> None:
        with mock.patch("polarum.cmd_update.check_node_updates",
                        return_value=NodeReport("node-a", error="boom")):
            self.assertEqual(self.run_update("node-a", "proceed"), 1)
        self.assertEqual(self.ops.names("start_unit"), [])


class StatusTests(Case):
    def test_status_one_node_by_ip_verbose(self) -> None:
        with mock.patch("polarum.cmd.check_node",
                        return_value=HealthResult("ONLINE", 2.0)), \
             mock.patch("polarum.cmd.check_port", side_effect=[1.5, None]):
            node = Node("node-a", "192.168.1.11", "AA:BB:CC:DD:EE:01", "192.168.1.11",
                        health_ports=(22, 8006))
            self.assertEqual(cmd.cmd_status(SETTINGS, [node], "192.168.1.11", True), 0)
        text = self.out.getvalue()
        self.assertIn("22 up (1.5 ms)", text)
        self.assertIn("8006 DOWN", text)
        self.assertIn("polarum@192.168.1.11", text)

    def test_status_unknown(self) -> None:
        self.assertEqual(cmd.cmd_status(SETTINGS, NODES, "nope"), 2)


class ParserTests(unittest.TestCase):
    def parse(self, *argv):
        return cli.parse_args(list(argv))

    def test_update_flags(self) -> None:
        a = self.parse("-u", "node-a")
        self.assertEqual((a.command, a.target), ("update", "node-a"))
        b = self.parse("-ua")
        self.assertEqual((b.command, b.target), ("update", None))

    def test_status_forms(self) -> None:
        a = self.parse("status", "-a")
        self.assertTrue(a.all)
        b = self.parse("status", "node-a", "-v")
        self.assertEqual((b.target, b.verbose), ("node-a", True))

    def test_conflicts(self) -> None:
        for argv in (["-u", "x", "-ua"], ["-ua", "status"], ["status", "-a", "x"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    self.parse(*argv)


if __name__ == "__main__":
    unittest.main()
