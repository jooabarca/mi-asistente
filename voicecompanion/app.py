"""The conversation loop: microphone -> provider backend -> earbuds, plus tools, memory, sleep and reconnects.

Everything provider-specific (OpenAI or Gemini) lives in voicecompanion/backends/. The app runs on an
asyncio loop; the window (gui.py) or the console talks to it through the thread-safe methods at the end.
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timezone

from . import ui
from .audio import Microphone, Player, looks_like_headphones
from .backends import create_backend
from .config import VOICES, Profile, Settings, find_profile, load_preferences, save_preference
from .errors import BackendError, FatalBackendError, ReconnectNow
from .hotkeys import Hotkeys
from .memory import MemoryStore
from .tools import SLOW_TOOLS, ToolExecutor, ToolResult, build_tool_schemas
from .usage import UsageTracker

MAX_RECONNECT_ATTEMPTS = 8
GREETING_PROMPT = "(Session started. Greet the user in at most six words, in the style of your profile.)"
# Transcription markers such as "<no speech>" or "{pause}" are not words anyone said.
_MARKUP = re.compile(r"<[^<>]*>|\{[^{}]*\}|\[(?:noise|silence|inaudible|music)\]", re.IGNORECASE)
_EMPTY_TRANSCRIPTS = {"", "no speech", "nospeech"}


def clean_transcript(text: str) -> str:
    text = re.sub(r"\s+", " ", _MARKUP.sub(" ", text or "")).strip()
    return "" if text.lower().strip(" .") in _EMPTY_TRANSCRIPTS else text


class VoiceCompanionApp:
    def __init__(
        self,
        settings: Settings,
        api_key: str,
        profiles: dict[str, Profile],
        profile: Profile,
        memory: MemoryStore,
        input_device: int | None = None,
        output_device: int | None = None,
        debug: bool = False,
        console_keys: bool = True,
    ):
        self.settings = settings
        saved_voice = load_preferences().get(f"{settings.provider}_voice")  # chosen in the window
        if saved_voice in VOICES[settings.provider]:
            settings.voice = saved_voice
        self.api_key = api_key
        self.profiles = profiles
        self.profile = profile
        self.memory = memory
        self.debug = debug
        self.console_keys = console_keys
        self.tools = ToolExecutor(
            memory,
            settings.memory.auto_save_categories,
            get_profile_name=lambda: self.profile.name,
            switch_profile=self._switch_profile,
        )
        self.tool_schemas = build_tool_schemas(list(profiles))
        self.usage = UsageTracker(settings.model, settings.pricing)
        self.quiet = settings.quiet_hours.is_active()
        self.history: list[tuple[str, str]] = []
        self.muted = False
        self.sleeping = False
        self.running = False
        self.connected = False
        self.connection_ok = False  # the server accepted the session on the current connection
        self.profile_changed = False  # set by SwitchProfile; the backend applies the new instructions
        self.reconnect_requested = False
        self.pending_prompt: str | None = None  # said right after the next (re)connect
        self.last_activity = time.monotonic()
        self.started_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        self.backend = create_backend(self)
        self.player = Player(output_device)
        self.mic = Microphone(input_device, settings.audio.block_ms, self._on_mic_audio,
                              target_rate=self.backend.input_rate)
        self.echo_guard = settings.audio.echo_guard is True  # "auto" is decided once the speakers are open

    # ================================================================== instructions

    def build_instructions(self, include_history: bool = False) -> str:
        s = self.settings
        parts = [self.profile.prompt]
        if not self.profile.conversational:
            parts.append(
                "## Listener mode (not conversational)\n"
                "You are a background listener, not a conversation partner. Most of what you hear is not "
                "addressed to you. Never greet, chat, react to small talk or ask questions. For every turn where "
                "your rules don't call for a short interjection, call StaySilent and produce no speech."
            )
        if s.max_response_length_hint:
            parts.append(f"Length rule: {s.max_response_length_hint}")
        parts.append(f"Current local date and time: {datetime.now():%A %Y-%m-%d %H:%M}.")

        memory = [
            "## Long-term memory",
            "You have a long-term memory organized in folders, with the SaveMemory and SearchMemory tools. "
            "Your short-term memory of this conversation only lasts a few minutes, so save what matters.",
            "- When the user asks you to remember, save or note something, call SaveMemory with "
            "UserRequested=true and the best folder (Category), then confirm in a few words.",
            "- When the user asks what they saved or what you remember about something, call SearchMemory "
            "and answer from the results.",
        ]
        auto = s.memory.auto_save_categories
        if auto:
            memory.append(
                f"- Without being asked you may call SaveMemory with UserRequested=false, but only for these "
                f"folders: {', '.join(auto)}. Do it silently, without mentioning it."
            )
        else:
            memory.append("- Never save anything unless the user explicitly asks.")
        summary = self.memory.summary(self.profile.name, s.memory.inject_count)
        if summary:
            memory.append("What you already know (from memory):\n" + summary)
        parts.append("\n".join(memory))

        checking = [
            "## Checking facts",
            "Before correcting someone, or when asked about a fact you are not completely sure of (recent events, "
            "who holds a position now, exact numbers or dates), call LookUpFacts to check it on Wikipedia first. "
            "Don't look up things you are certain about, and never mention that you looked something up.",
        ]
        if getattr(self.backend, "google_search", False):
            checking.append("You also have Google Search: use it for very recent information (news, results, prices).")
        parts.append("\n".join(checking))

        others = "\n".join(f"- {p.name} (\"{p.label}\"): {p.description}"
                           for p in self.profiles.values() if p.name != self.profile.name)
        parts.append(
            f"## Profiles\nYour current profile is {self.profile.name} (\"{self.profile.label}\"). "
            "If the user asks to switch (e.g. 'switch to English coach', 'pon el verificador'), call SwitchProfile "
            f"with the best match. Other profiles:\n{others or '- none'}"
        )
        parts.append(
            "## Silence\nIf your rules say you should not respond to a turn, call StaySilent and produce no speech."
        )
        if self.quiet and self.profile.proactive:
            parts.append(
                "## Quiet hours are active\nDo not speak unprompted and do not correct the user. "
                "Only answer direct questions; otherwise call StaySilent."
            )
        if include_history and self.history:
            lines = "\n".join(f"{who}: {text}" for who, text in self.history[-12:])
            parts.append(f"## Conversation so far (the connection was restarted, continue naturally)\n{lines}")
        return "\n\n".join(parts)

    # ================================================================== hooks used by backends

    def on_connected(self) -> None:
        if not self.connection_ok:
            self.connection_ok = True
            ui.info(f"Connected ({self.backend.name} {self.settings.model}, profile {self.profile.name}). "
                    "Start talking.")

    def show_user(self, text: str) -> None:
        text = clean_transcript(text)
        if text:
            self.last_activity = time.monotonic()
            ui.user(text)
            self._remember_turn("User", text)

    def show_assistant(self, text: str, interrupted: bool = False) -> None:
        text = clean_transcript(text)
        if text:
            self.last_activity = time.monotonic()
            ui.assistant(text + (" [interrupted]" if interrupted else ""))
            self._remember_turn("Assistant", text)

    async def run_tool(self, name: str, arguments: str) -> ToolResult:
        if name in SLOW_TOOLS:  # network lookups run on a worker thread so audio and events keep flowing
            result = await asyncio.to_thread(self.tools.execute, name, arguments)
        else:
            result = self.tools.execute(name, arguments)
        if result.log and (self.debug or not result.log.startswith("(")):
            ui.tool(result.log)
        return result

    def quiet_hours_changed(self) -> bool:
        quiet = self.settings.quiet_hours.is_active()
        if quiet == self.quiet:
            return False
        self.quiet = quiet
        ui.info("Quiet hours started." if quiet else "Quiet hours ended.")
        return True

    def _switch_profile(self, wanted: str) -> str | None:
        profile = find_profile(self.profiles, wanted)
        if profile is None:
            return f"Unknown profile '{wanted}'. Available: {', '.join(self.profiles)}"
        if profile.name != self.profile.name:
            self.profile = profile
            self.profile_changed = True
            ui.info(f"Profile switched to {profile.name}.")
        return None

    def _remember_turn(self, who: str, text: str) -> None:
        self.history.append((who, text))
        del self.history[:-50]

    def request_reconnect(self) -> None:
        """Drop the current connection and connect again right away (e.g. new profile on Gemini)."""
        self.reconnect_requested = True
        self.leave_event.set()

    # ================================================================== state for the window

    @property
    def state(self) -> str:
        """One of: starting, sleeping, connecting, speaking, muted, listening, stopped."""
        if not self.running:
            return "stopped" if getattr(self, "loop", None) else "starting"
        if self.sleeping:
            return "sleeping"
        if not self.connection_ok:
            return "connecting"
        if self.player.is_playing:
            return "speaking"
        return "muted" if self.muted else "listening"

    # ================================================================== main loop

    def _init_async(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.stop_event = asyncio.Event()
        self.leave_event = asyncio.Event()  # leave the current connection (sleep, profile change)
        self.wake_event = asyncio.Event()
        self.mic_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=100)

    async def run(self) -> None:
        self._init_async()
        self.player.start()
        self.mic.start()
        ui.info(f"Microphone: {self.mic.description}")
        ui.info(f"Speakers:   {self.player.description}")
        saved_mode = load_preferences().get("echo_guard")  # chosen with the Altavoz / Audífonos switch
        if isinstance(saved_mode, bool):
            self.echo_guard = saved_mode
        elif self.settings.audio.echo_guard == "auto":
            self.echo_guard = not looks_like_headphones(self.player.device_name)
        if self.echo_guard:
            ui.warn("Speaker mode (echo guard ON): the mic is ignored while the AI speaks, so you can't interrupt "
                    "it by voice. With headphones, switch to headset mode.")

        hk = self.settings.hotkeys
        hotkeys = Hotkeys(
            {hk.mute: self.toggle_mute, hk.sleep: self.toggle_sleep, hk.quit: self.stop},
            console_keys={"m": self.toggle_mute, "s": self.toggle_sleep, "q": self.stop} if self.console_keys else None,
        )
        warning = hotkeys.start()
        if warning:
            ui.warn(warning)
        ui.info(f"Hotkeys: {hk.mute} = mute/unmute, {hk.sleep} = sleep/wake, {hk.quit} = quit.")
        self.running = True
        watchdog = asyncio.create_task(self._auto_sleep_watch())
        try:
            await self._connection_loop()
        finally:
            self.running = False
            watchdog.cancel()
            hotkeys.stop()
            self.mic.stop()
            self.player.stop()
            self._finish_session()

    async def _wait_any(self, *events: asyncio.Event, timeout: float | None = None) -> None:
        """Wait until one of the events (or stop) is set, or the timeout passes."""
        tasks = [asyncio.create_task(e.wait()) for e in (*events, self.stop_event)]
        try:
            await asyncio.wait(tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()

    async def _connection_loop(self) -> None:
        attempt = 0
        first = True
        while not self.stop_event.is_set():
            if self.sleeping:
                ui.info("Sleeping: disconnected, nothing is sent. Wake me up to continue.")
                await self._wait_any(self.wake_event)
                self.wake_event.clear()
                self.leave_event.clear()
                if not self.stop_event.is_set():
                    ui.info("Awake.")
                    self.last_activity = time.monotonic()
                continue

            planned: ReconnectNow | None = None
            try:
                if first or attempt:
                    ui.info(f"Connecting to {self.backend.name}..." if first else "Reconnecting...")
                await self.backend.connect(first=first)
                self.connected = True
                try:
                    self._drain_mic_queue()
                    if (first and self.settings.greet_on_start and not self.quiet and not self.profile.proactive
                        and self.profile.conversational):
                        await self.backend.send_prompt(GREETING_PROMPT)
                    if self.pending_prompt:
                        prompt, self.pending_prompt = self.pending_prompt, None
                        await self.backend.send_prompt(prompt)
                    first = False
                    await self._run_connection()
                finally:
                    self.connected = False
                    await self.backend.close()
            except FatalBackendError:
                raise
            except ReconnectNow as exc:
                planned = exc
            except BackendError as exc:
                ui.warn(str(exc))

            healthy, self.connection_ok = self.connection_ok, False
            left_on_purpose = self.sleeping or self.reconnect_requested
            self.reconnect_requested = False
            if self.sleeping or (planned is None and not left_on_purpose) or (planned and planned.interrupt_playback):
                self.player.interrupt()
            if self.stop_event.is_set():
                break
            if planned is not None or left_on_purpose:
                if self.debug and planned is not None:
                    ui.debug(f"Planned reconnect: {planned}")
                attempt = 0
                continue
            attempt = 1 if healthy else attempt + 1
            if attempt > MAX_RECONNECT_ATTEMPTS:
                raise FatalBackendError(f"Gave up after {MAX_RECONNECT_ATTEMPTS} failed reconnection attempts.")
            delay = min(30, 2 ** (attempt - 1))
            ui.warn(f"Connection lost. Retrying in {delay}s (attempt {attempt}/{MAX_RECONNECT_ATTEMPTS})...")
            await self._wait_any(self.leave_event, timeout=delay)  # sleep or stop cut the wait short
            self.leave_event.clear()

    async def _run_connection(self) -> None:
        # leave_event is deliberately not cleared here: a Sleep pressed while connecting must still count.
        tasks = [
            asyncio.create_task(self._send_mic_audio()),
            asyncio.create_task(self.backend.receive_loop()),
            asyncio.create_task(self.stop_event.wait()),
            asyncio.create_task(self.leave_event.wait()),
        ]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.leave_event.clear()
        for task in done:
            if not task.cancelled() and task.exception():
                raise task.exception()

    async def _auto_sleep_watch(self) -> None:
        minutes = self.settings.auto_sleep_minutes
        if not minutes:
            return
        while True:
            await asyncio.sleep(15)
            idle = time.monotonic() - self.last_activity
            if not self.sleeping and self.connection_ok and idle > minutes * 60 and not self.player.is_playing:
                ui.info(f"No conversation for {minutes} min: going to sleep.")
                self._sleep()

    # ================================================================== microphone

    def _on_mic_audio(self, chunk: bytes) -> None:
        """Audio thread -> event loop."""
        try:
            self.loop.call_soon_threadsafe(self._queue_mic_chunk, chunk)
        except RuntimeError:
            pass  # loop already closed during shutdown

    def _queue_mic_chunk(self, chunk: bytes) -> None:
        if self.mic_queue.full():
            self.mic_queue.get_nowait()  # drop the oldest audio rather than fall behind
        self.mic_queue.put_nowait(chunk)

    def _drain_mic_queue(self) -> None:
        while not self.mic_queue.empty():
            self.mic_queue.get_nowait()

    async def _send_mic_audio(self) -> None:
        tail_s = self.settings.audio.echo_tail_ms / 1000
        while True:
            chunk = await self.mic_queue.get()
            if self.muted:
                continue
            if self.echo_guard and self.player.recently_played(tail_s):
                chunk = bytes(len(chunk))  # silence keeps the stream steady for voice detection
            await self.backend.send_audio(chunk)

    # ================================================================== controls (run on the event loop)

    def _toggle_mute(self) -> None:
        self.muted = not self.muted
        ui.info("Microphone MUTED." if self.muted else "Microphone live.")
        if self.muted and self.connected:
            async def clear():
                try:
                    await self.backend.clear_input()  # drop any half-finished sentence
                except BackendError:
                    pass  # the receive loop reports connection problems

            asyncio.ensure_future(clear())

    def _sleep(self) -> None:
        if self.sleeping:
            return
        self.sleeping = True
        self.player.interrupt()
        self.leave_event.set()

    def _wake(self) -> None:
        if not self.sleeping:
            return
        self.sleeping = False
        self.wake_event.set()

    def _select_voice(self, voice: str) -> None:
        """The voice is fixed for a whole session, so a new voice means a new session (recent turns carry over)."""
        if voice not in VOICES[self.settings.provider] or voice == self.settings.voice:
            return
        self.settings.voice = voice
        save_preference(f"{self.settings.provider}_voice", voice)
        ui.info(f"Voice changed to {voice}.")
        self.backend.forget_session()
        self.pending_prompt = ("(The user just changed your voice. Say a short, friendly hello in their language, "
                               "in at most six words, so they can hear how you sound now.)")
        if self.connected:
            self.request_reconnect()
        elif self.sleeping:
            self.pending_prompt = None  # no surprise hello when waking up later

    def _select_profile(self, name: str) -> None:
        error = self._switch_profile(name)
        if error:
            ui.warn(error)
        elif self.profile_changed:
            asyncio.ensure_future(self.backend.apply_profile_change())

    # ================================================================== thread-safe controls (window, hotkeys)

    def _call(self, fn, *args) -> None:
        loop = getattr(self, "loop", None)
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(fn, *args)
            except RuntimeError:
                pass  # loop shutting down

    def toggle_mute(self) -> None:
        self._call(self._toggle_mute)

    def sleep(self) -> None:
        self._call(self._sleep)

    def wake(self) -> None:
        self._call(self._wake)

    def toggle_sleep(self) -> None:
        self._call(self._wake if self.sleeping else self._sleep)

    def select_profile(self, name: str) -> None:
        self._call(self._select_profile, name)

    def select_voice(self, voice: str) -> None:
        self._call(self._select_voice, voice)

    def set_speaker_mode(self, speaker: bool) -> None:
        """Speaker mode = echo guard on (it can't hear itself); headset mode = you can interrupt it.
        A plain bool read by the mic loop, so it can be set from any thread. Remembered for next time."""
        self.echo_guard = speaker
        save_preference("echo_guard", speaker)
        ui.info("Speaker mode: echo guard ON." if speaker else "Headset mode: you can interrupt by voice.")

    def stop(self) -> None:
        self._call(lambda: self.stop_event.set())

    # ================================================================== shutdown, checks

    def _finish_session(self) -> None:
        totals = self.usage.totals
        ui.info(f"Session usage: {self.usage.summary()}")
        if totals.responses:
            try:
                self.memory.log_session(self.profile.name, self.settings.model, self.started_at, totals.responses,
                                        totals.input_tokens, totals.output_tokens, self.usage.cost())
            except Exception as exc:
                ui.warn(f"Could not write the session log: {exc}")

    async def check_connection(self) -> bool:
        """Connect, send the full session config, and report whether the provider accepted it. No audio."""
        ui.info(f"Checking {self.backend.name} with model {self.settings.model}...")
        ui.info(await self.backend.check())
        return True
