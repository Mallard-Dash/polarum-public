"""Configuration loading and validation.

All problems are collected and reported together so a user can fix the file in
one pass. Nothing here touches the network.
"""
from __future__ import annotations

import ipaddress
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REBOOT_POLICIES = ("manual", "if_needed", "never")
DEFAULT_ALLOWED_ORIGINS = ("Debian", "Debian-Security", "Proxmox")

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")
HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
USER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9._-]*$")
COMMAND_RE = re.compile(r"^[A-Za-z0-9_./-]+$")

TOP_KEYS = {"settings", "nodes"}
SETTINGS_KEYS = {
    "relay", "relay_broadcast", "relay_wol_command", "wake_timeout", "update",
}
UPDATE_KEYS = {
    "allowed_origins", "block_on_removals", "snapshot_before",
    "require_snapshot", "health_timeout", "reboot_policy",
    "many_updates_threshold",
}
NODE_KEYS = {
    "name", "ip", "mac", "ssh_host", "ssh_user", "health_ports",
    "update_order", "canary",
}


class ConfigError(Exception):
    """Raised with every validation problem found, not just the first."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("\n".join(errors))


@dataclass(frozen=True)
class UpdateSettings:
    allowed_origins: tuple[str, ...] = DEFAULT_ALLOWED_ORIGINS
    block_on_removals: bool = True
    snapshot_before: bool = True
    require_snapshot: bool = False
    health_timeout: float = 300.0
    reboot_policy: str = "manual"
    many_updates_threshold: int = 50


@dataclass(frozen=True)
class Settings:
    relay: str = ""      # empty: send the magic packet directly from this machine
    broadcast: str = "255.255.255.255"
    wol_command: str = "wakeonlan"
    wake_timeout: float = 180.0
    update: UpdateSettings = field(default_factory=UpdateSettings)


@dataclass(frozen=True)
class Node:
    name: str
    ip: str
    mac: str
    ssh_host: str
    ssh_user: str = "polarum"
    health_ports: tuple[int, ...] = (22,)
    update_order: int | None = None
    canary: bool = False

    @property
    def ssh_target(self) -> str:
        return f"{self.ssh_user}@{self.ssh_host}"


def default_config_path() -> Path:
    return Path.home() / ".config" / "polarum" / "nodes.toml"


def default_state_dir() -> Path:
    return Path.home() / ".local" / "state" / "polarum"


def default_log_path() -> Path:
    return default_state_dir() / "events.log"


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_keys(table: dict, allowed: set[str], where: str, errors: list[str]) -> None:
    for key in sorted(set(table) - allowed):
        errors.append(f"{where}: unknown key '{key}' (allowed: {', '.join(sorted(allowed))})")


def _get_bool(table: dict, key: str, default: bool, where: str, errors: list[str]) -> bool:
    value = table.get(key, default)
    if not isinstance(value, bool):
        errors.append(f"{where}: '{key}' must be true or false")
        return default
    return value


def _get_seconds(
    table: dict, key: str, default: float, where: str, errors: list[str],
) -> float:
    value = table.get(key, default)
    if not _is_number(value) or not 0 < value <= 86400:
        errors.append(f"{where}: '{key}' must be a number of seconds between 1 and 86400")
        return default
    return float(value)


def _parse_update(raw: Any, errors: list[str]) -> UpdateSettings:
    where = "[settings.update]"
    if not isinstance(raw, dict):
        errors.append("[settings.update] must be a table")
        return UpdateSettings()
    _check_keys(raw, UPDATE_KEYS, where, errors)
    defaults = UpdateSettings()

    origins = raw.get("allowed_origins", list(defaults.allowed_origins))
    if (not isinstance(origins, list) or not origins
            or not all(isinstance(o, str) and o.strip() for o in origins)):
        errors.append(f"{where}: 'allowed_origins' must be a non-empty list of strings")
        origins = list(defaults.allowed_origins)

    policy = raw.get("reboot_policy", defaults.reboot_policy)
    if policy not in REBOOT_POLICIES:
        errors.append(
            f"{where}: 'reboot_policy' must be one of {', '.join(REBOOT_POLICIES)}")
        policy = defaults.reboot_policy

    threshold = raw.get("many_updates_threshold", defaults.many_updates_threshold)
    if not _is_int(threshold) or threshold < 1:
        errors.append(f"{where}: 'many_updates_threshold' must be a positive integer")
        threshold = defaults.many_updates_threshold

    update = UpdateSettings(
        allowed_origins=tuple(o.strip() for o in origins),
        block_on_removals=_get_bool(
            raw, "block_on_removals", defaults.block_on_removals, where, errors),
        snapshot_before=_get_bool(
            raw, "snapshot_before", defaults.snapshot_before, where, errors),
        require_snapshot=_get_bool(
            raw, "require_snapshot", defaults.require_snapshot, where, errors),
        health_timeout=_get_seconds(
            raw, "health_timeout", defaults.health_timeout, where, errors),
        reboot_policy=policy,
        many_updates_threshold=threshold,
    )
    if update.require_snapshot and not update.snapshot_before:
        errors.append(f"{where}: 'require_snapshot' needs 'snapshot_before = true'")
    return update


def _parse_settings(raw: Any, errors: list[str]) -> Settings | None:
    where = "[settings]"
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        errors.append("[settings] must be a table")
        return None
    _check_keys(raw, SETTINGS_KEYS, where, errors)

    relay = raw.get("relay", "")
    if not isinstance(relay, str):
        errors.append(f"{where}: 'relay' must be a string (SSH host/alias, or empty for "
                      "direct UDP broadcast)")
        relay = ""
    elif relay and not re.fullmatch(r"([A-Za-z_][A-Za-z0-9._-]*@)?[A-Za-z0-9][A-Za-z0-9._:-]*", relay):
        errors.append(f"{where}: 'relay' contains invalid characters: {relay!r}")

    broadcast = raw.get("relay_broadcast", "255.255.255.255")
    try:
        ipaddress.IPv4Address(broadcast)
    except (ValueError, TypeError):
        errors.append(f"{where}: 'relay_broadcast' is not a valid IPv4 address: {broadcast!r}")
        broadcast = "255.255.255.255"

    command = raw.get("relay_wol_command", "wakeonlan")
    if not isinstance(command, str) or not COMMAND_RE.fullmatch(command):
        errors.append(
            f"{where}: 'relay_wol_command' must be a bare command name or path "
            "without spaces")
        command = "wakeonlan"

    return Settings(
        relay=relay,
        broadcast=broadcast,
        wol_command=command,
        wake_timeout=_get_seconds(raw, "wake_timeout", 180.0, where, errors),
        update=_parse_update(raw.get("update", {}), errors),
    )


def _parse_ports(value: Any, where: str, errors: list[str]) -> tuple[int, ...]:
    if (not isinstance(value, list) or not value
            or not all(_is_int(p) and 1 <= p <= 65535 for p in value)):
        errors.append(f"{where}: 'health_ports' must be a non-empty list of ports 1-65535")
        return (22,)
    if len(set(value)) != len(value):
        errors.append(f"{where}: 'health_ports' contains duplicates")
    return tuple(value)


def _parse_node(raw: Any, index: int, errors: list[str]) -> Node | None:
    if not isinstance(raw, dict):
        errors.append(f"nodes[{index}]: must be a table")
        return None
    name = raw.get("name")
    where = f"nodes[{index}]" + (f" ({name})" if isinstance(name, str) else "")
    problems_before = len(errors)
    _check_keys(raw, NODE_KEYS, where, errors)

    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        errors.append(
            f"{where}: 'name' is required: 1-32 characters from A-Z a-z 0-9 . _ -, "
            "starting with a letter or digit")
        name = ""

    ip = raw.get("ip")
    try:
        ip = str(ipaddress.ip_address(ip))
    except ValueError:
        errors.append(f"{where}: 'ip' is not a valid IP address: {ip!r}")
        ip = ""

    mac = raw.get("mac")
    if not isinstance(mac, str) or not MAC_RE.fullmatch(mac):
        errors.append(f"{where}: 'mac' must look like AA:BB:CC:DD:EE:01, got {mac!r}")
        mac = ""

    ssh_host = raw.get("ssh_host", ip)
    if not isinstance(ssh_host, str) or not HOST_RE.fullmatch(ssh_host):
        errors.append(f"{where}: 'ssh_host' is invalid: {ssh_host!r}")
        ssh_host = ip
    ssh_user = raw.get("ssh_user", "polarum")
    if not isinstance(ssh_user, str) or not USER_RE.fullmatch(ssh_user):
        errors.append(f"{where}: 'ssh_user' is invalid: {ssh_user!r}")
        ssh_user = "polarum"

    ports = _parse_ports(raw.get("health_ports", [22]), where, errors)

    order = raw.get("update_order")
    if order is not None and (not _is_int(order) or order < 1):
        errors.append(f"{where}: 'update_order' must be a positive integer")
        order = None
    canary = _get_bool(raw, "canary", False, where, errors)
    if canary and order is None:
        errors.append(f"{where}: 'canary = true' requires an 'update_order'")

    if len(errors) > problems_before:
        return None
    return Node(
        name=name, ip=ip, mac=mac.upper(), ssh_host=ssh_host, ssh_user=ssh_user,
        health_ports=ports, update_order=order, canary=canary,
    )


def _check_duplicates(nodes: list[Node], errors: list[str]) -> None:
    keyed = {
        "name": lambda n: n.name.lower(),
        "ip": lambda n: n.ip,
        "mac": lambda n: n.mac,
        "update_order": lambda n: n.update_order,
    }
    for label, key in keyed.items():
        seen: dict[object, str] = {}
        for node in nodes:
            value = key(node)
            if value is None:
                continue
            if value in seen:
                errors.append(
                    f"duplicate {label} {value!r}: used by '{seen[value]}' and '{node.name}'")
            else:
                seen[value] = node.name


def parse_config(data: dict[str, Any]) -> tuple[Settings, list[Node]]:
    """Validate a parsed TOML document. Raises ConfigError listing all problems."""
    errors: list[str] = []
    _check_keys(data, TOP_KEYS, "top level", errors)
    settings = _parse_settings(data.get("settings"), errors)

    raw_nodes = data.get("nodes")
    nodes: list[Node] = []
    if not isinstance(raw_nodes, list) or not raw_nodes:
        errors.append("no [[nodes]] entries found")
    else:
        for index, raw in enumerate(raw_nodes, start=1):
            node = _parse_node(raw, index, errors)
            if node is not None:
                nodes.append(node)
        _check_duplicates(nodes, errors)

    if errors or settings is None:
        raise ConfigError(errors)
    return settings, nodes


def load_config(path: Path) -> tuple[Settings, list[Node]]:
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError([f"config file not found: {path}"]) from None
    except OSError as exc:
        raise ConfigError([f"cannot read {path}: {exc.strerror}"]) from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError([f"invalid TOML in {path}: {exc}"]) from None
    return parse_config(data)
