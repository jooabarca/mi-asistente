"""Approximate token usage and cost per session. Each backend converts its provider's usage data."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class UsageTotals:
    text_input: int = 0
    cached_text_input: int = 0
    audio_input: int = 0
    cached_audio_input: int = 0
    text_output: int = 0
    audio_output: int = 0
    responses: int = 0

    @property
    def input_tokens(self) -> int:
        return self.text_input + self.cached_text_input + self.audio_input + self.cached_audio_input

    @property
    def output_tokens(self) -> int:
        return self.text_output + self.audio_output


class UsageTracker:
    def __init__(self, model: str, pricing: dict | None):
        self.model = model
        self.prices = (pricing or {}).get(model)
        self.totals = UsageTotals()

    def add(
        self,
        text_input: int = 0,
        cached_text_input: int = 0,
        audio_input: int = 0,
        cached_audio_input: int = 0,
        text_output: int = 0,
        audio_output: int = 0,
    ) -> None:
        """Add one response's (uncached and cached) token counts."""
        t = self.totals
        t.responses += 1
        t.text_input += max(0, text_input)
        t.cached_text_input += max(0, cached_text_input)
        t.audio_input += max(0, audio_input)
        t.cached_audio_input += max(0, cached_audio_input)
        t.text_output += max(0, text_output)
        t.audio_output += max(0, audio_output)

    def cost(self) -> float | None:
        if not self.prices:
            return None
        t, p = self.totals, self.prices
        per_million = (
            t.text_input * p.get("text_input", 0)
            + t.cached_text_input * p.get("cached_text_input", p.get("text_input", 0))
            + t.audio_input * p.get("audio_input", 0)
            + t.cached_audio_input * p.get("cached_audio_input", p.get("audio_input", 0))
            + t.text_output * p.get("text_output", 0)
            + t.audio_output * p.get("audio_output", 0)
        )
        return per_million / 1_000_000

    def summary(self) -> str:
        t = self.totals
        cost = self.cost()
        cost_text = f"~${cost:.4f}" if cost is not None else f"(no price for '{self.model}' in settings.yaml)"
        return (
            f"{t.responses} responses | input {t.input_tokens} tokens "
            f"(audio {t.audio_input}+{t.cached_audio_input} cached, text {t.text_input}+{t.cached_text_input} cached) | "
            f"output {t.output_tokens} tokens (audio {t.audio_output}, text {t.text_output}) | cost {cost_text}"
        )
