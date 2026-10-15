"""Curses front end. Slow work (network, SSH) always runs in worker threads."""
from __future__ import annotations

import curses
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait

from .actions import ACTIONS, ActionResult, ActionSpec, run_action
from .config import Settings
from .events import EventLog
from .health import HealthResult, check_node
from .preflight import Preflight, report_lines, run_preflight, summary
from .reports import log_reports
from .runner import UpdateRun
from .updates import NodeReport, apply_order, check_node_updates, evaluate
from .ui_updates import review_screen, run_screen, summary_screen, wait_for_checks
from .state import (
    BOOTING, OFFLINE, ONLINE, NodeState, apply_health, display_status, track_reboot,
    wake_timed_out,
)
from .tui import (
    LEVEL_PAIRS, LINE_ATTRS, PAIR_ACCENT, PAIR_BAD, PAIR_OK, PAIR_WARN,
    confirm_light, confirm_typed, pair, put,
)

HEALTH_INTERVAL = 2.0
LOG_LINES = 5
DRAIN_TIMEOUT = 15.0

TITLE = " POLARUM // Homelab Power Console "
CONTROLS = (
    "↑/↓ J/K select   W wake   P power off   B reboot   R refresh   Q quit",
    "U check updates (selected node)   Shift+U check all nodes in update_order",
)


def preflight_screen_lines(pf: Preflight) -> list[tuple[str, int]]:
    return [(line.text, LINE_ATTRS[line.level]()) for line in report_lines(pf)]


def wait_for_preflight(stdscr, future: Future, node_name: str) -> Preflight | None:
    """Show a waiting screen while the worker runs; ESC cancels. Never blocks the workers."""
    while True:
        stdscr.erase()
        put(stdscr, 1, 2, " PRE-FLIGHT ", curses.A_BOLD | pair(PAIR_ACCENT))
        put(stdscr, 3, 2, f"Running read-only checks on {node_name}...")
        put(stdscr, 5, 2, "Press ESC to cancel.", curses.A_DIM)
        stdscr.refresh()
        if future.done():
            try:
                return future.result()
            except Exception as exc:
                return Preflight(error=f"internal error: {type(exc).__name__}")
        if stdscr.getch() == 27:
            return None


def confirm_lines(spec: ActionSpec, st: NodeState, resend: bool) -> list[tuple[str, int]]:
    node = st.node
    head = [(f"Target node: {node.name} ({node.ip})", curses.A_BOLD)]
    if spec.kind == "wake":
        head.append((f"MAC: {node.mac}", 0))
        if resend:
            head.append(("The previous wake attempt timed out.", pair(PAIR_WARN)))
            head.append(("Resend the Wake-on-LAN magic packet?", 0))
        else:
            head.append(("Send a Wake-on-LAN magic packet?", 0))
        return head
    warning = [
        "WARNING: This changes the node's power state.",
        "Do NOT continue if the node may be writing important data (databases,",
        "updates, backups, replication, VM/container activity, or similar work).",
    ]
    return head + [("", 0)] + [(line, pair(PAIR_WARN)) for line in warning] + [("", 0)]


# --- application state and loop ---

class App:
    def __init__(self, settings: Settings, states: list[NodeState], events: EventLog) -> None:
        self.settings = settings
        self.states = states
        self.events = events
        self.selected = 0
        self.message = ""
        self.message_until = 0.0
        self.last_health = 0.0
        self.health_pool = ThreadPoolExecutor(max_workers=max(4, len(states)))
        self.action_pool = ThreadPoolExecutor(max_workers=4)
        self.update_pool = ThreadPoolExecutor(max_workers=max(4, len(states) + 1))
        self.update_run: UpdateRun | None = None
        self.health_jobs: dict[Future, NodeState] = {}
        self.action_jobs: dict[Future, tuple[NodeState, str]] = {}

    # messages
    def say(self, text: str, seconds: float = 4.0) -> None:
        self.message = text
        self.message_until = time.monotonic() + seconds

    # health polling
    def refresh_health(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self.last_health < HEALTH_INTERVAL:
            return
        in_flight = {id(st) for st in self.health_jobs.values()}
        for st in self.states:
            if id(st) not in in_flight:
                self.health_jobs[self.health_pool.submit(check_node, st.node)] = st
        self.last_health = now

    def poll_health(self) -> None:
        for future in [f for f in self.health_jobs if f.done()]:
            st = self.health_jobs.pop(future)
            try:
                result: HealthResult = future.result()
            except Exception:
                result = HealthResult(OFFLINE, None)
            was_waking = st.wake_sent_at
            apply_health(st, result.health, result.latency_ms)
            track_reboot(st, result.health)
            if was_waking is not None and st.health == ONLINE:
                elapsed = time.monotonic() - was_waking
                self.events.append(st.node.name, "wake", f"node online after {int(elapsed)}s")

    def check_wake_timeouts(self) -> None:
        now = time.monotonic()
        timeout = self.settings.wake_timeout
        for st in self.states:
            if wake_timed_out(st, now, timeout) and not st.wake_timeout_reported:
                st.wake_timeout_reported = True
                self.events.append(
                    st.node.name, "wake", f"timeout: not online after {int(timeout)}s")
                self.say(f"{st.node.name}: wake timed out; select it and press W to resend.", 8)

    # actions
    def refusal(self, st: NodeState, kind: str) -> str | None:
        name = st.node.name
        if st.busy:
            return f"{name} is busy ({st.busy} in progress)."
        if kind == "wake" and st.health in (ONLINE, BOOTING):
            return f"{name} is already running; wake not sent."
        if kind in ("poweroff", "reboot") and st.health not in (ONLINE, BOOTING):
            return f"{name} is not reachable; {ACTIONS[kind].title} not sent."
        return None

    def request_action(self, stdscr, kind: str) -> None:
        st = self.states[self.selected]
        spec = ACTIONS[kind]
        problem = self.refusal(st, kind)
        if problem:
            self.say(problem)
            return
        resend = kind == "wake" and wake_timed_out(
            st, time.monotonic(), self.settings.wake_timeout)
        lines = confirm_lines(spec, st, resend)
        if spec.strict:
            future = self.action_pool.submit(run_preflight, st.node)
            pf = wait_for_preflight(stdscr, future, st.node.name)
            if pf is None:
                self.say(f"{spec.title.capitalize()} cancelled.", 2)
                return
            self.events.append(st.node.name, "preflight", summary(pf))
            if pf.error:
                self.say(f"{st.node.name}: pre-flight failed ({pf.error}); "
                         f"{spec.title} not sent.", 8)
                return
            confirmed = confirm_typed(
                stdscr, spec.title, lines + preflight_screen_lines(pf))
        else:
            confirmed = confirm_light(stdscr, spec.title, lines)
        if not confirmed:
            self.say(f"{spec.title.capitalize()} cancelled.", 2)
            return
        self.submit(st, kind)

    def submit(self, st: NodeState, kind: str) -> None:
        st.busy = kind
        self.events.append(st.node.name, kind, "started")
        future = self.action_pool.submit(run_action, self.settings, st.node, kind)
        self.action_jobs[future] = (st, kind)

    def poll_actions(self) -> None:
        for future in [f for f in self.action_jobs if f.done()]:
            st, kind = self.action_jobs.pop(future)
            try:
                result: ActionResult = future.result()
            except Exception as exc:
                result = ActionResult(False, f"internal error: {type(exc).__name__}")
            st.busy = None
            name = st.node.name
            title = ACTIONS[kind].title
            if result.ok:
                self.events.append(name, kind, f"ok: {result.detail}")
                self.say(f"{name}: {result.detail}", 5)
                if kind == "wake":
                    st.wake_sent_at = time.monotonic()
                    st.wake_timeout_reported = False
                else:
                    st.wake_sent_at = None
                    if kind == "reboot":
                        st.reboot_sent = True
                        st.reboot_seen_down = False
            else:
                self.events.append(name, kind, f"FAILED: {result.detail}")
                self.say(f"{name}: {title} failed: {result.detail}", 8)

    def tick(self) -> None:
        self.poll_health()
        self.poll_actions()
        self.refresh_health()
        self.check_wake_timeouts()
        if self.message and time.monotonic() >= self.message_until:
            self.message = ""

    def handle_key(self, stdscr, key: int) -> bool:
        """Return False to quit."""
        count = len(self.states)
        if key in (ord("q"), ord("Q")):
            return False
        if key in (curses.KEY_UP, ord("k"), ord("K")):
            self.selected = (self.selected - 1) % count
        elif key in (curses.KEY_DOWN, ord("j"), ord("J")):
            self.selected = (self.selected + 1) % count
        elif key in (ord("r"), ord("R")):
            self.refresh_health(force=True)
            self.say("Refreshing...", 2)
        elif key in (ord("w"), ord("W")):
            self.request_action(stdscr, "wake")
        elif key in (ord("p"), ord("P")):
            self.request_action(stdscr, "poweroff")
        elif key in (ord("b"), ord("B")):
            self.request_action(stdscr, "reboot")
        elif key == ord("u"):
            self.start_update_flow(stdscr, check_all=False)
        elif key == ord("U"):
            self.start_update_flow(stdscr, check_all=True)
        return True

    # updates
    def update_targets(self, check_all: bool) -> list[NodeState] | str:
        """Nodes to check, or a refusal message."""
        if check_all:
            chosen = [st for st in self.states if st.node.update_order is not None]
            if not chosen:
                return "No node has an update_order; nothing to check. See nodes.toml."
        else:
            chosen = [self.states[self.selected]]
        for st in chosen:
            if st.busy:
                return f"{st.node.name} is busy ({st.busy} in progress)."
            if st.health != ONLINE:
                return f"{st.node.name} is not ONLINE; update check not started."
        by_name = {st.node.name: st for st in chosen}
        return [by_name[n.name] for n in apply_order([st.node for st in chosen])]

    def start_update_flow(self, stdscr, check_all: bool) -> None:
        targets = self.update_targets(check_all)
        if isinstance(targets, str):
            self.say(targets, 6)
            return
        scope = "all" if check_all else targets[0].node.name
        self.events.append(scope, "update-check", "started (read-only)")
        futures = {st.node.name: self.update_pool.submit(check_node_updates, st.node)
                   for st in targets}
        reports = wait_for_checks(stdscr, futures, self.tick)
        if reports is None:
            self.events.append(scope, "update-check", "cancelled")
            self.say("Update check cancelled.", 3)
            return
        self.record_reports(reports)
        verdict = evaluate(reports, self.settings.update)
        if not review_screen(stdscr, reports, self.settings.update, verdict, self.tick):
            self.say("Review closed; nothing was installed.", 4)
            return
        self.confirm_and_run(stdscr, targets, reports, verdict)

    def record_reports(self, reports: list[NodeReport]) -> None:
        by_name = {st.node.name: st for st in self.states}
        for rep in reports:
            if not rep.error:
                by_name[rep.node_name].reboot_needed = rep.reboot_needed
        log_reports(self.events, reports)

    def confirm_and_run(self, stdscr, targets: list[NodeState],
                        reports: list[NodeReport], verdict) -> None:
        cfg = self.settings.update
        lines = [("Rolling upgrade, one node at a time, in this order:", curses.A_BOLD)]
        for i, st in enumerate(targets[:6], start=1):
            tag = " (canary)" if st.node.canary else ""
            lines.append((f"  {i}. {st.node.name}{tag}", 0))
        if len(targets) > 6:
            lines.append((f"  ... and {len(targets) - 6} more", 0))
        findings = [("BLOCK: " + f"{f.node}: {f.text}", pair(PAIR_BAD)) for f in verdict.blocks]
        findings += [("WARN:  " + f"{f.node}: {f.text}", pair(PAIR_WARN)) for f in verdict.warns]
        lines += [("", 0)] + findings[:8]
        if len(findings) > 8:
            lines.append((f"... and {len(findings) - 8} more (see the review)", pair(PAIR_WARN)))
        reboot_text = {
            "manual": "Reboots: manual (nodes are never rebooted automatically)",
            "if_needed": "Reboots: AUTOMATIC when needed (reboot_policy = if_needed)",
            "never": "Reboots: ignored (reboot_policy = never)",
        }[cfg.reboot_policy]
        lines += [("", 0), (reboot_text, pair(PAIR_BAD) if cfg.reboot_policy == "if_needed"
                            else 0)]
        snap = ("required (stops if impossible)" if cfg.require_snapshot
                else "ZFS snapshot before each node if possible" if cfg.snapshot_before else "off")
        lines.append((f"Snapshot: {snap}", 0))
        lines.append(("Any failure or timeout stops the run; remaining nodes are SKIPPED.", 0))
        lines.append(("", 0))
        if not confirm_typed(stdscr, "install updates", lines, verdict.phrase):
            self.say("Installation cancelled; nothing was installed.", 4)
            return
        quorum_override = any(r.preflight is not None and r.preflight.quorate is False
                              for r in reports)
        run = UpdateRun(cfg, reports, [st.node for st in targets], self.events,
                        quorum_override=quorum_override)
        self.events.append("all" if len(targets) > 1 else targets[0].node.name, "update",
                           f"run confirmed ({verdict.phrase}); order: "
                           + ", ".join(n.name for n in run.order))
        self.update_run = run
        self.update_pool.submit(run.run)
        snapshot = run_screen(stdscr, run, self.states, self.tick)
        self.update_run = None
        summary_screen(stdscr, snapshot, self.tick)
        failed = any(r.status == "FAILED" for r in snapshot.results)
        self.say("Update run FAILED; see the summary and event log." if failed
                 else "Update run finished.", 8)

    def close(self) -> None:
        """Let in-flight actions finish so their results are logged."""
        if self.update_run is not None:
            self.update_run.abandon()
        pending = list(self.action_jobs)
        if pending:
            wait(pending, timeout=DRAIN_TIMEOUT)
            self.poll_actions()
        self.health_pool.shutdown(wait=False, cancel_futures=True)
        self.action_pool.shutdown(wait=False, cancel_futures=True)
        self.update_pool.shutdown(wait=False, cancel_futures=True)

    # drawing
    def draw(self, stdscr) -> None:
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        put(stdscr, 0, 2, TITLE, curses.A_BOLD | pair(PAIR_ACCENT))
        route = (f"WoL relay: {self.settings.relay}" if self.settings.relay
                 else "WoL: direct UDP broadcast")
        put(stdscr, 1, 2, f"{route}   Broadcast: {self.settings.broadcast}", curses.A_DIM)
        if w < 70 or h < 12:
            put(stdscr, 3, 2, "Terminal too small. Resize to at least 70x12.", pair(PAIR_BAD))
            stdscr.refresh()
            return

        top, left, right = 3, 2, w - 3
        accent = pair(PAIR_ACCENT)
        put(stdscr, top, left, "┌" + "─" * (right - left - 1) + "┐", accent)
        put(stdscr, top + 1, left, "│", accent)
        put(stdscr, top + 1, right, "│", accent)
        put(stdscr, top + 1, left + 2,
            f"{'NODE':<16} {'IP ADDRESS':<16} {'STATUS':<14} {'LATENCY':>10}", curses.A_BOLD)
        put(stdscr, top + 2, left, "├" + "─" * (right - left - 1) + "┤", accent)

        now = time.monotonic()
        for i, st in enumerate(self.states):
            y = top + 3 + i
            label, level = display_status(st, now, self.settings.wake_timeout)
            latency = f"{st.latency_ms:.1f} ms" if st.latency_ms is not None else "—"
            row = (f"{st.node.name:<16.16} {st.node.ip:<16.16} "
                   f"{label:<14.14} {latency:>10}")
            attr = pair(LEVEL_PAIRS[level])
            if i == self.selected:
                attr |= curses.A_REVERSE | curses.A_BOLD
            put(stdscr, y, left, "│", accent)
            put(stdscr, y, left + 2, row, attr)
            put(stdscr, y, right, "│", accent)

        border_y = top + 3 + len(self.states)
        put(stdscr, border_y, left, "└" + "─" * (right - left - 1) + "┘", accent)
        for i, text in enumerate(CONTROLS):
            put(stdscr, border_y + 2 + i, 2, text, curses.A_DIM)
        self.draw_log(stdscr, border_y + 5, h)
        if self.message:
            put(stdscr, h - 1, 2, self.message, pair(PAIR_WARN))
        stdscr.refresh()

    def draw_log(self, stdscr, y: int, height: int) -> None:
        put(stdscr, y, 2, "Recent events", curses.A_BOLD)
        events = self.events.tail(LOG_LINES)
        if not events:
            put(stdscr, y + 1, 2, "(none yet)", curses.A_DIM)
        for i, event in enumerate(events):
            if y + 1 + i < height - 1:
                attr = pair(PAIR_BAD) if "FAILED" in event.result or "timeout" in event.result else 0
                put(stdscr, y + 1 + i, 2, event.short(), attr)


def run_ui(stdscr, settings: Settings, states: list[NodeState], events: EventLog) -> None:
    curses.curs_set(0)
    stdscr.timeout(100)
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(PAIR_OK, curses.COLOR_GREEN, -1)
    curses.init_pair(PAIR_BAD, curses.COLOR_RED, -1)
    curses.init_pair(PAIR_WARN, curses.COLOR_YELLOW, -1)
    curses.init_pair(PAIR_ACCENT, curses.COLOR_CYAN, -1)

    app = App(settings, states, events)
    try:
        app.refresh_health(force=True)
        while True:
            app.tick()
            app.draw(stdscr)
            key = stdscr.getch()
            if key != -1 and not app.handle_key(stdscr, key):
                break
    finally:
        app.close()
