"""Global hotkeys through Windows RegisterHotKey (no keyboard hook)."""

from __future__ import annotations

import sys

import pytest

from voicecompanion.hotkeys import Hotkeys, parse_combo


@pytest.mark.parametrize("combo,expected", [
    ("ctrl+shift+m", (0x2 | 0x4, ord("M"))),
    ("Ctrl + Shift + F9", (0x2 | 0x4, 0x78)),
    ("alt+1", (0x1, ord("1"))),
    ("win+space", (0x8, 0x20)),
])
def test_parse_combo(combo, expected):
    assert parse_combo(combo) == expected


@pytest.mark.parametrize("combo", ["ctrl+shift", "ctrl+a+b", "ctrl+banana", "f25"])
def test_parse_combo_rejects_bad_text(combo):
    with pytest.raises(ValueError):
        parse_combo(combo)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows hotkeys")
def test_registers_and_reports_taken_or_bad_combos():
    combo = "ctrl+shift+alt+f24"  # unlikely to be used by anything else
    first = Hotkeys({combo: lambda: None})
    assert first.start() is None
    try:
        second = Hotkeys({combo: lambda: None, "ctrl+banana": lambda: None})
        warning = second.start()
        second.stop()
        assert "already used by another program" in warning and "banana" in warning
    finally:
        first.stop()
    third = Hotkeys({combo: lambda: None})  # freed again after stop()
    for _ in range(50):
        if third.start() is None:
            break
        third.stop()
        third = Hotkeys({combo: lambda: None})
    else:
        pytest.fail("hotkey was not released")
    third.stop()
