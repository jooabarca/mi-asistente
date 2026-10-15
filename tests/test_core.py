"""Offline tests: settings, profiles, memory, tools, usage and session config. No API calls, no audio."""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from voicecompanion.config import (
    ConfigError, QuietHoursSettings, find_profile, load_profiles, load_settings, parse_profile,
)
from voicecompanion.memory import MemoryStore
from voicecompanion.tools import ToolExecutor, build_tool_schemas
from voicecompanion.usage import UsageTracker


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(tmp_path / "test.db")
    yield s
    s.close()


# ---------------------------------------------------------------- config / profiles

def test_settings_file_loads():
    settings = load_settings()
    assert settings.model
    assert settings.gemini.voice and settings.openai.voice
    assert settings.turn_detection.type in ("server_vad", "semantic_vad")


def test_unknown_setting_is_rejected(tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text("modle: oops\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="modle"):
        load_settings(path)


def test_too_small_context_is_rejected(tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text("gemini:\n  max_context_tokens: 500\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="max_context_tokens"):
        load_settings(path)


def test_bad_auto_save_category_is_rejected(tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text("memory:\n  auto_save_categories: [Gossip]\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="Gossip"):
        load_settings(path)


def test_starter_profiles_exist_and_match_aliases():
    profiles = load_profiles()
    assert {"Default", "EnglishCoach", "FactChecker"} <= set(profiles)
    assert find_profile(profiles, "english coach").name == "EnglishCoach"
    assert find_profile(profiles, "Fact-Checker").name == "FactChecker"
    assert find_profile(profiles, "pirate") is None


def test_profile_without_front_matter_uses_file_name():
    profile = parse_profile("Just be nice.", "Nice")
    assert profile.name == "Nice" and profile.prompt == "Just be nice."


@pytest.mark.parametrize("hour,expected", [(23, True), (3, True), (7, False), (12, False)])
def test_quiet_hours_wrap_midnight(hour, expected):
    quiet = QuietHoursSettings(enabled=True, start="22:00", end="07:00")
    assert quiet.is_active(datetime(2026, 1, 1, hour, 0)) is expected


# ---------------------------------------------------------------- memory

def test_save_and_search(store):
    store.save("Recommendation", "Watch the movie Arrival", "Default", "UserRequest")
    store.save("Correction", "Say 'I am 30 years old', not 'I have 30 years'", "EnglishCoach", "Auto")
    assert [i.content for i in store.search("what did I save about movies arrival")] == ["Watch the movie Arrival"]
    assert store.search("corrections")[0].category == "Correction"
    assert store.search("zebra") == []


def test_recent_prefers_current_profile(store):
    store.save("Note", "coach note", "EnglishCoach", "UserRequest")
    store.save("Note", "default note", "Default", "UserRequest")
    assert store.recent(1, "EnglishCoach")[0].content == "coach note"
    assert "coach note" in store.summary("EnglishCoach", 5)


def test_invalid_category_rejected(store):
    with pytest.raises(ValueError):
        store.save("Gossip", "x", "Default", "UserRequest")


def test_session_log(store):
    store.log_session("Default", "gpt-realtime-2.1", "2026-01-01T00:00:00+00:00", 3, 1000, 500, 0.05)
    row = store.conn.execute("SELECT ResponseCount, EstimatedCostUsd FROM SessionLog").fetchone()
    assert row == (3, 0.05)


# ---------------------------------------------------------------- tools

def _executor(store, auto=("Correction",)):
    state = {"profile": "Default"}

    def switch(name):
        if name not in ("Default", "EnglishCoach"):
            return "unknown"
        state["profile"] = name
        return None

    return ToolExecutor(store, list(auto), lambda: state["profile"], switch), state


def test_tool_schemas_are_valid():
    names = [t["name"] for t in build_tool_schemas(["Default", "EnglishCoach"])]
    assert names == ["SaveMemory", "SearchMemory", "SwitchProfile", "StaySilent", "LookUpFacts"]


def test_save_memory_respects_auto_save_setting(store):
    executor, _ = _executor(store, auto=("Correction",))
    asked = executor.execute("SaveMemory", json.dumps({"Category": "Opinion", "Content": "Likes jazz", "UserRequested": True}))
    assert asked.output["saved"] and asked.respond
    auto_ok = executor.execute("SaveMemory", json.dumps({"Category": "Correction", "Content": "x not y", "UserRequested": False}))
    assert auto_ok.output["saved"] and not auto_ok.respond  # silent
    auto_blocked = executor.execute("SaveMemory", json.dumps({"Category": "Opinion", "Content": "meh", "UserRequested": False}))
    assert auto_blocked.output["saved"] is False
    assert store.count() == 2
    sources = {r[0] for r in store.conn.execute("SELECT SourceType FROM MemoryItem")}
    assert sources == {"UserRequest", "Auto"}


def test_search_and_switch_tools(store):
    executor, state = _executor(store)
    store.save("Agenda", "Call the dentist on Friday", "Default", "UserRequest")
    result = executor.execute("SearchMemory", json.dumps({"Query": "dentist"}))
    assert result.output["results"][0]["Content"] == "Call the dentist on Friday"
    assert executor.execute("SwitchProfile", json.dumps({"ProfileName": "EnglishCoach"})).output["switched"]
    assert state["profile"] == "EnglishCoach"
    assert executor.execute("StaySilent", "{}").respond is False
    assert "error" in executor.execute("SaveMemory", "not json").output


# ---------------------------------------------------------------- usage

def test_usage_cost():
    tracker = UsageTracker("m", {"m": {"audio_input": 32, "cached_audio_input": 0.4, "text_input": 4,
                                       "text_output": 24, "audio_output": 64}})
    tracker.add(text_input=1000, audio_input=1000, cached_audio_input=1000, text_output=100, audio_output=500)
    t = tracker.totals
    assert (t.audio_input, t.cached_audio_input, t.text_input, t.responses) == (1000, 1000, 1000, 1)
    expected = (1000 * 4 + 1000 * 0.4 + 1000 * 32 + 100 * 24 + 500 * 64) / 1_000_000
    assert tracker.cost() == pytest.approx(expected)
    assert UsageTracker("unknown", {}).cost() is None


def test_every_default_model_has_a_price():
    settings = load_settings()
    assert settings.gemini.model in settings.pricing
    assert settings.openai.model in settings.pricing


# ---------------------------------------------------------------- provider settings

def test_provider_switch_and_model_override():
    settings = load_settings()
    assert settings.provider == "gemini" and settings.model == settings.gemini.model
    settings.provider = "openai"
    settings.model = "gpt-realtime-2.1-mini"
    assert settings.openai.model == "gpt-realtime-2.1-mini" and settings.gemini.model == "gemini-3.8-live"


def test_api_key_is_per_provider(monkeypatch, tmp_path):
    from voicecompanion import config

    monkeypatch.setattr(config, "ROOT", tmp_path)  # no .env there
    monkeypatch.setenv("GEMINI_API_KEY", "real-gemini-key")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-your-key-here")  # placeholder
    assert config.load_api_key("gemini") == "real-gemini-key"
    with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
        config.load_api_key("openai")


def _app(store, provider):
    from voicecompanion.app import VoiceCompanionApp

    settings = load_settings()
    settings.provider = provider
    profiles = load_profiles()
    return VoiceCompanionApp(settings, "test-key", profiles, profiles["Default"], store), settings


def test_openai_session_config_shape(store):
    store.save("Note", "My cat is called Pixel", "Default", "UserRequest")
    app, settings = _app(store, "openai")
    assert app.mic.target_rate == 24000
    session = app.backend.session_config()
    assert session["type"] == "realtime"
    assert session["audio"]["input"]["format"] == {"type": "audio/pcm", "rate": 24000}
    assert session["audio"]["input"]["turn_detection"]["interrupt_response"] is True
    assert session["audio"]["output"]["voice"] == settings.openai.voice
    assert "Pixel" in session["instructions"]
    assert {t["name"] for t in session["tools"]} >= {"SaveMemory", "SearchMemory"}

    assert app._switch_profile("english coach") is None
    assert app.profile.name == "EnglishCoach" and app.profile_changed
    assert "Say X, not Y" in app.build_instructions()


def test_gemini_config_is_accepted_by_the_sdk(store):
    from google.genai import types

    store.save("Note", "My cat is called Pixel", "Default", "UserRequest")
    app, settings = _app(store, "gemini")
    settings.turn_detection.start_sensitivity = "low"
    assert app.mic.target_rate == 16000
    config = app.backend.build_config()  # pydantic validates every field here
    assert config.response_modalities == [types.Modality.AUDIO]
    assert "Pixel" in config.system_instruction
    assert config.speech_config.voice_config.prebuilt_voice_config.voice_name == settings.gemini.voice
    names = [fd.name for fd in config.tools[0].function_declarations]
    assert names == ["SaveMemory", "SearchMemory", "SwitchProfile", "StaySilent", "LookUpFacts"]
    assert config.tools[1].google_search is not None  # requested; dropped automatically if the plan refuses it
    save = config.tools[0].function_declarations[0]
    assert save.parameters.type == types.Type.OBJECT
    assert save.parameters.properties["Category"].enum[0] == "AboutMe"
    assert config.tools[0].function_declarations[3].parameters is None
    aad = config.realtime_input_config.automatic_activity_detection
    assert aad.start_of_speech_sensitivity == types.StartSensitivity.START_SENSITIVITY_LOW
    assert config.context_window_compression.trigger_tokens == settings.gemini.max_context_tokens
