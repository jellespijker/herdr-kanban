"""Pure terminal I/O, ANSI styling, SGR mouse/key decoding, and cell-width helpers."""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import sys
import termios
import tty
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass

ALT_ON = "\x1b[?1049h\x1b[?25l"
ALT_OFF = "\x1b[?25h\x1b[?1049l"
CURSOR_SHOW = "\x1b[?25h"
CURSOR_HIDE = "\x1b[?25l"
CLEAR = "\x1b[H\x1b[2J"
MOUSE_ON = "\x1b[?1000h\x1b[?1006h"
MOUSE_OFF = "\x1b[?1006l\x1b[?1000l"

ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
MOUSE_SGR_RE = re.compile(rb"^\x1b\[<(\d+);(\d+);(\d+)([Mm])")

KEY_SEQUENCES: dict[bytes, str] = {
    b"\x1b[A": "up",
    b"\x1bOA": "up",
    b"\x1b[B": "down",
    b"\x1bOB": "down",
    b"\x1b[C": "right",
    b"\x1bOC": "right",
    b"\x1b[D": "left",
    b"\x1bOD": "left",
    b"\x1b[5~": "pageup",
    b"\x1b[6~": "pagedown",
    b"\x1b[H": "home",
    b"\x1b[F": "end",
}


@dataclass(slots=True)
class InputEvent:
    kind: str  # key | mouse
    key: str = ""
    button: int = 0
    col: int = 0
    row: int = 0
    pressed: bool = True


def style(text: str, sgr: str) -> str:
    if not sgr:
        return text
    return f"\x1b[{sgr}m{text}\x1b[0m"


def bold(text: str) -> str:
    return style(text, "1")


def dim(text: str) -> str:
    return style(text, "2")


def cyan(text: str) -> str:
    return style(text, "36")


def green(text: str) -> str:
    return style(text, "32")


def yellow(text: str) -> str:
    return style(text, "33")


def red(text: str) -> str:
    return style(text, "31")


def magenta(text: str) -> str:
    return style(text, "35")


def reverse(text: str) -> str:
    return style(text, "7")


def visible_len(text: str) -> int:
    plain = ANSI_ESCAPE_RE.sub("", text)
    width = 0
    for ch in plain:
        if unicodedata.combining(ch):
            continue
        eaw = unicodedata.east_asian_width(ch)
        width += 2 if eaw in ("W", "F") else 1
    return width


def truncate(text: str, width: int, ellipsis: str = "…") -> str:
    """Truncate plain text to fit within `width` terminal cells."""
    if width <= 0:
        return ""
    if visible_len(text) <= width:
        return text
    target = max(0, width - visible_len(ellipsis))
    out: list[str] = []
    used = 0
    for ch in text:
        w = 0 if unicodedata.combining(ch) else (2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1)
        if used + w > target:
            break
        out.append(ch)
        used += w
    return "".join(out) + (ellipsis if width >= visible_len(ellipsis) else "")


def pad_ansi(text: str, width: int) -> str:
    """Pad an ANSI-styled string on the right to exact cell `width`."""
    cur = visible_len(text)
    if cur >= width:
        return text
    return text + (" " * (width - cur))


def wrap_lines(text: str, width: int, max_lines: int) -> list[str]:
    if width <= 4 or max_lines <= 0:
        return []
    out: list[str] = []
    for raw_line in text.splitlines() or [""]:
        line = raw_line.rstrip()
        if not line:
            out.append("")
        else:
            while visible_len(line) > width:
                piece = truncate(line, width, ellipsis="")
                # Prefer breaking at whitespace when reasonable
                space_idx = piece.rfind(" ")
                if space_idx > width // 2:
                    piece = piece[:space_idx]
                    line = line[space_idx + 1 :].lstrip()
                else:
                    line = line[len(piece) :]
                out.append(piece)
                if len(out) >= max_lines:
                    return out[:max_lines]
            out.append(line)
        if len(out) >= max_lines:
            return out[:max_lines]
    return out[:max_lines]


def term_size(default_cols: int = 120, default_rows: int = 36) -> tuple[int, int]:
    size = shutil.get_terminal_size((default_cols, default_rows))
    return max(40, size.columns), max(14, size.lines)


def open_url(url: str) -> bool:
    if not url:
        return False
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    exe = shutil.which(opener)
    if not exe:
        return False
    pid = os.spawnlp(os.P_NOWAIT, exe, exe, url)
    return pid > 0


@contextlib.contextmanager
def raw_terminal(in_fd: int = 0, out_fd: int = 1, *, mouse: bool = True) -> Iterator[None]:
    old_attrs = termios.tcgetattr(in_fd)
    os.write(out_fd, (ALT_ON + (MOUSE_ON if mouse else "")).encode())
    try:
        tty.setraw(in_fd)
        yield
    finally:
        termios.tcsetattr(in_fd, termios.TCSADRAIN, old_attrs)
        os.write(out_fd, ((MOUSE_OFF if mouse else "") + ALT_OFF).encode())


def parse_input_bytes(buf: bytes) -> list[InputEvent]:
    """Decode raw terminal input buffer into discrete InputEvents."""
    events: list[InputEvent] = []
    idx = 0
    n = len(buf)
    while idx < n:
        match = MOUSE_SGR_RE.match(buf[idx:])
        if match:
            btn = int(match.group(1))
            col = int(match.group(2))
            row = int(match.group(3))
            pressed = match.group(4) == b"M"
            events.append(InputEvent(kind="mouse", button=btn, col=col, row=row, pressed=pressed))
            idx += match.end()
            continue

        matched_seq = False
        for seq, name in KEY_SEQUENCES.items():
            if buf[idx:].startswith(seq):
                events.append(InputEvent(kind="key", key=name))
                idx += len(seq)
                matched_seq = True
                break
        if matched_seq:
            continue

        b = buf[idx]
        if b == 0x1B:
            # Bare Escape (if no more trailing escape bytes immediately present)
            events.append(InputEvent(kind="key", key="esc"))
            idx += 1
            continue
        if b in (0x0D, 0x0A):
            events.append(InputEvent(kind="key", key="enter"))
            idx += 1
            continue
        if b in (0x7F, 0x08):
            events.append(InputEvent(kind="key", key="backspace"))
            idx += 1
            continue
        if b == 0x03:
            events.append(InputEvent(kind="key", key="ctrl+c"))
            idx += 1
            continue
        if b == 0x09:
            events.append(InputEvent(kind="key", key="tab"))
            idx += 1
            continue

        # Decode UTF-8 character starting at idx
        ch_len = 1
        if (b & 0xE0) == 0xC0:
            ch_len = 2
        elif (b & 0xF0) == 0xE0:
            ch_len = 3
        elif (b & 0xF8) == 0xF0:
            ch_len = 4
        char = buf[idx : idx + ch_len].decode("utf-8", errors="ignore")
        if char:
            events.append(InputEvent(kind="key", key=char))
        idx += ch_len
    return events
