"""Function-calling tools exposed to the model, and their execution."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable

from . import wikipedia
from .config import CATEGORIES, CATEGORY_BY_NAME, MEMORY_CATEGORIES
from .memory import SOURCE_AUTO, SOURCE_USER_REQUEST, MemoryStore


def build_tool_schemas(profile_names: list[str]) -> list[dict]:
    """Tool definitions in the Realtime API session format (converted for Gemini by its backend)."""
    folders = "; ".join(f"{c.name}: {c.hint}" for c in CATEGORIES)
    return [
        {
            "type": "function",
            "name": "SaveMemory",
            "description": (
                "Save something to the user's long-term memory, in the right folder (Category). Use when the user "
                "asks you to remember something, or (only for allowed auto-save categories) when you notice "
                f"something worth keeping. Folders: {folders}"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "Category": {"type": "string", "enum": list(MEMORY_CATEGORIES)},
                    "Content": {
                        "type": "string",
                        "description": "Self-contained text to save, understandable later without context, "
                                       "in the language the user used.",
                    },
                    "UserRequested": {
                        "type": "boolean",
                        "description": "true if the user explicitly asked to remember/save this.",
                    },
                    "DueAt": {
                        "type": "string",
                        "description": "Agenda only: date 'YYYY-MM-DD' or date and time 'YYYY-MM-DDTHH:MM' "
                                       "(work it out from today's date). Empty if there is no date.",
                    },
                },
                "required": ["Category", "Content", "UserRequested"],
            },
        },
        {
            "type": "function",
            "name": "SearchMemory",
            "description": "Search the user's saved memories, e.g. when they ask 'what did I save about...'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "Query": {"type": "string", "description": "Keywords to look for (can include a category)."},
                },
                "required": ["Query"],
            },
        },
        {
            "type": "function",
            "name": "SwitchProfile",
            "description": "Switch the assistant behavior profile when the user asks (e.g. 'switch to English coach').",
            "parameters": {
                "type": "object",
                "properties": {"ProfileName": {"type": "string", "enum": profile_names}},
                "required": ["ProfileName"],
            },
        },
        {
            "type": "function",
            "name": "StaySilent",
            "description": "Call this instead of speaking when your rules say you should not respond to this turn.",
            "parameters": {"type": "object", "properties": {}},
        },
        {
            "type": "function",
            "name": "LookUpFacts",
            "description": (
                "Look up a fact on Wikipedia before correcting someone or answering about facts you are not sure of "
                "(dates, numbers, people, places, who holds a position now, events). Returns the opening paragraph "
                "of the best matching articles. Use short keyword queries, e.g. 'capital de Australia'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "Query": {"type": "string", "description": "A few keywords, not a full sentence."},
                    "Language": {"type": "string", "description": "Wikipedia language code: 'es' or 'en'."},
                },
                "required": ["Query"],
            },
        },
        {
            "type": "function",
            "name": "SetTranslationMode",
            "description": (
                "Language coach modes only: turn 'translate everything' on or off. When on, you add a short "
                "translation into the learner's own language after each of your sentences. Call it when the "
                "learner accepts your offer or asks for it (or asks to stop)."
            ),
            "parameters": {
                "type": "object",
                "properties": {"On": {"type": "boolean"}},
                "required": ["On"],
            },
        },
    ]


# Tools that wait on the network: the app runs them on a worker thread so audio keeps flowing.
SLOW_TOOLS = {"LookUpFacts"}


@dataclass
class ToolResult:
    output: dict
    respond: bool = True  # ask the model to continue speaking after the tool result
    log: str = ""  # line shown in the console


class ToolExecutor:
    def __init__(
        self,
        memory: MemoryStore,
        auto_save_categories: list[str],
        get_profile_name: Callable[[], str],
        switch_profile: Callable[[str], str | None],
        set_translation: Callable[[bool], str | None] | None = None,
    ):
        self.memory = memory
        self.auto_save_categories = set(auto_save_categories)
        self.get_profile_name = get_profile_name
        self.switch_profile = switch_profile  # returns error text, or None on success
        self.set_translation = set_translation  # same convention

    def execute(self, name: str, arguments: str) -> ToolResult:
        try:
            args = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            return ToolResult({"error": "Arguments were not valid JSON."})
        handler = {
            "SaveMemory": self._save_memory,
            "SearchMemory": self._search_memory,
            "SwitchProfile": self._switch_profile,
            "StaySilent": self._stay_silent,
            "LookUpFacts": self._look_up_facts,
            "SetTranslationMode": self._set_translation_mode,
        }.get(name)
        if handler is None:
            return ToolResult({"error": f"Unknown tool {name}."})
        try:
            return handler(args)
        except Exception as exc:  # report tool failures to the model instead of crashing
            return ToolResult({"error": str(exc)}, log=f"{name} failed: {exc}")

    def _save_memory(self, args: dict) -> ToolResult:
        wanted = str(args.get("Category", "")).strip().lower()
        category = next((c for c in MEMORY_CATEGORIES if c.lower() == wanted), "Note")
        folder = CATEGORY_BY_NAME[category]
        content = str(args.get("Content", ""))
        user_requested = bool(args.get("UserRequested", False))
        if not user_requested and category not in self.auto_save_categories:
            return ToolResult(
                {"saved": False, "reason": f"Auto-saving {category} items is disabled. Do not mention this."},
                respond=False,
                log=f"(no guardado: el guardado automático de {folder.label} está desactivado)",
            )
        source = SOURCE_USER_REQUEST if user_requested else SOURCE_AUTO
        item = self.memory.save(category, content, self.get_profile_name(), source, args.get("DueAt"))
        when = f" (para {item.due_at.replace('T', ' ')})" if item.due_at else ""
        # Auto-saves happen silently; explicit requests get a spoken confirmation.
        return ToolResult(
            {"saved": True, "MemoryItemId": item.memory_item_id, "Folder": folder.label},
            respond=user_requested,
            log=f"Guardado en {folder.icon} {folder.label}{when}: {item.content}",
        )

    def _search_memory(self, args: dict) -> ToolResult:
        query = str(args.get("Query", ""))
        items = self.memory.search(query)
        return ToolResult(
            {
                "results": [item.as_dict() for item in items],
                "note": "No matches." if not items else "Summarize briefly for the user.",
            },
            log=f"Búsqueda «{query}»: {len(items)} resultado(s)",
        )

    def _switch_profile(self, args: dict) -> ToolResult:
        wanted = str(args.get("ProfileName", ""))
        error = self.switch_profile(wanted)
        if error:
            return ToolResult({"switched": False, "error": error})
        return ToolResult(
            {"switched": True, "profile": self.get_profile_name(), "note": "Confirm the switch in a few words."},
            log=f"Perfil cambiado a {self.get_profile_name()}",
        )

    def _look_up_facts(self, args: dict) -> ToolResult:
        query = str(args.get("Query", "")).strip()
        language = str(args.get("Language") or "es").strip().lower()
        if not query:
            return ToolResult({"error": "Query is empty."})
        try:
            results = wikipedia.lookup(query, language)
        except OSError as exc:  # offline, timeout, Wikipedia down
            return ToolResult(
                {"error": f"Wikipedia is not reachable ({exc}). Only correct if you are certain."},
                log=f"🔎 Wikipedia no respondió ({exc})",
            )
        found = ", ".join(r["Title"] for r in results[:2]) or "nada"
        return ToolResult(
            {
                "results": results,
                "note": "No matches: only correct if you are certain." if not results else
                        "Use these to verify; if they don't settle it, stay silent or say you're not sure.",
            },
            log=f"🔎 Wikipedia «{query}» → {found}",
        )

    def _set_translation_mode(self, args: dict) -> ToolResult:
        on = bool(args.get("On", False))
        error = self.set_translation(on) if self.set_translation else "Not available."
        if error:
            return ToolResult({"ok": False, "error": error})
        return ToolResult(
            {"ok": True, "translate_everything": on,
             "note": "From now on add a short translation after each sentence." if on else
                     "Stop adding translations unless they need one."},
            log=f"🔁 Traducir todo: {'activado' if on else 'desactivado'}",
        )

    def _stay_silent(self, args: dict) -> ToolResult:
        return ToolResult({"ok": True}, respond=False, log="(se quedó en silencio)")
