"""Rolling update runner.

One node at a time: quorum re-check, optional ZFS snapshot, upgrade inside a
transient systemd unit (so it survives SSH/TUI disconnects), progress polling,
health check and reboot policy. ANY failure or timeout stops the whole run and
marks the remaining nodes SKIPPED; the runner never continues on its own.

All remote operations live in `RemoteOps` so tests can substitute a fake.
"""
from __future__ import annotations

import re
import shlex
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Sequence

from .actions import ActionResult, remote_power
from .config import Node, UpdateSettings
from .events import EventLog
from .health import HealthResult, check_node
from .preflight import Preflight, parse_sections, quorum_lost_by_shutdown, run_preflight
from .ssh import SshResult, map_ssh_error, run_ssh
from .state import ONLINE
from .updates import (
    AptPlan, NodeReport, apply_order, check_node_updates, chunk_packages, package_summary,
)

UNIT_NAME = "polarum-upgrade"
STATUS_FILE = f"/run/{UNIT_NAME}.status"
UPGRADE_COMMAND = (
    "DEBIAN_FRONTEND=noninteractive apt-get dist-upgrade -y "
    "-o Dpkg::Options::=--force-confold -o Dpkg::Options::=--force-confdef"
)
# Runs inside the transient unit as root; records "<token> <exit code>" for us to read
# even after --collect has unloaded the unit.
UNIT_WRAPPER = (
    f"export LC_ALL=C; {UPGRADE_COMMAND}; rc=$?; "
    f"printf '%s %s\\n' \"$1\" \"$rc\" > {STATUS_FILE}.tmp && mv {STATUS_FILE}.tmp {STATUS_FILE}; "
    "exit $rc"
)

# The exact privileged commands, as argv lists. The scripts below and the sudoers
# rules printed by `polarum --print-sudoers` are both built from these.
JOURNAL_ARGV = ("journalctl", "-u", UNIT_NAME, "-n", "12", "--no-pager", "-o", "cat")


def start_argv(token: str) -> list[str]:
    return [
        "systemd-run", f"--unit={UNIT_NAME}", "--collect",
        "--description=Polarum apt dist-upgrade", "--quiet",
        "--", "sh", "-c", UNIT_WRAPPER, UNIT_NAME, token,
    ]


def snapshot_argv(dataset: str, name: str) -> list[str]:
    return ["zfs", "snapshot", f"{dataset}@{name}"]


POLL_INTERVAL = 3.0
UNREACHABLE_LIMIT = 600.0     # consecutive seconds without SSH contact before giving up
UPGRADE_MAX_SECONDS = 7200.0
DOWN_TIMEOUT = 180.0          # after a reboot command: wait this long for the node to go down
HEALTH_POLL = 3.0
ALREADY_RUNNING_RC = 42

DATASET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]*$")
TOKEN_RE = re.compile(r"^[0-9a-f]{12}$")

PENDING, RUNNING, OK, FAILED, SKIPPED = "PENDING", "RUNNING", "OK", "FAILED", "SKIPPED"

PREAMBLE = r"""
export LC_ALL=C
PATH="$PATH:/usr/local/sbin:/usr/sbin:/sbin"
S=""
[ "$(id -u)" = "0" ] || S="sudo -n"
"""


# --- pure helpers (unit-tested) ---

def parse_root_dataset(output: str) -> str | None:
    """Parse `findmnt -n -o FSTYPE,SOURCE /`; return the ZFS dataset or None."""
    tokens = output.split()
    if len(tokens) == 2 and tokens[0] == "zfs" and DATASET_RE.fullmatch(tokens[1]):
        return tokens[1]
    return None


def snapshot_name(now: datetime | None = None) -> str:
    return "polarum-pre-upgrade-" + (now or datetime.now()).strftime("%Y%m%d-%H%M%S")


def plan_differences(reviewed: AptPlan, current: AptPlan) -> list[str]:
    """Human-readable differences between two plans; empty means identical."""
    def installs(plan: AptPlan) -> set[tuple[str, str | None, str]]:
        return {(p.name, p.old, p.new) for p in plan.installs}

    diffs: list[str] = []
    before, after = installs(reviewed), installs(current)
    added = sorted({n for n, _, _ in after - before})
    gone = sorted({n for n, _, _ in before - after})
    if added:
        diffs.append("added/changed: " + ", ".join(added[:5]) + (" ..." if len(added) > 5 else ""))
    if gone:
        diffs.append("no longer planned: " + ", ".join(gone[:5]) + (" ..." if len(gone) > 5 else ""))
    removed_before = {r.name for r in reviewed.removals}
    removed_after = {r.name for r in current.removals}
    if removed_before != removed_after:
        diffs.append("removals differ: " + ", ".join(sorted(removed_before ^ removed_after)[:5]))
    return diffs


def new_token() -> str:
    return uuid.uuid4().hex[:12]


def start_script(token: str) -> str:
    """Script that starts the transient unit; refuses if one is already active."""
    if not TOKEN_RE.fullmatch(token):
        raise ValueError("invalid token")
    run = shlex.join(start_argv(token))
    return (PREAMBLE
            + f'if systemctl is-active --quiet {UNIT_NAME}; then\n'
              f'  echo "{UNIT_NAME} is already running" >&2; exit {ALREADY_RUNNING_RC}\nfi\n'
            + f"$S {run}\n")


POLL_SCRIPT = PREAMBLE + f"""
printf '@@state rc=0\\n%s\\n' "$(systemctl is-active {UNIT_NAME} 2>&1)"
printf '@@status rc=0\\n'
cat {STATUS_FILE} 2>/dev/null
printf '@@log rc=0\\n'
$S {shlex.join(JOURNAL_ARGV)} 2>&1
exit 0
"""

POLL_RUNNING, POLL_DONE, POLL_ENDED = "running", "done", "ended-without-status"


@dataclass(frozen=True)
class PollState:
    kind: str
    exit_code: int | None = None
    tail: tuple[str, ...] = ()


def interpret_poll(token: str, output: str) -> PollState:
    """Decide what a poll means. The status file (matching our token) is authoritative."""
    sec = parse_sections(output)
    tail = tuple(sec["log"].output.splitlines()[-12:]) if "log" in sec else ()
    status = sec["status"].output.split() if "status" in sec else []
    if len(status) == 2 and status[0] == token and status[1].isdigit():
        return PollState(POLL_DONE, int(status[1]), tail)
    state = sec["state"].output.strip() if "state" in sec else ""
    if state in ("active", "activating", "deactivating", "reloading"):
        return PollState(POLL_RUNNING, None, tail)
    return PollState(POLL_ENDED, None, tail)


# --- remote operations ---

class RemoteOps:
    """Real implementation; every call is SSH with BatchMode (see ssh.run_ssh)."""

    def preflight(self, node: Node) -> Preflight:
        return run_preflight(node)

    def root_dataset(self, node: Node) -> tuple[str | None, str]:
        res = run_ssh(node.ssh_target, ["findmnt", "-n", "-o", "FSTYPE,SOURCE", "/"], 15)
        if not res.ok:
            return None, map_ssh_error(res)
        dataset = parse_root_dataset(res.stdout)
        if dataset is None:
            return None, f"root filesystem is not ZFS ({res.stdout.strip() or 'unknown'})"
        return dataset, ""

    def snapshot(self, node: Node, dataset: str, name: str) -> SshResult:
        if not DATASET_RE.fullmatch(dataset) or not re.fullmatch(r"[A-Za-z0-9_.:-]+", name):
            return SshResult(1, stderr="refusing unsafe snapshot name")
        script = PREAMBLE + f"$S {shlex.join(snapshot_argv(dataset, name))}\n"
        return run_ssh(node.ssh_target, ["sh", "-s"], 60, input_text=script)

    def start_unit(self, node: Node, token: str) -> SshResult:
        return run_ssh(node.ssh_target, ["sh", "-s"], 60, input_text=start_script(token))

    def poll_unit(self, node: Node) -> SshResult:
        return run_ssh(node.ssh_target, ["sh", "-s"], 30, input_text=POLL_SCRIPT)

    def health(self, node: Node) -> HealthResult:
        return check_node(node)

    def reboot(self, node: Node) -> ActionResult:
        return remote_power(node, "reboot")

    def post_check(self, node: Node) -> NodeReport:
        return check_node_updates(node, refresh=False, with_preflight=False)

    def recheck_plan(self, node: Node) -> NodeReport:
        """Re-simulate the upgrade (no apt-get update) to compare with the reviewed plan."""
        return check_node_updates(node, refresh=False, with_preflight=False)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def now(self) -> float:
        return time.monotonic()


# --- run state ---

@dataclass
class NodeResult:
    name: str
    status: str = PENDING
    detail: str = ""
    busy_kind: str | None = None        # "update" / "reboot" while in flight (for the UI)
    packages: tuple[str, ...] = ()
    reboot_needed: bool = False
    error: str | None = None
    snapshot: str | None = None


@dataclass(frozen=True)
class RunSnapshot:
    results: tuple[NodeResult, ...]
    tail: tuple[str, ...]
    finished: bool
    error: str | None


class StepFailed(Exception):
    """A step failed; the run must stop."""


@dataclass
class UpdateRun:
    settings: UpdateSettings
    reports: Sequence[NodeReport]
    nodes: Sequence[Node]
    events: EventLog
    ops: RemoteOps = field(default_factory=RemoteOps)
    quorum_override: bool = False   # user overrode a lost-quorum block for this run

    def __post_init__(self) -> None:
        by_name = {n.name: n for n in self.nodes}
        self.order: list[Node] = apply_order([by_name[r.node_name] for r in self.reports])
        self._reports = {r.node_name: r for r in self.reports}
        self._results = {n.name: NodeResult(n.name) for n in self.order}
        self._tail: tuple[str, ...] = ()
        self._finished = False
        self._error: str | None = None
        self._lock = threading.Lock()
        self.stop_requested = threading.Event()
        self.abandoned = threading.Event()

    # thread-safe view for the UI
    def snapshot(self) -> RunSnapshot:
        with self._lock:
            return RunSnapshot(
                tuple(replace(r) for r in self._results.values()),
                self._tail, self._finished, self._error)

    def request_stop(self) -> None:
        """Stop after the node currently being updated (never mid-upgrade)."""
        self.stop_requested.set()

    def abandon(self) -> None:
        """Called when Polarum is closing: stop waiting. A started unit keeps running remotely."""
        self.abandoned.set()

    def _sleep(self, seconds: float) -> None:
        if self.abandoned.is_set():
            raise StepFailed(
                f"Polarum was closed during the run; if an upgrade was started it keeps "
                f"running on the node (inspect: journalctl -u {UNIT_NAME})")
        self.ops.sleep(seconds)

    def _set(self, name: str, **fields: object) -> None:
        with self._lock:
            for key, value in fields.items():
                setattr(self._results[name], key, value)

    def _log(self, name: str, result: str) -> None:
        self.events.append(name, "update", result)

    # main loop
    def run(self) -> None:
        try:
            for index, node in enumerate(self.order):
                if self.stop_requested.is_set():
                    self._skip_rest(index, "stopped by user")
                    self._fail_run("Run stopped by user; remaining nodes skipped.")
                    break
                try:
                    self._update_node(node)
                except StepFailed as exc:
                    self._set(node.name, status=FAILED, busy_kind=None,
                              error=str(exc), detail="failed")
                    self._log(node.name, f"FAILED: {exc}")
                    self._skip_rest(index + 1, f"skipped after failure on {node.name}")
                    self._fail_run(f"{node.name}: {exc}. Run stopped; remaining nodes skipped.")
                    break
                except Exception as exc:  # fail closed on anything unexpected
                    msg = f"internal error: {type(exc).__name__}: {exc}"
                    self._set(node.name, status=FAILED, busy_kind=None, error=msg,
                              detail="failed")
                    self._log(node.name, f"FAILED: {msg}")
                    self._skip_rest(index + 1, f"skipped after failure on {node.name}")
                    self._fail_run(f"{node.name}: {msg}. Run stopped; remaining nodes skipped.")
                    break
        finally:
            with self._lock:
                self._finished = True
            self._log("-", "run finished: " + ", ".join(
                f"{r.name}={r.status}" for r in self.snapshot().results))

    def _fail_run(self, message: str) -> None:
        with self._lock:
            self._error = message

    def _skip_rest(self, start: int, reason: str) -> None:
        for node in self.order[start:]:
            self._set(node.name, status=SKIPPED, detail=reason)
            self._log(node.name, f"SKIPPED: {reason}")

    # one node
    def _update_node(self, node: Node) -> None:
        name = node.name
        report = self._reports[name]
        self._set(name, status=RUNNING, detail="starting")
        if not report.plan.installs:
            self._set(name, status=OK, detail="nothing to update",
                      reboot_needed=report.reboot_needed)
            self._log(name, "ok: nothing to update")
            return

        self._check_quorum(node)
        self._verify_plan(node, report)
        self._snapshot(node)
        self._upgrade(node)
        packages = tuple(package_summary(p) for p in report.plan.installs)
        self._set(name, packages=packages)
        self._log(name, f"upgrade ok: {len(packages)} package(s) installed "
                        "(plan re-verified identical to the reviewed plan before start)")
        for chunk in chunk_packages(report.plan.installs):
            self._log(name, f"packages: {chunk}")

        self._wait_healthy(node, "health check")
        reboot_needed = self._reboot_needed_after(node, report)
        self._reboot_policy(node, reboot_needed)

    def _check_quorum(self, node: Node) -> None:
        self._set(node.name, detail="re-checking quorum")
        pf = self.ops.preflight(node)
        if pf.error:
            raise StepFailed(f"pre-flight failed: {pf.error}")
        if pf.quorate is False and not self.quorum_override:
            raise StepFailed("cluster quorum lost")
        self._log(node.name, "quorum re-check ok" if pf.quorate is not None
                  else "quorum re-check n/a (not a cluster node)")

    def _verify_plan(self, node: Node, reviewed: NodeReport) -> None:
        """Abort unless a fresh simulation matches exactly what the user reviewed."""
        self._set(node.name, detail="re-verifying reviewed plan")
        now = self.ops.recheck_plan(node)
        if now.error:
            raise StepFailed(f"could not re-verify the reviewed plan: {now.error}")
        diff = plan_differences(reviewed.plan, now.plan)
        if diff:
            raise StepFailed(
                "package plan changed since it was reviewed (" + "; ".join(diff) + "); "
                "nothing was installed on this node. Run the update check again")
        self._log(node.name, "plan re-verified: matches the reviewed plan")

    def _snapshot(self, node: Node) -> None:
        if not self.settings.snapshot_before:
            return
        name = node.name
        self._set(name, detail="snapshot")
        dataset, reason = self.ops.root_dataset(node)
        problem = reason
        if dataset is not None:
            snap = snapshot_name()
            res = self.ops.snapshot(node, dataset, snap)
            if res.ok:
                self._set(name, snapshot=f"{dataset}@{snap}")
                self._log(name, f"snapshot created: {dataset}@{snap}")
                return
            problem = map_ssh_error(res)
        if self.settings.require_snapshot:
            raise StepFailed(f"snapshot required but not possible: {problem}")
        self._log(name, f"NOTICE: no snapshot taken, continuing: {problem}")

    def _upgrade(self, node: Node) -> None:
        name = node.name
        token = new_token()
        self._set(name, detail="starting upgrade unit", busy_kind="update")
        res = self.ops.start_unit(node, token)
        if res.returncode == ALREADY_RUNNING_RC:
            raise StepFailed(f"{UNIT_NAME} is already running on the node; not starting another")
        if not res.ok:
            raise StepFailed(f"could not start upgrade unit: {map_ssh_error(res)}")
        self._log(name, f"upgrade started in systemd unit {UNIT_NAME}")
        self._set(name, detail="upgrading")
        self._follow(node, token)
        self._set(name, busy_kind=None)

    def _follow(self, node: Node, token: str) -> None:
        ops, name = self.ops, node.name
        start = ops.now()
        unreachable_since: float | None = None
        ended_strikes = 0
        while True:
            now = ops.now()
            if now - start > UPGRADE_MAX_SECONDS:
                raise StepFailed(
                    f"upgrade still running after {int(UPGRADE_MAX_SECONDS)}s; stopping the run "
                    f"(the unit was left running; inspect with: journalctl -u {UNIT_NAME})")
            res = ops.poll_unit(node)
            if not res.ok:
                unreachable_since = unreachable_since if unreachable_since is not None else now
                if now - unreachable_since > UNREACHABLE_LIMIT:
                    raise StepFailed(
                        f"lost contact for {int(now - unreachable_since)}s ({map_ssh_error(res)}); "
                        f"upgrade state unknown; check the node: systemctl status {UNIT_NAME}")
                self._set(name, detail="upgrading (connection lost, retrying)")
            else:
                unreachable_since = None
                poll = interpret_poll(token, res.stdout)
                with self._lock:
                    self._tail = poll.tail
                if poll.kind == POLL_DONE:
                    if poll.exit_code == 0:
                        return
                    last = poll.tail[-1] if poll.tail else "no output"
                    self._log(name, "apt output tail: " + " / ".join(poll.tail[-4:]))
                    raise StepFailed(f"apt-get exited with status {poll.exit_code}: {last[:150]}")
                if poll.kind == POLL_RUNNING:
                    ended_strikes = 0
                    self._set(name, detail="upgrading")
                else:
                    ended_strikes += 1
                    if ended_strikes >= 2:
                        raise StepFailed(
                            "upgrade unit ended without reporting a result "
                            "(killed or node restarted?); verify the node manually")
            self._sleep(POLL_INTERVAL)

    def _wait_healthy(self, node: Node, phase: str) -> None:
        ops = self.ops
        self._set(node.name, detail=phase)
        deadline = ops.now() + self.settings.health_timeout
        while True:
            if ops.health(node).health == ONLINE:
                self._log(node.name, f"{phase} ok: all health ports reachable")
                return
            if ops.now() >= deadline:
                raise StepFailed(
                    f"{phase} failed: health ports not reachable within "
                    f"{int(self.settings.health_timeout)}s")
            self._sleep(HEALTH_POLL)

    def _reboot_needed_after(self, node: Node, report: NodeReport) -> bool:
        self._set(node.name, detail="checking reboot need")
        post = self.ops.post_check(node)
        if post.error:
            self._log(node.name, f"NOTICE: post-upgrade check failed ({post.error}); "
                                 "assuming a reboot is needed")
            return True
        if post.plan.installs:
            self._log(node.name, f"NOTICE: {len(post.plan.installs)} package(s) still "
                                 "upgradable after the run")
        return post.reboot_needed

    def _check_safe_to_reboot(self, node: Node) -> None:
        """Fresh pre-flight before an automatic reboot; the upgrade itself already succeeded."""
        self._set(node.name, detail="pre-flight before reboot")
        pf = self.ops.preflight(node)
        problems = []
        if pf.error:
            problems.append(f"pre-flight failed: {pf.error}")
        if pf.backup_running:
            problems.append("a backup (vzdump) is in progress")
        if pf.zfs_activity:
            problems.append(f"ZFS {' + '.join(pf.zfs_activity)} in progress")
        if quorum_lost_by_shutdown(pf):
            problems.append("rebooting would drop the cluster below quorum")
        if problems:
            self._set(node.name, reboot_needed=True)
            raise StepFailed(
                "upgrade installed, but automatic reboot refused: " + "; ".join(problems)
                + ". The node still needs a reboot (press B when safe)")
        self._log(node.name, "pre-flight before reboot ok")

    def _reboot_policy(self, node: Node, needed: bool) -> None:
        name, policy = node.name, self.settings.reboot_policy
        if not needed or policy == "never":
            self._set(name, status=OK, detail="done", reboot_needed=False)
            self._log(name, "ok: no reboot needed" if not needed else
                      "ok: reboot needed but reboot_policy=never")
            return
        if policy == "manual":
            self._set(name, status=OK, detail="done; reboot needed", reboot_needed=True)
            self._log(name, "ok: REBOOT NEEDED (reboot_policy=manual)")
            return
        # if_needed
        ops = self.ops
        self._check_safe_to_reboot(node)
        self._set(name, detail="rebooting", busy_kind="reboot")
        self._log(name, "rebooting (reboot_policy=if_needed)")
        res = ops.reboot(node)
        if not res.ok:
            raise StepFailed(f"reboot failed: {res.detail}")
        deadline = ops.now() + DOWN_TIMEOUT
        while ops.health(node).health == ONLINE:
            if ops.now() >= deadline:
                raise StepFailed(f"node did not go down within {int(DOWN_TIMEOUT)}s of the "
                                 "reboot command")
            self._sleep(1.0)
        self._set(name, detail="waiting for node to return")
        self._wait_healthy(node, "post-reboot health check")
        self._set(name, status=OK, detail="done; rebooted", busy_kind=None, reboot_needed=False)
        self._log(name, "ok: rebooted and healthy")
