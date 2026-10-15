"""Curses-free report text shared by the TUI review screen and the CLI."""
from __future__ import annotations

from .config import UpdateSettings
from .events import EventLog
from .preflight import BAD, INFO, NA, WARN, Line, Preflight
from .updates import NodeReport, Verdict, evaluate_report, origin_counts


def preflight_compact(pf: Preflight | None) -> list[Line]:
    if pf is None:
        return [Line("Pre-flight: not available", NA)]
    guests = "n/a" if pf.running_vms is None and pf.running_cts is None else (
        f"{pf.running_vms if pf.running_vms is not None else 'n/a'} VM(s), "
        f"{pf.running_cts if pf.running_cts is not None else 'n/a'} container(s)")
    if pf.quorate is None:
        quorum = Line("Quorum: n/a", NA)
    else:
        votes = (f" (votes {pf.total_votes}, quorum {pf.quorum_needed})"
                 if pf.total_votes is not None and pf.quorum_needed is not None else "")
        quorum = Line(f"Quorum: {'OK' if pf.quorate else 'LOST'}{votes}",
                      INFO if pf.quorate else BAD)
    backup = ("n/a" if pf.backup_running is None
              else "IN PROGRESS" if pf.backup_running else "none")
    zfs = ("n/a" if pf.zfs_activity is None
           else (" + ".join(pf.zfs_activity) + " IN PROGRESS") if pf.zfs_activity else "none")
    return [
        quorum,
        Line(f"Running: {guests}", WARN if (pf.running_vms or pf.running_cts) else INFO),
        Line(f"Backup: {backup}", BAD if pf.backup_running else INFO),
        Line(f"ZFS scrub/resilver: {zfs}", BAD if pf.zfs_activity else INFO),
    ]


def node_detail_lines(report: NodeReport, settings: UpdateSettings) -> list[Line]:
    if report.error:
        return [Line(f"CHECK FAILED: {report.error}", BAD)]
    plan = report.plan
    lines = [Line(
        f"Upgrades: {len(plan.upgrades)}   New: {len(plan.new_packages)}   "
        f"Removed: {len(plan.removals)}   Held back: {plan.held_back}",
        BAD if plan.removals else INFO)]
    if report.reboot_needed:
        kernels = ""
        if report.running_kernel and report.newest_kernel \
                and report.running_kernel != report.newest_kernel:
            kernels = f" (kernel {report.running_kernel} -> {report.newest_kernel})"
        lines.append(Line(f"Reboot needed: yes{kernels}", WARN))
    else:
        lines.append(Line("Reboot needed: no", INFO))
    lines += preflight_compact(report.preflight)
    counts = origin_counts(plan)
    if counts:
        allowed = set(settings.allowed_origins)
        lines.append(Line("Packages per origin:", INFO))
        for label, count in counts:
            ok = label != "unknown" and set(label.split("+")) <= allowed
            lines.append(Line(f"  {label:<34.34}{count:>5}  {'allowed' if ok else 'NOT ALLOWED'}",
                              INFO if ok else BAD))
    verdict = evaluate_report(report, settings)
    lines += [Line(f"BLOCK: {f.text}", BAD) for f in verdict.blocks]
    lines += [Line(f"WARN:  {f.text}", WARN) for f in verdict.warns]
    return lines


def table_row(report: NodeReport, settings: UpdateSettings) -> Line:
    v = evaluate_report(report, settings)
    if report.error:
        return Line(f"{report.node_name:<16.16} CHECK FAILED", BAD)
    plan, pf = report.plan, report.preflight
    quorum = "n/a" if pf is None or pf.quorate is None else ("OK" if pf.quorate else "LOST")
    rules = "BLOCK" if v.blocks else "WARN" if v.warns else "ok"
    text = (f"{report.node_name:<16.16} {len(plan.upgrades):>4} {len(plan.new_packages):>4} "
            f"{len(plan.removals):>4} {plan.held_back:>5}  "
            f"{'yes' if report.reboot_needed else 'no':<7}{quorum:<7}{rules}")
    return Line(text, BAD if v.blocks else WARN if v.warns else INFO)


TABLE_HEADER = f"{'NODE':<16} {'UPG':>4} {'NEW':>4} {'REM':>4} {'HELD':>5}  {'REBOOT':<7}{'QUORUM':<7}RULES"


def verdict_banner(verdict: Verdict) -> Line:
    if verdict.errors:
        return Line(f"CHECK FAILED on {len(verdict.errors)} node(s): installation is disabled.", BAD)
    if verdict.blocks:
        return Line(f"BLOCKED: {len(verdict.blocks)} issue(s). Installing requires typing "
                    '"override".', BAD)
    if verdict.warns:
        return Line(f"{len(verdict.warns)} warning(s). Installing requires typing \"proceed\".", WARN)
    return Line('No issues found. Installing requires typing "proceed".', INFO)


def log_reports(events: EventLog, reports: list[NodeReport]) -> None:
    """Record the outcome of each update check in the event log."""
    for rep in reports:
        if rep.error:
            events.append(rep.node_name, "update-check", f"FAILED: {rep.error}")
            continue
        plan = rep.plan
        events.append(
            rep.node_name, "update-check",
            f"upgrades={len(plan.upgrades)} new={len(plan.new_packages)} "
            f"remove={len(plan.removals)} held={plan.held_back} "
            f"reboot={'yes' if rep.reboot_needed else 'no'}")
