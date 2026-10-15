"""Command-line entry point."""
from __future__ import annotations

import argparse
import curses
import locale
import os
import shutil
import sys
from pathlib import Path

from . import __version__
from .config import ConfigError, default_config_path, default_log_path, load_config
from .events import EventLog
from .cmd import run_command
from .cmd_update import cmd_update
from .ssh import AGENT_OK, agent_status, ensure_agent
from .state import NodeState
from .ui import run_ui


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="polarum", description="Homelab power-state console (SSH + Wake-on-LAN)")
    parser.add_argument(
        "-c", "--config", default=str(default_config_path()),
        help="path to nodes.toml (default: %(default)s)")
    parser.add_argument(
        "--check-config", action="store_true",
        help="validate the configuration and exit")
    parser.add_argument("--version", action="version", version=f"polarum {__version__}")
    parser.add_argument(
        "-u", "--update", metavar="NODE",
        help="update one node (name or IP): check, review, typed confirmation, then install")
    parser.add_argument(
        "-ua", "--update-all", action="store_true",
        help="update every node that has an update_order, one at a time")
    sub = parser.add_subparsers(dest="command", metavar="command",
                                help="omit to start the interactive TUI")
    status = sub.add_parser("status", help="print the health of one node or all nodes")
    status.add_argument("target", nargs="?", metavar="node|ip",
                        help="node name or IP address (default: all nodes)")
    status.add_argument("-a", "--all", action="store_true", help="show all nodes (default)")
    status.add_argument("-v", "--verbose", action="store_true",
                        help="also show MAC, SSH target and each health port")
    wake = sub.add_parser("wake", help="send a Wake-on-LAN magic packet")
    wake.add_argument("node")
    wake.add_argument("-y", "--yes", action="store_true",
                      help="skip the confirmation question (wake only)")
    off = sub.add_parser("off", help="power off a node (typed confirmation required)")
    off.add_argument("node")
    check = sub.add_parser("check", help="read-only update check; never installs")
    check.add_argument("target", metavar="node|all")
    args = parser.parse_args(argv)
    if args.update and args.update_all:
        parser.error("-u and -ua cannot be combined")
    if (args.update or args.update_all) and args.command:
        parser.error("-u/-ua cannot be combined with a command")
    if args.command == "status" and args.all and args.target:
        parser.error("status: give a node or -a, not both")
    if args.update or args.update_all:
        args.command = "update"
        args.target = args.update  # None means all nodes with an update_order
    return args


def run_cli_command(args: argparse.Namespace, settings, nodes) -> int:
    needs_ssh = args.command in ("off", "check", "update") or (
        args.command == "wake" and bool(settings.relay))
    if needs_ssh and shutil.which("ssh") is None:
        print("polarum: 'ssh' was not found in PATH.", file=sys.stderr)
        return 1
    events = None
    if args.command != "status":
        try:
            events = EventLog.open(default_log_path())
        except OSError as exc:
            print(f"polarum: cannot write event log {default_log_path()}: {exc.strerror}",
                  file=sys.stderr)
            return 1
    if needs_ssh:
        if sys.stdin.isatty():
            if not ensure_agent():
                return 1
        elif agent_status() != AGENT_OK:
            print("polarum: warning: no ssh-agent identities available; SSH calls "
                  "will fail unless a key without passphrase is configured.", file=sys.stderr)
    try:
        if args.command == "update":
            return cmd_update(settings, nodes, events, args.target)
        return run_command(args.command, args, settings, nodes, events)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    path = Path(args.config).expanduser()
    try:
        settings, nodes = load_config(path)
    except ConfigError as exc:
        print(f"polarum: invalid configuration ({path}):", file=sys.stderr)
        for error in exc.errors:
            print(f"  - {error}", file=sys.stderr)
        return 2
    if args.check_config:
        print(f"Configuration OK: {len(nodes)} node(s) in {path}")
        return 0

    if args.command:
        return run_cli_command(args, settings, nodes)

    if shutil.which("ssh") is None:
        print("polarum: 'ssh' was not found in PATH.", file=sys.stderr)
        return 1
    try:
        events = EventLog.open(default_log_path())
    except OSError as exc:
        print(f"polarum: cannot write event log {default_log_path()}: {exc.strerror}",
              file=sys.stderr)
        return 1
    if sys.stdin.isatty() and not ensure_agent():
        return 1

    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass  # fall back to the C locale; box-drawing may degrade
    os.environ.setdefault("ESCDELAY", "25")
    states = [NodeState(node) for node in nodes]
    try:
        curses.wrapper(run_ui, settings, states, events)
    except KeyboardInterrupt:
        pass
    return 0
