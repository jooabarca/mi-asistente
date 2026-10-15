"""Console output: transcripts and status lines."""

from __future__ import annotations

import sys
from datetime import datetime

try:
    from colorama import just_fix_windows_console

    just_fix_windows_console()
except ImportError:
    pass

_RESET = "\033[0m"
_COLORS = {"user": "\033[96m", "assistant": "\033[92m", "tool": "\033[95m", "info": "\033[90m",
           "warn": "\033[93m", "error": "\033[91m", "debug": "\033[90m"}

# Make sure accents and emoji in transcripts never crash printing on Windows consoles.
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(errors="replace")
    except AttributeError:
        pass


_listeners: list = []


def add_listener(callback) -> None:
    """callback(kind, text, stamp) is called for every line, from whichever thread printed it."""
    _listeners.append(callback)


def _line(kind: str, label: str, text: str) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    color = _COLORS.get(kind, "")
    print(f"{_COLORS['info']}{stamp}{_RESET} {color}{label}{_RESET} {text}", flush=True)  # no-op without a console
    for callback in _listeners:
        try:
            callback(kind, text, stamp)
        except Exception:
            pass


def user(text: str) -> None:
    _line("user", "You:", text)


def assistant(text: str) -> None:
    _line("assistant", "AI: ", text)


def tool(text: str) -> None:
    _line("tool", "[memory]", text)


def info(text: str) -> None:
    _line("info", "--", text)


def warn(text: str) -> None:
    _line("warn", "!!", text)


def error(text: str) -> None:
    _line("error", "ERROR:", text)


def debug(text: str) -> None:
    _line("debug", "..", text)
