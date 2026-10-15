from __future__ import annotations

import contextlib
import io
import unittest
from unittest import mock

from polarum import cli, sudoers
from polarum.actions import POWER_COMMANDS
from polarum.runner import (
    JOURNAL_ARGV, new_token, snapshot_argv, snapshot_name, start_argv,
)

DATASET = "rpool/ROOT/pve-1"


def rule_for(prefix: str) -> str:
    return next(r for r in sudoers.apply_rules(DATASET) if r.startswith(prefix))


class RuleMatchesRealCommands(unittest.TestCase):
    """Each rule must accept the exact command the code runs, and nothing wider."""

    def test_unit_start_matches_with_any_token(self) -> None:
        rule = rule_for("/usr/bin/systemd-run")
        for _ in range(5):
            self.assertTrue(sudoers.matches(rule, start_argv(new_token())))

    def test_unit_start_rejects_other_commands(self) -> None:
        rule = rule_for("/usr/bin/systemd-run")
        argv = start_argv("0123456789ab")
        evil = list(argv)
        evil[argv.index("-c") + 1] = "id"                 # different wrapper script
        self.assertFalse(sudoers.matches(rule, evil))
        self.assertFalse(sudoers.matches(rule, argv + ["extra"]))
        self.assertFalse(sudoers.matches(rule, argv[:-1] + ["0123456789abcd"]))
        self.assertFalse(sudoers.matches(rule, ["systemd-run", "--", "sh", "-c", "id"]))
        self.assertFalse(sudoers.matches(rule, ["systemd-run", "sh"] + argv[2:]))

    def test_journal_is_exact(self) -> None:
        rule = rule_for("/usr/bin/journalctl")
        self.assertTrue(sudoers.matches(rule, list(JOURNAL_ARGV)))
        self.assertFalse(sudoers.matches(rule, list(JOURNAL_ARGV)[:-2]))        # pager allowed
        self.assertFalse(sudoers.matches(rule, list(JOURNAL_ARGV) + ["--file=/x"]))

    def test_snapshot_limited_to_one_dataset(self) -> None:
        rule = rule_for("/usr/sbin/zfs")
        self.assertTrue(sudoers.matches(rule, snapshot_argv(DATASET, snapshot_name())))
        self.assertFalse(sudoers.matches(rule, snapshot_argv("tank/data", snapshot_name())))
        self.assertFalse(sudoers.matches(
            rule, snapshot_argv(DATASET, snapshot_name()) + ["tank/data@x"]))
        self.assertFalse(sudoers.matches(rule, ["zfs", "destroy", f"{DATASET}@x"]))

    def test_power_rules_match_actions(self) -> None:
        rules = sudoers.power_rules()
        for cmd in POWER_COMMANDS.values():
            argv = cmd[2:]                                   # drop "sudo", "-n"
            self.assertTrue(any(sudoers.matches(r, argv) for r in rules))
        self.assertFalse(any(sudoers.matches(r, ["systemctl", "stop", "ssh"]) for r in rules))


class Generated(unittest.TestCase):
    def test_special_characters_are_escaped(self) -> None:
        rule = rule_for("/usr/bin/systemd-run")
        self.assertIn("--unit\\=polarum-upgrade", rule)
        self.assertIn("Dpkg\\:\\:Options", rule)
        self.assertNotIn("*", rule)                          # no open-ended wildcard

    def test_no_open_wildcards_anywhere(self) -> None:
        for rule in (sudoers.apply_rules(DATASET) + sudoers.power_rules()
                     + sudoers.preflight_rules() + sudoers.check_rules()):
            self.assertNotIn("*", rule.replace("\\*", ""))

    def test_apply_line_is_commented_out(self) -> None:
        text = sudoers.generate("deploy", DATASET)
        self.assertIn("\n# deploy ALL=(root) NOPASSWD: POLARUM_APPLY", text)
        self.assertIn("\ndeploy ALL=(root) NOPASSWD: POLARUM_POWER", text)

    def test_cli_prints_and_validates(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--print-sudoers", "--sudoers-user", "ops"]), 0)
        self.assertIn("ops ALL=(root)", out.getvalue())
        err = io.StringIO()
        with contextlib.redirect_stderr(err), mock.patch("builtins.print"):
            self.assertEqual(cli.main(["--print-sudoers", "--sudoers-user", "a b"]), 2)


class TerminalSafety(unittest.TestCase):
    def test_printable_strips_escapes_and_bidi(self) -> None:
        from polarum.textutil import printable
        hostile = "ok\x1b[2J\x1b]52;c;ZXZpbA==\x07 a‮b\r\nnext\x00"
        cleaned = printable(hostile)
        for bad in ("\x1b", "\x07", "\x00", "\r", "‮"):
            self.assertNotIn(bad, cleaned)
        self.assertEqual(cleaned.count("\n"), 1)
        self.assertIn("ok", cleaned)

    def test_emit_sanitizes_remote_text(self) -> None:
        from polarum import cmd
        out = io.StringIO()
        cmd.emit("E: boom\x1b[2J\x1b]0;pwned\x07", stream=out)
        self.assertEqual(out.getvalue().count("\x1b"), 0)

    def test_event_log_is_single_line(self) -> None:
        from polarum.events import _clean
        self.assertEqual(_clean("a\nb\x1b[0m c"), "a b [0m c")


if __name__ == "__main__":
    unittest.main()
