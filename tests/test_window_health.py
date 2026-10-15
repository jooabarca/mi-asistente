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


def test_top_bar_views_theme_switch_and_status_bar(tmp_path):
    import customtkinter as ctk

    from voicecompanion.config import load_preferences
    from voicecompanion.gui import VIEW_CHAT, VIEW_THINGS

    app, window = _window(tmp_path, "Default")
    try:
        window._tick()
        assert window.views[VIEW_CHAT].winfo_manager() == "grid"
        assert window.views[VIEW_THINGS].winfo_manager() == ""
        window.show_view(VIEW_THINGS)  # the "Mis cosas" button
        assert window.views[VIEW_THINGS].winfo_manager() == "grid" and window.views[VIEW_CHAT].winfo_manager() == ""

        window.theme_switch.select()
        window._on_theme()
        assert ctk.get_appearance_mode() == "Dark" and load_preferences()["theme"] == "dark"
        window.theme_switch.deselect()
        window._on_theme()
        assert ctk.get_appearance_mode() == "Light" and load_preferences()["theme"] == "light"

        app.usage.add(text_input=10)
        window._tick()
        assert window.usage_label.cget("text") == "Esta sesión: 1 respuesta"
        assert int(window.usage_label.grid_info()["row"]) == 2  # bottom status bar, below the content

        tips = {tip.widget: tip.text for tip in window.tooltips}
        assert tips[window.mute_button] == "Atajo: Ctrl+Shift+M"
        assert "Ctrl+Shift+F9" in tips[window.sleep_button]
        window.tooltips[0]._show()
        assert window.tooltips[0]._tip is not None
        window.tooltips[0]._hide()
        assert window.tooltips[0]._tip is None
    finally:
        window.closing = True
        window.destroy()
        app.memory.close()


def test_window_start_is_retried_after_a_tcl_read_error(monkeypatch):
    import tkinter

    from voicecompanion import gui

    attempts = []

    def flaky_window(*args):
        attempts.append(args)
        if len(attempts) < 3:
            raise tkinter.TclError('couldn\'t read file ".../tk8.6/msgs/en.msg": no such file or directory')
        return "window"

    monkeypatch.setattr(gui, "MainWindow", flaky_window)
    monkeypatch.setattr(gui.time, "sleep", lambda s: None)
    assert gui.open_main_window("app") == "window" and len(attempts) == 3
    attempts.clear()
    monkeypatch.setattr(gui, "MainWindow", lambda *a: (_ for _ in ()).throw(tkinter.TclError("still broken")))
    with pytest.raises(tkinter.TclError):
        gui.open_main_window("app")  # gives up after 3 tries and reports it


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
