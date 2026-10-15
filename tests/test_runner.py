from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from polarum import runner
from polarum.actions import ActionResult
from polarum.config import Node, UpdateSettings
from polarum.events import EventLog
from polarum.health import HealthResult
from polarum.preflight import Preflight
from polarum.runner import (
    FAILED, OK, SKIPPED, RemoteOps, UpdateRun, interpret_poll, parse_root_dataset,
    snapshot_name, start_script,
)
from polarum.ssh import SshResult
from polarum.state import OFFLINE, ONLINE
from polarum.updates import AptPlan, NodeReport, PackageChange

TOKEN = "0123456789ab"


def node(name: str, order: int, canary: bool = False) -> Node:
    return Node(name=name, ip="192.168.1.11", mac="AA:BB:CC:DD:EE:01",
                ssh_host="192.168.1.11", update_order=order, canary=canary)


def plan(count: int = 2) -> AptPlan:
    return AptPlan(installs=tuple(
        PackageChange(f"pkg{i}", "1.0", "2.0", ("Debian",)) for i in range(count)))


def poll_text(token: str, state: str, status: str = "", log: str = "line") -> str:
    return f"@@state rc=0\n{state}\n@@status rc=0\n{status}\n@@log rc=0\n{log}\n"


class FakeOps(RemoteOps):
    """Scriptable stand-in for RemoteOps with a fake clock; no network at all."""

    def __init__(self) -> None:
        self.clock = 0.0
        self.calls: list[tuple[str, str]] = []
        self.preflights: dict[str, Preflight] = {}
        self.dataset: tuple[str | None, str] = ("rpool/ROOT/pve-1", "")
        self.snapshot_result = SshResult(0)
        self.start_result = SshResult(0)
        self.polls: dict[str, list[str]] = {}       # per node: running | done:N | ended | down
        self.health_up = True
        self.health_fn = None
        self.reboot_result = ActionResult(True, "ok")
        self.reboot_at: float | None = None
        self.post_report = NodeReport("x", reboot_needed=False)
        self.tokens: dict[str, str] = {}
        self.rechecks: dict[str, NodeReport] = {}

    def _c(self, what: str, node: Node) -> None:
        self.calls.append((what, node.name))

    def preflight(self, node):
        self._c("preflight", node)
        return self.preflights.get(node.name, Preflight(quorate=True))

    def root_dataset(self, node):
        self._c("root_dataset", node)
        return self.dataset

    def snapshot(self, node, dataset, name):
        self._c("snapshot", node)
        return self.snapshot_result

    def start_unit(self, node, token):
        self._c("start_unit", node)
        self.tokens[node.name] = token
        return self.start_result

    def poll_unit(self, node):
        self._c("poll", node)
        queue = self.polls.setdefault(node.name, ["running", "done:0"])
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        token = self.tokens[node.name]
        if item == "down":
            return SshResult(255, stderr="Connection timed out")
        if item == "running":
            return SshResult(0, poll_text(token, "active"))
        if item == "ended":
            return SshResult(0, poll_text(token, "inactive"))
        rc = item.split(":")[1]
        return SshResult(0, poll_text(token, "inactive", f"{token} {rc}", "E: boom" if rc != "0" else "done"))

    def health(self, node):
        self._c("health", node)
        if self.health_fn:
            return HealthResult(self.health_fn(self), None)
        return HealthResult(ONLINE if self.health_up else OFFLINE, None)

    def reboot(self, node):
        self._c("reboot", node)
        self.reboot_at = self.clock
        return self.reboot_result

    def post_check(self, node):
        self._c("post_check", node)
        return self.post_report

    def recheck_plan(self, node):
        self._c("recheck_plan", node)
        return self.rechecks[node.name]

    def sleep(self, seconds):
        self.clock += seconds

    def now(self):
        return self.clock

    def names(self, what: str) -> list[str]:
        return [n for w, n in self.calls if w == what]


class RunnerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.events = EventLog.open(Path(self.tmp.name) / "events.log")
        self.ops = FakeOps()

    def make(self, nodes, plans=None, settings=None, **kw) -> UpdateRun:
        plans = plans or {}
        reports = [NodeReport(n.name, plan=plans.get(n.name, plan()),
                              preflight=Preflight(quorate=True)) for n in nodes]
        for rep in reports:
            self.ops.rechecks.setdefault(rep.node_name, NodeReport(rep.node_name, plan=rep.plan))
        return UpdateRun(settings or UpdateSettings(), reports, nodes, self.events,
                         ops=self.ops, **kw)

    def statuses(self, run: UpdateRun) -> dict[str, str]:
        return {r.name: r.status for r in run.snapshot().results}

    def log_text(self) -> str:
        return self.events.path.read_text()


class HappyPathTests(RunnerTestCase):
    def test_canary_first_manual_reboot_policy(self) -> None:
        nodes = [node("node-b", 2), node("node-c", 3), node("node-a", 9, canary=True)]
        self.ops.post_report = NodeReport("x", reboot_needed=True)
        run = self.make(nodes)
        run.run()
        self.assertEqual([n.name for n in run.order], ["node-a", "node-b", "node-c"])
        self.assertEqual(self.statuses(run), {"node-a": OK, "node-b": OK, "node-c": OK})
        self.assertEqual(self.ops.names("start_unit"), ["node-a", "node-b", "node-c"])
        self.assertEqual(self.ops.names("snapshot"), ["node-a", "node-b", "node-c"])
        self.assertEqual(self.ops.names("reboot"), [])           # manual: never reboots
        result = run.snapshot().results[0]
        self.assertTrue(result.reboot_needed)
        self.assertEqual(len(result.packages), 2)
        self.assertTrue(run.snapshot().finished)
        self.assertIsNone(run.snapshot().error)
        log = self.log_text()
        self.assertIn("packages: pkg0 1.0>2.0, pkg1 1.0>2.0", log)
        self.assertIn("REBOOT NEEDED", log)
        self.assertIn("snapshot created: rpool/ROOT/pve-1@polarum-pre-upgrade-", log)

    def test_changed_plan_aborts_before_touching_node(self) -> None:
        run = self.make([node("node-a", 1), node("node-b", 2)])
        self.ops.rechecks["node-a"] = NodeReport("node-a", plan=plan(3))
        run.run()
        self.assertEqual(self.statuses(run), {"node-a": FAILED, "node-b": SKIPPED})
        self.assertIn("plan changed", run.snapshot().results[0].error)
        for step in ("snapshot", "start_unit"):
            self.assertEqual(self.ops.names(step), [])

    def test_recheck_error_aborts(self) -> None:
        run = self.make([node("node-a", 1)])
        self.ops.rechecks["node-a"] = NodeReport("node-a", error="boom")
        run.run()
        self.assertEqual(self.statuses(run), {"node-a": FAILED})
        self.assertEqual(self.ops.names("start_unit"), [])

    def test_one_node_at_a_time(self) -> None:
        run = self.make([node("node-a", 1), node("node-b", 2)])
        run.run()
        seq = [n for w, n in self.ops.calls if w == "start_unit"]
        first_b = next(i for i, (w, n) in enumerate(self.ops.calls) if n == "node-b")
        last_a = max(i for i, (w, n) in enumerate(self.ops.calls) if n == "node-a")
        self.assertLess(last_a, first_b)
        self.assertEqual(seq, ["node-a", "node-b"])

    def test_nothing_to_update_touches_nothing(self) -> None:
        run = self.make([node("node-a", 1)], plans={"node-a": AptPlan()})
        run.run()
        self.assertEqual(self.statuses(run), {"node-a": OK})
        self.assertEqual(self.ops.calls, [])

    def test_no_reboot_needed(self) -> None:
        run = self.make([node("node-a", 1)])
        run.run()
        self.assertFalse(run.snapshot().results[0].reboot_needed)


class FailureTests(RunnerTestCase):
    def test_failure_stops_and_skips_rest(self) -> None:
        self.ops.polls["node-a"] = ["running", "done:100"]
        run = self.make([node("node-a", 1, canary=True), node("node-b", 2), node("node-c", 3)])
        run.run()
        self.assertEqual(self.statuses(run),
                         {"node-a": FAILED, "node-b": SKIPPED, "node-c": SKIPPED})
        self.assertEqual(self.ops.names("start_unit"), ["node-a"])
        self.assertIn("status 100", run.snapshot().results[0].error)
        self.assertIn("remaining nodes skipped", run.snapshot().error)
        self.assertIn("SKIPPED", self.log_text())
        self.assertIn("apt output tail", self.log_text())

    def test_second_node_failure_keeps_first_ok(self) -> None:
        self.ops.polls["node-b"] = ["done:1"]
        run = self.make([node("node-a", 1), node("node-b", 2), node("node-c", 3)])
        run.run()
        self.assertEqual(self.statuses(run),
                         {"node-a": OK, "node-b": FAILED, "node-c": SKIPPED})

    def test_health_timeout_stops_run(self) -> None:
        self.ops.health_up = False
        run = self.make([node("node-a", 1), node("node-b", 2)],
                        settings=UpdateSettings(health_timeout=30))
        run.run()
        self.assertEqual(self.statuses(run), {"node-a": FAILED, "node-b": SKIPPED})
        self.assertIn("health check failed", run.snapshot().results[0].error)
        self.assertGreaterEqual(self.ops.clock, 30)

    def test_start_failure(self) -> None:
        self.ops.start_result = SshResult(255, stderr="Permission denied (publickey).")
        run = self.make([node("node-a", 1)])
        run.run()
        self.assertIn("ssh-add", run.snapshot().results[0].error)

    def test_unit_already_running_refuses(self) -> None:
        self.ops.start_result = SshResult(runner.ALREADY_RUNNING_RC, stderr="already")
        run = self.make([node("node-a", 1)])
        run.run()
        self.assertIn("already running", run.snapshot().results[0].error)

    def test_unit_ended_without_status_fails_after_two_polls(self) -> None:
        self.ops.polls["node-a"] = ["running", "ended"]
        run = self.make([node("node-a", 1)])
        run.run()
        self.assertEqual(self.statuses(run), {"node-a": FAILED})
        self.assertIn("without reporting", run.snapshot().results[0].error)

    def test_dropped_connection_is_not_treated_as_failure(self) -> None:
        self.ops.polls["node-a"] = ["running", "down", "down", "running", "done:0"]
        run = self.make([node("node-a", 1)])
        run.run()
        self.assertEqual(self.statuses(run), {"node-a": OK})

    def test_prolonged_loss_of_contact_fails(self) -> None:
        self.ops.polls["node-a"] = ["down"]
        run = self.make([node("node-a", 1)])
        run.run()
        self.assertEqual(self.statuses(run), {"node-a": FAILED})
        self.assertIn("lost contact", run.snapshot().results[0].error)

    def test_quorum_lost_stops_before_touching_the_node(self) -> None:
        self.ops.preflights["node-a"] = Preflight(quorate=False)
        run = self.make([node("node-a", 1), node("node-b", 2)])
        run.run()
        self.assertEqual(self.statuses(run), {"node-a": FAILED, "node-b": SKIPPED})
        self.assertEqual(self.ops.names("start_unit"), [])
        self.assertEqual(self.ops.names("snapshot"), [])

    def test_quorum_override_allows_run(self) -> None:
        self.ops.preflights["node-a"] = Preflight(quorate=False)
        run = self.make([node("node-a", 1)], quorum_override=True)
        run.run()
        self.assertEqual(self.statuses(run), {"node-a": OK})

    def test_preflight_ssh_error_stops(self) -> None:
        self.ops.preflights["node-a"] = Preflight(error="Connection refused")
        run = self.make([node("node-a", 1)])
        run.run()
        self.assertEqual(self.statuses(run), {"node-a": FAILED})

    def test_unexpected_exception_fails_closed(self) -> None:
        def boom(*a):
            raise RuntimeError("kaboom")
        self.ops.start_unit = boom
        run = self.make([node("node-a", 1), node("node-b", 2)])
        run.run()
        self.assertEqual(self.statuses(run), {"node-a": FAILED, "node-b": SKIPPED})
        self.assertIn("internal error", run.snapshot().results[0].error)


class SnapshotTests(RunnerTestCase):
    def test_not_zfs_continues_with_notice(self) -> None:
        self.ops.dataset = (None, "root filesystem is not ZFS (ext4 /dev/sda1)")
        run = self.make([node("node-a", 1)])
        run.run()
        self.assertEqual(self.statuses(run), {"node-a": OK})
        self.assertIn("NOTICE: no snapshot taken", self.log_text())
        self.assertEqual(self.ops.names("snapshot"), [])

    def test_snapshot_failure_continues_unless_required(self) -> None:
        self.ops.snapshot_result = SshResult(1, stderr="sudo: a password is required")
        run = self.make([node("node-a", 1)])
        run.run()
        self.assertEqual(self.statuses(run), {"node-a": OK})
        self.assertIn("NOTICE", self.log_text())

    def test_require_snapshot_stops(self) -> None:
        self.ops.dataset = (None, "root filesystem is not ZFS")
        run = self.make([node("node-a", 1), node("node-b", 2)],
                        settings=UpdateSettings(require_snapshot=True))
        run.run()
        self.assertEqual(self.statuses(run), {"node-a": FAILED, "node-b": SKIPPED})
        self.assertEqual(self.ops.names("start_unit"), [])

    def test_snapshot_disabled(self) -> None:
        run = self.make([node("node-a", 1)], settings=UpdateSettings(snapshot_before=False))
        run.run()
        self.assertEqual(self.ops.calls and self.ops.names("root_dataset"), [])

    def test_helpers(self) -> None:
        self.assertEqual(parse_root_dataset("zfs rpool/ROOT/pve-1\n"), "rpool/ROOT/pve-1")
        self.assertIsNone(parse_root_dataset("ext4 /dev/mapper/pve-root"))
        self.assertIsNone(parse_root_dataset("zfs bad;name"))
        self.assertIsNone(parse_root_dataset(""))
        self.assertEqual(snapshot_name(datetime(2026, 1, 2, 3, 4, 5)),
                         "polarum-pre-upgrade-20260102-030405")


class RebootPolicyTests(RunnerTestCase):
    def reboot_settings(self, policy: str) -> UpdateSettings:
        return UpdateSettings(reboot_policy=policy, health_timeout=120)

    def test_if_needed_reboots_waits_down_then_up(self) -> None:
        self.ops.post_report = NodeReport("x", reboot_needed=True)

        def health(ops: FakeOps) -> str:
            if ops.reboot_at is not None and ops.clock < ops.reboot_at + 10:
                return OFFLINE if ops.clock >= ops.reboot_at + 2 else ONLINE
            return ONLINE
        self.ops.health_fn = health
        run = self.make([node("node-a", 1), node("node-b", 2)],
                        settings=self.reboot_settings("if_needed"))
        run.run()
        self.assertEqual(self.ops.names("reboot"), ["node-a", "node-b"])
        res = run.snapshot().results[0]
        self.assertEqual(res.status, OK)
        self.assertFalse(res.reboot_needed)
        self.assertIn("rebooted and healthy", self.log_text())

    def test_if_needed_refuses_reboot_during_backup(self) -> None:
        self.ops.post_report = NodeReport("x", reboot_needed=True)
        self.ops.preflights["node-a"] = Preflight(quorate=True, backup_running=True)
        run = self.make([node("node-a", 1)], settings=UpdateSettings(reboot_policy="if_needed"))
        run.run()
        self.assertEqual(self.statuses(run), {"node-a": FAILED})
        self.assertEqual(self.ops.names("reboot"), [])
        self.assertTrue(run.snapshot().results[0].reboot_needed)

    def test_if_needed_without_need_does_not_reboot(self) -> None:
        run = self.make([node("node-a", 1)], settings=self.reboot_settings("if_needed"))
        run.run()
        self.assertEqual(self.ops.names("reboot"), [])

    def test_node_never_goes_down_fails(self) -> None:
        self.ops.post_report = NodeReport("x", reboot_needed=True)
        run = self.make([node("node-a", 1), node("node-b", 2)],
                        settings=self.reboot_settings("if_needed"))
        run.run()
        self.assertEqual(self.statuses(run), {"node-a": FAILED, "node-b": SKIPPED})
        self.assertIn("did not go down", run.snapshot().results[0].error)

    def test_reboot_command_failure_stops(self) -> None:
        self.ops.post_report = NodeReport("x", reboot_needed=True)
        self.ops.reboot_result = ActionResult(False, "sudo needs a password")
        run = self.make([node("node-a", 1)], settings=self.reboot_settings("if_needed"))
        run.run()
        self.assertEqual(self.statuses(run), {"node-a": FAILED})

    def test_never_ignores_reboot(self) -> None:
        self.ops.post_report = NodeReport("x", reboot_needed=True)
        run = self.make([node("node-a", 1)], settings=self.reboot_settings("never"))
        run.run()
        self.assertEqual(self.ops.names("reboot"), [])
        self.assertFalse(run.snapshot().results[0].reboot_needed)

    def test_failed_post_check_assumes_reboot_needed(self) -> None:
        self.ops.post_report = NodeReport("x", error="ssh broke")
        run = self.make([node("node-a", 1)])
        run.run()
        self.assertTrue(run.snapshot().results[0].reboot_needed)
        self.assertIn("assuming a reboot is needed", self.log_text())


class ControlTests(RunnerTestCase):
    def test_stop_request_skips_remaining_nodes(self) -> None:
        run = self.make([node("node-a", 1), node("node-b", 2)])
        run.request_stop()
        run.run()
        self.assertEqual(self.statuses(run), {"node-a": SKIPPED, "node-b": SKIPPED})
        self.assertEqual(self.ops.calls, [])
        self.assertIn("stopped by user", run.snapshot().error)

    def test_abandon_stops_waiting(self) -> None:
        run = self.make([node("node-a", 1)])
        run.abandon()
        run.run()
        self.assertEqual(self.statuses(run), {"node-a": FAILED})
        self.assertIn("keeps running", run.snapshot().results[0].error)


class PollTests(unittest.TestCase):
    def test_done_requires_matching_token(self) -> None:
        done = interpret_poll(TOKEN, poll_text(TOKEN, "inactive", f"{TOKEN} 0"))
        self.assertEqual((done.kind, done.exit_code), (runner.POLL_DONE, 0))
        failed = interpret_poll(TOKEN, poll_text(TOKEN, "inactive", f"{TOKEN} 100"))
        self.assertEqual(failed.exit_code, 100)
        stale = interpret_poll(TOKEN, poll_text(TOKEN, "active", "ffffffffffff 0"))
        self.assertEqual(stale.kind, runner.POLL_RUNNING)       # old run's file is ignored
        stale_ended = interpret_poll(TOKEN, poll_text(TOKEN, "inactive", "ffffffffffff 0"))
        self.assertEqual(stale_ended.kind, runner.POLL_ENDED)

    def test_states(self) -> None:
        for state in ("active", "activating"):
            self.assertEqual(interpret_poll(TOKEN, poll_text(TOKEN, state)).kind, runner.POLL_RUNNING)
        for state in ("inactive", "failed", "unknown", ""):
            self.assertEqual(interpret_poll(TOKEN, poll_text(TOKEN, state)).kind, runner.POLL_ENDED)
        self.assertEqual(interpret_poll(TOKEN, "").kind, runner.POLL_ENDED)

    def test_tail_is_captured(self) -> None:
        poll = interpret_poll(TOKEN, poll_text(TOKEN, "active", log="a\nb\nc"))
        self.assertEqual(poll.tail, ("a", "b", "c"))


class ScriptTests(unittest.TestCase):
    def test_start_script_contents(self) -> None:
        script = start_script(TOKEN)
        self.assertIn("systemd-run --unit=polarum-upgrade --collect", script)
        self.assertIn("DEBIAN_FRONTEND=noninteractive apt-get dist-upgrade -y", script)
        self.assertIn("Dpkg::Options::=--force-confold", script)
        self.assertIn("Dpkg::Options::=--force-confdef", script)
        self.assertIn("is-active --quiet polarum-upgrade", script)   # refuses a second run
        self.assertNotIn("apt-get update", script)                   # never changes the reviewed plan
        self.assertIn(TOKEN, script)

    def test_invalid_token_rejected(self) -> None:
        with self.assertRaises(ValueError):
            start_script("x; reboot")

    def test_script_survives_shell_parsing(self) -> None:
        import shlex
        line = [ln for ln in start_script(TOKEN).splitlines() if "systemd-run" in ln][0]
        words = shlex.split(line)
        self.assertEqual(words[-1], TOKEN)
        self.assertIn("apt-get dist-upgrade", words[-3])


if __name__ == "__main__":
    unittest.main()
