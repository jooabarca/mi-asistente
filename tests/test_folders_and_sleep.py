"""Memory folders (categories, dates, migration, deleting), sleep / wake, and a smoke test of the window."""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from voicecompanion.config import CATEGORIES, MEMORY_CATEGORIES, load_profiles, load_settings
from voicecompanion.memory import MemoryStore, normalize_due_at
from voicecompanion.tools import ToolExecutor


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(tmp_path / "test.db")
    yield s
    s.close()


# ---------------------------------------------------------------- folders

def test_folders_have_unique_names_and_labels():
    assert len({c.name for c in CATEGORIES}) == len(CATEGORIES)
    assert len({c.label for c in CATEGORIES}) == len(CATEGORIES)
    assert {"AboutMe", "Agenda", "Learning", "InterestingFact", "Music"} <= set(MEMORY_CATEGORIES)


def test_old_database_is_migrated(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE MemoryItem
        (
              MemoryItemId INTEGER PRIMARY KEY AUTOINCREMENT
            , Category TEXT NOT NULL CHECK (Category IN ('Note', 'Correction', 'Recommendation', 'Opinion', 'Reminder'))
            , Content TEXT NOT NULL
            , Profile TEXT NOT NULL
            , SourceType TEXT NOT NULL
            , CreatedAt TEXT NOT NULL
        );
        CREATE INDEX IX_MemoryItem_CreatedAt ON MemoryItem (CreatedAt);
        INSERT INTO MemoryItem (Category, Content, Profile, SourceType, CreatedAt)
        VALUES ('Reminder', 'Dentist on Friday', 'Default', 'UserRequest', '2026-10-01T10:00:00+00:00');
        """
    )
    conn.commit()
    conn.close()

    store = MemoryStore(path)
    items = store.list_category("Agenda")
    assert [(i.memory_item_id, i.content) for i in items] == [(1, "Dentist on Friday")]
    store.save("Music", "Bohemian Rhapsody - Queen", "Default", "UserRequest")  # new folder allowed now
    assert store.counts() == {"Agenda": 1, "Music": 1}
    store.close()


def test_about_me_is_always_in_the_summary(store):
    store.save("AboutMe", "Se llama Juan", "Default", "UserRequest")
    for n in range(30):
        store.save("Note", f"note {n}", "Default", "UserRequest")
    summary = store.summary("Default", limit=5)
    assert "Se llama Juan" in summary
    assert summary.count("note ") == 5


def test_agenda_sorted_by_date_and_upcoming_in_summary(store):
    store.save("Agenda", "Later", "Default", "UserRequest", "2099-05-02")
    store.save("Agenda", "Sooner", "Default", "UserRequest", "2099-05-01T09:30")
    store.save("Agenda", "Past", "Default", "UserRequest", "2001-01-01")
    store.save("Agenda", "No date", "Default", "UserRequest")
    assert [i.content for i in store.list_category("Agenda")] == ["Past", "Sooner", "Later", "No date"]
    summary = store.summary("Default", limit=5)
    assert "Sooner" in summary and "Past" not in summary


@pytest.mark.parametrize("raw,expected", [
    ("2026-10-17", "2026-10-17"),
    ("2026-10-17T10:00:00Z", "2026-10-17T10:00"),
    ("2026-10-17 18:30", "2026-10-17T18:30"),
    ("next friday", None),
    ("", None),
])
def test_normalize_due_at(raw, expected):
    assert normalize_due_at(raw) == expected


def test_search_understands_folder_names_in_spanish(store):
    store.save("Music", "Bohemian Rhapsody de Queen", "Default", "UserRequest")
    store.save("Note", "Comprar pilas", "Default", "UserRequest")
    store.save("AboutMe", "Vive en Madrid", "Default", "UserRequest")
    assert [i.content for i in store.search("¿qué canciones guardé?")] == ["Bohemian Rhapsody de Queen"]
    assert [i.content for i in store.search("qué sabes sobre mí")] == ["Vive en Madrid"]
    assert [i.content for i in store.search("canción de queen")] == ["Bohemian Rhapsody de Queen"]


def test_filter_and_delete(store):
    first = store.save("Idea", "App para aprender inglés", "Default", "UserRequest")
    store.save("Idea", "Huerto en el balcón", "Default", "UserRequest")
    assert [i.content for i in store.list_category("Idea", "ingles")] == ["App para aprender inglés"]
    version = store.version
    assert store.delete(first.memory_item_id) is True
    assert store.version > version
    assert [i.content for i in store.list_category("Idea")] == ["Huerto en el balcón"]


def test_save_tool_picks_folder_case_insensitively_with_date(store):
    executor = ToolExecutor(store, ["AboutMe"], lambda: "Default", lambda name: None)
    result = executor.execute("SaveMemory", json.dumps({"Category": "agenda", "Content": "Cita médica",
                                                        "UserRequested": True, "DueAt": "2026-11-03T16:00"}))
    assert result.output["saved"] and "Agenda" in result.log
    assert store.list_category("Agenda")[0].due_at == "2026-11-03T16:00"
    auto = executor.execute("SaveMemory", json.dumps({"Category": "AboutMe", "Content": "Le gusta el café",
                                                      "UserRequested": False}))
    assert auto.output["saved"] and not auto.respond
    assert store.list_category("AboutMe")[0].source_type == "Auto"


# ---------------------------------------------------------------- sleep / wake

class FakeBackend:
    name = "Fake"
    input_rate = 16000

    def __init__(self):
        self.connects = 0
        self.closes = 0

    async def connect(self, first):
        self.connects += 1

    async def close(self):
        self.closes += 1

    async def send_prompt(self, text):
        pass

    async def send_audio(self, pcm):
        pass

    async def receive_loop(self):
        await asyncio.Event().wait()  # a quiet, healthy connection


class FakePlayer:
    is_playing = False

    def interrupt(self):
        return None

    def recently_played(self, tail_s):
        return False


def test_sleep_disconnects_and_wake_reconnects(tmp_path):
    from voicecompanion.app import VoiceCompanionApp

    settings = load_settings()
    settings.greet_on_start = False
    profiles = load_profiles()
    app = VoiceCompanionApp(settings, "key", profiles, profiles["Default"], MemoryStore(tmp_path / "m.db"))
    app.backend = FakeBackend()
    app.player = FakePlayer()

    async def scenario():
        app._init_async()
        app.running = True
        loop_task = asyncio.create_task(app._connection_loop())
        await asyncio.sleep(0.05)
        assert app.backend.connects == 1 and app.connected

        app._sleep()
        await asyncio.sleep(0.05)
        assert app.state == "sleeping" and not app.connected and app.backend.closes == 1
        await asyncio.sleep(0.1)
        assert app.backend.connects == 1  # stays disconnected while sleeping

        app._wake()
        await asyncio.sleep(0.05)
        assert app.backend.connects == 2 and app.connected

        app.request_reconnect()  # e.g. profile chosen in the window on Gemini
        await asyncio.sleep(0.05)
        assert app.backend.connects == 3

        app.stop_event.set()
        await asyncio.wait_for(loop_task, 1)

    asyncio.run(scenario())


# ---------------------------------------------------------------- window

def _texts(widget):
    for child in widget.winfo_children():
        try:
            yield child.cget("text")
        except Exception:
            pass
        yield from _texts(child)


def test_window_builds_and_shows_saved_things(tmp_path, monkeypatch):
    pytest.importorskip("customtkinter")
    import queue

    from conftest import open_window

    from voicecompanion import config
    from voicecompanion.app import VoiceCompanionApp
    from voicecompanion.gui import MODE_HEADSET, MODE_SPEAKER, format_date

    monkeypatch.setattr(config, "PREFERENCES_PATH", tmp_path / "preferences.json")
    assert format_date("2026-10-16T10:00", with_weekday=True) == "vie 16 oct 10:00"
    settings = load_settings()
    profiles = load_profiles()
    memory = MemoryStore(tmp_path / "m.db")
    memory.save("Music", "Bohemian Rhapsody - Queen", "Default", "UserRequest")
    app = VoiceCompanionApp(settings, "key", profiles, profiles["Default"], memory)
    window = open_window(app, memory, queue.Queue(), None, {})  # retries rare Tcl startup read errors
    try:
        window.update()
        window._select_folder("Music")
        window.update()
        assert "Bohemian Rhapsody - Queen" in list(_texts(window.items))

        window.events.put(("user", "Hola", "10:00:00"))
        window.events.put(("assistant", "¡Hola! [interrupted]", "10:00:02"))
        window._tick()
        chat = list(_texts(window.chat))
        assert "Hola" in chat and "¡Hola!" in chat and any("interrumpido" in t for t in chat)
        assert window.status_text.cget("text") == "Despertando…"

        window._on_mode(MODE_HEADSET)  # the Altavoz / Audífonos switch
        assert app.echo_guard is False and config.load_preferences() == {"echo_guard": False}
        window._on_mode(MODE_SPEAKER)
        assert app.echo_guard is True and config.load_preferences() == {"echo_guard": True}
    finally:
        window.closing = True
        window.destroy()
        memory.close()
