# Polarum

A terminal console for managing the power state and updates of a homelab. It
runs on your laptop and talks to your machines over SSH and Wake-on-LAN. Pure
Python standard library: no pip dependencies, no agent on the nodes.

- **Wake** nodes with Wake-on-LAN, through an SSH relay host or directly by UDP broadcast.
- **Power off / reboot** nodes over SSH, after a read-only pre-flight (running VMs, backups, ZFS scrubs, cluster quorum).
- **Check and apply updates** (Debian / Proxmox VE, `apt`) as a reviewed, rolling, one-node-at-a-time upgrade that stops on the first problem.
- **Health** is a TCP connect check against the ports you choose, not ICMP.
- Everything is logged to an append-only event log.

> **Status:** early software. It changes the power state of real machines and
> installs packages as root. Read [Safety model](#safety-model) and
> [Security notes](#security-notes) first, and try it on a non-critical node.

## The TUI

```text
 POLARUM // Homelab Power Console
 WoL relay: relay-host   Broadcast: 192.168.1.255

 ┌──────────────────────────────────────────────────────────────────────┐
 │ NODE             IP ADDRESS       STATUS            LATENCY          │
 ├──────────────────────────────────────────────────────────────────────┤
 │ node-a           192.168.1.11     ONLINE             1.2 ms          │
 │ node-b           192.168.1.12     REBOOT NEEDED      1.4 ms          │
 │ node-c           192.168.1.13     WAKING 18s              —          │
 └──────────────────────────────────────────────────────────────────────┘
 ↑/↓ J/K select   W wake   P power off   B reboot   R refresh   Q quit
 U check updates (selected node)   Shift+U check all nodes in update_order

 Recent events
 21:03:11 node-c wake: started
 21:03:12 node-c wake: ok: magic packet sent via relay-host
 21:02:40 node-b update: ok: REBOOT NEEDED (reboot_policy=manual)
```

Update review (`U` / `Shift+U`):

```text
 UPDATE REVIEW: 2 node(s)
 BLOCKED: 1 issue(s). Installing requires typing "override".

 NODE              UPG  NEW  REM  HELD  REBOOT QUORUM RULES
 node-a             12    1    0     0  yes    OK     BLOCK
 node-b              3    0    0     0  no     OK     ok

 Node: node-a
 Upgrades: 12   New: 1   Removed: 0   Held back: 0
 Reboot needed: yes (kernel 6.8.12-3-pve -> 6.8.12-4-pve)
 Quorum: OK (votes 3, quorum 2)
 Running: 3 VM(s), 2 container(s)
 Backup: none
 ZFS scrub/resilver: none
 Packages per origin:
   Proxmox                                 9  allowed
   Debian-Security                         3  allowed
   Docker                                  1  NOT ALLOWED
 BLOCK: 1 package(s) from origin(s) not in allowed_origins [Docker]: docker-ce
 WARN:  reboot needed (kernel 6.8.12-3-pve -> 6.8.12-4-pve)

 ←/→ switch node   D package list   I install   ESC cancel
```

## Architecture

```text
Laptop --SSH--> relay-host --UDP broadcast--> node          (wake via relay)
Laptop ------------UDP broadcast------------> node          (wake, no relay configured)
Laptop --SSH--> node                                        (power off, reboot, checks, updates)
Laptop --TCP connect--> node:health_ports                   (status)
```

Nothing is installed on the nodes. Polarum runs short POSIX shell scripts and
ordinary commands (`systemctl`, `apt-get`, `qm`, `zpool`, ...) over SSH. The
only thing left behind is the transient systemd unit `polarum-upgrade` during an
update, plus a small status file in `/run`.

Source layout (`polarum/`):

| Module | Role |
|---|---|
| `config.py` | Load and validate `nodes.toml` |
| `state.py` | Per-node runtime state and status logic (pure) |
| `health.py` | TCP connect checks |
| `ssh.py` | SSH command building, error mapping, ssh-agent pre-check |
| `actions.py`, `wol.py` | Wake, power off, reboot; magic packets |
| `preflight.py` | Read-only pre-flight checks and their rendering |
| `updates.py` | apt simulation parsing, kernel/reboot detection, rules, ordering |
| `runner.py` | The rolling update run |
| `reports.py` | Curses-free report text shared by TUI and CLI |
| `ui.py`, `ui_updates.py`, `tui.py` | Curses front end |
| `cmd.py`, `cli.py` | Non-interactive commands and entry point |
| `events.py` | Event log |

## Requirements

- **Laptop:** Linux, Python 3.11+, `ssh`, `ssh-add`.
- **Relay host** (optional): SSH access, the `wakeonlan` command, and layer-2 access to the nodes' broadcast domain. Without a relay, your laptop must be on that segment itself.
- **Nodes:** an SSH account (a dedicated `polarum` account is recommended), `sudo`, `systemd`. The update feature expects Debian-style `apt`. Pre-flight extras (`qm`, `pct`, `pvecm`, `zpool`) are optional and show as `n/a` when absent.
- Wake-on-LAN must be enabled in the nodes' firmware/NIC settings.

## Install

A guided setup. Work top to bottom; each step has a command, a check that tells
you whether it worked, and a collapsible section for when it did not. Replace the
example names (`node-a`, `relay-host`, `192.168.1.x`) with your own.

### Step 1: Check the prerequisites

```bash
python3 --version     # needs 3.11 or newer
ssh -V
ssh-add -l            # exit status 0 or 1 is fine; 2 means no ssh-agent is running
```

- [ ] Python is 3.11+
- [ ] `ssh` and `ssh-add` exist

<details>
<summary>Python is too old, or a command is missing</summary>

On Debian/Ubuntu: `sudo apt install python3 openssh-client`. Polarum needs
Python 3.11+ (it uses `tomllib`); if your distribution ships an older one, install
a newer Python and make sure `python3` points to it before running `install.sh`.
</details>

### Step 2: Get the code and install it

```bash
git clone <repository-url> polarum
cd polarum
./install.sh
```

The installer only writes inside your home directory (see the table below) and
never needs root.

- [ ] The output ends with `Installed Polarum.`

<details>
<summary><code>polarum: command not found</code></summary>

`~/.local/bin` is not on your `PATH`. Add `export PATH="$HOME/.local/bin:$PATH"`
to your shell profile and open a new terminal, or run `python3 -m polarum` from
the checkout instead.
</details>

### Step 3: Describe your nodes

```bash
$EDITOR ~/.config/polarum/nodes.toml
polarum --check-config
```

At minimum each `[[nodes]]` entry needs `name`, `ip` and `mac`. Everything else
has a default; the file is commented. Decide how wake packets are sent:

| You are... | Set |
|---|---|
| on the same LAN segment as the nodes | leave `relay` empty; set `relay_broadcast` to the subnet broadcast address |
| elsewhere (VPN, other subnet) | `relay = "relay-host"` (an SSH alias of a machine on that LAN with `wakeonlan` installed) and `relay_broadcast` |

- [ ] `polarum --check-config` prints `Configuration OK`

<details>
<summary>The config is rejected</summary>

Every problem is listed at once, and unknown keys are errors (a typo is caught
instead of silently ignored). Fix each line and run the check again. The
[configuration reference](#configuration-reference) lists every key.
</details>

### Step 4: Make SSH work without prompts

Polarum never asks for passwords: every SSH call uses `BatchMode=yes`.

```bash
ssh-keygen -t ed25519            # skip if you already have a key
ssh-copy-id polarum@192.168.1.11 # repeat for each node (and the relay host)
ssh-add                          # load the key into your agent
ssh polarum@192.168.1.11 true && echo SSH OK
```

- [ ] `SSH OK` is printed for every node, without a password prompt

<details>
<summary>Permission denied, or a host key warning</summary>

`Permission denied (publickey)` usually means the key is not in the agent: run
`ssh-add`. For an unknown host key, connect once manually with `ssh` and accept
it; Polarum will not do that for you. See [SSH setup](#ssh-setup) for an
agent that starts automatically.
</details>

### Step 5: Look before you touch anything

These commands are read-only (TCP connects only):

```bash
polarum status
polarum status node-a -v
```

- [ ] Your running nodes show `ONLINE`, powered-off ones `OFFLINE`

<details>
<summary>A running node shows OFFLINE</summary>

Health is a TCP connect to `health_ports` (default `[22]`). Make sure the port is
reachable from this machine and listed in the node's `health_ports`. A firewall
that drops SSH from your laptop looks exactly like a powered-off node.
</details>

### Step 6 (optional): Enable power control and updates on a node

Everything above works with an ordinary SSH account. Power off, reboot, update
checks and installs additionally need `sudo` rules on the node. Follow
[Sudoers](#sudoers-a-dedicated-account-with-narrow-rules): generate the rules
with `polarum --print-sudoers` and install only the ones you need. The
update-apply rule is commented out by default; enable it only on nodes you want
Polarum to update.

```bash
polarum --print-sudoers > polarum.sudoers   # review, then install on the node
polarum check node-a      # read-only update check
polarum -u node-a         # review, type the confirmation word, install
```

- [ ] `polarum check node-a` prints a review and ends with `Nothing was installed`

<details>
<summary>"sudo needs a password"</summary>

The node's sudoers file is missing a `NOPASSWD` rule for that exact command, or
the binary path differs. Check with `command -v systemctl apt-get qm` on the node
and adjust the paths in the rule.
</details>

### Step 7: Run it

```bash
polarum          # interactive console
```

Try it on a non-critical node first. Start with `W` (wake) or `R` (refresh); every
state-changing action asks for confirmation.

| What | Where |
|---|---|
| Config | `~/.config/polarum/nodes.toml` |
| Event log | `~/.local/state/polarum/events.log` |
| Installed program | `~/.local/share/polarum/` (launcher symlinked at `~/.local/bin/polarum`) |

You can also run it straight from a checkout: `python3 -m polarum`.

## Controls

| Key | Action |
|---|---|
| `↑` `↓` / `J` `K` | Select node |
| `W` | Wake (single `y` confirmation) |
| `P` | Power off (pre-flight, then typed `proceed`) |
| `B` | Reboot (pre-flight, then typed `proceed`) |
| `u` | Check updates on the selected node (read-only) |
| `U` (Shift+u) | Check updates on all nodes that have an `update_order` (read-only) |
| `R` | Refresh health now |
| `Q` | Quit |

Statuses: `OFFLINE`, `BOOTING` (some but not all `health_ports` up), `ONLINE`,
`SENDING...`, `WAKING 18s`, `WAKE TIMEOUT` (press `W` to resend), `SHUTTING DOWN`,
`REBOOTING`, `UPDATING`, `REBOOT NEEDED`.

## Command-line mode

```text
polarum status [-a] [node|ip] [-v]   health table (all nodes by default); -v adds MAC,
                                     SSH target and each health port
polarum -u <node|ip>                 update one node: check, review, typed confirmation, install
polarum -ua                          update every node that has an update_order, one at a time
polarum wake <node> [--yes]          wake; asks y/N unless --yes
polarum off <node>                   pre-flight + typed "proceed"; needs a terminal
polarum check <node|all>             read-only update check; never installs
polarum --check-config               validate the config and exit
```

`-c/--config PATH` selects another config file. `-u` and `-ua` cannot be combined
with each other or with a command.

`polarum -u` / `-ua` follow the same workflow as the TUI, in the terminal:

1. All target nodes must be `ONLINE`, then a read-only check and review are printed (counts, packages, reboot need, quorum, rule findings).
2. You are shown the plan (order, reboot policy, snapshot setting) and must type `proceed`, or `override` when a rule blocks. Anything else cancels and installs nothing.
3. The rolling run prints a timestamped line for each step while you wait. Ctrl-C once stops after the current node; a second Ctrl-C stops waiting (a started upgrade keeps running on the node).
4. A summary table is printed. Exit code is `0` only if every node finished OK.

Rules:

- `--yes` exists **only** for `wake`. `off`, `-u` and `-ua` always need a terminal and the typed word; there is no way to script them.
- `check` never installs. `-u`/`-ua` are the only command-line ways to install, and they never skip the review or the typed confirmation.
- Exit codes: `0` ok, `1` failed/refused/cancelled, `2` usage or config error, `3` `check` found a blocking issue (`override` level), `130` interrupted.

## Configuration reference

`~/.config/polarum/nodes.toml`. The file is validated at startup; every problem is
reported at once, and unknown keys are errors. See the commented example in the
repository's [`nodes.toml`](nodes.toml).

### `[settings]`

| Key | Default | Description |
|---|---|---|
| `relay` | empty | SSH host/alias (`host` or `user@host`) that sends the magic packet. Empty or missing: send directly by UDP from this machine. |
| `relay_broadcast` | `255.255.255.255` | Broadcast address of the nodes' subnet (IPv4). |
| `relay_wol_command` | `wakeonlan` | Command on the relay host (bare name or path, no spaces). Called as `CMD -i BROADCAST MAC`. |
| `wake_timeout` | `180` | Seconds before a woken node shows `WAKE TIMEOUT`. |

### `[settings.update]`

| Key | Default | Description |
|---|---|---|
| `allowed_origins` | `["Debian", "Debian-Security", "Proxmox"]` | apt origins that are accepted. A package whose origins are not **all** listed (or that has none) needs `override`. |
| `block_on_removals` | `true` | Packages that would be removed need `override`. |
| `snapshot_before` | `true` | Take a ZFS snapshot of the root dataset before each node. |
| `require_snapshot` | `false` | Stop the run if no snapshot can be taken (needs `snapshot_before`). Otherwise a notice is logged and the run continues. |
| `health_timeout` | `300` | Seconds for all `health_ports` to come back after an upgrade or reboot. |
| `reboot_policy` | `"manual"` | `"manual"`, `"if_needed"` or `"never"`. See below. |
| `many_updates_threshold` | `50` | Warn when a node has more pending changes than this. |

### `[[nodes]]`

| Key | Default | Description |
|---|---|---|
| `name` | required | 1-32 chars: letters, digits, `.` `_` `-`. Unique (case-insensitive). |
| `ip` | required | IPv4/IPv6 address used for health checks. Unique. |
| `mac` | required | `AA:BB:CC:DD:EE:01` format. Unique. |
| `ssh_host` | `ip` | Host or alias used for SSH. |
| `ssh_user` | `"polarum"` | SSH account. |
| `health_ports` | `[22]` | TCP ports that must all accept connections for `ONLINE`. |
| `update_order` | unset | Positive integer, unique. Nodes without one are skipped by "check all". |
| `canary` | `false` | Update this node first (requires `update_order`). |

Values for `relay`, `ssh_host`, `ssh_user` and `relay_wol_command` are validated so
they cannot be turned into SSH options or shell commands.

## SSH setup

Polarum runs every SSH command with `BatchMode=yes`: ssh can never prompt for a
password or passphrase, and Polarum never reads, stores or logs one. Your key must
already be loaded in an ssh-agent (or have no passphrase).

On startup (before the TUI) Polarum runs `ssh-add -l`:

- exit 0: fine.
- exit 1 (no identities): it runs `ssh-add` in your terminal so you can type the passphrase to ssh-add itself.
- exit 2 (no agent): it explains the problem and lets you continue or quit.

Load keys automatically on first use, in `~/.ssh/config`:

```text
Host *
    AddKeysToAgent yes
```

Run ssh-agent as a systemd user service, `~/.config/systemd/user/ssh-agent.service`:

```ini
[Unit]
Description=SSH key agent

[Service]
Type=simple
Environment=SSH_AUTH_SOCK=%t/ssh-agent.socket
ExecStart=/usr/bin/ssh-agent -D -a $SSH_AUTH_SOCK

[Install]
WantedBy=default.target
```

Then `systemctl --user enable --now ssh-agent` and, in your shell profile,
`export SSH_AUTH_SOCK="$XDG_RUNTIME_DIR/ssh-agent.socket"`.

Reuse one connection to the relay host (faster, fewer authentications):

```text
Host relay-host
    HostName 192.168.1.5
    User polarum
    ControlMaster auto
    ControlPath ~/.ssh/cm-%C
    ControlPersist 10m
```

Common SSH failures are translated into plain messages: key not in agent
(`run ssh-add`), timeouts, refused connections, unknown or changed host keys,
unresolvable names, and `sudo` asking for a password.

## Sudoers: a dedicated account with narrow rules

Create an account (for example `polarum`) on each node, add your public key, and
give it only what Polarum needs. Verify the real binary paths first
(`command -v systemctl qm pct pvecm apt-get systemd-run journalctl zfs`) and put
the rules in `/etc/sudoers.d/polarum` (edit with `visudo -f`).

Do not write these by hand. Polarum prints rules built from the exact commands
it runs. Generate them on the machine where Polarum is installed, review the
output, then install the file on each node:

```bash
polarum --print-sudoers --sudoers-user polarum --sudoers-dataset rpool/ROOT/pve-1 > polarum.sudoers
scp polarum.sudoers admin@node-a:/tmp/       # copy it however you like
# on the node, as root:
visudo -cf /tmp/polarum.sudoers && install -m 0440 /tmp/polarum.sudoers /etc/sudoers.d/polarum
```

Replace the dataset with the node's root dataset (`findmnt -n -o SOURCE /`) and
check the binary paths with
`command -v systemctl qm pct pvecm apt-get systemd-run journalctl zfs`. The
update-apply line is commented out in the output; uncomment it only on nodes
where Polarum should install updates.

What it grants:

| Alias | Allows | Notes |
|---|---|---|
| `POLARUM_POWER` | `systemctl poweroff`, `systemctl reboot` | exact arguments |
| `POLARUM_PREFLIGHT` | `qm list`, `pct list`, `pvecm status` | read-only, Proxmox only |
| `POLARUM_CHECK` | `apt-get update` | exact arguments |
| `POLARUM_APPLY` | the one `systemd-run` command that starts `polarum-upgrade`, its journal tail, and a `zfs snapshot` of **one** dataset | commented out by default |

Every rule is a single complete command line. The only variable parts are the
12-character run token and the snapshot timestamp, and both use fixed-length `?`
wildcards, so no extra arguments can be appended. An SSH key that can use these
rules can power nodes off and start an apt upgrade, but cannot run arbitrary
commands as root and cannot open a pager through `journalctl`.

> The rules are verified in this project's tests against the commands the code
> executes, using a model of sudo's matching. They are not exercised against a
> real `sudo` in CI. Run `visudo -cf` on the file, and test `polarum check <node>`
> before relying on them; if a rule does not match, the action fails closed with
> "sudo needs a password".

The relay host needs no sudo: the account only runs `wakeonlan`.

## Safety model

- **No state change without confirmation.** Wake: a single `y`. Power off, reboot and installing updates (TUI or `polarum -u`/`-ua`): the word `proceed` typed exactly (or `override` when the review has a blocking rule).
- **Fail closed.** On any error the action stops and reports; nothing continues on its own. If the pre-flight cannot run (for example, SSH fails) the power action is not offered at all.
- **Read-only pre-flight** before power off/reboot: uptime, running VMs/containers (`qm list`, `pct list`), a running `vzdump`, ZFS scrub/resilver (`zpool status`), and cluster quorum (`pvecm status`). Each check shows `n/a` if the command is missing or not permitted, never a misleading "0". If taking the node down would drop the cluster below quorum (assuming one vote per node) you get a red warning.
- **BatchMode SSH only**, input never read from your terminal while the TUI runs.
- **Audit trail.** Every action and result goes to `~/.local/state/polarum/events.log` (mode `0600`) as `timestamp | node | action | result`. Polarum refuses to start if it cannot write the log. The last five entries appear in the TUI.
- Passwords and passphrases are never embedded, stored or logged.

## The update workflow

Press `u` for one node or `Shift+U` for all nodes that have an `update_order`.

### 1. Check (read-only)

Per node, over one SSH session: `apt-get update` (refreshes package lists only),
`LC_ALL=C apt-get -s dist-upgrade` (a simulation), the running kernel, the
installed kernels and `/var/run/reboot-required`; then the pre-flight. Nothing is
installed. For "check all", every node is checked first and a combined review is
shown. If any node's check fails, installing is disabled.

### 2. Review

Counts (upgrades / new / removed / held back), reboot need (with kernel versions),
quorum, running VMs/containers, backup status, and a per-origin package table. `D`
opens the scrollable package list; `ESC` cancels.

| Level | Condition | Confirmation |
|---|---|---|
| **BLOCK** (red) | Any package that would be removed (with `block_on_removals`); any package from an origin not in `allowed_origins`; lost cluster quorum; a backup (`vzdump`) in progress; a ZFS scrub or resilver in progress | type `override` |
| **WARN** (yellow) | Reboot needed; held-back packages; more than `many_updates_threshold` changes | type `proceed` |

A package available from several origins is allowed only if **all** of them are
allowed.

### 3. Apply (rolling)

One node at a time: canary nodes first, then ascending `update_order`. For each
node:

1. Re-check quorum (stops the run if lost, unless you overrode a lost-quorum block).
2. If `snapshot_before`: snapshot the root ZFS dataset (`<dataset>@polarum-pre-upgrade-<timestamp>`). If the root is not ZFS or it fails, a notice is logged and the run continues, unless `require_snapshot = true`.
3. Run the upgrade inside a transient unit so it survives SSH or TUI disconnects:
   `systemd-run --unit=polarum-upgrade --collect ... DEBIAN_FRONTEND=noninteractive apt-get dist-upgrade -y -o Dpkg::Options::=--force-confold -o Dpkg::Options::=--force-confdef`.
   It does **not** run `apt-get update` again. Right before starting, Polarum re-simulates the upgrade and aborts that node (nothing installed) if the plan differs from the one you reviewed. The unit records its exit status in `/run/polarum-upgrade.status` (with a per-run token) because `--collect` removes the unit afterwards.
4. Follow progress by polling the unit state and `journalctl -u polarum-upgrade` every few seconds and show the tail. A dropped connection is retried, not treated as failure; after about 10 minutes without contact the run stops and says the state is unknown.
5. Health check: all `health_ports` reachable within `health_timeout`.
6. Apply `reboot_policy`:
   - `manual` (default): only mark the node `REBOOT NEEDED`; you press `B`.
   - `if_needed`: run a fresh pre-flight (refuses if a backup or ZFS scrub/resilver is running, or the reboot would drop the cluster below quorum), reboot automatically, wait for the node to go down, then wait for it to return and pass the health check. The confirmation screen says so explicitly.
   - `never`: ignore.
7. Log which packages were installed.

**On any failure or timeout the whole run stops**, the remaining nodes are marked
`SKIPPED`, everything is logged and a clear error is shown. `ESC` on the run
screen stops after the current node; a running upgrade is never interrupted. A
summary screen lists every node as OK / FAILED / SKIPPED with package counts and
reboot need.

Nodes with nothing to update are left alone.

## Security notes

- **apt signature verification protects against tampered packages in transit, not against a malicious upstream.** Polarum's `allowed_origins` rule only checks the repository labels apt reports. It cannot tell you whether a trusted repository itself ships something harmful. The review is a tripwire for surprises (new origins, removals), not a guarantee.
- **An update-capable key can still change a node.** The generated apply rule runs one fixed `apt-get dist-upgrade`, so it is not a general root shell, but whoever holds the key can start an upgrade, and apt maintainer scripts run as root. Do not enable `POLARUM_APPLY` on nodes you do not want Polarum to update. Hand-written wildcard rules such as `systemd-run *` **are** effectively root; use `polarum --print-sudoers` instead.
- **Remote text is untrusted.** Output from nodes (apt messages, SSH errors, package names) is stripped of control characters before it is shown or logged, so a compromised node cannot redraw your terminal or fake a prompt. The typed confirmation phrase is the last line of defence; read the review before typing it.
- **Protect the SSH key.** Use a passphrase and an ssh-agent, keep the key on the laptop only, restrict the account to the laptop's address in `sshd` or `authorized_keys` (`from="..."`) where practical, and do not enable the update rule (`POLARUM_APPLY`) on nodes where you do not need it.
- The event log can reveal your node names and activity. It is created with mode `0600`.
- Polarum has been exercised against mocked SSH only so far. Test on a non-critical node and keep backups.

## Development

```bash
python3 -m unittest discover -s tests -t .
```

Tests use only the standard library, sample apt/pre-flight outputs in
`tests/fixtures/`, and mocked `subprocess` or a fake remote-operations object.
They never contact real hosts. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT, see [LICENSE](LICENSE).
