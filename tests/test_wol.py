from __future__ import annotations

import socket
import unittest
from unittest import mock

from polarum.actions import wake_node
from polarum.config import Node, Settings
from polarum.wol import build_magic_packet, send_magic_packet

NODE = Node(name="node-a", ip="192.168.1.11", mac="AA:BB:CC:DD:EE:01", ssh_host="192.168.1.11")


class MagicPacketTests(unittest.TestCase):
    def test_packet_layout(self) -> None:
        packet = build_magic_packet("aa:bb:cc:dd:ee:01")
        self.assertEqual(len(packet), 102)
        self.assertEqual(packet[:6], b"\xff" * 6)
        self.assertEqual(packet[6:12], bytes.fromhex("aabbccddee01"))
        self.assertEqual(packet[6:], bytes.fromhex("aabbccddee01") * 16)

    def test_invalid_mac_rejected(self) -> None:
        for bad in ("", "AA:BB:CC:DD:EE", "AA-BB-CC-DD-EE-01", "GG:BB:CC:DD:EE:01"):
            with self.assertRaises(ValueError):
                build_magic_packet(bad)

    def test_send_over_loopback_udp(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as rx:
            rx.bind(("127.0.0.1", 0))
            rx.settimeout(3)
            send_magic_packet("AA:BB:CC:DD:EE:01", "127.0.0.1", rx.getsockname()[1])
            data, _ = rx.recvfrom(2048)
        self.assertEqual(data, build_magic_packet("AA:BB:CC:DD:EE:01"))

    def test_broadcast_option_is_enabled(self) -> None:
        with mock.patch("socket.socket") as sock_cls:
            sock = sock_cls.return_value.__enter__.return_value
            send_magic_packet("AA:BB:CC:DD:EE:01", "192.168.1.255")
        sock.setsockopt.assert_called_with(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.assertEqual(sock.sendto.call_args.args[1], ("192.168.1.255", 9))


class DirectWakeTests(unittest.TestCase):
    def test_no_relay_sends_directly_without_ssh(self) -> None:
        settings = Settings(relay="", broadcast="192.168.1.255")
        with mock.patch("polarum.actions.send_magic_packet") as send, \
                mock.patch("subprocess.run") as run:
            result = wake_node(settings, NODE)
        self.assertTrue(result.ok)
        send.assert_called_once_with("AA:BB:CC:DD:EE:01", "192.168.1.255")
        run.assert_not_called()
        self.assertIn("directly", result.detail)

    def test_direct_failure_is_reported(self) -> None:
        with mock.patch("polarum.actions.send_magic_packet",
                        side_effect=OSError(101, "Network is unreachable")):
            result = wake_node(Settings(), NODE)
        self.assertFalse(result.ok)
        self.assertIn("Network is unreachable", result.detail)

    def test_relay_still_uses_ssh(self) -> None:
        import subprocess
        done = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch("subprocess.run", return_value=done) as run, \
                mock.patch("polarum.actions.send_magic_packet") as send:
            self.assertTrue(wake_node(Settings(relay="relay-host"), NODE).ok)
        run.assert_called_once()
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
