from __future__ import annotations

import stat
import tempfile
import unittest
from pathlib import Path

from polarum.events import EventLog, parse_line


class EventLogTests(unittest.TestCase):
    def test_append_tail_and_reload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state" / "events.log"
            log = EventLog.open(path)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            for i in range(7):
                log.append("node-a", "wake", f"ok {i}")
            self.assertEqual([e.result for e in log.tail(5)][-1], "ok 6")
            self.assertEqual(len(log.tail(5)), 5)
            reopened = EventLog.open(path)
            self.assertEqual(len(reopened.tail(100)), 7)

    def test_fields_are_sanitized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = EventLog.open(Path(tmp) / "events.log")
            event = log.append("node-a", "x\ny", "line1\nline2\x1b[31m" + "z" * 1000)
            self.assertNotIn("\n", event.line())
            self.assertLessEqual(len(event.result), 300)
            self.assertEqual(len(log.path.read_text().splitlines()), 1)

    def test_parse_line(self) -> None:
        e = parse_line("2026-01-01T10:00:00+00:00 | node-a | wake | ok | with pipe")
        self.assertEqual(e.result, "ok | with pipe")
        self.assertEqual(e.short(), "10:00:00 node-a wake: ok | with pipe")
        self.assertIsNone(parse_line("garbage"))


if __name__ == "__main__":
    unittest.main()
