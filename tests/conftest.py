import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def isolated_preferences(tmp_path, monkeypatch):
    """Tests never read or change the user's real preferences.json (chosen voice, speaker mode)."""
    from voicecompanion import config

    monkeypatch.setattr(config, "PREFERENCES_PATH", tmp_path / "preferences.json")


def open_window(*args):
    """Create the app window, skipping only when there is really no screen (any other Tk error fails the test)."""
    import tkinter

    from voicecompanion.gui import open_main_window

    try:
        # Retries the occasional Tcl/Tk library-read failure on startup, like the app does.
        return open_main_window(*args, attempts=5)
    except tkinter.TclError as exc:
        if "display" in str(exc).lower():
            pytest.skip("no display")
        raise
