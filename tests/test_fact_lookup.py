"""Fact checking support: Wikipedia lookups (simulated network), and Google Search that turns itself off
when the Gemini plan doesn't allow it."""

from __future__ import annotations

import asyncio
import io
import json

import pytest

from voicecompanion import wikipedia
from voicecompanion.config import load_profiles, load_settings
from voicecompanion.errors import FatalBackendError
from voicecompanion.memory import MemoryStore
from voicecompanion.tools import ToolExecutor, build_tool_schemas

FAKE_ANSWER = {"query": {"pages": [
    {"index": 2, "title": "Sídney", "extract": "Sídney es la ciudad más poblada de Australia.",
     "fullurl": "https://es.wikipedia.org/wiki/S%C3%ADdney"},
    {"index": 1, "title": "Canberra", "extract": "Canberra es la capital de Australia.",
     "fullurl": "https://es.wikipedia.org/wiki/Canberra"},
    {"index": 3, "title": "Sin resumen"},
]}}


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def fake_wikipedia(monkeypatch):
    requests = []

    def fake_urlopen(request, timeout):
        requests.append(request)
        return FakeResponse(json.dumps(FAKE_ANSWER).encode("utf-8"))

    monkeypatch.setattr(wikipedia.urllib.request, "urlopen", fake_urlopen)
    return requests


def test_lookup_returns_best_matches_first(fake_wikipedia):
    results = wikipedia.lookup("capital de Australia", "es")
    assert [r["Title"] for r in results] == ["Canberra", "Sídney"]  # ordered by relevance, empty ones dropped
    request = fake_wikipedia[0]
    assert request.full_url.startswith("https://es.wikipedia.org/w/api.php?")
    assert "MiAsistente" in request.get_header("User-agent")


def test_lookup_rejects_strange_language_codes(fake_wikipedia):
    wikipedia.lookup("x", "evil.com/")
    assert fake_wikipedia[0].full_url.startswith("https://es.wikipedia.org/")


def _executor(tmp_path):
    return ToolExecutor(MemoryStore(tmp_path / "m.db"), [], lambda: "Default", lambda name: None)


def test_look_up_facts_tool(tmp_path, fake_wikipedia):
    assert "LookUpFacts" in [t["name"] for t in build_tool_schemas(["Default"])]
    result = _executor(tmp_path).execute("LookUpFacts", json.dumps({"Query": "capital de Australia"}))
    assert result.output["results"][0]["Summary"] == "Canberra es la capital de Australia."
    assert result.log == "🔎 Wikipedia «capital de Australia» → Canberra, Sídney"


def test_look_up_facts_offline(tmp_path, monkeypatch):
    def offline(request, timeout):
        raise OSError("no internet")

    monkeypatch.setattr(wikipedia.urllib.request, "urlopen", offline)
    result = _executor(tmp_path).execute("LookUpFacts", json.dumps({"Query": "x"}))
    assert "not reachable" in result.output["error"] and "certain" in result.output["error"]


def _gemini_app(tmp_path, profile="FactChecker"):
    from voicecompanion.app import VoiceCompanionApp

    profiles = load_profiles()
    return VoiceCompanionApp(load_settings(), "key", profiles, profiles[profile], MemoryStore(tmp_path / "m.db"))


def test_slow_lookups_run_off_the_event_loop(tmp_path, fake_wikipedia):
    import threading

    app = _gemini_app(tmp_path)
    threads = []
    original = app.tools.execute

    def spy(name, arguments):
        threads.append(threading.current_thread() is threading.main_thread())
        return original(name, arguments)

    app.tools.execute = spy
    asyncio.run(app.run_tool("LookUpFacts", json.dumps({"Query": "Canberra"})))
    asyncio.run(app.run_tool("StaySilent", "{}"))
    assert threads == [False, True]  # the network lookup ran on a worker thread


def test_google_search_is_requested_and_mentioned_only_when_available(tmp_path):
    app = _gemini_app(tmp_path)
    backend = app.backend
    assert backend.google_search is True  # "auto" tries it
    config = backend.build_config()
    assert any(t.google_search for t in config.tools)
    assert "Google Search" in config.system_instruction and "LookUpFacts" in config.system_instruction
    backend.google_search = False
    config = backend.build_config()
    assert not any(t.google_search for t in config.tools)
    assert "Google Search" not in config.system_instruction and "LookUpFacts" in config.system_instruction


def test_google_search_turned_off_in_settings(tmp_path):
    from voicecompanion.app import VoiceCompanionApp

    settings = load_settings()
    settings.gemini.google_search = False
    profiles = load_profiles()
    app = VoiceCompanionApp(settings, "key", profiles, profiles["Default"], MemoryStore(tmp_path / "m.db"))
    assert app.backend.google_search is False


def test_free_plan_quota_error_falls_back_to_no_search(tmp_path):
    backend = _gemini_app(tmp_path).backend
    attempts = []

    async def fake_connect_once(include_history):
        attempts.append(backend.google_search)
        if backend.google_search:  # what a free key gets when the setup asks for Google Search
            raise FatalBackendError("Gemini usage limit reached (code 1011: You exceeded your current quota)")

    backend._connect_once = fake_connect_once
    asyncio.run(backend._open_session(include_history=False))
    assert attempts == [True, False] and backend.google_search is False


def test_real_quota_problems_are_still_reported(tmp_path):
    backend = _gemini_app(tmp_path).backend
    backend.google_search = False

    async def always_over_quota(include_history):
        raise FatalBackendError("Gemini usage limit reached (code 1011: You exceeded your current quota)")

    backend._connect_once = always_over_quota
    with pytest.raises(FatalBackendError, match="quota"):
        asyncio.run(backend._open_session(include_history=False))
