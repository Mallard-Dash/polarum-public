"""Text that came from a remote host must never reach a terminal unfiltered.

A compromised node (or a hostile package repository) controls apt output, SSH
error text and package names. Raw escape sequences in them could clear the
screen, redraw a fake confirmation prompt, retitle the window or write to the
clipboard. Everything remote-supplied is passed through `printable` before it
is shown or logged.
"""
from __future__ import annotations


def printable(text: str) -> str:
    """Replace control, format (e.g. bidi override) and non-space whitespace characters.

    Newlines are kept as line breaks; every other unprintable character becomes
    a single space so the layout is preserved and nothing is silently glued
    together.
    """
    return "\n".join(
        "".join(ch if ch.isprintable() else " " for ch in line)
        for line in str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    )
