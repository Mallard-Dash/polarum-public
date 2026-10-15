"""Per-node runtime state and the pure status logic derived from it."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .config import Node

# Health values produced by port checks.
UNKNOWN, OFFLINE, BOOTING, ONLINE = "UNKNOWN", "OFFLINE", "BOOTING", "ONLINE"

# Transient label shown while an action job is in flight, keyed by action kind.
BUSY_LABELS = {
    "wake": "SENDING...",
    "poweroff": "SHUTTING DOWN",
    "reboot": "REBOOTING",
    "update": "UPDATING",
}

# Severity levels used by the UI to pick colors.
LEVEL_OK, LEVEL_WARN, LEVEL_BAD = "ok", "warn", "bad"


@dataclass
class NodeState:
    node: Node
    health: str = UNKNOWN
    latency_ms: float | None = None
    busy: str | None = None            # action kind while a job is in flight
    wake_sent_at: float | None = None  # monotonic time the magic packet was sent
    wake_timeout_reported: bool = False
    reboot_needed: bool = False        # set by the update feature; cleared once a reboot completes
    reboot_sent: bool = False          # reboot command accepted, waiting to see down then up
    reboot_seen_down: bool = False


def track_reboot(state: NodeState, health: str) -> None:
    """Clear reboot_needed only after the node was seen going down and coming back."""
    if not state.reboot_sent:
        return
    if health != ONLINE:
        state.reboot_seen_down = True
    elif state.reboot_seen_down:
        state.reboot_needed = False
        state.reboot_sent = False
        state.reboot_seen_down = False


def classify_ports(ports_up: Sequence[bool]) -> str:
    """OFFLINE: none up. BOOTING: some but not all. ONLINE: all up."""
    if not ports_up or not any(ports_up):
        return OFFLINE
    return ONLINE if all(ports_up) else BOOTING


def apply_health(state: NodeState, health: str, latency_ms: float | None) -> None:
    state.health = health
    state.latency_ms = latency_ms if health != OFFLINE else None
    if health == ONLINE:
        state.wake_sent_at = None
        state.wake_timeout_reported = False


def wake_elapsed(state: NodeState, now: float) -> float | None:
    if state.wake_sent_at is None:
        return None
    return max(0.0, now - state.wake_sent_at)


def wake_timed_out(state: NodeState, now: float, wake_timeout: float) -> bool:
    elapsed = wake_elapsed(state, now)
    return (elapsed is not None and elapsed >= wake_timeout
            and state.health != ONLINE)


def display_status(state: NodeState, now: float, wake_timeout: float) -> tuple[str, str]:
    """Return (label, level). Priority: busy job, ONLINE, wake timeout, BOOTING, WAKING."""
    if state.busy is not None:
        return BUSY_LABELS.get(state.busy, "WORKING..."), LEVEL_WARN
    if state.health == ONLINE:
        if state.reboot_needed:
            return "REBOOT NEEDED", LEVEL_WARN
        return ONLINE, LEVEL_OK
    if wake_timed_out(state, now, wake_timeout):
        return "WAKE TIMEOUT", LEVEL_BAD
    if state.health == BOOTING:
        return BOOTING, LEVEL_WARN
    elapsed = wake_elapsed(state, now)
    if elapsed is not None:
        return f"WAKING {int(elapsed)}s", LEVEL_WARN
    if state.health == OFFLINE:
        return OFFLINE, LEVEL_BAD
    return UNKNOWN, LEVEL_WARN
