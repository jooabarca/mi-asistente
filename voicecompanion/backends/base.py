"""What every provider backend implements. The app owns audio, tools, memory and reconnects."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..app import VoiceCompanionApp


class Backend:
    name = "?"
    input_rate = 24000  # microphone sample rate the provider expects

    def __init__(self, app: "VoiceCompanionApp"):
        self.app = app

    async def connect(self, first: bool) -> None:
        """Open a connection and configure the session. first=False means a reconnect."""
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError

    async def send_audio(self, pcm: bytes) -> None:
        raise NotImplementedError

    async def clear_input(self) -> None:
        """Microphone was muted: drop any half-finished user speech."""

    async def send_prompt(self, text: str) -> None:
        """Make the model say something now (greeting, profile-switch confirmation)."""
        raise NotImplementedError

    async def receive_loop(self) -> None:
        """Handle server messages until the connection ends (always ends by raising BackendError)."""
        raise NotImplementedError

    async def on_quiet_hours_changed(self) -> None:
        """Quiet hours started or ended mid-session."""

    def forget_session(self) -> None:
        """The next connection must start a brand-new session (new voice or instructions)."""

    async def apply_profile_change(self) -> None:
        """The profile was changed from the window: apply the new instructions (default: reconnect)."""
        self.app.profile_changed = False
        if self.app.connected:
            self.app.request_reconnect()

    async def check(self) -> str:
        """Connect, validate the session settings, disconnect. Returns a success message."""
        raise NotImplementedError
