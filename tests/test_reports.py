from __future__ import annotations

import unittest

from polarum.config import UpdateSettings
from polarum.preflight import BAD, INFO, WARN, Preflight
from polarum.reports import node_detail_lines, table_row, verdict_banner
from polarum.updates import AptPlan, NodeReport, PackageChange, Removal, evaluate

SETTINGS = UpdateSettings()


def rep(**kw) -> NodeReport:
    plan = AptPlan(
        installs=(PackageChange("libc6", "1", "2", ("Debian",)),
                  PackageChange("docker-ce", "1", "2", ("Docker",)),
                  PackageChange("newpkg", None, "1", ("Debian",))),
        removals=(Removal("old"),))
    return NodeReport("node-a", plan=plan, preflight=Preflight(quorate=True, running_vms=2), **kw)


class ContentTests(unittest.TestCase):
    def test_detail_lines(self) -> None:
        lines = node_detail_lines(rep(reboot_needed=True, running_kernel="6.1.0-17-amd64",
                                      newest_kernel="6.1.0-18-amd64"), SETTINGS)
        text = "\n".join(line.text for line in lines)
        self.assertIn("Upgrades: 2   New: 1   Removed: 1", text)
        self.assertIn("6.1.0-17-amd64 -> 6.1.0-18-amd64", text)
        self.assertIn("Docker", text)
        self.assertIn("NOT ALLOWED", text)
        self.assertIn("BLOCK:", text)
        levels = {line.text.split()[0]: line.level for line in lines if line.text.startswith("BLOCK")}
        self.assertEqual(levels["BLOCK:"], BAD)

    def test_error_report(self) -> None:
        lines = node_detail_lines(NodeReport("node-a", error="boom"), SETTINGS)
        self.assertEqual((lines[0].level, "boom" in lines[0].text), (BAD, True))
        self.assertEqual(table_row(NodeReport("node-a", error="boom"), SETTINGS).level, BAD)

    def test_table_row_and_banner(self) -> None:
        self.assertEqual(table_row(rep(), SETTINGS).level, BAD)
        clean = NodeReport("node-a", plan=AptPlan(), preflight=Preflight(quorate=True))
        self.assertEqual(table_row(clean, SETTINGS).level, INFO)
        self.assertIn("override", verdict_banner(evaluate([rep()], SETTINGS)).text)
        self.assertEqual(verdict_banner(evaluate([clean], SETTINGS)).level, INFO)
        warn = NodeReport("node-a", plan=AptPlan(), reboot_needed=True)
        self.assertEqual(verdict_banner(evaluate([warn], SETTINGS)).level, WARN)
        failed = evaluate([NodeReport("node-a", error="x")], SETTINGS)
        self.assertIn("disabled", verdict_banner(failed).text)


if __name__ == "__main__":
    unittest.main()
