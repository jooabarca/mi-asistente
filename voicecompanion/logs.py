"""Log file and freeze detector. Opened from the desktop shortcut the app has no console, so without this
any error or freeze would leave no trace.

- logs/mi-asistente.log: every console line plus uncaught errors (rotates at 1 MB, keeps 3 old files)
- logs/freezes.log: if the window stops responding for FREEZE_SECONDS, the stack of every thread is dumped,
  which shows exactly what the app was stuck on
"""

from __future__ import annotations

import faulthandler
import logging
import sys
import threading
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import ui
from .config import ROOT

LOG_DIR = ROOT / "logs"
FREEZE_SECONDS = 5.0

log = logging.getLogger("mi_asistente")


def setup_logging(log_dir: Path = LOG_DIR) -> Path:
    """Send console lines and uncaught exceptions to a rotating log file. Returns the log file path."""
    log_dir.mkdir(exist_ok=True)
    path = log_dir / "mi-asistente.log"
    if not log.handlers:
        handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        levels = {"error": logging.ERROR, "warn": logging.WARNING, "debug": logging.DEBUG}
        ui.add_listener(lambda kind, text, stamp: log.log(levels.get(kind, logging.INFO), "%s: %s", kind, text))

        def excepthook(exc_type, exc, tb):
            log.critical("Uncaught error", exc_info=(exc_type, exc, tb))

        def thread_excepthook(args):
            log.critical("Uncaught error in thread %s", args.thread.name if args.thread else "?",
                         exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

        sys.excepthook = excepthook
        threading.excepthook = thread_excepthook
    log.info("---- %s started (Python %s) ----", "Mi Asistente", sys.version.split()[0])
    return path


class FreezeWatchdog:
    """The window calls beat() on every refresh; if beats stop for FREEZE_SECONDS while the app is open,
    all thread stacks are written to logs/freezes.log (once per freeze)."""

    def __init__(self, log_dir: Path = LOG_DIR, limit: float = FREEZE_SECONDS):
        self.limit = limit
        self.path = log_dir / "freezes.log"
        log_dir.mkdir(exist_ok=True)
        self.last_beat = time.monotonic()
        self.reported = False
        self._stop = threading.Event()
        self.freezes = 0

    def beat(self) -> None:
        if self.reported:
            log.warning("Window responsive again after a freeze.")
        self.last_beat = time.monotonic()
        self.reported = False

    def check(self, now: float | None = None) -> bool:
        """Dump stacks if the window is frozen right now. Returns True if it wrote a report."""
        now = time.monotonic() if now is None else now
        stalled = now - self.last_beat
        if stalled < self.limit or self.reported:
            return False
        self.reported = True
        self.freezes += 1
        log.error("Window not responding for %.0f s; thread stacks written to %s", stalled, self.path.name)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S}: window not responding for {stalled:.0f} s =====\n")
            f.flush()
            faulthandler.dump_traceback(file=f, all_threads=True)
        return True

    def start(self) -> None:
        def watch():
            while not self._stop.wait(1.0):
                try:
                    self.check()
                except Exception:  # the watchdog must never take the app down
                    pass

        threading.Thread(target=watch, name="freeze-watchdog", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
