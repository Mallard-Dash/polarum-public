"""`polarum -u NODE` / `polarum -ua`: review, typed confirmation, rolling install.

Kept apart from cmd.py, which holds the read-only and power commands. The flow
mirrors the TUI: read-only check, review, the typed word "proceed" (or
"override" when a rule blocks) in a real terminal, then one node at a time with
live progress. Any failure stops the run and skips the remaining nodes.
"""
from __future__ import annotations

import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Sequence

from .cmd import (
    EXIT_FAIL, EXIT_OK, _ask, emit, fail, resolve_node, unknown_node,
)
from .config import Node, Settings
from .events import EventLog
from .health import check_node
from .preflight import BAD, INFO, NA, WARN
from .reports import TABLE_HEADER, log_reports, node_detail_lines, table_row, verdict_banner
from .runner import FAILED, OK, RUNNING, SKIPPED, RemoteOps, UpdateRun
from .state import ONLINE
from .updates import NodeReport, apply_order, check_node_updates, chunk_packages, evaluate

STATUS_TAGS = {OK: "OK", FAILED: "FAIL", SKIPPED: "SKIP", RUNNING: "RUN", "PENDING": "WAIT"}
STATUS_LEVELS = {OK: INFO, FAILED: BAD, SKIPPED: WARN, RUNNING: INFO, "PENDING": NA}
MAX_PACKAGE_LINES = 8
PROGRESS_POLL = 0.5


def _stamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _print_review(reports: list[NodeReport], settings: Settings, verdict) -> None:
    cfg = settings.update
    if len(reports) > 1:
        emit(TABLE_HEADER)
        for rep in reports:
            line = table_row(rep, cfg)
            emit(line.text, line.level)
        emit("")
    for rep in reports:
        emit(f"== {rep.node_name} ==")
        for line in node_detail_lines(rep, cfg):
            emit(line.text, line.level)
        if not rep.error and rep.plan.installs:
            chunks = chunk_packages(rep.plan.installs)
            emit("Packages:")
            for chunk in chunks[:MAX_PACKAGE_LINES]:
                emit(f"  {chunk}")
            if len(chunks) > MAX_PACKAGE_LINES:
                emit(f"  ... and {len(chunks) - MAX_PACKAGE_LINES} more line(s); "
                     "the full list goes to the event log")
        emit("")
    banner = verdict_banner(verdict)
    emit(banner.text, banner.level)


def _print_plan(order: Sequence[Node], settings: Settings) -> None:
    cfg = settings.update
    emit("Rolling upgrade, one node at a time, in this order:")
    for i, node in enumerate(order, start=1):
        emit(f"  {i}. {node.name}" + (" (canary)" if node.canary else ""))
    reboots = {
        "manual": "Reboots: manual (nodes are never rebooted automatically)",
        "if_needed": "Reboots: AUTOMATIC when needed (reboot_policy = if_needed)",
        "never": "Reboots: ignored (reboot_policy = never)",
    }[cfg.reboot_policy]
    emit(reboots, BAD if cfg.reboot_policy == "if_needed" else INFO)
    snap = ("required (stops if impossible)" if cfg.require_snapshot
            else "ZFS snapshot before each node if possible" if cfg.snapshot_before else "off")
    emit(f"Snapshot: {snap}")
    emit("Any failure or timeout stops the run; remaining nodes are SKIPPED.")
    emit("Ctrl-C stops after the current node; a running upgrade is never interrupted.")


def _follow_run(run: UpdateRun) -> int:
    """Print progress lines as they change; returns the number of Ctrl-C presses seen."""
    seen: dict[str, tuple[str, str]] = {}
    interrupts = 0
    while True:
        snap = run.snapshot()
        for r in snap.results:
            current = (r.status, r.detail)
            if seen.get(r.name) != current and not (r.status == "PENDING" and r.name not in seen):
                seen[r.name] = current
                emit(f"{_stamp()} [{STATUS_TAGS[r.status]:^4}] {r.name}: {r.detail or r.status.lower()}",
                     STATUS_LEVELS[r.status])
        if snap.finished:
            return interrupts
        try:
            time.sleep(PROGRESS_POLL)
        except KeyboardInterrupt:
            interrupts += 1
            if interrupts == 1:
                run.request_stop()
                emit(f"{_stamp()} Stop requested: finishing the current node, then stopping. "
                     "Press Ctrl-C again to stop waiting (a started upgrade keeps running "
                     "on the node).", WARN)
            else:
                run.abandon()
                emit(f"{_stamp()} Stopped waiting.", WARN)
                time.sleep(PROGRESS_POLL * 2)
                return interrupts


def _print_summary(snap) -> int:
    emit("")
    emit(f"{'NODE':<18} {'RESULT':<8} {'PACKAGES':>8}  {'REBOOT':<7} DETAIL")
    for r in snap.results:
        detail = r.error or r.detail
        emit(f"{r.name:<18.18} {r.status:<8} {len(r.packages):>8}  "
             f"{'YES' if r.reboot_needed else 'no':<7} {detail}", STATUS_LEVELS[r.status])
        if r.snapshot:
            emit(f"    snapshot: {r.snapshot}", NA)
    if snap.error:
        emit(f"ERROR: {snap.error}", BAD)
    if any(r.reboot_needed for r in snap.results):
        emit("Nodes marked REBOOT NEEDED: reboot them from the TUI (select the node, press B).",
             WARN)
    ok = not snap.error and all(r.status == OK for r in snap.results)
    return EXIT_OK if ok else EXIT_FAIL


def cmd_update(settings: Settings, nodes: Sequence[Node], events: EventLog, target: str | None,
               ops: RemoteOps | None = None) -> int:
    """Check, review, confirm in the terminal, then run the rolling upgrade.

    `target` is a node name or IP; None means every node that has an update_order.
    """
    if target is None:
        chosen = apply_order([n for n in nodes if n.update_order is not None])
        if not chosen:
            return fail("no node has an update_order; nothing to update")
        scope = "all"
    else:
        node = resolve_node(nodes, target)
        if node is None:
            return unknown_node(nodes, target)
        chosen, scope = [node], node.name
    if not sys.stdin.isatty():
        return fail("installing updates needs the typed confirmation, which requires a terminal")

    with ThreadPoolExecutor(max_workers=max(4, len(chosen))) as pool:
        health = list(pool.map(check_node, chosen))
    down = [n.name for n, h in zip(chosen, health, strict=True) if h.health != ONLINE]
    if down:
        return fail(f"not ONLINE: {', '.join(down)}; update not started.")

    emit(f"Checking {len(chosen)} node(s) for updates (read-only)...")
    events.append(scope, "update-check", "started (read-only, cli)")
    with ThreadPoolExecutor(max_workers=max(4, len(chosen))) as pool:
        reports: list[NodeReport] = list(pool.map(check_node_updates, chosen))
    log_reports(events, reports)
    verdict = evaluate(reports, settings.update)
    emit("")
    _print_review(reports, settings, verdict)
    if verdict.errors:
        return EXIT_FAIL
    if not any(r.plan.installs for r in reports):
        emit("Nothing to update.")
        return EXIT_OK

    emit("")
    order = apply_order(chosen)
    _print_plan(order, settings)
    emit("")
    emit("WARNING: This installs packages as root on the node(s) above.", WARN)
    answer = _ask(f'Type exactly "{verdict.phrase}" to install updates on '
                  f"{scope if scope != 'all' else str(len(chosen)) + ' node(s)'}: ")
    if answer != verdict.phrase:
        emit("Cancelled; nothing was installed.")
        return EXIT_FAIL

    quorum_override = any(r.preflight is not None and r.preflight.quorate is False
                          for r in reports)
    run = UpdateRun(settings.update, reports, chosen, events,
                    ops=ops if ops is not None else RemoteOps(),
                    quorum_override=quorum_override)
    events.append(scope, "update",
                  f"run confirmed ({verdict.phrase}, cli); order: "
                  + ", ".join(n.name for n in run.order))
    emit("")
    worker = threading.Thread(target=run.run, name="polarum-update-run", daemon=True)
    worker.start()
    interrupts = _follow_run(run)
    if interrupts < 2:
        worker.join()
    code = _print_summary(run.snapshot())
    return 130 if interrupts >= 2 else code
