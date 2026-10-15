"""State-changing actions. Callers must obtain confirmation before invoking these."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .config import Node, Settings
from .ssh import is_connection_closed, map_ssh_error, run_ssh
from .wol import send_magic_packet

WAKE_TIMEOUT_SSH = 10.0
POWER_TIMEOUT_SSH = 10.0

POWER_COMMANDS = {
    "poweroff": ["sudo", "-n", "systemctl", "poweroff"],
    "reboot": ["sudo", "-n", "systemctl", "reboot"],
}


@dataclass(frozen=True)
class ActionSpec:
    kind: str
    title: str      # human wording, e.g. "power off"
    strict: bool    # True: typed "proceed"; False: single 'y'


ACTIONS = {
    "wake": ActionSpec("wake", "wake", strict=False),
    "poweroff": ActionSpec("poweroff", "power off", strict=True),
    "reboot": ActionSpec("reboot", "reboot", strict=True),
}


@dataclass(frozen=True)
class ActionResult:
    ok: bool
    detail: str


def wake_direct(settings: Settings, node: Node) -> ActionResult:
    """No relay configured: broadcast the magic packet from this machine."""
    try:
        send_magic_packet(node.mac, settings.broadcast)
    except (OSError, ValueError) as exc:
        reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc)
        return ActionResult(False, f"direct UDP broadcast failed: {reason}")
    return ActionResult(True, f"magic packet broadcast directly to {settings.broadcast}")


def wake_node(settings: Settings, node: Node) -> ActionResult:
    if not settings.relay:
        return wake_direct(settings, node)
    remote = [settings.wol_command, "-i", settings.broadcast, node.mac]
    result = run_ssh(settings.relay, remote, WAKE_TIMEOUT_SSH)
    if not result.ok:
        return ActionResult(False, f"relay {settings.relay}: {map_ssh_error(result)}")
    return ActionResult(True, f"magic packet sent via {settings.relay}")


def remote_power(node: Node, kind: str) -> ActionResult:
    command = POWER_COMMANDS.get(kind)
    if command is None:
        return ActionResult(False, f"unknown action {kind!r}")
    result = run_ssh(node.ssh_target, command, POWER_TIMEOUT_SSH)
    if result.ok:
        return ActionResult(True, f"{kind} command accepted")
    if is_connection_closed(result):
        return ActionResult(True, f"{kind} sent; connection closed by node (expected)")
    if result.timed_out:
        return ActionResult(False, "SSH timed out; state uncertain, verify the node manually")
    return ActionResult(False, map_ssh_error(result))


def run_action(settings: Settings, node: Node, kind: str) -> ActionResult:
    """Dispatch an action by kind. Runs in a worker thread, never in the UI loop."""
    runners: dict[str, Callable[[], ActionResult]] = {
        "wake": lambda: wake_node(settings, node),
        "poweroff": lambda: remote_power(node, "poweroff"),
        "reboot": lambda: remote_power(node, "reboot"),
    }
    runner = runners.get(kind)
    if runner is None:
        return ActionResult(False, f"unknown action {kind!r}")
    return runner()
