from __future__ import annotations

import unittest

from polarum.config import Node
from polarum.state import (
    BOOTING, LEVEL_BAD, LEVEL_OK, LEVEL_WARN, OFFLINE, ONLINE, UNKNOWN,
    NodeState, apply_health, classify_ports, display_status, wake_timed_out,
)

NODE = Node(name="node-a", ip="192.168.1.11", mac="AA:BB:CC:DD:EE:01", ssh_host="192.168.1.11")
TIMEOUT = 180.0


class StatusTests(unittest.TestCase):
    def test_classify_ports(self) -> None:
        self.assertEqual(classify_ports([]), OFFLINE)
        self.assertEqual(classify_ports([False, False]), OFFLINE)
        self.assertEqual(classify_ports([True, False]), BOOTING)
        self.assertEqual(classify_ports([True, True]), ONLINE)

    def test_initial_unknown(self) -> None:
        self.assertEqual(display_status(NodeState(NODE), 0, TIMEOUT), (UNKNOWN, LEVEL_WARN))

    def test_offline_online(self) -> None:
        st = NodeState(NODE)
        apply_health(st, OFFLINE, None)
        self.assertEqual(display_status(st, 0, TIMEOUT), (OFFLINE, LEVEL_BAD))
        apply_health(st, ONLINE, 1.5)
        self.assertEqual(display_status(st, 0, TIMEOUT), (ONLINE, LEVEL_OK))
        self.assertEqual(st.latency_ms, 1.5)

    def test_busy_overrides_everything(self) -> None:
        st = NodeState(NODE, health=ONLINE, busy="poweroff")
        self.assertEqual(display_status(st, 0, TIMEOUT), ("SHUTTING DOWN", LEVEL_WARN))
        st.busy = "reboot"
        self.assertEqual(display_status(st, 0, TIMEOUT)[0], "REBOOTING")
        st.busy = "wake"
        self.assertEqual(display_status(st, 0, TIMEOUT)[0], "SENDING...")

    def test_waking_counter_then_timeout(self) -> None:
        st = NodeState(NODE, health=OFFLINE, wake_sent_at=100.0)
        self.assertEqual(display_status(st, 118.9, TIMEOUT), ("WAKING 18s", LEVEL_WARN))
        self.assertFalse(wake_timed_out(st, 279.9, TIMEOUT))
        self.assertEqual(display_status(st, 280.0, TIMEOUT), ("WAKE TIMEOUT", LEVEL_BAD))

    def test_booting_during_wake(self) -> None:
        st = NodeState(NODE, health=BOOTING, wake_sent_at=100.0)
        self.assertEqual(display_status(st, 110, TIMEOUT), (BOOTING, LEVEL_WARN))

    def test_online_clears_wake_state(self) -> None:
        st = NodeState(NODE, health=OFFLINE, wake_sent_at=100.0, wake_timeout_reported=True)
        apply_health(st, ONLINE, 2.0)
        self.assertIsNone(st.wake_sent_at)
        self.assertFalse(st.wake_timeout_reported)
        self.assertEqual(display_status(st, 500, TIMEOUT)[0], ONLINE)

    def test_offline_clears_latency(self) -> None:
        st = NodeState(NODE, latency_ms=3.0)
        apply_health(st, OFFLINE, 3.0)
        self.assertIsNone(st.latency_ms)


class RebootNeededTests(unittest.TestCase):
    def test_marker_only_when_online_and_idle(self) -> None:
        st = NodeState(NODE, health=ONLINE, reboot_needed=True)
        self.assertEqual(display_status(st, 0, TIMEOUT), ("REBOOT NEEDED", LEVEL_WARN))
        st.busy = "reboot"
        self.assertEqual(display_status(st, 0, TIMEOUT)[0], "REBOOTING")
        st.busy = "update"
        self.assertEqual(display_status(st, 0, TIMEOUT)[0], "UPDATING")
        st.busy = None
        st.health = OFFLINE
        self.assertEqual(display_status(st, 0, TIMEOUT)[0], OFFLINE)


if __name__ == "__main__":
    unittest.main()


class RebootTrackingTests(unittest.TestCase):
    def test_flag_cleared_only_after_down_then_up(self) -> None:
        from polarum.state import track_reboot
        st = NodeState(node=None, health=ONLINE, reboot_needed=True, reboot_sent=True)
        track_reboot(st, ONLINE)          # command accepted, node not down yet
        self.assertTrue(st.reboot_needed)
        track_reboot(st, "OFFLINE")
        self.assertTrue(st.reboot_needed)
        track_reboot(st, ONLINE)
        self.assertFalse(st.reboot_needed)
        self.assertFalse(st.reboot_sent)
