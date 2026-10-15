from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest import mock

from polarum.config import Node
from polarum.preflight import (
    BAD, INFO, NA, WARN, SCRIPT, Preflight, parse_preflight, parse_sections,
    quorum_lost_by_shutdown, report_lines, run_preflight, summary,
)

FIXTURES = Path(__file__).parent / "fixtures"
NODE = Node(name="node-a", ip="192.168.1.11", mac="AA:BB:CC:DD:EE:01", ssh_host="192.168.1.11")


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def levels(pf: Preflight) -> dict[str, str]:
    return {line.text.split(":")[0]: line.level for line in report_lines(pf)[1:]
            if ":" in line.text}


class ParseTests(unittest.TestCase):
    def test_busy_cluster(self) -> None:
        pf = parse_preflight(fixture("preflight_cluster_busy.txt"))
        self.assertEqual(pf.uptime, "up 3 weeks, 2 days, 4 hours")
        self.assertEqual(pf.running_vms, 2)
        self.assertEqual(pf.running_cts, 1)
        self.assertTrue(pf.backup_running)
        self.assertEqual(pf.zfs_activity, ("scrub",))
        self.assertEqual((pf.quorate, pf.total_votes, pf.quorum_needed), (True, 3, 2))
        self.assertFalse(quorum_lost_by_shutdown(pf))

    def test_plain_debian_degrades_to_na(self) -> None:
        pf = parse_preflight(fixture("preflight_plain_debian.txt"))
        self.assertEqual(pf.uptime, "up 2 days, 1 hour")
        self.assertIsNone(pf.running_vms)
        self.assertIsNone(pf.running_cts)
        self.assertFalse(pf.backup_running)
        self.assertIsNone(pf.zfs_activity)
        self.assertIsNone(pf.quorate)
        self.assertEqual(sum(1 for ln in report_lines(pf) if ln.level == NA), 4)

    def test_quiet_two_node_cluster(self) -> None:
        pf = parse_preflight(fixture("preflight_two_node_quiet.txt"))
        self.assertEqual(pf.running_vms, 0)
        self.assertIsNone(pf.running_cts)      # sudo refused -> n/a, not 0
        self.assertEqual(pf.zfs_activity, ())
        self.assertTrue(quorum_lost_by_shutdown(pf))   # 2 votes, quorum 2

    def test_garbage_and_empty_input(self) -> None:
        self.assertEqual(parse_sections("no headers here"), {})
        pf = parse_preflight("")
        self.assertIsNone(pf.uptime)
        self.assertIsNone(pf.backup_running)

    def test_lost_quorum_parse(self) -> None:
        text = "@@pvecm rc=0\nQuorate:          No\nTotal votes:      1\nQuorum:           2\n"
        pf = parse_preflight(text)
        self.assertIs(pf.quorate, False)


class ReportTests(unittest.TestCase):
    def test_levels(self) -> None:
        lv = levels(parse_preflight(fixture("preflight_cluster_busy.txt")))
        self.assertEqual(lv["VMs running"], WARN)
        self.assertEqual(lv["Containers running"], WARN)
        self.assertEqual(lv["Backup (vzdump)"], BAD)
        self.assertEqual(lv["ZFS scrub/resilver"], BAD)
        self.assertEqual(lv["Cluster quorum"], INFO)

    def test_shutdown_quorum_warning(self) -> None:
        lines = report_lines(parse_preflight(fixture("preflight_two_node_quiet.txt")))
        self.assertTrue(any(ln.level == BAD and "below quorum" in ln.text for ln in lines))

    def test_error_report_and_summary(self) -> None:
        pf = Preflight(error="Connection refused")
        self.assertEqual(report_lines(pf)[0].level, BAD)
        self.assertIn("failed", summary(pf))
        self.assertIn("backup=True", summary(parse_preflight(fixture("preflight_cluster_busy.txt"))))


class RunTests(unittest.TestCase):
    def test_run_preflight_sends_script_on_stdin(self) -> None:
        done = subprocess.CompletedProcess([], 0, fixture("preflight_plain_debian.txt"), "")
        with mock.patch("subprocess.run", return_value=done) as run:
            pf = run_preflight(NODE)
        self.assertEqual(pf.running_vms, None)
        self.assertEqual(run.call_args.kwargs["input"], SCRIPT)
        self.assertEqual(run.call_args.args[0][-2:], ["sh", "-s"])
        self.assertIn("BatchMode=yes", run.call_args.args[0])

    def test_ssh_failure_is_reported(self) -> None:
        failed = subprocess.CompletedProcess([], 255, "", "Permission denied (publickey).")
        with mock.patch("subprocess.run", return_value=failed):
            pf = run_preflight(NODE)
        self.assertIn("ssh-add", pf.error)

    def test_script_is_read_only(self) -> None:
        for forbidden in ("rm ", "reboot", "poweroff", "shutdown", " > ", "dd ", "kill"):
            self.assertNotIn(forbidden, SCRIPT)


if __name__ == "__main__":
    unittest.main()
