"""Google Gemini Live API backend, using the official google-genai SDK.

Docs: https://ai.google.dev/gemini-api/docs/live  (protocol: https://ai.google.dev/api/live)
Audio: 16 kHz PCM16 in, 24 kHz PCM16 out. A connection lasts ~10 minutes; the server warns
with GoAway and we resume the same conversation on a new connection with a resumption handle.
"""

from __future__ import annotations

import json

import pydantic
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from websockets.exceptions import ConnectionClosed

from .. import ui
from ..errors import BackendError, FatalBackendError, ReconnectNow
from .base import Backend

INPUT_RATE = 16000
INPUT_MIME = f"audio/pcm;rate={INPUT_RATE}"


def _schema(js: dict) -> types.Schema:
    """JSON-schema subset (as used in tools.py) -> Gemini Schema."""
    kwargs: dict = {"type": js["type"].upper()}
    if "description" in js:
        kwargs["description"] = js["description"]
    if "enum" in js:
        kwargs["enum"] = list(js["enum"])
    if js.get("properties"):
        kwargs["properties"] = {name: _schema(prop) for name, prop in js["properties"].items()}
    if js.get("required"):
        kwargs["required"] = list(js["required"])
    return types.Schema(**kwargs)


def function_declarations(tool_schemas: list[dict]) -> list[types.FunctionDeclaration]:
    declarations = []
    for tool in tool_schemas:
        params = tool.get("parameters") or {}
        declarations.append(types.FunctionDeclaration(
            name=tool["name"],
            description=tool["description"],
            parameters=_schema(params) if params.get("properties") else None,
        ))
    return declarations


def _sensitivity(enum_cls, prefix: str, value):
    return None if value is None else enum_cls(f"{prefix}_SENSITIVITY_{str(value).upper()}")


def classify_error(exc: Exception, model: str) -> BackendError:
    """Turn SDK / websocket errors into 'retry' or 'stop with a clear message'."""
    if isinstance(exc, BackendError):
        return exc
    if isinstance(exc, genai_errors.APIError):
        code, text = exc.code, str(exc.message or exc.details or "")
    elif isinstance(exc, ConnectionClosed):
        frame = exc.rcvd
        code, text = (frame.code, frame.reason) if frame else (1006, "network dropped")
    elif isinstance(exc, (OSError, TimeoutError)):
        return BackendError(f"Can't reach Google ({exc}). Check your internet connection.")
    else:
        return BackendError(f"Gemini connection problem: {exc}")

    low = text.lower()
    detail = f"(code {code}: {text})" if text else f"(code {code})"
    if any(k in low for k in ("api key", "api_key", "unauthenticated", "permission")):
        return FatalBackendError(f"Google rejected the API key. Check GEMINI_API_KEY in .env. {detail}")
    if any(k in low for k in ("quota", "resource_exhausted", "rate limit")):
        return FatalBackendError(
            f"Gemini usage limit reached (free-tier limits, or billing needed). Try later or check "
            f"https://aistudio.google.com. {detail}"
        )
    if "session" in low and ("not found" in low or "expired" in low):
        return BackendError(f"The previous Gemini session expired. {detail}")  # resumption failed: start fresh
    if "model" in low and ("not found" in low or "not supported" in low):
        return FatalBackendError(f"Gemini model '{model}' was not found or doesn't support the Live API. {detail}")
    if code in (1007, 1008):
        return FatalBackendError(f"Gemini rejected the session settings. {detail}")
    return BackendError(f"Gemini connection closed {detail}.")


class GeminiBackend(Backend):
    name = "Gemini"
    input_rate = INPUT_RATE

    def __init__(self, app):
        super().__init__(app)
        self.client = genai.Client(api_key=app.api_key)
        self.resume_handle: str | None = None  # survives reconnects: same conversation continues
        self.session = None
        self._session_cm = None
        self.turn_index = 0
        self._reset_turn()
        self._rotate = False
        self._last_usage: types.UsageMetadata | None = None
        # Google Search grounding needs a paid (billing-enabled) key. Tried on each app start; if Google
        # refuses it, the rest of the run continues without it (Wikipedia lookups still work).
        self.google_search = app.settings.gemini.google_search is not False

    def _reset_turn(self) -> None:
        self._user_text: list[str] = []
        self._ai_text: list[str] = []
        self._in_turn = False  # the model is producing output
        self._silenced = False  # StaySilent was called: drop the rest of this turn's audio
        self._searched = False  # a Google search was already shown for this turn

    @property
    def model(self) -> str:
        return self.app.settings.gemini.model

    # ------------------------------------------------------------------ session config

    def build_config(self, include_history: bool = False) -> types.LiveConnectConfig:
        g = self.app.settings.gemini
        td = self.app.settings.turn_detection
        try:
            return types.LiveConnectConfig(
                response_modalities=[types.Modality.AUDIO],
                system_instruction=self.app.build_instructions(include_history),
                speech_config=types.SpeechConfig(
                    voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=g.voice))
                ),
                tools=[types.Tool(function_declarations=function_declarations(self.app.tool_schemas))]
                + ([types.Tool(google_search=types.GoogleSearch())] if self.google_search else []),
                # Only the allowed languages, so noise isn't transcribed as, say, Chinese.
                input_audio_transcription=types.AudioTranscriptionConfig(
                    language_codes=list(self.app.settings.languages)),
                output_audio_transcription=types.AudioTranscriptionConfig(),
                realtime_input_config=types.RealtimeInputConfig(
                    automatic_activity_detection=types.AutomaticActivityDetection(
                        prefix_padding_ms=td.prefix_padding_ms,
                        silence_duration_ms=td.silence_duration_ms,
                        start_of_speech_sensitivity=_sensitivity(types.StartSensitivity, "START", td.start_sensitivity),
                        end_of_speech_sensitivity=_sensitivity(types.EndSensitivity, "END", td.end_sensitivity),
                    )
                ),
                context_window_compression=types.ContextWindowCompressionConfig(
                    trigger_tokens=g.max_context_tokens,
                    sliding_window=types.SlidingWindow(target_tokens=g.max_context_tokens // 2),
                ),
                session_resumption=types.SessionResumptionConfig(handle=self.resume_handle),
            )
        except (pydantic.ValidationError, ValueError) as exc:
            raise FatalBackendError(f"Invalid Gemini settings in settings.yaml: {exc}") from exc

    # ------------------------------------------------------------------ connection

    async def connect(self, first: bool) -> None:
        resuming = await self._open(first)
        if self.app.debug and resuming:
            ui.debug("Resumed the previous Gemini session.")
        self.app.on_connected()

    async def _open(self, first: bool) -> bool:
        """Open the session; returns True if it resumed the previous conversation."""
        self._reset_turn()
        self._rotate = False
        if self.resume_handle and not first:
            try:
                await self._open_session(include_history=False)
                return True
            except FatalBackendError:
                raise
            except BackendError as exc:
                # The old session expired or can't be resumed: start a fresh one with recent turns instead.
                if self.app.debug:
                    ui.debug(f"Could not resume ({exc}); starting a fresh session.")
        self.resume_handle = None
        await self._open_session(include_history=not first)
        return False

    async def _open_session(self, include_history: bool) -> None:
        try:
            await self._connect_once(include_history)
        except FatalBackendError as exc:
            if not (self.google_search and "quota" in str(exc).lower()):
                raise
            # Free keys can't use Google Search in the Live API: carry on without it.
            self.google_search = False
            ui.info("Google Search isn't available with this Gemini plan; checking facts with Wikipedia instead.")
            await self._connect_once(include_history)

    async def _connect_once(self, include_history: bool) -> None:
        config = self.build_config(include_history=include_history)
        self._session_cm = self.client.aio.live.connect(model=self.model, config=config)
        try:
            self.session = await self._session_cm.__aenter__()  # returns once the server accepted the setup
        except Exception as exc:
            self._session_cm = None
            raise classify_error(exc, self.model) from exc

    async def close(self) -> None:
        self._flush_turn(interrupted=self._in_turn)
        self._add_usage()
        cm, self._session_cm, self.session = self._session_cm, None, None
        if cm is not None:
            try:
                await cm.__aexit__(None, None, None)
            except Exception:
                pass

    async def _send(self, coro) -> None:
        try:
            await coro
        except (ConnectionClosed, genai_errors.APIError, OSError) as exc:
            raise classify_error(exc, self.model) from exc

    async def send_audio(self, pcm: bytes) -> None:
        await self._send(self.session.send_realtime_input(audio=types.Blob(data=pcm, mime_type=INPUT_MIME)))

    async def clear_input(self) -> None:
        await self._send(self.session.send_realtime_input(audio_stream_end=True))

    async def send_prompt(self, text: str) -> None:
        content = types.Content(role="user", parts=[types.Part(text=text)])
        await self._send(self.session.send_client_content(turns=content, turn_complete=True))

    async def receive_loop(self) -> None:
        try:
            while True:  # session.receive() ends after each model turn
                async for message in self.session.receive():
                    await self._handle(message)
        except (ConnectionClosed, genai_errors.APIError, OSError) as exc:
            raise classify_error(exc, self.model) from exc

    def forget_session(self) -> None:
        self.resume_handle = None

    async def apply_profile_change(self) -> None:
        self.forget_session()  # new instructions need a fresh session
        await super().apply_profile_change()

    async def check(self) -> str:
        await self._open(first=True)
        await self.close()
        g = self.app.settings.gemini
        return f"OK: Gemini accepted the session (model {g.model}, voice {g.voice}, {len(self.app.tool_schemas)} tools)."

    # ------------------------------------------------------------------ server messages

    async def _handle(self, msg: types.LiveServerMessage) -> None:
        if self.app.debug:
            fields = [k for k, v in msg if v is not None]
            if fields != ["server_content"] or not (msg.server_content.model_turn):
                ui.debug(f"gemini: {', '.join(fields)}")

        if msg.usage_metadata is not None:
            self._last_usage = msg.usage_metadata
        update = msg.session_resumption_update
        if update is not None and update.resumable and update.new_handle:
            self.resume_handle = update.new_handle
        if msg.go_away is not None:
            # The server will close this connection soon. Reconnect between turns so no speech is cut.
            self._rotate = True
            if self.app.debug:
                ui.debug(f"GoAway received (time left {msg.go_away.time_left}).")
            if not self._in_turn:
                raise ReconnectNow("server is rotating the connection")

        content = msg.server_content
        if content is not None:
            await self._handle_content(content)
        if msg.tool_call is not None:
            await self._handle_tool_call(msg.tool_call)

    async def _handle_content(self, content: types.LiveServerContent) -> None:
        if content.input_transcription is not None and content.input_transcription.text:
            self._user_text.append(content.input_transcription.text)

        if content.model_turn is not None:
            for part in content.model_turn.parts or []:
                if part.thought:
                    continue
                blob = part.inline_data
                if blob is not None and blob.data:
                    self._start_model_output()
                    if not self._silenced:
                        self.app.player.enqueue(f"gemini-{self.turn_index}", blob.data)

        grounding = content.grounding_metadata
        if grounding is not None and grounding.web_search_queries and not self._searched:
            self._searched = True  # show each turn's Google searches once in the conversation
            ui.tool(f"🔎 Google «{', '.join(grounding.web_search_queries[:3])}»")

        if content.output_transcription is not None and content.output_transcription.text:
            self._start_model_output()
            self._ai_text.append(content.output_transcription.text)

        if content.interrupted:
            # The user talked over the model: the server already stopped generating; stop playback.
            self.app.player.interrupt()
            self._end_turn(interrupted=True)
        elif content.turn_complete:
            self._end_turn(interrupted=False)
            if self.app.quiet_hours_changed():
                await self.on_quiet_hours_changed()  # applied on the next reconnect for Gemini

        if self._rotate and not self._in_turn:
            raise ReconnectNow("server is rotating the connection")

    def _start_model_output(self) -> None:
        if not self._in_turn:
            self._in_turn = True
            self._flush_user()  # print what the user said before the answer

    def _flush_user(self) -> None:
        if self._user_text:
            self.app.show_user("".join(self._user_text))
            self._user_text = []

    def _flush_turn(self, interrupted: bool) -> None:
        self._flush_user()
        if self._ai_text and not self._silenced:
            self.app.show_assistant("".join(self._ai_text), interrupted=interrupted)
        self._ai_text = []

    def _end_turn(self, interrupted: bool) -> None:
        self._flush_turn(interrupted)
        self._add_usage()
        self.turn_index += 1
        self._in_turn = False
        self._silenced = False

    async def _handle_tool_call(self, tool_call: types.LiveServerToolCall) -> None:
        responses = []
        for call in tool_call.function_calls or []:
            result = await self.app.run_tool(call.name or "", json.dumps(call.args or {}))
            if call.name == "StaySilent":
                self._silenced = True
            responses.append(types.FunctionResponse(id=call.id, name=call.name, response=result.output))
        if responses:
            await self._send(self.session.send_tool_response(function_responses=responses))
        if self.app.profile_changed:
            # Gemini can't change instructions mid-session: start a fresh session with the new
            # profile (recent turns are carried over in the instructions) and confirm the switch.
            self.app.profile_changed = False
            self.resume_handle = None
            self.app.pending_prompt = (f"(The user just switched you to the {self.app.profile.name} profile. "
                                       "Confirm the switch in a few words.)")
            raise ReconnectNow("profile switch", interrupt_playback=True)

    def _add_usage(self) -> None:
        usage, self._last_usage = self._last_usage, None
        if usage is None:
            return
        prompt = {d.modality: d.token_count or 0 for d in usage.prompt_tokens_details or []}
        cached = {d.modality: d.token_count or 0 for d in usage.cache_tokens_details or []}
        response = {d.modality: d.token_count or 0 for d in usage.response_tokens_details or []}
        audio, text = types.MediaModality.AUDIO, types.MediaModality.TEXT
        prompt_audio = prompt.get(audio, 0)
        prompt_text = sum(v for k, v in prompt.items() if k != audio)
        if not prompt and usage.prompt_token_count:
            prompt_text = usage.prompt_token_count  # no breakdown given
        response_audio = response.get(audio, 0)
        response_text = sum(v for k, v in response.items() if k != audio)
        if not response and usage.response_token_count:
            response_audio = usage.response_token_count  # this app only asks for audio output
        cached_audio = cached.get(audio, 0)
        cached_text = sum(v for k, v in cached.items() if k != audio)
        self.app.usage.add(
            text_input=prompt_text - cached_text,
            cached_text_input=cached_text,
            audio_input=prompt_audio - cached_audio,
            cached_audio_input=cached_audio,
            text_output=response_text + (usage.thoughts_token_count or 0),
            audio_output=response_audio,
        )
        if self.app.debug:
            ui.debug(f"Usage so far: {self.app.usage.summary()}")
