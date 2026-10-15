from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from polarum import ssh
from polarum.actions import remote_power, wake_node
from polarum.config import Node, Settings
from polarum.ssh import SshResult, map_ssh_error

NODE = Node(name="node-a", ip="192.168.1.11", mac="AA:BB:CC:DD:EE:01", ssh_host="192.168.1.11")
SETTINGS = Settings(relay="relay-host", broadcast="192.168.1.255")


def completed(rc: int, out: str = "", err: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], rc, out, err)


class ErrorMappingTests(unittest.TestCase):
    def check(self, stderr: str, expected: str, rc: int = 255) -> None:
        self.assertIn(expected, map_ssh_error(SshResult(rc, stderr=stderr)))

    def test_known_errors(self) -> None:
        self.check("polarum@h: Permission denied (publickey).", "run ssh-add")
        self.check("ssh: connect to host h port 22: Connection refused", "Connection refused")
        self.check("ssh: connect to host h port 22: Connection timed out", "timed out")
        self.check("Host key verification failed.", "Host key not trusted")
        self.check("@@@ REMOTE HOST IDENTIFICATION HAS CHANGED! @@@\n"
                   "Host key verification failed.", "Host key changed")
        self.check("ssh: Could not resolve hostname x", "resolve hostname")
        self.check("sudo: a password is required", "NOPASSWD", rc=1)

    def test_timeout_and_fallbacks(self) -> None:
        self.assertIn("timed out", map_ssh_error(SshResult(255, timed_out=True)))
        self.assertEqual(map_ssh_error(SshResult(3)), "SSH exited with status 3")
        self.assertEqual(map_ssh_error(SshResult(1, stderr="a\nweird thing\n")), "weird thing")


class CommandTests(unittest.TestCase):
    def test_batch_mode_always_set(self) -> None:
        cmd = ssh.ssh_command("polarum@host", ["true"])
        self.assertIn("BatchMode=yes", cmd)
        self.assertEqual(cmd[-2:], ["polarum@host", "true"])

    def test_run_ssh_handles_failures(self) -> None:
        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("ssh", 1)):
            self.assertTrue(ssh.run_ssh("h", ["true"]).timed_out)
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            self.assertEqual(ssh.run_ssh("h", ["true"]).returncode, 127)


class ActionTests(unittest.TestCase):
    def test_wake_builds_relay_command(self) -> None:
        with mock.patch("subprocess.run", return_value=completed(0)) as run:
            result = wake_node(SETTINGS, NODE)
        self.assertTrue(result.ok)
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[-5:], ["relay-host", "wakeonlan", "-i", "192.168.1.255",
                                    "AA:BB:CC:DD:EE:01"])

    def test_wake_failure_is_mapped(self) -> None:
        err = "Permission denied (publickey)."
        with mock.patch("subprocess.run", return_value=completed(255, err=err)):
            result = wake_node(SETTINGS, NODE)
        self.assertFalse(result.ok)
        self.assertIn("ssh-add", result.detail)

    def test_power_commands_and_outcomes(self) -> None:
        with mock.patch("subprocess.run", return_value=completed(0)) as run:
            self.assertTrue(remote_power(NODE, "poweroff").ok)
        self.assertEqual(run.call_args.args[0][-4:], ["sudo", "-n", "systemctl", "poweroff"])
        closed = completed(255, err="Connection to h closed by remote host.")
        with mock.patch("subprocess.run", return_value=closed):
            self.assertTrue(remote_power(NODE, "reboot").ok)
        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("ssh", 1)):
            self.assertFalse(remote_power(NODE, "reboot").ok)
        with mock.patch("subprocess.run", return_value=completed(1, err="sudo: a password is required")):
            self.assertFalse(remote_power(NODE, "poweroff").ok)
        with mock.patch("subprocess.run") as run:
            self.assertFalse(remote_power(NODE, "halt").ok)
            run.assert_not_called()


class AgentTests(unittest.TestCase):
    def test_decision(self) -> None:
        self.assertEqual(ssh.agent_decision(0), ssh.AGENT_OK)
        self.assertEqual(ssh.agent_decision(1), ssh.AGENT_ADD)
        self.assertEqual(ssh.agent_decision(2), ssh.AGENT_UNREACHABLE)
        self.assertEqual(ssh.agent_decision(None), ssh.AGENT_UNREACHABLE)

    def test_ensure_agent_flows(self) -> None:
        with mock.patch.object(ssh, "_ssh_add_list", side_effect=[0]):
            self.assertTrue(ssh.ensure_agent())
        ran: list[list[str]] = []
        with mock.patch.object(ssh, "_ssh_add_list", side_effect=[1, 0]), \
                mock.patch("builtins.print"):
            self.assertTrue(ssh.ensure_agent(run_interactive=lambda c: ran.append(c) or 0))
        self.assertEqual(ran, [["ssh-add"]])
        with mock.patch.object(ssh, "_ssh_add_list", side_effect=[2]), \
                mock.patch("builtins.print"):
            self.assertFalse(ssh.ensure_agent(input_fn=lambda _: "n"))
        with mock.patch.object(ssh, "_ssh_add_list", side_effect=[2]), \
                mock.patch("builtins.print"):
            self.assertTrue(ssh.ensure_agent(input_fn=lambda _: "y"))
        with mock.patch.object(ssh, "_ssh_add_list", side_effect=[1, 1]), \
                mock.patch("builtins.print"):
            self.assertFalse(ssh.ensure_agent(run_interactive=lambda c: 1,
                                              input_fn=lambda _: ""))


if __name__ == "__main__":
    unittest.main()
