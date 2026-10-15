"""Keeping the window healthy in long meetings: freeze reports, no needless redraws, quiet chat in listener modes."""

from __future__ import annotations

import queue
import threading

import pytest

from voicecompanion.config import load_profiles, load_settings
from voicecompanion.logs import FreezeWatchdog
from voicecompanion.memory import MemoryStore


def test_freeze_watchdog_dumps_thread_stacks_once_per_freeze(tmp_path):
    dog = FreezeWatchdog(log_dir=tmp_path, limit=5)
    dog.beat()
    start = dog.last_beat
    assert not dog.check(start + 4)  # a slow moment is not a freeze
    assert dog.check(start + 6)
    report = (tmp_path / "freezes.log").read_text(encoding="utf-8")
    assert "window not responding for 6 s" in report
    assert f"Thread 0x{threading.get_ident():016x}" in report or "Current thread" in report  # stacks were dumped
    assert not dog.check(start + 20)  # one report per freeze
    dog.beat()  # the window recovered
    assert dog.check(dog.last_beat + 6) and dog.freezes == 2


class FakeWidget:
    def __init__(self):
        self.calls = []

    def configure(self, **options):
        self.calls.append(options)


def _window(tmp_path, profile):
    pytest.importorskip("customtkinter")
    from conftest import open_window

    from voicecompanion.app import VoiceCompanionApp

    profiles = load_profiles()
    app = VoiceCompanionApp(load_settings(), "key", profiles, profiles[profile], MemoryStore(tmp_path / "m.db"))
    return app, open_window(app, app.memory, queue.Queue(), None, {})


def _texts(widget):
    for child in widget.winfo_children():
        try:
            yield child.cget("text")
        except Exception:
            pass
        yield from _texts(child)


def test_widgets_are_only_redrawn_when_something_changes(tmp_path):
    app, window = _window(tmp_path, "Default")
    try:
        widget = FakeWidget()
        window._set(widget, "x", text="hola", text_color="red")
        window._set(widget, "x", text="hola", text_color="red")
        window._set(widget, "x", text="chao", text_color="red")
        assert widget.calls == [{"text": "hola", "text_color": "red"}, {"text": "chao", "text_color": "red"}]
    finally:
        window.closing = True
        window.destroy()
        app.memory.close()


def test_listener_modes_show_only_the_assistants_notes(tmp_path):
    app, window = _window(tmp_path, "FactInsights")
    try:
        for kind, text in [("user", "El presupuesto se revisa el viernes"),
                           ("user", "En Chile el postnatal del padre es de cinco días"),
                           ("assistant", "Dato: el permiso postnatal parental puede compartirse con el padre."),
                           ("user", "Ah, no sabía")]:
            window.events.put((kind, text, "10:00:00"))
        window._tick()
        shown = list(_texts(window.chat))
        assert any("postnatal parental" in t for t in shown)
        assert not any("presupuesto" in t or "no sabía" in t for t in shown)
    finally:
        window.closing = True
        window.destroy()
        app.memory.close()


def test_window_keeps_refreshing_after_an_error(tmp_path, monkeypatch):
    app, window = _window(tmp_path, "Default")
    try:
        scheduled = []
        monkeypatch.setattr(window, "after", lambda ms, fn: scheduled.append(fn))
        monkeypatch.setattr(window, "_update_status", lambda: 1 / 0)
        with pytest.raises(ZeroDivisionError):
            window._tick()
        assert scheduled == [window._tick]  # the next refresh is still scheduled
    finally:
        window.closing = True
        window.destroy()
        app.memory.close()
