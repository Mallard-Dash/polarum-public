"""SSH helpers: command building, error mapping and the agent pre-check.

Every call uses BatchMode=yes, so ssh can never prompt for (or see) a password
or passphrase. Secrets are never read, stored or logged by Polarum.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Callable, Sequence

DEFAULT_TIMEOUT = 10.0

AGENT_OK, AGENT_ADD, AGENT_UNREACHABLE = "ok", "add", "unreachable"


@dataclass(frozen=True)
class SshResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


def ssh_command(target: str, remote: Sequence[str], connect_timeout: int = 5) -> list[str]:
    return [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={connect_timeout}",
        "-o", "ServerAliveInterval=3",
        "-o", "ServerAliveCountMax=1",
        target,
        *remote,
    ]


def run_ssh(
    target: str, remote: Sequence[str], timeout: float = DEFAULT_TIMEOUT,
    input_text: str | None = None,
) -> SshResult:
    """Run a remote command. Never raises; failures are reported in the result.

    `input_text` is sent on the remote command's stdin (used to pass scripts
    to `sh -s` without any shell quoting).
    """
    stdin: dict = {"input": input_text} if input_text is not None else {
        "stdin": subprocess.DEVNULL}
    try:
        proc = subprocess.run(
            ssh_command(target, remote),
            capture_output=True, text=True, timeout=timeout, **stdin,
        )
    except subprocess.TimeoutExpired:
        return SshResult(255, timed_out=True)
    except FileNotFoundError:
        return SshResult(127, stderr="ssh: command not found")
    except OSError as exc:
        return SshResult(126, stderr=f"cannot run ssh: {exc.strerror}")
    return SshResult(proc.returncode, proc.stdout, proc.stderr)


# (substring, friendly message), checked in order against ssh/sudo stderr.
_ERROR_PATTERNS: tuple[tuple[str, str], ...] = (
    ("Permission denied (publickey",
     "SSH key not available in agent; run ssh-add"),
    ("Permission denied",
     "SSH authentication failed; check the account and your key"),
    ("REMOTE HOST IDENTIFICATION HAS CHANGED",
     "Host key changed (possible tampering or reinstall); verify the host, "
     "then fix ~/.ssh/known_hosts"),
    ("Host key verification failed",
     "Host key not trusted; connect once manually with ssh to verify and accept it"),
    ("Connection refused",
     "Connection refused; is sshd running on the node?"),
    ("Connection timed out",
     "Connection timed out; host unreachable or blocked by a firewall"),
    ("Operation timed out",
     "Connection timed out; host unreachable or blocked by a firewall"),
    ("No route to host", "No route to host; check the network"),
    ("Network is unreachable", "Network is unreachable; check your connection"),
    ("Could not resolve hostname",
     "Could not resolve hostname; check ssh_host / the relay name"),
    ("a password is required",
     "sudo needs a password; add a NOPASSWD sudoers rule for this command"),
    ("a terminal is required",
     "sudo needs a password; add a NOPASSWD sudoers rule for this command"),
    ("is not in the sudoers file",
     "Account is not allowed to use sudo on the node"),
    ("command not found", "A required command is not installed on the remote host"),
)


def map_ssh_error(result: SshResult) -> str:
    """Translate a failed SshResult into a short, actionable English message."""
    if result.timed_out:
        return "SSH timed out; the host may be down, busy or blocked"
    for needle, message in _ERROR_PATTERNS:
        if needle in result.stderr:
            return message
    lines = [ln.strip() for ln in result.stderr.splitlines() if ln.strip()]
    if lines:
        return lines[-1][:200]
    return f"SSH exited with status {result.returncode}"


def is_connection_closed(result: SshResult) -> bool:
    """True if ssh dropped because the remote side went away (expected on poweroff)."""
    return result.returncode == 255 and any(
        s in result.stderr for s in ("closed by remote host", "Connection reset"))


# --- ssh-agent pre-check (runs in the normal terminal, before curses) ---

def agent_decision(returncode: int | None) -> str:
    """Map `ssh-add -l` exit status: 0 ok, 1 no identities, anything else unreachable."""
    if returncode == 0:
        return AGENT_OK
    if returncode == 1:
        return AGENT_ADD
    return AGENT_UNREACHABLE


def _ssh_add_list() -> int | None:
    try:
        return subprocess.run(
            ["ssh-add", "-l"], capture_output=True, stdin=subprocess.DEVNULL, timeout=10,
        ).returncode
    except (OSError, subprocess.SubprocessError):
        return None


def agent_status() -> str:
    """Non-interactive agent check for CLI use: AGENT_OK, AGENT_ADD or AGENT_UNREACHABLE."""
    return agent_decision(_ssh_add_list())


def _ask_continue(input_fn: Callable[[str], str]) -> bool:
    try:
        return input_fn("Continue anyway? [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def ensure_agent(
    input_fn: Callable[[str], str] = input,
    run_interactive: Callable[[list[str]], int] | None = None,
) -> bool:
    """Make sure an ssh-agent with a key is available. Returns False to quit."""
    if run_interactive is None:
        def run_interactive(cmd: list[str]) -> int:
            return subprocess.run(cmd).returncode

    decision = agent_decision(_ssh_add_list())
    if decision == AGENT_OK:
        return True
    if decision == AGENT_ADD:
        print("Your ssh-agent has no identities loaded. Running ssh-add...")
        try:
            run_interactive(["ssh-add"])
        except OSError as exc:
            print(f"Could not run ssh-add: {exc.strerror}")
        if agent_decision(_ssh_add_list()) == AGENT_OK:
            return True
        print("No key was added to the agent. SSH actions will fail with "
              "'Permission denied (publickey)' unless a key is available.")
        return _ask_continue(input_fn)
    print("No ssh-agent is reachable (is SSH_AUTH_SOCK set, and is ssh-add installed?).\n"
          "Polarum uses BatchMode and cannot ask for passphrases, so SSH actions "
          "will fail unless the key has no passphrase.\n"
          "See the README section on SSH agent setup.")
    return _ask_continue(input_fn)
