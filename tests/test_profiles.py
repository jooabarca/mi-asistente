"""Profiles: Spanish names, descriptions, listener (non-conversational) modes, and what the AI is told."""

from __future__ import annotations

import pytest

from voicecompanion.config import find_profile, load_profiles, load_settings
from voicecompanion.memory import MemoryStore


@pytest.fixture
def profiles():
    return load_profiles()


def test_every_profile_has_a_label_and_description(profiles):
    assert {"Default", "EnglishCoach", "FactChecker", "FactInsights"} <= set(profiles)
    for profile in profiles.values():
        assert profile.label and profile.description, profile.name


def test_language_coaches_and_mode_order(profiles):
    assert list(profiles) == ["Default", "EnglishCoach", "SpanishCoach", "PortugueseCoach", "JapaneseCoach",
                              "FactChecker", "FactInsights"]  # conversation, the 4 coaches, then the listeners
    for name, correction in [("EnglishCoach", "Say X, not Y"), ("SpanishCoach", "Di X, no Y"),
                             ("PortugueseCoach", "Diga X, não Y"), ("JapaneseCoach", "見ました")]:
        assert profiles[name].conversational and correction in profiles[name].prompt


def test_listener_modes_are_not_conversational(profiles):
    assert not profiles["FactChecker"].conversational and not profiles["FactInsights"].conversational
    assert profiles["Default"].conversational and profiles["EnglishCoach"].conversational
    assert "No conversa" in profiles["FactChecker"].description
    insights, checker = profiles["FactInsights"].prompt, profiles["FactChecker"].prompt
    assert "Dato:" in insights and "Contrapunto:" in insights  # notes are marked so they're not mistaken for facts
    assert "Contrapunto:" in checker and "Dato:" not in checker  # the checker corrects and counters, no extras
    assert "opinión polémica" in profiles["FactChecker"].description


@pytest.mark.parametrize("spoken,name", [
    ("Verificador de datos", "FactChecker"),
    ("verificador + datos útiles", "FactInsights"),
    ("datos utiles", "FactInsights"),
    ("fact checker plus", "FactInsights"),
    ("Coach de inglés", "EnglishCoach"),
    ("coach de portugués", "PortugueseCoach"),
    ("practicar japonés", "JapaneseCoach"),
    ("learn spanish", "SpanishCoach"),
    ("conversación", "Default"),
])
def test_find_profile_by_label_or_alias(profiles, spoken, name):
    assert find_profile(profiles, spoken).name == name


def _app(tmp_path, profile_name):
    from voicecompanion.app import VoiceCompanionApp

    profiles = load_profiles()
    return VoiceCompanionApp(load_settings(), "key", profiles, profiles[profile_name], MemoryStore(tmp_path / "m.db"))


def test_ai_is_told_about_listener_mode_and_other_profiles(tmp_path):
    listener = _app(tmp_path, "FactInsights").build_instructions()
    assert "Listener mode (not conversational)" in listener
    assert "Verificador de datos" in listener  # other profiles are described, so it can switch by voice
    normal = _app(tmp_path, "Default").build_instructions()
    assert "Listener mode" not in normal
    assert "No conversa" in normal  # it knows what the listener profiles do


def test_choosing_a_voice_saves_it_and_starts_a_fresh_session(tmp_path, monkeypatch):
    import asyncio

    from voicecompanion import config
    from voicecompanion.config import VOICES

    monkeypatch.setattr(config, "PREFERENCES_PATH", tmp_path / "preferences.json")
    assert len(VOICES["gemini"]) == 30 and "Kore" in VOICES["gemini"]
    app = _app(tmp_path, "Default")
    assert app.settings.provider == "gemini"

    async def scenario():
        app._init_async()
        app.connected = True
        app.backend.resume_handle = "old"
        app._select_voice("Sulafat")
        assert app.settings.gemini.voice == "Sulafat"
        assert app.backend.build_config().speech_config.voice_config.prebuilt_voice_config.voice_name == "Sulafat"
        assert app.backend.resume_handle is None  # a voice can't change inside a session
        assert app.reconnect_requested and app.leave_event.is_set()
        assert "hello" in app.pending_prompt  # it greets in the new voice so you can hear it
        app.reconnect_requested = False
        app._select_voice("NotAVoice")  # unknown voices are ignored
        assert app.settings.gemini.voice == "Sulafat" and not app.reconnect_requested

    asyncio.run(scenario())
    assert config.load_preferences() == {"gemini_voice": "Sulafat"}
    assert _app(tmp_path, "Default").settings.gemini.voice == "Sulafat"  # remembered next time


def test_voice_menu_shows_women_then_men():
    from voicecompanion.config import VOICE_GENDERS
    from voicecompanion.gui import voice_labels

    genders = VOICE_GENDERS["gemini"]
    assert list(genders.values()).count("mujer") == 14 and list(genders.values()).count("hombre") == 16
    assert genders["Kore"] == "mujer" and genders["Puck"] == "hombre" and genders["Sulafat"] == "mujer"
    labels = list(voice_labels("gemini"))
    assert labels[0] == "Achernar  ·  mujer, suave" and labels[-1] == "Zubenelgenubi  ·  hombre, casual"
    assert labels.index("Zephyr  ·  mujer, brillante") < labels.index("Achird  ·  hombre, amigable")
    assert "marin  ·  recomendada" in voice_labels("openai")  # no official genders for OpenAI


def test_window_profile_menu_uses_spanish_labels(tmp_path):
    pytest.importorskip("customtkinter")
    import queue

    from conftest import open_window

    app = _app(tmp_path, "Default")
    chosen = []
    app.select_profile = chosen.append
    window = open_window(app, app.memory, queue.Queue(), None, {})
    try:
        window._tick()
        assert window.profile_menu.get() == "Conversación"
        assert "Verificador + datos útiles" in window.profile_by_label
        window._on_profile("Verificador + datos útiles")
        assert chosen == ["FactInsights"]
        app.profile = app.profiles["FactInsights"]  # as the app does after switching
        window._tick()
        assert window.listener_badge.winfo_manager() == "grid"  # "No conversa" badge visible
        assert window.profile_about.cget("text").startswith("No conversa")

        voices = []
        app.select_voice = voices.append
        assert window.voice_menu.get() == f"{app.settings.voice}  ·  mujer, firme"  # Kore by default
        window._on_voice("Sulafat  ·  mujer, cálida")
        assert voices == ["Sulafat"]
    finally:
        window.closing = True
        window.destroy()
        app.memory.close()
