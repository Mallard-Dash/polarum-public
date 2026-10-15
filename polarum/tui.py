"""Shared curses helpers: colors, bounded drawing and confirmation prompts."""
from __future__ import annotations

import curses

from .preflight import BAD, INFO, NA, WARN
from .state import LEVEL_BAD, LEVEL_OK, LEVEL_WARN
from .textutil import printable

STRICT_PHRASE = "proceed"
PAIR_OK, PAIR_BAD, PAIR_WARN, PAIR_ACCENT = 1, 2, 3, 4
LEVEL_PAIRS = {LEVEL_OK: PAIR_OK, LEVEL_BAD: PAIR_BAD, LEVEL_WARN: PAIR_WARN}


def put(win, y: int, x: int, text: str, attr: int = 0) -> None:
    h, w = win.getmaxyx()
    if y < 0 or y >= h or x < 0 or x >= w:
        return
    text = printable(text).replace("\n", " ")
    try:
        win.addstr(y, x, text[:max(0, w - x - 1)], attr)
    except curses.error:
        pass


def pair(n: int) -> int:
    return curses.color_pair(n)


# --- modal confirmation screens (blocking input; workers keep running) ---

def confirm_light(stdscr, title: str, lines: list[tuple[str, int]]) -> bool:
    """Single-key confirmation: only 'y' proceeds."""
    stdscr.timeout(-1)
    try:
        stdscr.erase()
        put(stdscr, 1, 2, f" CONFIRM {title.upper()} ", curses.A_BOLD | pair(PAIR_WARN))
        for i, (text, attr) in enumerate(lines):
            put(stdscr, 3 + i, 2, text, attr)
        put(stdscr, 4 + len(lines), 2, "Press y to confirm; any other key cancels.")
        stdscr.refresh()
        return stdscr.getch() in (ord("y"), ord("Y"))
    finally:
        stdscr.timeout(100)


def confirm_typed(
    stdscr, title: str, lines: list[tuple[str, int]], phrase: str = STRICT_PHRASE,
) -> bool:
    """Typed confirmation: the user must enter `phrase` exactly."""
    stdscr.timeout(-1)
    curses.curs_set(1)
    typed = ""
    try:
        while True:
            stdscr.erase()
            h, w = stdscr.getmaxyx()
            put(stdscr, 1, 2, f" CONFIRM {title.upper()} ", curses.A_BOLD | pair(PAIR_BAD))
            for i, (text, attr) in enumerate(lines):
                put(stdscr, 3 + i, 2, text, attr)
            y = min(h - 2, 5 + len(lines) + 2)
            put(stdscr, y - 2, 2, f'Type exactly "{phrase}" and press Enter to continue.')
            put(stdscr, y - 1, 2, "Press ESC to cancel.")
            put(stdscr, y, 2, "> ", curses.A_BOLD)
            put(stdscr, y, 4, typed)
            try:
                stdscr.move(y, min(w - 2, 4 + len(typed)))
            except curses.error:
                pass
            stdscr.refresh()
            key = stdscr.getch()
            if key == 27:
                return False
            if key in (10, 13):
                return typed == phrase
            if key in (curses.KEY_BACKSPACE, 127, 8):
                typed = typed[:-1]
            elif 32 <= key <= 126 and len(typed) < 32:
                typed += chr(key)
    finally:
        curses.curs_set(0)
        stdscr.timeout(100)


LINE_ATTRS = {INFO: lambda: 0, WARN: lambda: pair(PAIR_WARN),
              BAD: lambda: pair(PAIR_BAD) | curses.A_BOLD, NA: lambda: curses.A_DIM}


