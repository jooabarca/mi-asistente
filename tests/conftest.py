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

    from voicecompanion.gui import MainWindow

    for attempt in range(3):
        try:
            return MainWindow(*args)
        except tkinter.TclError as exc:
            if "display" in str(exc).lower():
                pytest.skip("no display")
            # Creating many Tk windows in one test process sometimes fails to read Tcl's init.tcl on Windows
            # ("couldn't read file ... No error"); the app itself only ever creates one window.
            if "init.tcl" not in str(exc) or attempt == 2:
                raise
