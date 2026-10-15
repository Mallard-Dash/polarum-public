"""Non-interactive commands: status, wake, off, check.

They follow the TUI's safety rules: wake needs a single confirmation (or --yes),
power off needs a read-only pre-flight plus the typed word "proceed" in a real
terminal, and `check` never installs anything. Installing lives in cmd_update.py. Anything uncertain stops and
reports (exit code 1).
"""
from __future__ import annotations

import ipaddress
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Sequence

from .actions import run_action
from .config import Node, Settings
from .events import EventLog
from .health import check_node, check_port
from .preflight import BAD, INFO, NA, WARN, report_lines, run_preflight, summary
from .reports import TABLE_HEADER, log_reports, node_detail_lines, table_row, verdict_banner
from .state import BOOTING, ONLINE, classify_ports
from .updates import NodeReport, apply_order, check_node_updates, evaluate

EXIT_OK, EXIT_FAIL, EXIT_USAGE, EXIT_BLOCKED = 0, 1, 2, 3
CONFIRM_PHRASE = "proceed"

_ANSI = {INFO: "", WARN: "\x1b[33m", BAD: "\x1b[31;1m", NA: "\x1b[2m"}


def emit(text: str, level: str = INFO, stream=None) -> None:
    stream = stream or sys.stdout
    use_color = stream.isatty() and "NO_COLOR" not in os.environ and _ANSI.get(level)
    print(f"{_ANSI[level]}{text}\x1b[0m" if use_color else text, file=stream)


def fail(text: str) -> int:
    emit(f"polarum: {text}", BAD, sys.stderr)
    return EXIT_FAIL


def find_node(nodes: Sequence[Node], name: str) -> Node | None:
    for node in nodes:
        if node.name.lower() == name.lower():
            return node
    return None


def resolve_node(nodes: Sequence[Node], text: str) -> Node | None:
    """Find a node by name (case-insensitive) or by IP address."""
    node = find_node(nodes, text)
    if node is not None:
        return node
    try:
        wanted = ipaddress.ip_address(text)
    except ValueError:
        return None
    for node in nodes:
        if ipaddress.ip_address(node.ip) == wanted:
            return node
    return None


def unknown_node(nodes: Sequence[Node], name: str) -> int:
    emit(f"polarum: unknown node {name!r}. Configured nodes: "
         f"{', '.join(n.name for n in nodes)}", BAD, sys.stderr)
    return EXIT_USAGE


def _ask(prompt: str) -> str | None:
    try:
        return input(prompt)
    except EOFError:
        return None


# --- status ---

def _status_level(health: str) -> str:
    return {"ONLINE": INFO, "BOOTING": WARN}.get(health, BAD)


def _verbose_lines(node: Node) -> list[tuple[str, str]]:
    """Per-node detail for `status -v`: configuration plus one TCP check per port."""
    ports = []
    for port in node.health_ports:
        ms = check_port(node.ip, port)
        ports.append((port, ms))
    health = classify_ports([ms is not None for _, ms in ports])
    port_text = ", ".join(
        f"{port} up ({ms:.1f} ms)" if ms is not None else f"{port} DOWN" for port, ms in ports)
    order = "-" if node.update_order is None else (
        f"{node.update_order}" + (" (canary)" if node.canary else ""))
    rows = [
        ("Node", node.name), ("Status", health), ("IP", node.ip), ("MAC", node.mac),
        ("SSH target", node.ssh_target), ("Health ports", port_text),
        ("Update order", order),
    ]
    return [(f"  {label + ':':<14}{value}",
             _status_level(health) if label == "Status" else INFO) for label, value in rows]


def cmd_status(settings: Settings, nodes: Sequence[Node], target: str | None = None,
               verbose: bool = False) -> int:
    chosen = list(nodes)
    if target is not None:
        node = resolve_node(nodes, target)
        if node is None:
            return unknown_node(nodes, target)
        chosen = [node]
    with ThreadPoolExecutor(max_workers=max(4, len(chosen))) as pool:
        results = list(pool.map(check_node, chosen))
    emit(f"{'NODE':<16} {'IP ADDRESS':<16} {'STATUS':<10} {'LATENCY':>10}")
    for node, result in zip(chosen, results, strict=True):
        latency = f"{result.latency_ms:.1f} ms" if result.latency_ms is not None else "-"
        emit(f"{node.name:<16.16} {node.ip:<16.16} {result.health:<10} {latency:>10}",
             _status_level(result.health))
    if verbose:
        for node in chosen:
            emit("")
            for text, level in _verbose_lines(node):
                emit(text, level)
    return EXIT_OK


# --- wake ---

def cmd_wake(settings: Settings, nodes: Sequence[Node], events: EventLog,
             name: str, assume_yes: bool) -> int:
    node = find_node(nodes, name)
    if node is None:
        return unknown_node(nodes, name)
    health = check_node(node).health
    if health in (ONLINE, BOOTING):
        emit(f"{node.name} is already running ({health}); nothing to do.")
        return EXIT_OK
    if not assume_yes:
        if not sys.stdin.isatty():
            return fail("confirmation needs a terminal; run interactively or pass --yes")
        answer = _ask(f"Send Wake-on-LAN magic packet to {node.name} ({node.mac})? [y/N] ")
        if answer is None or answer.strip().lower() not in ("y", "yes"):
            emit("Cancelled.")
            return EXIT_FAIL
    events.append(node.name, "wake", "started (cli)" + (" --yes" if assume_yes else ""))
    result = run_action(settings, node, "wake")
    if result.ok:
        events.append(node.name, "wake", f"ok: {result.detail}")
        emit(f"{node.name}: {result.detail}")
        return EXIT_OK
    events.append(node.name, "wake", f"FAILED: {result.detail}")
    return fail(f"{node.name}: wake failed: {result.detail}")


# --- power off ---

def cmd_off(settings: Settings, nodes: Sequence[Node], events: EventLog, name: str) -> int:
    node = find_node(nodes, name)
    if node is None:
        return unknown_node(nodes, name)
    if check_node(node).health not in (ONLINE, BOOTING):
        return fail(f"{node.name} is not reachable; power off not sent.")
    if not sys.stdin.isatty():
        return fail("power off needs the typed confirmation, which requires a terminal; "
                    "there is no --yes for power off")
    pf = run_preflight(node)
    events.append(node.name, "preflight", summary(pf) + " (cli)")
    for line in report_lines(pf):
        emit(line.text, line.level)
    if pf.error:
        return fail(f"pre-flight failed; power off of {node.name} not sent.")
    emit("")
    emit("WARNING: This changes the node's power state.", WARN)
    emit("Do NOT continue if the node may be writing important data (databases,", WARN)
    emit("updates, backups, replication, VM/container activity, or similar work).", WARN)
    answer = _ask(f'Type exactly "{CONFIRM_PHRASE}" to power off {node.name}: ')
    if answer != CONFIRM_PHRASE:
        emit("Cancelled; nothing was sent.")
        return EXIT_FAIL
    events.append(node.name, "poweroff", "started (cli)")
    result = run_action(settings, node, "poweroff")
    if result.ok:
        events.append(node.name, "poweroff", f"ok: {result.detail}")
        emit(f"{node.name}: {result.detail}")
        return EXIT_OK
    events.append(node.name, "poweroff", f"FAILED: {result.detail}")
    return fail(f"{node.name}: power off failed: {result.detail}")


# --- update check (read-only) ---

def cmd_check(settings: Settings, nodes: Sequence[Node], events: EventLog, target: str) -> int:
    if target.lower() == "all":
        chosen = apply_order([n for n in nodes if n.update_order is not None])
        if not chosen:
            return fail("no node has an update_order; nothing to check")
    else:
        node = find_node(nodes, target)
        if node is None:
            return unknown_node(nodes, target)
        chosen = [node]
    events.append(target, "update-check", "started (read-only, cli)")
    with ThreadPoolExecutor(max_workers=max(4, len(chosen))) as pool:
        reports: list[NodeReport] = list(pool.map(check_node_updates, chosen))
    log_reports(events, reports)
    verdict = evaluate(reports, settings.update)

    if len(reports) > 1:
        emit(TABLE_HEADER)
        for rep in reports:
            line = table_row(rep, settings.update)
            emit(line.text, line.level)
        emit("")
    for rep in reports:
        emit(f"== {rep.node_name} ==")
        for line in node_detail_lines(rep, settings.update):
            emit(line.text, line.level)
        emit("")
    banner = verdict_banner(verdict)
    emit(banner.text, banner.level)
    emit("Nothing was installed (check is read-only; install with `polarum -u NODE` or from the TUI).")
    if verdict.errors:
        return EXIT_FAIL
    return EXIT_BLOCKED if verdict.needs_override else EXIT_OK


def run_command(
    command: str, args, settings: Settings, nodes: Sequence[Node], events: EventLog | None,
) -> int:
    if command == "status":
        return cmd_status(settings, nodes, getattr(args, "target", None),
                          getattr(args, "verbose", False))
    if events is None:
        return fail(f"internal error: no event log for {command!r}")
    if command == "wake":
        return cmd_wake(settings, nodes, events, args.node, args.yes)
    if command == "off":
        return cmd_off(settings, nodes, events, args.node)
    if command == "check":
        return cmd_check(settings, nodes, events, args.target)
    return EXIT_USAGE
