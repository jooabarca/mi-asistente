"""Provider backends: each one speaks one live voice API and reports back to the app."""

from __future__ import annotations

from .base import Backend


def create_backend(app) -> Backend:
    provider = app.settings.provider
    if provider == "gemini":
        from .gemini_backend import GeminiBackend

        return GeminiBackend(app)
    if provider == "openai":
        from .openai_backend import OpenAIBackend

        return OpenAIBackend(app)
    raise ValueError(f"Unknown provider {provider!r}")
