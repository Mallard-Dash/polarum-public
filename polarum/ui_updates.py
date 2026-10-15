"""Curses screens for the update workflow: review, package list, run, summary."""
from __future__ import annotations

import curses
from concurrent.futures import Future
from typing import Callable, Sequence

from .config import UpdateSettings
from .preflight import BAD, INFO, NA, WARN, Line
from .reports import (
    TABLE_HEADER, node_detail_lines, table_row, verdict_banner,
)
from .runner import FAILED, OK, PENDING, RUNNING, SKIPPED, RunSnapshot, UpdateRun
from .state import NodeState
from .tui import LINE_ATTRS, PAIR_ACCENT, PAIR_BAD, PAIR_WARN, pair, put
from .updates import NodeReport, Verdict

Tick = Callable[[], None]
STATUS_TAGS = {PENDING: "WAIT", RUNNING: "RUN", OK: "OK", FAILED: "FAIL", SKIPPED: "SKIP"}
STATUS_LEVELS = {PENDING: NA, RUNNING: WARN, OK: INFO, FAILED: BAD, SKIPPED: WARN}


def attr_for(level: str) -> int:
    return LINE_ATTRS[level]()


def draw_lines(stdscr, start_y: int, lines: Sequence[Line], x: int = 2) -> int:
    y = start_y
    for line in lines:
        put(stdscr, y, x, line.text, attr_for(line.level))
        y += 1
    return y


# --- screens ---

def wait_for_checks(
    stdscr, futures: dict[str, Future], tick: Tick, title: str = " CHECKING FOR UPDATES ",
) -> list[NodeReport] | None:
    """Progress screen while read-only checks run in workers. ESC cancels."""
    while True:
        tick()
        stdscr.erase()
        put(stdscr, 1, 2, title, curses.A_BOLD | pair(PAIR_ACCENT))
        put(stdscr, 3, 2, "Read-only: apt-get update + simulation + pre-flight. "
                          "Nothing is installed.", curses.A_DIM)
        y = 5
        for name, future in futures.items():
            put(stdscr, y, 2, f"{name:<20}{'done' if future.done() else 'checking...'}",
                0 if future.done() else pair(PAIR_WARN))
            y += 1
        put(stdscr, y + 1, 2, "Press ESC to cancel.", curses.A_DIM)
        stdscr.refresh()
        if all(f.done() for f in futures.values()):
            reports = []
            for name, future in futures.items():
                try:
                    reports.append(future.result())
                except Exception as exc:
                    reports.append(NodeReport(name, error=f"internal error: {type(exc).__name__}"))
            return reports
        if stdscr.getch() == 27:
            return None


def package_list_screen(stdscr, report: NodeReport, tick: Tick) -> None:
    rows: list[tuple[str, int]] = []
    for pkg in report.plan.installs:
        rows.append((f"{pkg.name:<34.34} {pkg.old or '(new)':<20.20} {pkg.new:<22.22} "
                     f"{pkg.origin_label}", 0))
    for rem in report.plan.removals:
        rows.append((f"{rem.name:<34.34} {rem.version or '':<20.20} {'REMOVE':<22}",
                     pair(PAIR_BAD) | curses.A_BOLD))
    offset = 0
    while True:
        tick()
        h, _ = stdscr.getmaxyx()
        page = max(1, h - 6)
        stdscr.erase()
        put(stdscr, 1, 2, f" PACKAGES on {report.node_name} ({len(rows)}) ",
            curses.A_BOLD | pair(PAIR_ACCENT))
        put(stdscr, 3, 2, f"{'PACKAGE':<34} {'OLD VERSION':<20} {'NEW VERSION':<22} ORIGIN",
            curses.A_BOLD)
        for i, (text, attr) in enumerate(rows[offset:offset + page]):
            put(stdscr, 4 + i, 2, text, attr)
        put(stdscr, h - 2, 2, f"↑/↓ PgUp/PgDn scroll   ESC back   "
                              f"({offset + 1}-{min(len(rows), offset + page)} of {len(rows)})",
            curses.A_DIM)
        stdscr.refresh()
        key = stdscr.getch()
        if key in (27, ord("q"), ord("Q"), ord("d"), ord("D")):
            return
        if key in (curses.KEY_DOWN, ord("j")):
            offset += 1
        elif key in (curses.KEY_UP, ord("k")):
            offset -= 1
        elif key == curses.KEY_NPAGE:
            offset += page
        elif key == curses.KEY_PPAGE:
            offset -= page
        elif key == curses.KEY_HOME:
            offset = 0
        elif key == curses.KEY_END:
            offset = len(rows)
        offset = max(0, min(offset, max(0, len(rows) - page)))


def review_screen(
    stdscr, reports: Sequence[NodeReport], settings: UpdateSettings, verdict: Verdict,
    tick: Tick, notice: str = "",
) -> bool:
    """Show the combined review. Returns True if the user chose to install."""
    selected = 0
    message = notice
    multi = len(reports) > 1
    while True:
        tick()
        stdscr.erase()
        report = reports[selected]
        title = f" UPDATE REVIEW: {len(reports)} node(s) " if multi else \
            f" UPDATE REVIEW: {report.node_name} "
        put(stdscr, 0, 2, title, curses.A_BOLD | pair(PAIR_ACCENT))
        banner = verdict_banner(verdict)
        put(stdscr, 2, 2, banner.text, attr_for(banner.level) | curses.A_BOLD)
        y = 4
        if multi:
            put(stdscr, y, 2, TABLE_HEADER, curses.A_BOLD)
            y += 1
            for i, rep in enumerate(reports):
                line = table_row(rep, settings)
                attr = attr_for(line.level) | (curses.A_REVERSE if i == selected else 0)
                put(stdscr, y, 2, line.text, attr)
                y += 1
            y += 1
        put(stdscr, y, 2, f"Node: {report.node_name}", curses.A_BOLD | pair(PAIR_ACCENT))
        draw_lines(stdscr, y + 1, node_detail_lines(report, settings))
        h, _ = stdscr.getmaxyx()
        keys = "D package list   I install   ESC cancel"
        if multi:
            keys = "←/→ switch node   " + keys
        put(stdscr, h - 2, 2, keys, curses.A_DIM)
        if message:
            put(stdscr, h - 1, 2, message, pair(PAIR_WARN))
        stdscr.refresh()
        key = stdscr.getch()
        if key == -1:
            continue
        message = ""
        if key in (27, ord("q"), ord("Q")):
            return False
        if key in (curses.KEY_RIGHT, ord("l"), ord("\t")) and multi:
            selected = (selected + 1) % len(reports)
        elif key in (curses.KEY_LEFT, ord("h")) and multi:
            selected = (selected - 1) % len(reports)
        elif key in (ord("d"), ord("D")):
            if report.error:
                message = "No package list: this node's check failed."
            else:
                package_list_screen(stdscr, report, tick)
        elif key in (ord("i"), ord("I")):
            if verdict.can_apply:
                return True
            message = "Installation disabled: at least one node's check failed."


def sync_node_states(snapshot: RunSnapshot, states: Sequence[NodeState]) -> None:
    """Reflect run progress on the main table rows (UPDATING / REBOOTING / REBOOT NEEDED)."""
    by_name = {st.node.name: st for st in states}
    for result in snapshot.results:
        st = by_name.get(result.name)
        if st is None:
            continue
        if st.busy in (None, "update", "reboot"):
            st.busy = result.busy_kind if result.status == RUNNING else None
        if result.status == OK:
            st.reboot_needed = result.reboot_needed


def run_screen(stdscr, run: UpdateRun, states: Sequence[NodeState], tick: Tick) -> RunSnapshot:
    """Live progress of the rolling run. ESC requests a stop after the current node."""
    stop_noted = False
    while True:
        tick()
        snap = run.snapshot()
        sync_node_states(snap, states)
        stdscr.erase()
        put(stdscr, 0, 2, " UPDATE RUN (rolling, one node at a time) ",
            curses.A_BOLD | pair(PAIR_ACCENT))
        y = 2
        for r in snap.results:
            level = STATUS_LEVELS[r.status]
            put(stdscr, y, 2, f"[{STATUS_TAGS[r.status]:^5}] {r.name:<18.18} {r.detail}",
                attr_for(level))
            y += 1
        y += 1
        put(stdscr, y, 2, "Latest apt/journal output:", curses.A_BOLD)
        for i, line in enumerate(snap.tail[-10:]):
            put(stdscr, y + 1 + i, 4, line, curses.A_DIM)
        h, _ = stdscr.getmaxyx()
        if snap.error:
            put(stdscr, h - 3, 2, f"ERROR: {snap.error}", pair(PAIR_BAD) | curses.A_BOLD)
        if snap.finished:
            put(stdscr, h - 2, 2, "Run finished. Press any key for the summary.", curses.A_BOLD)
        elif run.stop_requested.is_set() or stop_noted:
            put(stdscr, h - 2, 2, "Stop requested: finishing the current node, then stopping.",
                pair(PAIR_WARN))
        else:
            put(stdscr, h - 2, 2, "ESC: stop after the current node (a running upgrade is "
                                  "never interrupted)", curses.A_DIM)
        stdscr.refresh()
        key = stdscr.getch()
        if snap.finished and key != -1:
            return snap
        if key == 27 and not snap.finished:
            run.request_stop()
            stop_noted = True


def summary_screen(stdscr, snap: RunSnapshot, tick: Tick) -> None:
    while True:
        tick()
        stdscr.erase()
        put(stdscr, 0, 2, " UPDATE SUMMARY ", curses.A_BOLD | pair(PAIR_ACCENT))
        put(stdscr, 2, 2, f"{'NODE':<18} {'RESULT':<8} {'PACKAGES':>8}  {'REBOOT NEEDED':<14} DETAIL",
            curses.A_BOLD)
        y = 3
        for r in snap.results:
            reboot = "YES" if r.reboot_needed else "no"
            detail = r.error or r.detail
            text = (f"{r.name:<18.18} {r.status:<8} {len(r.packages):>8}  {reboot:<14} {detail}")
            put(stdscr, y, 2, text, attr_for(STATUS_LEVELS[r.status]))
            if r.snapshot:
                y += 1
                put(stdscr, y, 4, f"snapshot: {r.snapshot}", curses.A_DIM)
            y += 1
        y += 1
        if snap.error:
            put(stdscr, y, 2, f"ERROR: {snap.error}", pair(PAIR_BAD) | curses.A_BOLD)
            y += 1
        if any(r.reboot_needed for r in snap.results):
            put(stdscr, y, 2, "Nodes marked REBOOT NEEDED: select the node and press B to reboot.",
                pair(PAIR_WARN))
        h, _ = stdscr.getmaxyx()
        put(stdscr, h - 2, 2, "Press any key to return.", curses.A_DIM)
        stdscr.refresh()
        if stdscr.getch() != -1:
            return
