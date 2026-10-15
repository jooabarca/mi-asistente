"""Feeds scripted server messages into each backend: interruptions, transcripts, tool calls,
profile switches, reconnects and usage. No network, no audio."""

from __future__ import annotations

import asyncio
import base64
import json

import pytest
from google.genai import errors as genai_errors
from google.genai import types

from voicecompanion.app import VoiceCompanionApp
from voicecompanion.backends.gemini_backend import classify_error
from voicecompanion.config import load_profiles, load_settings
from voicecompanion.errors import BackendError, FatalBackendError, ReconnectNow
from voicecompanion.memory import MemoryStore


class FakePlayer:
    def __init__(self):
        self.chunks: list[tuple[str, bytes]] = []
        self.interrupts = 0
        self.recent: list[bool] = []  # scripted answers for recently_played()

    def recently_played(self, tail_s):
        return self.recent.pop(0) if self.recent else False

    def enqueue(self, item_id, pcm):
        self.chunks.append((item_id, pcm))

    def interrupt(self):
        self.interrupts += 1
        if not self.chunks:
            return None
        item_id = self.chunks[0][0]
        self.chunks.clear()
        return item_id, 1234


def make_app(tmp_path, provider):
    settings = load_settings()
    settings.provider = provider
    profiles = load_profiles()
    app = VoiceCompanionApp(settings, "test-key", profiles, profiles["Default"], MemoryStore(tmp_path / "m.db"))
    app.player = FakePlayer()
    return app


def run(coro_fn, *items):
    async def go():
        for item in items:
            await coro_fn(item)
    asyncio.run(go())


# =========================================================================== OpenAI

class FakeOpenAIClient:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, event: dict) -> None:
        self.sent.append(event)

    def types(self) -> list[str]:
        return [e["type"] for e in self.sent]


def openai_app(tmp_path):
    app = make_app(tmp_path, "openai")
    app.backend.client = FakeOpenAIClient()
    return app


AUDIO = base64.b64encode(b"\x00\x00" * 100).decode()


def test_openai_barge_in_cancels_truncates_and_drops_late_audio(tmp_path):
    app = openai_app(tmp_path)
    run(app.backend._handle_event,
        {"type": "response.created", "response": {"id": "r1"}},
        {"type": "response.output_audio.delta", "response_id": "r1", "item_id": "i1", "delta": AUDIO},
        {"type": "input_audio_buffer.speech_started", "audio_start_ms": 10, "item_id": "u1"},
        {"type": "response.output_audio.delta", "response_id": "r1", "item_id": "i1", "delta": AUDIO})
    client = app.backend.client
    assert client.types() == ["response.cancel", "conversation.item.truncate"]
    assert client.sent[1] == {"type": "conversation.item.truncate", "item_id": "i1", "content_index": 0,
                              "audio_end_ms": 1234}
    assert app.player.chunks == []  # late audio from the cancelled response is not played


def test_openai_speech_while_idle_sends_nothing(tmp_path):
    app = openai_app(tmp_path)
    run(app.backend._handle_event, {"type": "input_audio_buffer.speech_started"})
    assert app.backend.client.sent == []


def test_openai_user_requested_save_then_reply_and_usage(tmp_path):
    app = openai_app(tmp_path)
    args = json.dumps({"Category": "Recommendation", "Content": "Read Dune", "UserRequested": True})
    usage = {"input_token_details": {"text_tokens": 1000, "audio_tokens": 2000,
                                     "cached_tokens_details": {"text_tokens": 0, "audio_tokens": 1500}},
             "output_token_details": {"text_tokens": 10, "audio_tokens": 300}}
    run(app.backend._handle_event,
        {"type": "response.created", "response": {"id": "r1"}},
        {"type": "response.function_call_arguments.done", "response_id": "r1", "call_id": "c1",
         "name": "SaveMemory", "arguments": args},
        {"type": "response.done", "response": {"id": "r1", "status": "completed", "usage": usage}})
    assert app.backend.client.types() == ["conversation.item.create", "response.create"]
    assert app.backend.client.sent[0]["item"]["call_id"] == "c1"
    assert app.memory.search("dune")[0].content == "Read Dune"
    t = app.usage.totals
    assert (t.audio_input, t.cached_audio_input, t.text_input, t.audio_output) == (500, 1500, 1000, 300)


def test_openai_stay_silent_does_not_trigger_reply(tmp_path):
    app = openai_app(tmp_path)
    run(app.backend._handle_event,
        {"type": "response.function_call_arguments.done", "response_id": "r2", "call_id": "c2",
         "name": "StaySilent", "arguments": "{}"},
        {"type": "response.done", "response": {"id": "r2", "status": "completed"}})
    assert app.backend.client.types() == ["conversation.item.create"]


def test_openai_switch_profile_updates_instructions_before_reply(tmp_path):
    app = openai_app(tmp_path)
    run(app.backend._handle_event,
        {"type": "response.function_call_arguments.done", "response_id": "r3", "call_id": "c3",
         "name": "SwitchProfile", "arguments": json.dumps({"ProfileName": "EnglishCoach"})},
        {"type": "response.done", "response": {"id": "r3", "status": "completed"}})
    client = app.backend.client
    assert client.types() == ["conversation.item.create", "session.update", "response.create"]
    assert "Spanish speaker" in client.sent[1]["session"]["instructions"]
    assert app.profile.name == "EnglishCoach"


# =========================================================================== Gemini

class FakeGeminiSession:
    def __init__(self):
        self.tool_responses: list = []

    async def send_tool_response(self, *, function_responses):
        self.tool_responses.extend(function_responses)


def gemini_app(tmp_path):
    app = make_app(tmp_path, "gemini")
    app.backend.session = FakeGeminiSession()
    return app


def content(**kwargs) -> types.LiveServerMessage:
    return types.LiveServerMessage(server_content=types.LiveServerContent(**kwargs))


def audio(data: bytes = b"\x01\x00" * 50) -> types.LiveServerMessage:
    return content(model_turn=types.Content(parts=[types.Part(inline_data=types.Blob(data=data,
                                                                                     mime_type="audio/pcm;rate=24000"))]))


def tool_call(name: str, args: dict, call_id: str = "call-1") -> types.LiveServerMessage:
    return types.LiveServerMessage(tool_call=types.LiveServerToolCall(
        function_calls=[types.FunctionCall(id=call_id, name=name, args=args)]))


def test_gemini_transcripts_audio_and_usage(tmp_path, capsys):
    app = gemini_app(tmp_path)
    usage = types.UsageMetadata(
        prompt_token_count=900, response_token_count=400,
        prompt_tokens_details=[types.ModalityTokenCount(modality="AUDIO", token_count=600),
                               types.ModalityTokenCount(modality="TEXT", token_count=300)],
        response_tokens_details=[types.ModalityTokenCount(modality="AUDIO", token_count=400)],
    )
    run(app.backend._handle,
        content(input_transcription=types.Transcription(text="What time")),
        content(input_transcription=types.Transcription(text=" is it?")),
        audio(),
        content(output_transcription=types.Transcription(text="It's ten.")),
        audio(),
        types.LiveServerMessage(usage_metadata=usage),
        content(turn_complete=True))
    out = capsys.readouterr().out
    assert "What time is it?" in out and "It's ten." in out
    assert out.index("What time") < out.index("It's ten.")
    assert [c[0] for c in app.player.chunks] == ["gemini-0", "gemini-0"]
    assert app.history == [("User", "What time is it?"), ("Assistant", "It's ten.")]
    t = app.usage.totals
    assert (t.audio_input, t.text_input, t.audio_output, t.responses) == (600, 300, 400, 1)
    assert app.usage.cost() == pytest.approx((600 * 3 + 300 * 0.75 + 400 * 12) / 1e6)


def test_gemini_interruption_stops_playback(tmp_path, capsys):
    app = gemini_app(tmp_path)
    run(app.backend._handle,
        audio(),
        content(output_transcription=types.Transcription(text="Once upon a time")),
        content(interrupted=True))
    assert app.player.chunks == [] and app.player.interrupts == 1
    assert "Once upon a time [interrupted]" in capsys.readouterr().out
    run(app.backend._handle, audio())
    assert app.player.chunks[0][0] == "gemini-1"  # the next answer is a new item


def test_gemini_save_memory_tool(tmp_path):
    app = gemini_app(tmp_path)
    run(app.backend._handle, tool_call("SaveMemory", {"Category": "Agenda", "Content": "Dentist on Friday",
                                                      "UserRequested": True}))
    response = app.backend.session.tool_responses[0]
    assert response.id == "call-1" and response.name == "SaveMemory" and response.response["saved"] is True
    assert app.memory.search("dentist")[0].content == "Dentist on Friday"


def test_gemini_stay_silent_drops_rest_of_turn(tmp_path):
    app = gemini_app(tmp_path)
    run(app.backend._handle, tool_call("StaySilent", {}), audio(),
        content(output_transcription=types.Transcription(text="Okay.")), content(turn_complete=True))
    assert app.player.chunks == [] and app.history == []
    run(app.backend._handle, audio())  # next turn plays normally
    assert len(app.player.chunks) == 1


def test_gemini_profile_switch_reconnects_fresh(tmp_path):
    app = gemini_app(tmp_path)
    app.backend.resume_handle = "old-handle"
    with pytest.raises(ReconnectNow) as info:
        run(app.backend._handle, tool_call("SwitchProfile", {"ProfileName": "EnglishCoach"}))
    assert info.value.interrupt_playback
    assert app.profile.name == "EnglishCoach" and not app.profile_changed
    assert app.backend.resume_handle is None  # new instructions need a new session
    assert "EnglishCoach" in app.pending_prompt
    assert app.backend.session.tool_responses[0].response["switched"] is True


def test_gemini_go_away_waits_for_end_of_turn(tmp_path):
    app = gemini_app(tmp_path)
    run(app.backend._handle,
        types.LiveServerMessage(session_resumption_update=types.LiveServerSessionResumptionUpdate(
            new_handle="handle-2", resumable=True)),
        audio())
    assert app.backend.resume_handle == "handle-2"
    run(app.backend._handle, types.LiveServerMessage(go_away=types.LiveServerGoAway(time_left="10s")))  # mid-answer
    with pytest.raises(ReconnectNow) as info:
        run(app.backend._handle, content(turn_complete=True))
    assert not info.value.interrupt_playback  # the end of the answer keeps playing
    assert app.backend.resume_handle == "handle-2"


def test_gemini_no_speech_markers_are_not_shown(tmp_path, capsys):
    app = gemini_app(tmp_path)
    run(app.backend._handle,
        content(input_transcription=types.Transcription(text="<no speech>")),
        content(output_transcription=types.Transcription(text="<no speech>{pause}")),
        content(turn_complete=True))
    assert capsys.readouterr().out == "" and app.history == []


# =========================================================================== shared

@pytest.mark.parametrize("raw,clean", [
    ("<no speech>{pause}", ""),
    ("no speech", ""),
    ("Hola {pause} ¿qué tal?", "Hola ¿qué tal?"),
    ("La capital es Tegucigalpa.", "La capital es Tegucigalpa."),
])
def test_clean_transcript(raw, clean):
    from voicecompanion.app import clean_transcript

    assert clean_transcript(raw) == clean


def test_echo_guard_sends_silence_while_ai_speaks(tmp_path):
    app = gemini_app(tmp_path)
    app.echo_guard = True
    app.player.recent = [True, False]  # first chunk while the AI talks, second after
    sent: list[bytes] = []

    async def send_audio(pcm):
        sent.append(pcm)

    app.backend.send_audio = send_audio

    async def go():
        app.mic_queue = asyncio.Queue()
        app.mic_queue.put_nowait(b"\x01\x02" * 4)
        app.mic_queue.put_nowait(b"\x03\x04" * 4)
        task = asyncio.create_task(app._send_mic_audio())
        await asyncio.sleep(0.05)
        task.cancel()

    asyncio.run(go())
    assert sent == [bytes(8), b"\x03\x04" * 4]


@pytest.mark.parametrize("name,headphones", [
    ("Speakers (Realtek(R) Audio)", False),
    ("Headphones (Galaxy Buds2 Pro)", True),
    ("Headset (WH-1000XM5 Hands-Free AG Audio)", True),
    ("Auriculares (Realtek(R) Audio)", True),
    ("Odyssey G50SF (NVIDIA High Definition Audio)", False),
])
def test_headphone_detection(name, headphones):
    from voicecompanion.audio import looks_like_headphones

    assert looks_like_headphones(name) is headphones


def test_gemini_expired_resumption_falls_back_to_a_fresh_session(tmp_path):
    app = gemini_app(tmp_path)
    app.backend.resume_handle = "expired-handle"
    app._remember_turn("User", "Hola")
    opened: list[bool] = []

    async def fake_open_session(include_history):
        opened.append(include_history)
        if len(opened) == 1:  # the resume attempt, as seen in a real log
            raise classify_error(genai_errors.APIError(
                1008, {"message": "BidiGenerateContent session not found or expired"}), "m")

    app.backend._open_session = fake_open_session
    resumed = asyncio.run(app.backend._open(first=False))
    assert resumed is False and opened == [False, True]  # retried fresh, with recent turns
    assert app.backend.resume_handle is None


@pytest.mark.parametrize("reason,fatal", [
    ("API key not valid. Please pass a valid API key.", True),
    ("Quota exceeded for quota metric", True),
    ("models/gemini-9-live is not found for API version v1beta", True),
    ("Internal error encountered.", False),
    ("BidiGenerateContent session not found or expired", False),
])
def test_gemini_error_classification(reason, fatal):
    error = classify_error(genai_errors.APIError(1011 if not fatal else 1008, {"message": reason}), "m")
    assert isinstance(error, FatalBackendError) is fatal
    assert isinstance(error, BackendError)
