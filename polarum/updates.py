"""Update checking: apt simulation parsing, reboot detection, rules and ordering.

Everything here is read-only. The only network entry point is
`check_node_updates`; the rest are pure functions that are unit-tested against
sample outputs.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Sequence

from .config import Node, UpdateSettings
from .preflight import Preflight, parse_sections, run_preflight
from .ssh import SshResult, map_ssh_error, run_ssh

CHECK_TIMEOUT = 420.0
EXAMPLE_NAMES = 5

CHECK_PREAMBLE = r"""
export LC_ALL=C DEBIAN_FRONTEND=noninteractive
PATH="$PATH:/usr/local/sbin:/usr/sbin:/sbin"
S=""
[ "$(id -u)" = "0" ] || S="sudo -n"
run() {
  name=$1; shift
  out=$("$@" 2>&1); rc=$?
  printf '@@%s rc=%s\n%s\n' "$name" "$rc" "$out"
}
"""
CHECK_UPDATE_STEP = 'out=$($S apt-get update 2>&1); rc=$?; ' \
    "printf '@@update rc=%s\\n%s\\n' \"$rc\" \"$(printf '%s\\n' \"$out\" | tail -n 8)\"\n"
CHECK_BODY = r"""
run sim apt-get -s dist-upgrade
run kernel uname -r
printf '@@boots rc=0\n'
ls /boot 2>/dev/null | sed -n 's/^vmlinuz-//p'
printf '@@rebootreq rc=0\n'
if [ -f /var/run/reboot-required ]; then echo yes; else echo no; fi
exit 0
"""


def check_script(refresh: bool) -> str:
    """Read-only script. `refresh` adds `apt-get update` (refreshes package lists only)."""
    return CHECK_PREAMBLE + (CHECK_UPDATE_STEP if refresh else "") + CHECK_BODY


# --- data model ---

@dataclass(frozen=True)
class PackageChange:
    name: str
    old: str | None            # None: newly installed package
    new: str
    origins: tuple[str, ...]   # origin names, e.g. ("Debian", "Debian-Security")
    arch: str = ""

    @property
    def is_new(self) -> bool:
        return self.old is None

    @property
    def origin_label(self) -> str:
        return "+".join(sorted(set(self.origins))) or "unknown"


@dataclass(frozen=True)
class Removal:
    name: str
    version: str | None = None


@dataclass(frozen=True)
class AptPlan:
    installs: tuple[PackageChange, ...] = ()
    removals: tuple[Removal, ...] = ()
    kept_back: tuple[str, ...] = ()
    not_upgraded: int = 0

    @property
    def upgrades(self) -> tuple[PackageChange, ...]:
        return tuple(p for p in self.installs if not p.is_new)

    @property
    def new_packages(self) -> tuple[PackageChange, ...]:
        return tuple(p for p in self.installs if p.is_new)

    @property
    def held_back(self) -> int:
        return max(len(self.kept_back), self.not_upgraded)


@dataclass(frozen=True)
class NodeReport:
    node_name: str
    error: str | None = None
    plan: AptPlan = field(default_factory=AptPlan)
    running_kernel: str | None = None
    newest_kernel: str | None = None
    reboot_needed: bool = False
    preflight: Preflight | None = None


# --- apt simulation parsing ---

INST_RE = re.compile(
    r"^Inst (?P<pkg>\S+)(?: \[(?P<old>[^\]]+)\])? "
    r"\((?P<new>\S+) (?P<origins>.+?)(?: \[(?P<arch>[^\]]+)\])?\)"
    r"(?: \[[^\]]*\])*\s*$")
REMV_RE = re.compile(r"^Remv (?P<pkg>\S+)(?: \[(?P<ver>[^\]]+)\])?")
SUMMARY_RE = re.compile(
    r"(\d+) upgraded, (\d+) newly installed, (\d+) to remove and (\d+) not upgraded")
KEPT_BACK_HEADER = "The following packages have been kept back:"


def parse_origins(raw: str) -> tuple[str, ...]:
    """'Debian:12.5/stable, Debian-Security:12/stable-security' -> ('Debian', 'Debian-Security')."""
    origins = []
    for part in raw.split(", "):
        name = part.strip().split(":", 1)[0].strip()
        if name:
            origins.append(name)
    return tuple(origins)


def parse_apt_simulation(text: str) -> AptPlan:
    installs: list[PackageChange] = []
    removals: list[Removal] = []
    kept_back: list[str] = []
    not_upgraded = 0
    in_kept_back = False
    for line in text.splitlines():
        if in_kept_back:
            if line.startswith(" ") and line.strip():
                kept_back.extend(line.split())
                continue
            in_kept_back = False
        if line.strip() == KEPT_BACK_HEADER:
            in_kept_back = True
            continue
        m = INST_RE.match(line)
        if m:
            installs.append(PackageChange(
                name=m["pkg"], old=m["old"], new=m["new"],
                origins=parse_origins(m["origins"]), arch=m["arch"] or ""))
            continue
        m = REMV_RE.match(line)
        if m:
            removals.append(Removal(m["pkg"], m["ver"]))
            continue
        m = SUMMARY_RE.search(line)
        if m:
            not_upgraded = int(m.group(4))
    return AptPlan(tuple(installs), tuple(removals), tuple(kept_back), not_upgraded)


def origin_counts(plan: AptPlan) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for pkg in plan.installs:
        counts[pkg.origin_label] = counts.get(pkg.origin_label, 0) + 1
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


# --- kernel / reboot detection ---

FLAVOR_RE = re.compile(r"^\d+(?:\.\d+)*-\d+(?P<flavor>.*)$")
KERNEL_PKG_RE = re.compile(
    r"^(?:linux-image|proxmox-kernel|pve-kernel)-"
    r"(?P<ver>\d+(?:\.\d+)*-\d+[A-Za-z0-9.+~-]*?)(?:-(?:un)?signed)?$")


def version_key(version: str) -> tuple:
    """Natural sort key: numbers compare numerically, text lexically."""
    return tuple((0, int(tok), "") if tok.isdigit() else (1, 0, tok)
                 for tok in re.split(r"(\d+)", version) if tok)


def kernel_flavor(release: str) -> str | None:
    m = FLAVOR_RE.match(release)
    return m["flavor"] if m else None


def pending_kernel_versions(plan: AptPlan) -> list[str]:
    versions = []
    for pkg in plan.installs:
        m = KERNEL_PKG_RE.match(pkg.name)
        if m:
            versions.append(m["ver"])
    return versions


def detect_reboot(
    running: str | None, installed: Sequence[str], plan: AptPlan, reboot_required_flag: bool,
) -> tuple[bool, str | None]:
    """Return (reboot_needed, newest_kernel_of_running_flavor).

    A reboot is needed if /var/run/reboot-required exists, or a kernel of the
    same flavor as the running one is installed (or about to be installed by
    the plan) with a higher version than the running kernel.
    """
    newest: str | None = None
    if running:
        flavor = kernel_flavor(running)
        if flavor is not None:
            candidates = [v for v in list(installed) + pending_kernel_versions(plan)
                          if kernel_flavor(v) == flavor]
            candidates.append(running)
            newest = max(candidates, key=version_key)
    newer = bool(running and newest and version_key(newest) > version_key(running))
    return reboot_required_flag or newer, newest


# --- check (network) ---

def _last_error_line(output: str) -> str:
    lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
    errors = [ln for ln in lines if ln.startswith("E:")]
    chosen = errors[-1] if errors else (lines[-1] if lines else "no output")
    return chosen[:200]


def parse_check(node_name: str, text: str) -> NodeReport:
    sec = parse_sections(text)
    update, sim = sec.get("update"), sec.get("sim")
    if update is not None and update.rc != 0:
        reason = map_ssh_error(SshResult(update.rc, stderr=update.output))
        return NodeReport(node_name, error=f"apt-get update failed: {reason}")
    if sim is None or sim.rc != 0:
        reason = _last_error_line(sim.output) if sim else "no output from node"
        return NodeReport(node_name, error=f"apt simulation failed: {reason}")
    plan = parse_apt_simulation(sim.output)

    kernel = sec.get("kernel")
    running = kernel.output.splitlines()[0].strip() if kernel and kernel.rc == 0 \
        and kernel.output else None
    boots = sec.get("boots")
    installed = boots.output.split() if boots else []
    flag = sec.get("rebootreq")
    needed, newest = detect_reboot(
        running, installed, plan, bool(flag and flag.output.strip() == "yes"))
    return NodeReport(node_name, plan=plan, running_kernel=running,
                      newest_kernel=newest, reboot_needed=needed)


def check_node_updates(node: Node, refresh: bool = True, with_preflight: bool = True) -> NodeReport:
    """Read-only check of one node. Blocking; call from a worker thread.

    With refresh=True this runs `apt-get update` (package lists only) and
    `apt-get -s dist-upgrade` (a simulation). Nothing is installed.
    """
    result = run_ssh(node.ssh_target, ["sh", "-s"], CHECK_TIMEOUT,
                     input_text=check_script(refresh))
    if not result.ok:
        return NodeReport(node.name, error=map_ssh_error(result))
    report = parse_check(node.name, result.stdout)
    if report.error or not with_preflight:
        return report
    pf = run_preflight(node)
    if pf.error:
        return replace(report, error=f"pre-flight failed: {pf.error}")
    return replace(report, preflight=pf)


# --- rules ---

@dataclass(frozen=True)
class Finding:
    node: str
    text: str


@dataclass(frozen=True)
class Verdict:
    blocks: tuple[Finding, ...] = ()
    warns: tuple[Finding, ...] = ()
    errors: tuple[Finding, ...] = ()

    @property
    def needs_override(self) -> bool:
        return bool(self.blocks)

    @property
    def can_apply(self) -> bool:
        return not self.errors

    @property
    def phrase(self) -> str:
        return "override" if self.needs_override else "proceed"


def _examples(names: Sequence[str]) -> str:
    shown = ", ".join(names[:EXAMPLE_NAMES])
    extra = len(names) - EXAMPLE_NAMES
    return shown + (f" (+{extra} more)" if extra > 0 else "")


def evaluate_report(report: NodeReport, settings: UpdateSettings) -> Verdict:
    name = report.node_name
    if report.error:
        return Verdict(errors=(Finding(name, f"check failed: {report.error}"),))
    blocks: list[Finding] = []
    warns: list[Finding] = []
    plan = report.plan

    if plan.removals and settings.block_on_removals:
        blocks.append(Finding(name, f"{len(plan.removals)} package(s) would be REMOVED: "
                                    f"{_examples([r.name for r in plan.removals])}"))

    allowed = set(settings.allowed_origins)
    foreign = [p for p in plan.installs if not p.origins or not set(p.origins) <= allowed]
    if foreign:
        origins = sorted({o for p in foreign for o in (p.origins or ("unknown",))
                          if o not in allowed})
        blocks.append(Finding(
            name, f"{len(foreign)} package(s) from origin(s) not in allowed_origins "
                  f"[{', '.join(origins)}]: {_examples([p.name for p in foreign])}"))

    pf = report.preflight
    if pf is not None:
        if pf.quorate is False:
            blocks.append(Finding(name, "cluster quorum is LOST"))
        if pf.backup_running:
            blocks.append(Finding(name, "a backup (vzdump) is in progress"))
        if pf.zfs_activity:
            blocks.append(Finding(name, f"ZFS {' + '.join(pf.zfs_activity)} in progress"))

    if report.reboot_needed:
        detail = ""
        if report.running_kernel and report.newest_kernel \
                and report.running_kernel != report.newest_kernel:
            detail = f" (kernel {report.running_kernel} -> {report.newest_kernel})"
        warns.append(Finding(name, f"reboot needed{detail}"))
    if plan.held_back:
        warns.append(Finding(name, f"{plan.held_back} package(s) held back"))
    total = len(plan.installs)
    if total > settings.many_updates_threshold:
        warns.append(Finding(name, f"unusually many updates ({total} > "
                                   f"{settings.many_updates_threshold})"))
    return Verdict(tuple(blocks), tuple(warns))


def evaluate(reports: Sequence[NodeReport], settings: UpdateSettings) -> Verdict:
    blocks: list[Finding] = []
    warns: list[Finding] = []
    errors: list[Finding] = []
    for report in reports:
        v = evaluate_report(report, settings)
        blocks += v.blocks
        warns += v.warns
        errors += v.errors
    return Verdict(tuple(blocks), tuple(warns), tuple(errors))


# --- ordering and logging helpers ---

def apply_order(nodes: Sequence[Node]) -> list[Node]:
    """Canary nodes first, then by update_order (nodes without one go last)."""
    return sorted(nodes, key=lambda n: (not n.canary,
                                        n.update_order if n.update_order is not None else 10**9,
                                        n.name))


def package_summary(pkg: PackageChange) -> str:
    return f"{pkg.name} {pkg.old or '(new)'}>{pkg.new}"


def chunk_packages(packages: Sequence[PackageChange], limit: int = 240) -> list[str]:
    """Split a package list into log-sized lines."""
    chunks: list[str] = []
    current = ""
    for pkg in packages:
        item = package_summary(pkg)
        if current and len(current) + len(item) + 2 > limit:
            chunks.append(current)
            current = ""
        current = f"{current}, {item}" if current else item
    if current:
        chunks.append(current)
    return chunks
