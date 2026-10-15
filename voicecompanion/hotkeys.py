"""Hotkeys: global ones via Windows RegisterHotKey, plus single keys in the console window.

RegisterHotKey asks Windows for just these combinations (no keyboard hook), so antivirus programs don't
mistake the packaged app for a keylogger. While the app runs, other programs don't receive these combinations.
"""

from __future__ import annotations

import logging
import sys
import threading
from typing import Callable

MODIFIERS = {"alt": 0x1, "ctrl": 0x2, "control": 0x2, "shift": 0x4, "win": 0x8, "windows": 0x8}
MOD_NOREPEAT = 0x4000  # holding the keys down fires once
NAMED_KEYS = {
    "space": 0x20, "enter": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B, "pause": 0x13, "insert": 0x2D,
    "delete": 0x2E, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
}
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012

log = logging.getLogger("mi_asistente")


def parse_combo(combo: str) -> tuple[int, int]:
    """'ctrl+shift+m' -> (modifier flags, virtual-key code). Raises ValueError for text it can't read."""
    modifiers, key = 0, None
    for part in (p.strip().lower() for p in combo.split("+")):
        if part in MODIFIERS:
            modifiers |= MODIFIERS[part]
        elif key is not None:
            raise ValueError(f"'{combo}' has more than one key")
        elif len(part) == 1 and part.isascii() and part.isalnum():
            key = ord(part.upper())
        elif part[:1] == "f" and part[1:].isdigit() and 1 <= int(part[1:]) <= 24:
            key = 0x6F + int(part[1:])  # VK_F1 = 0x70
        elif part in NAMED_KEYS:
            key = NAMED_KEYS[part]
        else:
            raise ValueError(f"unknown key '{part}' in '{combo}'")
    if key is None:
        raise ValueError(f"'{combo}' has no key, only modifiers")
    return modifiers, key


class Hotkeys:
    def __init__(self, combos: dict[str, Callable[[], None]],
                 console_keys: dict[str, Callable[[], None]] | None = None):
        """combos: {"ctrl+shift+m": callback}; console_keys: {"m": callback} for the console window.
        Callbacks run on the hotkey thread, so they must be thread-safe."""
        self.combos = combos
        self.console_keys = console_keys
        self._stop = threading.Event()
        self._thread_id: int | None = None
        self._problems: list[str] = []

    def start(self) -> str | None:
        """Start listening. Returns a warning if some global hotkeys aren't available."""
        warning = None
        if sys.platform == "win32":
            ready = threading.Event()
            threading.Thread(target=self._hotkey_loop, args=(ready,), name="hotkeys", daemon=True).start()
            ready.wait(5)
            if self._problems:
                warning = f"Some global hotkeys are unavailable: {'; '.join(self._problems)}."
        else:
            warning = "Global hotkeys are only available on Windows."
        if self.console_keys and sys.platform == "win32" and sys.stdin is not None:
            threading.Thread(target=self._console_loop, name="console-keys", daemon=True).start()
        return warning

    def stop(self) -> None:
        self._stop.set()
        if self._thread_id is not None:
            import ctypes

            ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)

    def _hotkey_loop(self, ready: threading.Event) -> None:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        callbacks: dict[int, Callable[[], None]] = {}
        try:
            for number, (combo, callback) in enumerate(self.combos.items(), start=1):
                if not combo:
                    continue
                try:
                    modifiers, key = parse_combo(combo)
                except ValueError as exc:
                    self._problems.append(str(exc))
                    continue
                if user32.RegisterHotKey(None, number, modifiers | MOD_NOREPEAT, key):
                    callbacks[number] = callback
                else:
                    self._problems.append(f"{combo} is already used by another program")
            self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        finally:
            ready.set()
        message = wintypes.MSG()
        try:
            while not self._stop.is_set() and user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                callback = callbacks.get(message.wParam) if message.message == WM_HOTKEY else None
                if callback is not None:
                    try:
                        callback()
                    except Exception:
                        log.exception("Hotkey action failed")
        finally:
            for number in callbacks:
                user32.UnregisterHotKey(None, number)

    def _console_loop(self) -> None:
        import msvcrt

        while not self._stop.wait(0.05):
            if msvcrt.kbhit():
                callback = self.console_keys.get(msvcrt.getwch().lower())
                if callback:
                    callback()
