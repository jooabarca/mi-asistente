"""Settings, .env loading, profiles and quiet hours."""

from __future__ import annotations

import json
import os
import re
import unicodedata
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
APP_NAME = "Mi Asistente"


@dataclass(frozen=True)
class Category:
    name: str  # stored in memory.db and used by the model
    label: str  # folder name shown in the app
    icon: str
    hint: str  # tells the model what belongs here
    about: str  # shown under the folder name in the app


# The "folders" of long-term memory, in the order the app shows them.
CATEGORIES = (
    Category("AboutMe", "Sobre mí", "👤",
             "Personal facts about the user: name, family, work, where they live, likes, routines. Always remembered.",
             "Tu nombre, familia, trabajo y gustos. El asistente siempre lo tiene presente."),
    Category("Agenda", "Agenda", "📅", "Appointments, plans, reminders and to-dos. Set DueAt when there is a date.",
             "Citas, planes, recordatorios y pendientes, ordenados por fecha."),
    Category("Learning", "Aprendizaje", "📚", "Things the user is learning: concepts, words, explanations worth keeping.",
             "Conceptos, palabras y explicaciones que quieres repasar."),
    Category("InterestingFact", "Datos curiosos", "💡", "Interesting facts or trivia the user liked.",
             "Datos interesantes que te gustaron."),
    Category("Music", "Canciones", "🎵", "Songs, artists and albums the user mentioned or wants to listen to.",
             "Canciones, artistas y álbumes para escuchar."),
    Category("Recommendation", "Recomendaciones", "⭐",
             "Books, movies, series, podcasts, places or products recommended.",
             "Libros, películas, series, podcasts y lugares recomendados."),
    Category("Idea", "Ideas", "🧠", "The user's own ideas and projects to think about later.",
             "Tus ideas y proyectos para pensar más tarde."),
    Category("Shopping", "Compras", "🛒", "Things to buy.", "Cosas por comprar."),
    Category("Correction", "Correcciones", "✏",
             "Language mistakes the user made while practicing (English, Spanish, Portuguese, Japanese…), "
             "as 'Say X, not Y', starting with the language.",
             "Tus errores de idiomas corregidos (inglés, español, portugués, japonés…), para no repetirlos."),
    Category("Opinion", "Opiniones", "💬", "The user's opinions and preferences on topics.",
             "Lo que piensas sobre distintos temas."),
    Category("Note", "Notas", "📝", "Anything else worth keeping.", "Todo lo demás."),
)
CATEGORY_BY_NAME = {c.name: c for c in CATEGORIES}
MEMORY_CATEGORIES = tuple(c.name for c in CATEGORIES)
VALID_VOICES = ("alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse", "marin", "cedar")

# Voices per provider -> short description shown in the app (Gemini's own descriptors, in Spanish).
# Gemini: https://ai.google.dev/gemini-api/docs/speech-generation#voices (Live models accept the same voices)
VOICES = {
    "gemini": {
        "Achernar": "suave", "Achird": "amigable", "Algenib": "rasposa", "Algieba": "tersa", "Alnilam": "firme",
        "Aoede": "ligera", "Autonoe": "brillante", "Callirrhoe": "tranquila", "Charon": "informativa",
        "Despina": "tersa", "Enceladus": "susurrante", "Erinome": "clara", "Fenrir": "entusiasta",
        "Gacrux": "madura", "Iapetus": "clara", "Kore": "firme", "Laomedeia": "alegre", "Leda": "juvenil",
        "Orus": "firme", "Puck": "alegre", "Pulcherrima": "directa", "Rasalgethi": "informativa",
        "Sadachbia": "vivaz", "Sadaltager": "sabia", "Schedar": "serena", "Sulafat": "cálida",
        "Umbriel": "relajada", "Vindemiatrix": "gentil", "Zephyr": "brillante", "Zubenelgenubi": "casual",
    },
    "openai": {"marin": "recomendada", "cedar": "recomendada", "alloy": "", "ash": "", "ballad": "", "coral": "",
               "echo": "", "sage": "", "shimmer": "", "verse": ""},
}
# Language codes allowed in settings.yaml `languages`, with the name the AI is told.
LANGUAGE_NAMES = {
    "es": "Spanish", "en": "English", "pt": "Portuguese", "ja": "Japanese", "fr": "French", "it": "Italian",
    "de": "German", "zh": "Chinese", "ko": "Korean", "ru": "Russian", "ar": "Arabic", "hi": "Hindi", "nl": "Dutch",
}

# Female / male as Google lists them for these voices: https://docs.cloud.google.com/text-to-speech/docs/chirp3-hd
# (OpenAI doesn't publish genders for its voices, so they are shown without one.)
_GEMINI_WOMEN = {"Achernar", "Aoede", "Autonoe", "Callirrhoe", "Despina", "Erinome", "Gacrux", "Kore", "Laomedeia",
                 "Leda", "Pulcherrima", "Sulafat", "Vindemiatrix", "Zephyr"}
VOICE_GENDERS = {
    "gemini": {name: ("mujer" if name in _GEMINI_WOMEN else "hombre") for name in VOICES["gemini"]},
    "openai": {},
}


class ConfigError(Exception):
    """Raised for invalid settings or profiles, with a message meant for the user."""


# --------------------------------------------------------------------------- settings


@dataclass
class AudioSettings:
    input_device: Any = None  # None = Windows default, int = index, str = part of the device name
    output_device: Any = None
    block_ms: int = 40  # size of each microphone chunk sent to the API
    # Mic sends silence while the AI speaks, so speakers can't make it hear itself.
    # "auto" = on unless the output device looks like headphones. YAML on/off = true/false.
    echo_guard: Any = "auto"
    echo_tail_ms: int = 400  # keep guarding this long after playback stops


@dataclass
class TurnDetectionSettings:
    prefix_padding_ms: int = 300
    silence_duration_ms: int = 500
    # OpenAI only
    type: str = "server_vad"  # "server_vad" or "semantic_vad"
    threshold: float = 0.5
    eagerness: str = "auto"  # only used by semantic_vad
    # Gemini only: "low", "high" or None (server default)
    start_sensitivity: Any = None
    end_sensitivity: Any = None


@dataclass
class OpenAISettings:
    model: str = "gpt-realtime-2.1"
    voice: str = "marin"
    speed: float = 1.0
    transcription_model: str = "gpt-4o-mini-transcribe"
    transcription_language: Any = None
    noise_reduction: Any = "near_field"
    realtime_url: str = "wss://api.openai.com/v1/realtime"


@dataclass
class GeminiSettings:
    model: str = "gemini-3.8-live"
    voice: str = "Kore"
    # Older conversation is dropped beyond this size: bounds cost per reply and lets
    # sessions run longer than Gemini's 15-minute limit for uncompressed audio sessions.
    max_context_tokens: int = 8000
    # Google Search grounding: "auto" tries it and quietly turns it off if the key's plan doesn't allow it
    # (free keys don't); false never asks for it. Wikipedia lookups work either way.
    google_search: Any = "auto"


@dataclass
class HotkeySettings:
    mute: str = "ctrl+shift+m"
    quit: str = "ctrl+shift+x"
    sleep: str = "ctrl+shift+f9"  # toggles sleep / wake


@dataclass
class QuietHoursSettings:
    enabled: bool = False
    start: str = "22:00"
    end: str = "07:00"

    def is_active(self, now: datetime | None = None) -> bool:
        if not self.enabled:
            return False
        current = (now or datetime.now()).time()
        start, end = _parse_hhmm(self.start), _parse_hhmm(self.end)
        if start <= end:
            return start <= current < end
        return current >= start or current < end  # wraps past midnight


@dataclass
class MemorySettings:
    db_path: str = "memory.db"
    auto_save_categories: list = field(default_factory=lambda: ["AboutMe", "Correction", "Recommendation"])
    inject_count: int = 10


PROVIDERS = ("gemini", "openai")


@dataclass
class Settings:
    provider: str = "gemini"
    active_profile: str = "Default"
    max_response_length_hint: str = ""
    max_output_tokens: Any = 400
    greet_on_start: bool = True
    auto_sleep_minutes: int = 0  # 0 = never; otherwise go to sleep after this long without conversation
    coach_nudge_seconds: int = 8  # coach modes: offer help after this long without an answer; 0 = never
    # The only languages the assistant hears and speaks (codes from LANGUAGE_NAMES).
    languages: list = field(default_factory=lambda: ["es", "en", "pt", "ja"])
    openai: OpenAISettings = field(default_factory=OpenAISettings)
    gemini: GeminiSettings = field(default_factory=GeminiSettings)
    audio: AudioSettings = field(default_factory=AudioSettings)
    turn_detection: TurnDetectionSettings = field(default_factory=TurnDetectionSettings)
    hotkeys: HotkeySettings = field(default_factory=HotkeySettings)
    quiet_hours: QuietHoursSettings = field(default_factory=QuietHoursSettings)
    memory: MemorySettings = field(default_factory=MemorySettings)
    pricing: dict = field(default_factory=dict)

    @property
    def model(self) -> str:
        return self.gemini.model if self.provider == "gemini" else self.openai.model

    @model.setter
    def model(self, value: str) -> None:
        if self.provider == "gemini":
            self.gemini.model = value
        else:
            self.openai.model = value

    @property
    def voice(self) -> str:
        return self.gemini.voice if self.provider == "gemini" else self.openai.voice

    @voice.setter
    def voice(self, value: str) -> None:
        if self.provider == "gemini":
            self.gemini.voice = value
        else:
            self.openai.voice = value

    @property
    def db_path(self) -> Path:
        path = Path(self.memory.db_path)
        return path if path.is_absolute() else ROOT / path


def _parse_hhmm(value: str) -> time:
    try:
        return datetime.strptime(str(value).strip(), "%H:%M").time()
    except ValueError as exc:
        raise ConfigError(f"Quiet hours time '{value}' must look like HH:MM (e.g. 22:30).") from exc


def _build(cls, data: dict, where: str):
    """Build a (possibly nested) dataclass from a dict, rejecting unknown keys."""
    if data is None:
        return cls()
    if not isinstance(data, dict):
        raise ConfigError(f"'{where}' in settings.yaml must be a mapping.")
    known = {f.name: f for f in fields(cls)}
    unknown = set(data) - set(known)
    if unknown:
        raise ConfigError(f"Unknown setting(s) in '{where}': {', '.join(sorted(unknown))}")
    kwargs = {}
    for name, value in data.items():
        default = known[name].default_factory() if callable(known[name].default_factory) else None
        if is_dataclass(default):
            kwargs[name] = _build(type(default), value, f"{where}.{name}" if where != "settings" else name)
        else:
            kwargs[name] = value
    return cls(**kwargs)


def load_settings(path: Path | None = None) -> Settings:
    path = path or ROOT / "settings.yaml"
    if not path.exists():
        raise ConfigError(f"Settings file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"settings.yaml is not valid YAML: {exc}") from exc
    settings = _build(Settings, data, "settings")
    validate_settings(settings)
    return settings


def validate_settings(settings: Settings) -> None:
    if settings.provider not in PROVIDERS:
        raise ConfigError(f"provider must be one of: {', '.join(PROVIDERS)}")
    if settings.openai.voice not in VALID_VOICES:
        raise ConfigError(
            f"openai.voice '{settings.openai.voice}' is not valid. Choose one of: {', '.join(VALID_VOICES)}"
        )
    if not (isinstance(settings.gemini.max_context_tokens, int) and settings.gemini.max_context_tokens >= 2000):
        raise ConfigError("gemini.max_context_tokens must be a whole number of at least 2000.")
    unknown = [code for code in settings.languages if code not in LANGUAGE_NAMES]
    if not settings.languages or unknown:
        raise ConfigError(f"languages must be a list of codes from: {', '.join(LANGUAGE_NAMES)}"
                          + (f" (unknown: {', '.join(map(str, unknown))})" if unknown else ""))
    if settings.gemini.google_search not in (True, False, "auto"):
        raise ConfigError("gemini.google_search must be auto or off.")
    if settings.audio.echo_guard not in (True, False, "auto"):
        raise ConfigError("audio.echo_guard must be auto, on or off.")
    td = settings.turn_detection
    for name in ("start_sensitivity", "end_sensitivity"):
        if getattr(td, name) not in (None, "low", "high"):
            raise ConfigError(f"turn_detection.{name} must be low, high or null.")
    bad = [c for c in settings.memory.auto_save_categories if c not in MEMORY_CATEGORIES]
    if bad:
        raise ConfigError(f"memory.auto_save_categories has unknown categories {bad}. Valid: {list(MEMORY_CATEGORIES)}")
    if settings.turn_detection.type not in ("server_vad", "semantic_vad"):
        raise ConfigError("turn_detection.type must be 'server_vad' or 'semantic_vad'.")
    if settings.openai.noise_reduction not in (None, "near_field", "far_field"):
        raise ConfigError("openai.noise_reduction must be near_field, far_field or null.")
    mot = settings.max_output_tokens
    if not (mot == "inf" or (isinstance(mot, int) and 1 <= mot <= 4096)):
        raise ConfigError("max_output_tokens must be a number from 1 to 4096, or \"inf\".")
    _parse_hhmm(settings.quiet_hours.start)
    _parse_hhmm(settings.quiet_hours.end)


API_KEY_NAMES = {"openai": ("OPENAI_API_KEY",), "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY")}


PREFERENCES_PATH = ROOT / "preferences.json"


def load_preferences() -> dict:
    """Choices made in the window (e.g. speaker/headset mode), remembered between runs."""
    try:
        data = json.loads(PREFERENCES_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_preference(key: str, value) -> None:
    prefs = load_preferences()
    prefs[key] = value
    try:
        PREFERENCES_PATH.write_text(json.dumps(prefs, indent=2), encoding="utf-8")
    except OSError:
        pass  # not being able to remember a preference is never worth crashing over


def load_api_key(provider: str) -> str:
    load_dotenv(ROOT / ".env")
    names = API_KEY_NAMES[provider]
    for name in names:
        key = os.environ.get(name, "").strip()
        if key and "your" not in key.lower():  # skip the .env.example placeholders
            return key
    raise ConfigError(
        f"{names[0]} is missing (provider '{provider}'). Copy .env.example to .env and put your key in it."
    )


# --------------------------------------------------------------------------- profiles


@dataclass
class Profile:
    name: str
    prompt: str
    aliases: list = field(default_factory=list)
    description: str = ""
    proactive: bool = False  # speaks up without being asked (affected by quiet hours)
    label: str = ""  # name shown in the app, e.g. "Verificador de datos"
    conversational: bool = True  # False = background listener: only interjects, never chats
    order: int = 50  # position in the app's mode list (lower first)
    coach: bool = False  # language coach: offers help when you go quiet, and can translate everything
    path: Path | None = None


_FRONT_MATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


def parse_profile(text: str, fallback_name: str, path: Path | None = None) -> Profile:
    meta: dict = {}
    body = text
    match = _FRONT_MATTER.match(text.lstrip("\ufeff"))
    if match:
        try:
            meta = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"Profile {path or fallback_name}: front matter is not valid YAML: {exc}") from exc
        body = match.group(2)
    prompt = body.strip()
    if not prompt:
        raise ConfigError(f"Profile {path or fallback_name} has an empty system prompt.")
    name = str(meta.get("name") or fallback_name)
    return Profile(
        name=name,
        prompt=prompt,
        aliases=[str(a) for a in meta.get("aliases") or []],
        description=str(meta.get("description") or ""),
        proactive=bool(meta.get("proactive", False)),
        label=str(meta.get("label") or name),
        conversational=bool(meta.get("conversational", True)),
        order=int(meta.get("order", 50)),
        coach=bool(meta.get("coach", False)),
        path=path,
    )


def load_profiles(directory: Path | None = None) -> dict[str, Profile]:
    directory = directory or ROOT / "profiles"
    loaded = [parse_profile(path.read_text(encoding="utf-8"), path.stem, path)
              for path in sorted(directory.glob("*.md"))]
    if not loaded:
        raise ConfigError(f"No profiles found in {directory}. Add at least one .md file.")
    return {p.name: p for p in sorted(loaded, key=lambda p: (p.order, p.name))}


def _normalize(text: str) -> str:
    """Lowercase, no accents, no spaces or punctuation: 'Datos útiles' -> 'datosutiles'."""
    text = "".join(c for c in unicodedata.normalize("NFD", text.lower()) if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]", "", text)


def find_profile(profiles: dict[str, Profile], wanted: str) -> Profile | None:
    """Match by name, label or alias, ignoring case, accents, spaces and punctuation
    ("english coach" -> EnglishCoach, "verificador de datos" -> FactChecker)."""
    key = _normalize(wanted)
    for profile in profiles.values():
        names = [profile.name, profile.label, *profile.aliases]
        if key in (_normalize(n) for n in names):
            return profile
    return None
