from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest import mock

from polarum.config import Node, UpdateSettings
from polarum.preflight import Preflight
from polarum.updates import (
    AptPlan, NodeReport, PackageChange, apply_order, check_node_updates, check_script,
    chunk_packages, detect_reboot, evaluate, evaluate_report, origin_counts,
    parse_apt_simulation, parse_check, parse_origins, version_key,
)

FIXTURES = Path(__file__).parent / "fixtures"
SETTINGS = UpdateSettings()


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def node(name: str, order: int | None = None, canary: bool = False) -> Node:
    return Node(name=name, ip="192.168.1.11", mac="AA:BB:CC:DD:EE:01",
                ssh_host="192.168.1.11", update_order=order, canary=canary)


def pkg(name: str, old: str | None, origins: tuple[str, ...] = ("Debian",)) -> PackageChange:
    return PackageChange(name, old, "2.0", origins)


def report(plan: AptPlan, **kw) -> NodeReport:
    return NodeReport("node-a", plan=plan, **kw)


class AptParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = parse_apt_simulation(fixture("apt_sim_mixed.txt"))

    def test_inst_lines(self) -> None:
        names = [p.name for p in self.plan.installs]
        self.assertEqual(names, ["libc6", "proxmox-kernel-6.8.12-4-pve-signed",
                                 "pve-manager", "docker-ce"])
        libc = self.plan.installs[0]
        self.assertEqual((libc.old, libc.new, libc.arch), ("2.36-9", "2.36-9+deb12u4", "amd64"))
        self.assertEqual(libc.origins, ("Debian-Security", "Debian"))
        self.assertEqual(self.plan.installs[2].origins, ("Proxmox",))  # trailing [] ignored
        self.assertEqual(self.plan.installs[3].old, "5:24.0.0-1")      # epoch versions

    def test_new_vs_upgrade(self) -> None:
        self.assertEqual(len(self.plan.new_packages), 1)
        self.assertEqual(len(self.plan.upgrades), 3)
        self.assertTrue(self.plan.new_packages[0].is_new)

    def test_remv_and_held_back(self) -> None:
        self.assertEqual([(r.name, r.version) for r in self.plan.removals], [("libold1", "1.0-1")])
        self.assertEqual(self.plan.kept_back, ("linux-headers-amd64", "firmware-misc-nonfree"))
        self.assertEqual(self.plan.not_upgraded, 2)
        self.assertEqual(self.plan.held_back, 2)

    def test_origin_counts(self) -> None:
        counts = dict(origin_counts(self.plan))
        self.assertEqual(counts["Debian+Debian-Security"], 1)
        self.assertEqual(counts["Proxmox"], 2)
        self.assertEqual(counts["Docker"], 1)
        self.assertEqual(origin_counts(self.plan)[0], ("Proxmox", 2))

    def test_parse_origins(self) -> None:
        self.assertEqual(parse_origins("Debian:12.5/stable, Foo:x/y"), ("Debian", "Foo"))
        self.assertEqual(parse_origins("plainname"), ("plainname",))
        self.assertEqual(parse_origins(""), ())

    def test_empty_and_garbage(self) -> None:
        self.assertEqual(parse_apt_simulation(""), AptPlan())
        self.assertEqual(parse_apt_simulation("Inst broken line\nRemv"), AptPlan())


class KernelTests(unittest.TestCase):
    def test_version_key_is_numeric(self) -> None:
        self.assertGreater(version_key("6.1.0-18-amd64"), version_key("6.1.0-9-amd64"))
        self.assertGreater(version_key("6.10.1-1-pve"), version_key("6.9.9-1-pve"))

    def test_pending_kernel_means_reboot(self) -> None:
        plan = AptPlan(installs=(pkg("linux-image-6.1.0-18-amd64", None),))
        self.assertEqual(detect_reboot("6.1.0-17-amd64", ["6.1.0-17-amd64"], plan, False),
                         (True, "6.1.0-18-amd64"))

    def test_installed_newer_kernel_means_reboot(self) -> None:
        self.assertEqual(detect_reboot("6.1.0-17-amd64", ["6.1.0-17-amd64", "6.1.0-18-amd64"],
                                       AptPlan(), False), (True, "6.1.0-18-amd64"))

    def test_other_flavors_are_ignored(self) -> None:
        installed = ["6.8.12-4-pve", "6.1.0-99-amd64"]
        self.assertEqual(detect_reboot("6.8.12-4-pve", installed, AptPlan(), False),
                         (False, "6.8.12-4-pve"))

    def test_proxmox_signed_package_name(self) -> None:
        plan = AptPlan(installs=(pkg("proxmox-kernel-6.8.12-5-pve-signed", None),))
        self.assertEqual(detect_reboot("6.8.12-4-pve", [], plan, False), (True, "6.8.12-5-pve"))

    def test_flag_file_and_meta_packages(self) -> None:
        plan = AptPlan(installs=(pkg("linux-image-amd64", "6.1.76-1"),))
        self.assertEqual(detect_reboot("6.1.0-17-amd64", [], plan, False)[0], False)
        self.assertEqual(detect_reboot("6.1.0-17-amd64", [], plan, True)[0], True)
        self.assertEqual(detect_reboot(None, [], AptPlan(), False), (False, None))


class CheckTests(unittest.TestCase):
    def test_parse_check(self) -> None:
        rep = parse_check("node-a", fixture("check_output.txt"))
        self.assertIsNone(rep.error)
        self.assertEqual(len(rep.plan.installs), 3)
        self.assertTrue(rep.reboot_needed)
        self.assertEqual((rep.running_kernel, rep.newest_kernel),
                         ("6.1.0-17-amd64", "6.1.0-18-amd64"))

    def test_update_failure_is_an_error(self) -> None:
        text = "@@update rc=100\nE: Failed to fetch repo 401 Unauthorized\n@@sim rc=0\n"
        self.assertIn("apt-get update failed", parse_check("n", text).error)
        text = "@@update rc=1\nsudo: a password is required\n@@sim rc=0\n"
        self.assertIn("NOPASSWD", parse_check("n", text).error)

    def test_simulation_failure_is_an_error(self) -> None:
        text = "@@sim rc=100\nE: Unmet dependencies. Try 'apt --fix-broken install'\n"
        self.assertIn("simulation failed", parse_check("n", text).error)
        self.assertIn("simulation failed", parse_check("n", "").error)

    def test_script_variants_are_read_only(self) -> None:
        for refresh in (True, False):
            script = check_script(refresh)
            self.assertIn("apt-get -s dist-upgrade", script)
            self.assertNotIn("dist-upgrade -y", script)
            self.assertNotIn(" install", script)
            self.assertEqual("apt-get update" in script, refresh)

    def test_check_node_updates_mocked(self) -> None:
        ok = subprocess.CompletedProcess([], 0, fixture("check_output.txt"), "")
        with mock.patch("subprocess.run", return_value=ok) as run, \
                mock.patch("polarum.updates.run_preflight", return_value=Preflight(quorate=True)):
            rep = check_node_updates(node("node-a"))
        self.assertTrue(rep.preflight.quorate)
        self.assertIn("BatchMode=yes", run.call_args.args[0])
        fail = subprocess.CompletedProcess([], 255, "", "Permission denied (publickey).")
        with mock.patch("subprocess.run", return_value=fail):
            self.assertIn("ssh-add", check_node_updates(node("node-a")).error)
        with mock.patch("subprocess.run", return_value=ok), \
                mock.patch("polarum.updates.run_preflight", return_value=Preflight(error="x")):
            self.assertIn("pre-flight failed", check_node_updates(node("node-a")).error)


class RuleTests(unittest.TestCase):
    def test_clean_plan_has_no_findings(self) -> None:
        plan = parse_apt_simulation(fixture("apt_sim_clean.txt"))
        v = evaluate_report(report(plan, preflight=Preflight(quorate=True)), SETTINGS)
        self.assertEqual((v.blocks, v.warns, v.errors), ((), (), ()))
        self.assertEqual(v.phrase, "proceed")

    def test_removals_block_only_when_configured(self) -> None:
        plan = AptPlan(removals=(parse_apt_simulation("Remv foo [1]").removals[0],))
        self.assertTrue(evaluate_report(report(plan), SETTINGS).needs_override)
        lenient = UpdateSettings(block_on_removals=False)
        self.assertFalse(evaluate_report(report(plan), lenient).needs_override)

    def test_foreign_origin_blocks(self) -> None:
        plan = AptPlan(installs=(pkg("a", "1", ("Debian",)), pkg("docker-ce", "1", ("Docker",))))
        v = evaluate_report(report(plan), SETTINGS)
        self.assertEqual(len(v.blocks), 1)
        self.assertIn("Docker", v.blocks[0].text)
        self.assertIn("docker-ce", v.blocks[0].text)
        self.assertEqual(v.phrase, "override")

    def test_all_origins_must_be_allowed(self) -> None:
        plan = AptPlan(installs=(pkg("a", "1", ("Debian", "Mystery")),))
        self.assertTrue(evaluate_report(report(plan), SETTINGS).blocks)
        plan = AptPlan(installs=(pkg("a", "1", ("Debian", "Debian-Security")),))
        self.assertFalse(evaluate_report(report(plan), SETTINGS).blocks)
        plan = AptPlan(installs=(pkg("a", "1", ()),))      # unknown origin fails closed
        self.assertTrue(evaluate_report(report(plan), SETTINGS).blocks)

    def test_preflight_blockers(self) -> None:
        plan = AptPlan()
        pf = Preflight(quorate=False, backup_running=True, zfs_activity=("scrub",))
        v = evaluate_report(report(plan, preflight=pf), SETTINGS)
        text = " ".join(f.text for f in v.blocks)
        self.assertEqual(len(v.blocks), 3)
        for word in ("quorum", "backup", "scrub"):
            self.assertIn(word, text)

    def test_unknown_preflight_values_do_not_block(self) -> None:
        v = evaluate_report(report(AptPlan(), preflight=Preflight()), SETTINGS)
        self.assertFalse(v.blocks)

    def test_warnings(self) -> None:
        plan = AptPlan(installs=tuple(pkg(f"p{i}", "1") for i in range(51)), not_upgraded=1)
        rep = report(plan, reboot_needed=True, running_kernel="6.1.0-17-amd64",
                     newest_kernel="6.1.0-18-amd64")
        v = evaluate_report(rep, SETTINGS)
        self.assertEqual(len(v.warns), 3)
        self.assertFalse(v.blocks)
        self.assertIn("6.1.0-18-amd64", v.warns[0].text)
        self.assertEqual(v.phrase, "proceed")

    def test_errors_disable_apply(self) -> None:
        v = evaluate([NodeReport("node-b", error="boom"), report(AptPlan())], SETTINGS)
        self.assertFalse(v.can_apply)
        self.assertIn("boom", v.errors[0].text)

    def test_combined_verdict(self) -> None:
        blocked = report(AptPlan(installs=(pkg("x", "1", ("Weird",)),)))
        v = evaluate([blocked, report(AptPlan())], SETTINGS)
        self.assertTrue(v.needs_override)
        self.assertTrue(v.can_apply)


class OrderTests(unittest.TestCase):
    def test_canary_first_then_update_order(self) -> None:
        nodes = [node("c", 3), node("b", 2), node("a", 1), node("k", 9, canary=True),
                 node("z", None)]
        self.assertEqual([n.name for n in apply_order(nodes)], ["k", "a", "b", "c", "z"])

    def test_chunking(self) -> None:
        pkgs = [pkg(f"package-{i:03}", "1.0") for i in range(40)]
        chunks = chunk_packages(pkgs, limit=100)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= 100 for c in chunks))
        self.assertEqual(sum(c.count(">") for c in chunks), 40)
        self.assertEqual(chunk_packages([]), [])


if __name__ == "__main__":
    unittest.main()
