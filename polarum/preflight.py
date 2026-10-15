"""Read-only pre-flight checks shown before power off / reboot (and updates).

One SSH session runs a small POSIX script that probes each check. Every probe
degrades to "n/a" when the command is missing or not permitted, so non-Proxmox
nodes work too. Nothing here changes remote state.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .config import Node
from .ssh import map_ssh_error, run_ssh

PREFLIGHT_TIMEOUT = 25.0

# Levels used for presentation; the UI maps them to colors.
INFO, WARN, BAD, NA = "info", "warn", "bad", "n/a"

SCRIPT = r"""
export LC_ALL=C
PATH="$PATH:/usr/local/sbin:/usr/sbin:/sbin"
S=""
[ "$(id -u)" = "0" ] || S="sudo -n"
probe() {
  name=$1; use=$2; shift 2
  if ! command -v "$1" >/dev/null 2>&1; then
    printf '@@%s rc=127\n' "$name"; return
  fi
  if [ "$use" = 1 ]; then out=$($S "$@" 2>&1); else out=$("$@" 2>&1); fi
  rc=$?
  printf '@@%s rc=%s\n%s\n' "$name" "$rc" "$out"
}
probe uptime 0 uptime -p
probe qm 1 qm list
probe pct 1 pct list
probe vzdump 0 pgrep -x vzdump
probe zpool 0 zpool status
probe pvecm 1 pvecm status
exit 0
"""

_HEADER_RE = re.compile(r"^@@(\w+) rc=(\d+)$")


@dataclass(frozen=True)
class Section:
    rc: int
    output: str


@dataclass(frozen=True)
class Preflight:
    """None means n/a (command missing, not permitted, or not applicable)."""
    error: str | None = None            # set when SSH itself failed
    uptime: str | None = None
    running_vms: int | None = None
    running_cts: int | None = None
    backup_running: bool | None = None
    zfs_activity: tuple[str, ...] | None = None   # e.g. ("scrub",); () = none
    quorate: bool | None = None
    total_votes: int | None = None
    quorum_needed: int | None = None


@dataclass(frozen=True)
class Line:
    text: str
    level: str = INFO


def parse_sections(text: str) -> dict[str, Section]:
    sections: dict[str, tuple[int, list[str]]] = {}
    current: list[str] | None = None
    for line in text.splitlines():
        m = _HEADER_RE.match(line.strip())
        if m:
            current = []
            sections[m.group(1)] = (int(m.group(2)), current)
        elif current is not None:
            current.append(line)
    return {name: Section(rc, "\n".join(lines).strip()) for name, (rc, lines) in sections.items()}


def _usable(section: Section | None) -> bool:
    return section is not None and section.rc == 0


def count_running(output: str, status_index: int) -> int:
    """Count rows whose status column says 'running' in `qm list` / `pct list`."""
    count = 0
    for line in output.splitlines():
        tokens = line.split()
        if (len(tokens) > status_index and tokens[0].isdigit()
                and tokens[status_index] == "running"):
            count += 1
    return count


def zfs_activity(output: str) -> tuple[str, ...]:
    found = []
    if re.search(r"scrub in progress", output):
        found.append("scrub")
    if re.search(r"resilver in progress", output):
        found.append("resilver")
    return tuple(found)


def parse_quorum(output: str) -> tuple[bool | None, int | None, int | None]:
    quorate = re.search(r"^Quorate:\s+(Yes|No)\b", output, re.M)
    votes = re.search(r"^Total votes:\s+(\d+)", output, re.M)
    needed = re.search(r"^Quorum:\s+(\d+)", output, re.M)
    return (
        None if quorate is None else quorate.group(1) == "Yes",
        int(votes.group(1)) if votes else None,
        int(needed.group(1)) if needed else None,
    )


def parse_preflight(text: str) -> Preflight:
    sec = parse_sections(text)
    uptime = sec.get("uptime")
    qm, pct, zpool, pvecm = sec.get("qm"), sec.get("pct"), sec.get("zpool"), sec.get("pvecm")
    vzdump = sec.get("vzdump")  # pgrep: rc 0 running, rc 1 none, else n/a

    backup: bool | None = None
    if vzdump is not None and vzdump.rc in (0, 1):
        backup = vzdump.rc == 0

    quorate = votes = needed = None
    if _usable(pvecm):
        quorate, votes, needed = parse_quorum(pvecm.output)

    return Preflight(
        uptime=uptime.output.splitlines()[0] if _usable(uptime) and uptime.output else None,
        running_vms=count_running(qm.output, 2) if _usable(qm) else None,
        running_cts=count_running(pct.output, 1) if _usable(pct) else None,
        backup_running=backup,
        zfs_activity=zfs_activity(zpool.output) if _usable(zpool) else None,
        quorate=quorate,
        total_votes=votes,
        quorum_needed=needed,
    )


def run_preflight(node: Node) -> Preflight:
    """Run the checks over SSH (blocking; call from a worker thread)."""
    result = run_ssh(node.ssh_target, ["sh", "-s"], PREFLIGHT_TIMEOUT, input_text=SCRIPT)
    if not result.ok:
        return Preflight(error=map_ssh_error(result))
    return parse_preflight(result.stdout)


def _count_line(label: str, value: int | None) -> Line:
    if value is None:
        return Line(f"{label:<22}n/a", NA)
    return Line(f"{label:<22}{value}", WARN if value else INFO)


def quorum_lost_by_shutdown(pf: Preflight) -> bool:
    """True if taking this node down (assuming 1 vote) would drop below quorum."""
    return (pf.quorate is True and pf.total_votes is not None
            and pf.quorum_needed is not None
            and pf.total_votes - 1 < pf.quorum_needed)


def report_lines(pf: Preflight) -> list[Line]:
    """Render the pre-flight report as colored lines."""
    if pf.error:
        return [Line(f"Pre-flight failed: {pf.error}", BAD)]
    lines = [Line("Pre-flight checks (read-only):", INFO)]
    lines.append(Line(f"{'Uptime:':<22}{pf.uptime}", INFO) if pf.uptime
                 else Line(f"{'Uptime:':<22}n/a", NA))
    lines.append(_count_line("VMs running:", pf.running_vms))
    lines.append(_count_line("Containers running:", pf.running_cts))

    if pf.backup_running is None:
        lines.append(Line(f"{'Backup (vzdump):':<22}n/a", NA))
    elif pf.backup_running:
        lines.append(Line(f"{'Backup (vzdump):':<22}IN PROGRESS", BAD))
    else:
        lines.append(Line(f"{'Backup (vzdump):':<22}none", INFO))

    if pf.zfs_activity is None:
        lines.append(Line(f"{'ZFS scrub/resilver:':<22}n/a", NA))
    elif pf.zfs_activity:
        lines.append(Line(f"{'ZFS scrub/resilver:':<22}{' + '.join(pf.zfs_activity)} IN PROGRESS", BAD))
    else:
        lines.append(Line(f"{'ZFS scrub/resilver:':<22}none", INFO))

    if pf.quorate is None:
        lines.append(Line(f"{'Cluster quorum:':<22}n/a", NA))
    else:
        detail = ""
        if pf.total_votes is not None and pf.quorum_needed is not None:
            detail = f" (votes {pf.total_votes}, quorum {pf.quorum_needed})"
        lines.append(Line(f"{'Cluster quorum:':<22}{'OK' if pf.quorate else 'LOST'}{detail}",
                          INFO if pf.quorate else BAD))
        if quorum_lost_by_shutdown(pf):
            lines.append(Line("Taking this node down would drop the cluster below quorum "
                              "(assuming 1 vote per node).", BAD))
    return lines


def summary(pf: Preflight) -> str:
    """One-line form for the event log."""
    if pf.error:
        return f"failed: {pf.error}"

    def fmt(v: object) -> str:
        return "n/a" if v is None else str(v)
    zfs = "n/a" if pf.zfs_activity is None else (",".join(pf.zfs_activity) or "none")
    quorum = "n/a" if pf.quorate is None else ("ok" if pf.quorate else "LOST")
    return (f"vms={fmt(pf.running_vms)} cts={fmt(pf.running_cts)} "
            f"backup={fmt(pf.backup_running)} zfs={zfs} quorum={quorum}")
