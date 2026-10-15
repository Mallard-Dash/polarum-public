"""Append-only event log (timestamp | node | action | result)."""
from __future__ import annotations

import os
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

SEPARATOR = " | "
MAX_FIELD = 300


@dataclass(frozen=True)
class Event:
    timestamp: str
    node: str
    action: str
    result: str

    def line(self) -> str:
        return SEPARATOR.join((self.timestamp, self.node, self.action, self.result))

    def short(self) -> str:
        """Compact form for the TUI: time of day only."""
        stamp = self.timestamp[11:19] if len(self.timestamp) >= 19 else self.timestamp
        return f"{stamp} {self.node} {self.action}: {self.result}"


def _clean(text: str) -> str:
    """Single line, no control characters, bounded length."""
    flat = "".join(ch if ch.isprintable() else " " for ch in text).strip()
    return flat[:MAX_FIELD]


def parse_line(line: str) -> Event | None:
    parts = line.rstrip("\n").split(SEPARATOR, 3)
    if len(parts) != 4:
        return None
    return Event(*parts)


class EventLog:
    def __init__(self, path: Path, keep: int = 200) -> None:
        self.path = path
        self._recent: deque[Event] = deque(maxlen=keep)
        self._lock = threading.Lock()

    @classmethod
    def open(cls, path: Path) -> EventLog:
        """Create the log (private permissions) and load recent entries.

        Raises OSError if the log cannot be written: without an audit trail we
        refuse to start.
        """
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.close(os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600))
        log = cls(path)
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            lines = []
        for line in lines[-log._recent.maxlen:]:
            event = parse_line(line)
            if event:
                log._recent.append(event)
        return log

    def append(self, node: str, action: str, result: str) -> Event:
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        event = Event(stamp, _clean(node) or "-", _clean(action), _clean(result))
        with self._lock:
            self._recent.append(event)
            try:
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(event.line() + "\n")
            except OSError:
                pass  # still shown in the TUI for this session
        return event

    def tail(self, count: int) -> list[Event]:
        with self._lock:
            return list(self._recent)[-count:]
