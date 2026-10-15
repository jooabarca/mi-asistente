"""OpenAI Realtime API backend (GA WebSocket interface).

Docs: https://developers.openai.com/api/docs/guides/realtime-websocket
"""

from __future__ import annotations

import asyncio
import base64
import json

from .. import ui
from ..audio import SAMPLE_RATE
from ..errors import BackendError, FatalBackendError
from ..realtime_client import RealtimeClient
from .base import Backend

PCM_FORMAT = {"type": "audio/pcm", "rate": SAMPLE_RATE}
# Expected races, not real problems (e.g. cancelling a response the server already cancelled).
QUIET_ERROR_CODES = {"response_cancel_not_active", "conversation_already_has_active_response"}
NOISY_EVENTS = {"response.output_audio.delta", "response.output_audio_transcript.delta",
                "response.function_call_arguments.delta"}


class OpenAIBackend(Backend):
    name = "OpenAI"
    input_rate = SAMPLE_RATE

    def __init__(self, app):
        super().__init__(app)
        self.client: RealtimeClient | None = None
        self._reset()

    def _reset(self) -> None:
        self.active_response_id: str | None = None
        self.ignored_responses: set[str] = set()  # interrupted responses whose late audio we drop
        self.pending_calls: list[dict] = []

    # ------------------------------------------------------------------ session config

    def session_config(self, include_history: bool = False) -> dict:
        s = self.app.settings
        o = s.openai
        td = s.turn_detection
        if td.type == "semantic_vad":
            turn_detection = {"type": "semantic_vad", "eagerness": td.eagerness}
        else:
            turn_detection = {
                "type": "server_vad",
                "threshold": td.threshold,
                "prefix_padding_ms": td.prefix_padding_ms,
                "silence_duration_ms": td.silence_duration_ms,
            }
        turn_detection.update(create_response=True, interrupt_response=True)
        transcription = {"model": o.transcription_model}
        if o.transcription_language:
            transcription["language"] = o.transcription_language
        return {
            "type": "realtime",
            "output_modalities": ["audio"],
            "instructions": self.app.build_instructions(include_history),
            "audio": {
                "input": {
                    "format": PCM_FORMAT,
                    "transcription": transcription,
                    "turn_detection": turn_detection,
                    "noise_reduction": {"type": o.noise_reduction} if o.noise_reduction else None,
                },
                "output": {"format": PCM_FORMAT, "voice": o.voice, "speed": o.speed},
            },
            "tools": self.app.tool_schemas,
            "tool_choice": "auto",
            "max_output_tokens": s.max_output_tokens,
        }

    # ------------------------------------------------------------------ connection

    async def connect(self, first: bool) -> None:
        self._reset()
        o = self.app.settings.openai
        self.client = RealtimeClient(self.app.api_key, o.model, o.realtime_url)
        await self.client.connect()
        await self.client.send({"type": "session.update", "session": self.session_config(include_history=not first)})

    async def close(self) -> None:
        if self.client is not None:
            client, self.client = self.client, None
            await client.close()

    async def send_audio(self, pcm: bytes) -> None:
        await self.client.send({"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm).decode("ascii")})

    async def clear_input(self) -> None:
        await self.client.send({"type": "input_audio_buffer.clear"})

    async def send_prompt(self, text: str) -> None:
        await self.client.send({
            "type": "response.create",
            "response": {"instructions": self.app.build_instructions() + "\n\n" + text},
        })

    async def receive_loop(self) -> None:
        async for event in self.client.events():
            await self._handle_event(event)

    async def on_quiet_hours_changed(self) -> None:
        if self.app.profile.proactive:
            await self._update_instructions()

    async def apply_profile_change(self) -> None:
        self.app.profile_changed = False
        if self.app.connected and self.client is not None:
            try:
                await self._update_instructions()  # OpenAI can change instructions mid-session
            except BackendError:
                pass  # the receive loop reports connection problems

    async def _update_instructions(self) -> None:
        await self.client.send({"type": "session.update",
                                "session": {"type": "realtime", "instructions": self.app.build_instructions()}})

    async def check(self) -> str:
        await self.connect(first=True)
        try:
            async def wait_for_answer() -> str:
                async for event in self.client.events():
                    if event.get("type") == "session.updated":
                        session = event.get("session", {})
                        voice = session.get("audio", {}).get("output", {}).get("voice")
                        return (f"OK: OpenAI accepted the session (model {session.get('model')}, voice {voice}, "
                                f"{len(session.get('tools') or [])} tools).")
                    if event.get("type") == "error":
                        error = event.get("error") or {}
                        raise FatalBackendError(f"OpenAI rejected the session settings: {error.get('message') or error}")
                raise BackendError("Connection closed before the session was confirmed.")

            try:
                return await asyncio.wait_for(wait_for_answer(), 15)
            except asyncio.TimeoutError:
                raise BackendError("No answer from OpenAI within 15 seconds.") from None
        finally:
            await self.close()

    # ------------------------------------------------------------------ server events

    async def _handle_event(self, event: dict) -> None:
        etype = event.get("type", "")
        if self.app.debug and etype not in NOISY_EVENTS:
            ui.debug(etype)

        if etype == "session.updated":
            self.app.on_connected()
        elif etype == "input_audio_buffer.speech_started":
            await self._on_speech_started()
        elif etype == "input_audio_buffer.speech_stopped":
            if self.app.quiet_hours_changed():
                await self.on_quiet_hours_changed()
        elif etype == "conversation.item.input_audio_transcription.completed":
            self.app.show_user(event.get("transcript") or "")
        elif etype == "conversation.item.input_audio_transcription.failed":
            if self.app.debug:
                ui.debug(f"Transcription failed: {event.get('error')}")
        elif etype == "response.created":
            self.active_response_id = event.get("response", {}).get("id")
        elif etype == "response.output_audio.delta":
            if event.get("response_id") not in self.ignored_responses:
                self.app.player.enqueue(event["item_id"], base64.b64decode(event["delta"]))
        elif etype == "response.output_audio_transcript.done":
            self.app.show_assistant(event.get("transcript") or "",
                                    interrupted=event.get("response_id") in self.ignored_responses)
        elif etype == "response.function_call_arguments.done":
            self.pending_calls.append(event)
        elif etype == "response.done":
            await self._on_response_done(event.get("response") or {})
        elif etype == "error":
            self._on_error(event.get("error") or {})

    async def _on_speech_started(self) -> None:
        """The user started talking: stop playback right away and cancel/truncate the answer."""
        cut = self.app.player.interrupt()
        if self.active_response_id:
            self.ignored_responses.add(self.active_response_id)
            await self.client.send({"type": "response.cancel"})
        if cut:
            item_id, played_ms = cut
            # Tell the server how much was actually heard, so its memory of the reply matches ours.
            await self.client.send({
                "type": "conversation.item.truncate",
                "item_id": item_id,
                "content_index": 0,
                "audio_end_ms": played_ms,
            })
            if self.app.debug:
                ui.debug(f"Interrupted after {played_ms} ms")

    async def _on_response_done(self, response: dict) -> None:
        response_id = response.get("id")
        status = response.get("status")
        if response_id == self.active_response_id:
            self.active_response_id = None
        self._add_usage(response.get("usage"))
        if status == "failed":
            ui.warn(f"Response failed: {json.dumps(response.get('status_details'))}")

        calls = [c for c in self.pending_calls if c.get("response_id") == response_id]
        self.pending_calls = [c for c in self.pending_calls if c.get("response_id") != response_id]
        if not calls:
            return
        needs_reply = False
        for call in calls:
            result = await self.app.run_tool(call.get("name", ""), call.get("arguments", "{}"))
            await self.client.send({
                "type": "conversation.item.create",
                "item": {"type": "function_call_output", "call_id": call["call_id"],
                         "output": json.dumps(result.output, ensure_ascii=False)},
            })
            needs_reply = needs_reply or result.respond
        if self.app.profile_changed:  # SwitchProfile ran: apply the new rules before the reply
            self.app.profile_changed = False
            await self._update_instructions()
        if needs_reply and status != "cancelled":
            await self.client.send({"type": "response.create"})

    def _add_usage(self, usage: dict | None) -> None:
        if not usage:
            return
        inp = usage.get("input_token_details") or {}
        out = usage.get("output_token_details") or {}
        cached = inp.get("cached_tokens_details") or {}
        cached_text = cached.get("text_tokens", 0) or 0
        cached_audio = cached.get("audio_tokens", 0) or 0
        if not cached and inp.get("cached_tokens"):
            cached_text = inp["cached_tokens"]  # no breakdown: assume text (conservative for cost)
        self.app.usage.add(
            text_input=(inp.get("text_tokens", 0) or 0) - cached_text,
            cached_text_input=cached_text,
            audio_input=(inp.get("audio_tokens", 0) or 0) - cached_audio,
            cached_audio_input=cached_audio,
            text_output=out.get("text_tokens", 0) or 0,
            audio_output=out.get("audio_tokens", 0) or 0,
        )
        if self.app.debug:
            ui.debug(f"Usage so far: {self.app.usage.summary()}")

    def _on_error(self, error: dict) -> None:
        code = error.get("code") or ""
        if code in QUIET_ERROR_CODES:
            if self.app.debug:
                ui.debug(f"(ignored) {code}: {error.get('message')}")
            return
        ui.error(f"OpenAI: {error.get('message') or error} [{code or error.get('type')}]")
