"""Hotkeys: global ones via the `keyboard` package, plus single keys in the console window."""

from __future__ import annotations

import sys
import threading
from typing import Callable


class Hotkeys:
    def __init__(self, combos: dict[str, Callable[[], None]],
                 console_keys: dict[str, Callable[[], None]] | None = None):
        """combos: {"ctrl+shift+m": callback}; console_keys: {"m": callback} for the console window.
        Callbacks run on the hotkey thread, so they must be thread-safe."""
        self.combos = combos
        self.console_keys = console_keys
        self._stop = threading.Event()
        self._keyboard = None

    def start(self) -> str | None:
        """Start listening. Returns a warning if global hotkeys aren't available."""
        warning = None
        try:
            import keyboard

            for combo, callback in self.combos.items():
                if combo:
                    keyboard.add_hotkey(combo, callback, suppress=False)
            self._keyboard = keyboard
        except Exception as exc:  # e.g. invalid combo text
            warning = f"Global hotkeys unavailable ({exc})."
        if self.console_keys and sys.platform == "win32" and sys.stdin is not None:
            threading.Thread(target=self._console_loop, name="console-keys", daemon=True).start()
        return warning

    def stop(self) -> None:
        self._stop.set()
        if self._keyboard is not None:
            try:
                self._keyboard.unhook_all_hotkeys()
            except Exception:
                pass

    def _console_loop(self) -> None:
        import msvcrt

        while not self._stop.wait(0.05):
            if msvcrt.kbhit():
                callback = self.console_keys.get(msvcrt.getwch().lower())
                if callback:
                    callback()
