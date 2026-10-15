"""Coach help: offering help after a silence, and the translate-everything mode (tool, memory, window switch)."""

from __future__ import annotations

import asyncio
import json

import numpy as np
import pytest

from voicecompanion.app import NUDGE_PROMPT, VoiceCompanionApp, is_voice
from voicecompanion.config import load_preferences, load_profiles, load_settings
from voicecompanion.memory import MemoryStore


class FakePlayer:
    def __init__(self):
        self.is_playing = False
        self.last_output_time = 0.0

    def interrupt(self):
        return None

    def recently_played(self, tail_s):
        return False


class FakeBackend:
    name = "Fake"
    input_rate = 16000

    def __init__(self):
        self.prompts: list[str] = []

    async def send_prompt(self, text):
        self.prompts.append(text)


def make_app(tmp_path, profile="JapaneseCoach"):
    profiles = load_profiles()
    app = VoiceCompanionApp(load_settings(), "key", profiles, profiles[profile], MemoryStore(tmp_path / "m.db"))
    app.player = FakePlayer()
    app.connection_ok = True
    app.history = [("User", "こんにちは"), ("Assistant", "こんにちは！お元気ですか？")]
    return app


def test_voice_detection():
    silence = np.zeros(640, dtype=np.int16).tobytes()
    hum = (np.random.default_rng(1).normal(0, 80, 640)).astype(np.int16).tobytes()
    speech = (np.sin(np.arange(640) / 3) * 4000).astype(np.int16).tobytes()
    assert not is_voice(silence) and not is_voice(hum) and is_voice(speech)


def test_offers_help_once_after_the_learner_goes_quiet(tmp_path):
    app = make_app(tmp_path)
    app.last_user_voice = 90.0
    app.player.last_output_time = 100.0  # the coach finished speaking at t=100
    wait = app.settings.coach_nudge_seconds
    assert not app.nudge_due(100 + wait - 1)  # still within the grace period
    assert app.nudge_due(100 + wait)
    app.nudged = True
    assert not app.nudge_due(100 + wait + 30)  # only once per silence
    app._heard_user()  # the learner says something…
    app.player.last_output_time = app.last_user_voice + 1  # …the coach answers…
    assert app.nudge_due(app.player.last_output_time + wait)  # …and a new silence can be helped again


@pytest.mark.parametrize("change", ["not_coach", "muted", "sleeping", "speaking", "user_spoke_last",
                                    "no_exchange_yet", "disabled"])
def test_no_help_offer_when_it_would_be_out_of_place(tmp_path, change):
    app = make_app(tmp_path, "Default" if change == "not_coach" else "JapaneseCoach")
    app.last_user_voice, app.player.last_output_time = 90.0, 100.0
    if change == "muted":
        app.muted = True
    elif change == "sleeping":
        app.sleeping = True
    elif change == "speaking":
        app.player.is_playing = True
    elif change == "user_spoke_last":
        app.last_user_voice = 105.0
    elif change == "no_exchange_yet":
        app.history = [("Assistant", "Hello!")]  # just the greeting
    elif change == "disabled":
        app.settings.coach_nudge_seconds = 0
    assert not app.nudge_due(200.0)


def test_watcher_sends_the_help_prompt(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from voicecompanion import app as app_module

    app = make_app(tmp_path)
    app.backend = FakeBackend()
    app.last_user_voice = 1.0
    app.player.last_output_time = 2.0
    # Freeze only the app's clock (asyncio needs the real one for its own sleeps).
    monkeypatch.setattr(app_module, "time", SimpleNamespace(monotonic=lambda: 1000.0))

    async def scenario():
        task = asyncio.create_task(app._coach_nudge_watch())
        await asyncio.sleep(1.3)
        task.cancel()

    asyncio.run(scenario())
    assert app.backend.prompts == [NUDGE_PROMPT.format(seconds=app.settings.coach_nudge_seconds)]


def test_translation_mode_by_voice_is_remembered_per_language(tmp_path):
    app = make_app(tmp_path)
    assert "Translation mode is OFF" in app.build_instructions()
    result = app.tools.execute("SetTranslationMode", json.dumps({"On": True}))
    assert result.output["ok"] and result.log == "🔁 Traducir todo: activado"
    assert "Translation mode is ON" in app.build_instructions()
    assert load_preferences() == {"translate:JapaneseCoach": True}
    assert "JapaneseCoach" in make_app(tmp_path).translate_all  # still on after restarting the app
    assert "PortugueseCoach" not in make_app(tmp_path).translate_all  # each language has its own setting


def test_translation_mode_is_only_for_coaches(tmp_path):
    app = make_app(tmp_path, "FactChecker")
    result = app.tools.execute("SetTranslationMode", json.dumps({"On": True}))
    assert result.output["ok"] is False
    assert "Helping the learner" not in app.build_instructions()


def test_switching_translation_in_the_window_tells_the_model(tmp_path):
    app = make_app(tmp_path)
    app.backend = FakeBackend()
    app.connected = True

    async def scenario():
        app._set_translation(True)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert len(app.backend.prompts) == 1 and "turned ON" in app.backend.prompts[0]


def test_window_switch_shows_only_for_coaches(tmp_path):
    pytest.importorskip("customtkinter")
    import queue

    from conftest import open_window

    app = make_app(tmp_path, "Default")
    calls = []
    app.set_translation = calls.append
    window = open_window(app, app.memory, queue.Queue(), None, {})
    try:
        window._tick()
        assert window.translate_switch.winfo_manager() == ""  # hidden in Conversación
        app.profile = app.profiles["JapaneseCoach"]
        window._tick()
        assert window.translate_switch.winfo_manager() == "grid" and window.translate_switch.get() == 0
        app.translate_all.add("JapaneseCoach")  # turned on by voice
        window._tick()
        assert window.translate_switch.get() == 1
        window.translate_switch.deselect()
        window.translate_switch.toggle()  # the user clicks it
        assert calls == [True]
    finally:
        window.closing = True
        window.destroy()
        app.memory.close()
