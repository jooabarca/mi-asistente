"""The "Mi Asistente" window: a calm, warm place to see what the assistant is doing and what it saved.

The conversation loop (app.py) runs on its own thread with an asyncio loop. The window only calls the app's
thread-safe controls and polls its state; transcript lines arrive through ui.add_listener -> a queue.
"""

from __future__ import annotations

import asyncio
import queue
import sys
import threading
from datetime import datetime
from pathlib import Path
from tkinter import messagebox

import customtkinter as ctk

from . import ui
from .config import APP_NAME, CATEGORIES, CATEGORY_BY_NAME, VOICES
from .logs import FreezeWatchdog, log
from .memory import SOURCE_AUTO, SOURCE_MANUAL, MemoryItem, MemoryStore, normalize_due_at

ICON_PATH = Path(__file__).with_name("logo.ico")  # window, taskbar and desktop shortcut
LOGO_PATH = Path(__file__).with_name("logo.png")  # avatar in the sidebar

# ---------------------------------------------------------------- warm palette: (light mode, dark mode)
CREAM = ("#FBF6EF", "#211C19")       # window background
SAND = ("#F3E8DB", "#2B2420")        # sidebar, soft panels
CARD = ("#FFFDF9", "#342C27")        # cards and bubbles
BORDER = ("#EADBC8", "#463A33")
TEXT = ("#3B332E", "#F4EBE1")
MUTED = ("#8E8278", "#B3A598")
TERRACOTTA = ("#E07A5F", "#E58A70")
TERRACOTTA_DARK = ("#C9664C", "#D2775E")
SAGE = ("#7DAF97", "#7FAE98")
SAGE_DARK = ("#699C84", "#6C9B85")
LAVENDER = ("#A894D4", "#9D8BCB")
LAVENDER_DARK = ("#9580C5", "#8B78BC")
HONEY = ("#EFB26A", "#E2A65F")
HONEY_DARK = ("#DE9F55", "#D0954F")
ROSE = ("#D9827F", "#D07C79")
ROSE_DARK = ("#C76F6C", "#BD6A67")
PEACH_BUBBLE = ("#FBE1D3", "#5B3D31")
SELECTED = ("#F8DCCF", "#4A3830")
DISABLED = ("#E7DCCF", "#3A312C")
WHITE = ("#FFFFFF", "#FFF8F1")

# state -> (title, hint, tint behind the status card, dot color)
STATES = {
    "starting": ("Despertando…", "Un momento.", SAND, MUTED),
    "connecting": ("Conectando…", "Un momento, ya casi.", ("#FBEBD3", "#3E3226"), HONEY),
    "listening": ("Te escucho", "Habla cuando quieras.", ("#E3F0E8", "#2C3A32"), SAGE),
    "speaking": ("Hablando…", "", ("#FBE3D8", "#43302A"), TERRACOTTA),
    "muted": ("Micrófono en pausa", "No escucho nada hasta que lo actives.", ("#F7E0DF", "#402D2C"), ROSE),
    "sleeping": ("Descansando", "No envío nada. Despiértame cuando quieras.", ("#ECE6F7", "#332D40"), LAVENDER),
    "stopped": ("Apagado", "Hasta luego.", SAND, MUTED),
    "error": ("Algo salió mal", "Mira el mensaje de error.", ("#F7E0DF", "#402D2C"), ROSE),
}
DAYS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
DAYS_LONG = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MONTHS = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
MONTHS_LONG = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre",
               "octubre", "noviembre", "diciembre"]
SOURCES = {"UserRequest": "lo pediste tú", SOURCE_AUTO: "lo guardé yo", SOURCE_MANUAL: "añadido a mano"}
MODE_SPEAKER, MODE_HEADSET = "🔊 Altavoz", "🎧 Audífonos"
MAX_ITEMS_SHOWN = 200
MAX_BUBBLES = 80  # older chat lines are removed; each bubble is several widgets
MAX_LINES_PER_TICK = 10


def format_date(value: str | None, with_weekday: bool = False) -> str:
    """'2026-10-16T10:00' -> 'vie 16 oct 10:00' (Spanish, no locale needed)."""
    if not value:
        return ""
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if moment.tzinfo is not None:
        moment = moment.astimezone()  # stored in UTC, shown in local time
    text = f"{moment.day} {MONTHS[moment.month - 1]}"
    if with_weekday:
        text = f"{DAYS[moment.weekday()]} {text}"
    if moment.year != datetime.now().year:
        text += f" {moment.year}"
    if "T" in value and with_weekday:
        text += f" {moment:%H:%M}"
    return text


def greeting(now: datetime | None = None) -> str:
    hour = (now or datetime.now()).hour
    if 5 <= hour < 12:
        return "Buenos días ☀"
    if 12 <= hour < 20:
        return "Buenas tardes 🌿"
    return "Buenas noches 🌙"


class MainWindow(ctk.CTk):
    def __init__(self, app, memory: MemoryStore, events: "queue.Queue", worker: threading.Thread | None,
                 result: dict, watchdog: FreezeWatchdog | None = None):
        ctk.set_appearance_mode("system")
        super().__init__(fg_color=CREAM)
        self.app = app
        self.memory = memory
        self.events = events
        self.worker = worker
        self.result = result
        self.selected_folder: str | None = None  # None = all folders
        self.shown_version = -1
        self.error_shown = False
        self.closing = False
        self.bubbles: list = []
        self.watchdog = watchdog
        self._shown: dict[str, dict] = {}  # last options given to each widget, see _set()

        self.title(APP_NAME)
        if ICON_PATH.exists():
            self.iconbitmap(str(ICON_PATH))
        self.geometry("1160x800")
        self.minsize(960, 700)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        family = "Segoe UI"
        self.font_hello = ctk.CTkFont(family=family, size=28, weight="bold")
        self.font_title = ctk.CTkFont(family=family, size=22, weight="bold")
        self.font_heading = ctk.CTkFont(family=family, size=20, weight="bold")
        self.font_state = ctk.CTkFont(family=family, size=18, weight="bold")
        self.font_body = ctk.CTkFont(family=family, size=14)
        self.font_button = ctk.CTkFont(family=family, size=14, weight="bold")
        self.font_small = ctk.CTkFont(family=family, size=12)

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._build_sidebar()
        self._build_main()
        self._refresh_folders(force=True)
        self.after(150, self._tick)

    # ================================================================== sidebar

    def _build_sidebar(self) -> None:
        bar = ctk.CTkFrame(self, width=290, corner_radius=0, fg_color=SAND)
        bar.grid(row=0, column=0, sticky="nsew")
        bar.grid_propagate(False)
        bar.grid_columnconfigure(0, weight=1)

        head = ctk.CTkFrame(bar, fg_color=SAND)
        head.grid(row=0, column=0, padx=22, pady=(26, 0), sticky="w")
        avatar = self._avatar_image(52)
        if avatar is not None:
            ctk.CTkLabel(head, image=avatar, text="").grid(row=0, column=0, rowspan=2, padx=(0, 12))
        ctk.CTkLabel(head, text=APP_NAME, font=self.font_title, text_color=TEXT).grid(row=0, column=1, sticky="w")
        ctk.CTkLabel(head, text="tu compañera de voz", font=self.font_small, text_color=MUTED).grid(
            row=1, column=1, sticky="w")

        self.status_card = ctk.CTkFrame(bar, corner_radius=22, fg_color=SAND)
        self.status_card.grid(row=1, column=0, padx=18, pady=(20, 12), sticky="ew")
        self.status_card.grid_columnconfigure(1, weight=1)
        self.status_dot = ctk.CTkLabel(self.status_card, text="●", font=ctk.CTkFont(size=24))
        self.status_dot.grid(row=0, column=0, rowspan=2, padx=(18, 10), pady=14)
        self.status_text = ctk.CTkLabel(self.status_card, text="", font=self.font_state, text_color=TEXT)
        self.status_text.grid(row=0, column=1, pady=(14, 0), sticky="w")
        self.status_hint = ctk.CTkLabel(self.status_card, text="", font=self.font_small, text_color=MUTED,
                                        wraplength=190, justify="left")
        self.status_hint.grid(row=1, column=1, pady=(0, 14), padx=(0, 12), sticky="w")

        button = dict(height=44, corner_radius=22, font=self.font_button, text_color=WHITE,
                      text_color_disabled=MUTED)
        self.mute_button = ctk.CTkButton(bar, command=self.app.toggle_mute, **button)
        self.mute_button.grid(row=2, column=0, padx=18, pady=6, sticky="ew")
        self.sleep_button = ctk.CTkButton(bar, text="🌙  Descansar", command=self.app.sleep, **button)
        self.sleep_button.grid(row=3, column=0, padx=18, pady=6, sticky="ew")
        self.wake_button = ctk.CTkButton(bar, text="☀  Despertar", command=self.app.wake, **button)
        self.wake_button.grid(row=4, column=0, padx=18, pady=6, sticky="ew")

        self._section_label(bar, "¿Cómo me escuchas?", 5)
        self.mode_switch = ctk.CTkSegmentedButton(
            bar, values=[MODE_SPEAKER, MODE_HEADSET], command=self._on_mode, height=38, corner_radius=19,
            font=self.font_body, fg_color=CARD, selected_color=TERRACOTTA, selected_hover_color=TERRACOTTA_DARK,
            unselected_color=CARD, unselected_hover_color=SELECTED, text_color=TEXT)
        self.mode_switch.grid(row=6, column=0, padx=18, pady=(4, 0), sticky="ew")
        self.mode_hint = ctk.CTkLabel(bar, text="", font=self.font_small, text_color=MUTED, wraplength=225,
                                      justify="left")
        self.mode_hint.grid(row=7, column=0, padx=24, pady=(6, 0), sticky="w")

        self._section_label(bar, "Modo", 8)
        self.profile_by_label = {p.label: p.name for p in self.app.profiles.values()}
        self.profile_menu = ctk.CTkOptionMenu(
            bar, values=list(self.profile_by_label), command=self._on_profile, height=38, corner_radius=19,
            font=self.font_body, fg_color=CARD, button_color=SELECTED, button_hover_color=BORDER,
            text_color=TEXT, dropdown_fg_color=CARD, dropdown_hover_color=SELECTED, dropdown_text_color=TEXT,
            dropdown_font=self.font_body)
        self.profile_menu.grid(row=9, column=0, padx=18, pady=(4, 0), sticky="ew")
        self.listener_badge = ctk.CTkLabel(bar, text="👂  No conversa: solo escucha y avisa", font=self.font_small,
                                           text_color=TEXT, fg_color=("#ECE6F7", "#332D40"), corner_radius=12)
        self.translate_switch = ctk.CTkSwitch(
            bar, text="🔁  Traducir todo lo que dice", font=self.font_small, text_color=TEXT,
            progress_color=TERRACOTTA, button_color=WHITE, button_hover_color=CREAM, fg_color=BORDER,
            command=lambda: self.app.set_translation(bool(self.translate_switch.get())))
        self.profile_about = ctk.CTkLabel(bar, text="", font=self.font_small, text_color=MUTED, wraplength=225,
                                          justify="left")
        self.profile_about.grid(row=11, column=0, padx=24, pady=(6, 0), sticky="w")
        self.shown_profile = None

        self._section_label(bar, "Voz", 12)
        voices = VOICES[self.app.settings.provider]
        self.voice_by_label = {(f"{name}  ·  {about}" if about else name): name for name, about in voices.items()}
        self.voice_menu = ctk.CTkOptionMenu(
            bar, values=list(self.voice_by_label), command=self._on_voice, height=38, corner_radius=19,
            font=self.font_body, fg_color=CARD, button_color=SELECTED, button_hover_color=BORDER,
            text_color=TEXT, dropdown_fg_color=CARD, dropdown_hover_color=SELECTED, dropdown_text_color=TEXT,
            dropdown_font=self.font_body)
        self.voice_menu.grid(row=13, column=0, padx=18, pady=(4, 22), sticky="ew")
        bar.grid_rowconfigure(14, weight=1)

    def _voice_label(self, voice: str) -> str:
        return next((label for label, name in self.voice_by_label.items() if name == voice), voice)

    def _on_voice(self, label: str) -> None:
        self.app.select_voice(self.voice_by_label.get(label, label))

    def _on_profile(self, label: str) -> None:
        self.app.select_profile(self.profile_by_label.get(label, label))

    def _update_profile(self) -> None:
        profile = self.app.profile
        translating = profile.name in self.app.translate_all
        if profile.coach and bool(self.translate_switch.get()) != translating:
            (self.translate_switch.select if translating else self.translate_switch.deselect)()  # changed by voice
        if self.shown_profile is profile:
            return
        self.shown_profile = profile
        if self.profile_menu.get() != profile.label:
            self.profile_menu.set(profile.label)
        self.profile_about.configure(text=profile.description)
        if profile.conversational:
            self.listener_badge.grid_remove()
        else:
            self.listener_badge.grid(row=10, column=0, padx=18, pady=(8, 0), sticky="w", ipadx=10, ipady=3)
        if profile.coach:
            self.translate_switch.grid(row=10, column=0, padx=22, pady=(10, 0), sticky="w")
        else:
            self.translate_switch.grid_remove()

    def _section_label(self, parent, text: str, row: int) -> None:
        ctk.CTkLabel(parent, text=text, font=self.font_small, text_color=MUTED).grid(
            row=row, column=0, padx=24, pady=(14, 0), sticky="w")

    def _avatar_image(self, size: int):
        try:
            from PIL import Image

            image = Image.open(LOGO_PATH).convert("RGBA")
            return ctk.CTkImage(light_image=image, dark_image=image, size=(size, size))
        except Exception:
            return None

    # ================================================================== main area

    def _build_main(self) -> None:
        main = ctk.CTkFrame(self, fg_color=CREAM)
        main.grid(row=0, column=1, padx=26, pady=(22, 20), sticky="nsew")
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(1, weight=1)

        head = ctk.CTkFrame(main, fg_color=CREAM)
        head.grid(row=0, column=0, sticky="ew")
        head.grid_columnconfigure(1, weight=1)
        self.hello = ctk.CTkLabel(head, text=greeting(), font=self.font_hello, text_color=TEXT)
        self.hello.grid(row=0, column=0, sticky="w")
        now = datetime.now()
        ctk.CTkLabel(head, text=f"{DAYS_LONG[now.weekday()]} {now.day} de {MONTHS_LONG[now.month - 1]}",
                     font=self.font_body, text_color=MUTED).grid(row=1, column=0, sticky="w")
        self.usage_label = ctk.CTkLabel(head, text="", font=self.font_small, text_color=MUTED, justify="right")
        self.usage_label.grid(row=0, column=1, sticky="e")
        hk = self.app.settings.hotkeys
        ctk.CTkLabel(head, text=f"{hk.mute.title()} pausar micrófono  ·  {hk.sleep.title()} descansar",
                     font=self.font_small, text_color=MUTED).grid(row=1, column=1, sticky="e")

        self.tabs = ctk.CTkTabview(
            main, corner_radius=24, fg_color=CARD, border_width=1, border_color=BORDER,
            segmented_button_fg_color=SAND, segmented_button_selected_color=TERRACOTTA,
            segmented_button_selected_hover_color=TERRACOTTA_DARK, segmented_button_unselected_color=SAND,
            segmented_button_unselected_hover_color=SELECTED, segmented_button_font=self.font_button,
            text_color=TEXT)
        self.tabs.grid(row=1, column=0, pady=(14, 0), sticky="nsew")
        self._build_chat(self.tabs.add("💬  Conversación"))
        self._build_things(self.tabs.add("🗂  Mis cosas"))
        self.tabs.set("💬  Conversación")

    def _scroll_frame(self, parent, **kwargs) -> ctk.CTkScrollableFrame:
        # Explicit colors everywhere: "transparent" inside scrollable frames keeps the old colors
        # when Windows switches between light and dark mode while the app is open.
        return ctk.CTkScrollableFrame(parent, fg_color=CARD, scrollbar_button_color=BORDER,
                                      scrollbar_button_hover_color=MUTED, **kwargs)

    # ================================================================== conversation (chat bubbles)

    def _build_chat(self, tab) -> None:
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(0, weight=1)
        self.chat = self._scroll_frame(tab)
        self.chat.grid(row=0, column=0, padx=6, pady=6, sticky="nsew")
        self.chat.grid_columnconfigure(0, weight=1)
        self.chat_empty = ctk.CTkLabel(self.chat, text="Di «hola» para empezar ✨", font=self.font_body,
                                       text_color=MUTED)
        self.chat_empty.grid(row=0, column=0, pady=60)
        self.chat_row = 1

    def _append_line(self, kind: str, text: str, stamp: str) -> None:
        if kind not in ("user", "assistant", "tool", "warn", "error"):
            return  # connection details stay in the console; the status card shows the state
        if kind == "user" and not self.app.profile.conversational:
            return  # listener modes: in a meeting that would be every sentence everyone says; show only the notes
        if self.chat_empty is not None:
            self.chat_empty.destroy()
            self.chat_empty = None
        interrupted = text.endswith(" [interrupted]")
        text = text.removesuffix(" [interrupted]")
        row = ctk.CTkFrame(self.chat, fg_color=CARD)
        row.grid(row=self.chat_row, column=0, padx=8, pady=5, sticky="ew")
        row.grid_columnconfigure(0, weight=1)
        self.chat_row += 1

        if kind in ("user", "assistant"):
            mine = kind == "user"
            bubble = ctk.CTkFrame(row, corner_radius=20, fg_color=PEACH_BUBBLE if mine else SAND)
            bubble.grid(row=0, column=0, sticky="e" if mine else "w", padx=(80, 0) if mine else (0, 80))
            ctk.CTkLabel(bubble, text=text, font=self.font_body, text_color=TEXT, wraplength=480,
                         justify="left").grid(row=0, column=0, padx=16, pady=(10, 2), sticky="w")
            meta = f"{'Tú' if mine else 'Asistente'} · {stamp[:5]}" + ("  · interrumpido" if interrupted else "")
            ctk.CTkLabel(bubble, text=meta, font=self.font_small, text_color=MUTED).grid(
                row=1, column=0, padx=16, pady=(0, 8), sticky="e" if mine else "w")
        else:
            tint = {"tool": SELECTED, "warn": ("#FBEBD3", "#3E3226"), "error": ("#F7E0DF", "#402D2C")}[kind]
            icon = {"tool": "✓", "warn": "•", "error": "!"}[kind]
            ctk.CTkLabel(row, text=f"{icon}  {text}", font=self.font_small, text_color=TEXT, fg_color=tint,
                         corner_radius=14, wraplength=560, justify="center").grid(row=0, column=0, ipadx=12, ipady=4)

        self.bubbles.append(row)
        while len(self.bubbles) > MAX_BUBBLES:
            self.bubbles.pop(0).destroy()
        self.after(60, self._scroll_chat_to_end)

    def _scroll_chat_to_end(self) -> None:
        try:
            self.chat._parent_canvas.yview_moveto(1.0)  # customtkinter has no public "scroll to end"
        except Exception:
            pass

    # ================================================================== saved things

    def _build_things(self, tab) -> None:
        tab.grid_columnconfigure(1, weight=1)
        tab.grid_rowconfigure(0, weight=1)
        self.folder_list = self._scroll_frame(tab, width=230)
        self.folder_list.grid(row=0, column=0, padx=(4, 14), pady=6, sticky="ns")
        self.folder_list.grid_columnconfigure(0, weight=1)
        self.folder_buttons: dict[str | None, ctk.CTkButton] = {}
        for row, key in enumerate([None] + [c.name for c in CATEGORIES]):
            button = ctk.CTkButton(self.folder_list, text="", anchor="w", height=40, corner_radius=20,
                                   font=self.font_body, fg_color=CARD, text_color=TEXT,
                                   hover_color=SELECTED, command=lambda k=key: self._select_folder(k))
            button.grid(row=row, column=0, padx=2, pady=2, sticky="ew")
            self.folder_buttons[key] = button

        right = ctk.CTkFrame(tab, fg_color=CARD)
        right.grid(row=0, column=1, pady=6, sticky="nsew")
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(3, weight=1)
        self.folder_title = ctk.CTkLabel(right, text="", font=self.font_heading, text_color=TEXT)
        self.folder_title.grid(row=0, column=0, padx=4, sticky="w")
        self.folder_about = ctk.CTkLabel(right, text="", font=self.font_small, text_color=MUTED)
        self.folder_about.grid(row=1, column=0, padx=4, sticky="w")

        entry = dict(height=40, corner_radius=20, font=self.font_body, fg_color=CREAM, border_color=BORDER,
                     text_color=TEXT, placeholder_text_color=MUTED)
        tools = ctk.CTkFrame(right, fg_color=CARD)
        tools.grid(row=2, column=0, pady=(12, 8), sticky="ew")
        tools.grid_columnconfigure(0, weight=1)
        self.search = ctk.CTkEntry(tools, placeholder_text="🔍  Buscar…", **entry)
        self.search.grid(row=0, column=0, columnspan=3, pady=(0, 8), sticky="ew")
        self.search.bind("<KeyRelease>", lambda _e: self._refresh_items())
        self.new_text = ctk.CTkEntry(tools, placeholder_text="Añadir algo…", **entry)
        self.new_text.grid(row=1, column=0, sticky="ew")
        self.new_text.bind("<Return>", lambda _e: self._add_item())
        self.new_date = ctk.CTkEntry(tools, placeholder_text="AAAA-MM-DD HH:MM", width=170, **entry)
        self.add_button = ctk.CTkButton(tools, text="Añadir", width=100, height=40, corner_radius=20,
                                        font=self.font_button, fg_color=TERRACOTTA, hover_color=TERRACOTTA_DARK,
                                        text_color=WHITE, command=self._add_item)
        self.add_button.grid(row=1, column=2, padx=(10, 0))

        self.items = self._scroll_frame(right)
        self.items.grid(row=3, column=0, sticky="nsew")
        self.items.grid_columnconfigure(0, weight=1)
        self._select_folder(None)

    def _select_folder(self, key: str | None) -> None:
        self.selected_folder = key
        for name, button in self.folder_buttons.items():
            button.configure(fg_color=SELECTED if name == key else CARD)
        if key is None:
            self.folder_title.configure(text="🗂  Todo")
            self.folder_about.configure(text="Todo lo que he guardado para ti, lo más nuevo primero.")
            self.new_text.configure(placeholder_text="Elige una carpeta para añadir algo")
        else:
            folder = CATEGORY_BY_NAME[key]
            self.folder_title.configure(text=f"{folder.icon}  {folder.label}")
            self.folder_about.configure(text=folder.about)
            self.new_text.configure(placeholder_text=f"Añadir a {folder.label}…")
        if key == "Agenda":
            self.new_date.grid(row=1, column=1, padx=(10, 0))
        else:
            self.new_date.grid_remove()
        self._refresh_items()

    def _refresh_folders(self, force: bool = False) -> None:
        if not force and self.memory.version == self.shown_version:
            return
        self.shown_version = self.memory.version
        counts = self.memory.counts()
        self.folder_buttons[None].configure(text=f"🗂   Todo   ·  {sum(counts.values())}")
        for c in CATEGORIES:
            count = counts.get(c.name, 0)
            self.folder_buttons[c.name].configure(text=f"{c.icon}   {c.label}" + (f"   ·  {count}" if count else ""))
        if not force:
            self._refresh_items()

    def _refresh_items(self) -> None:
        for child in self.items.winfo_children():
            child.destroy()
        items = self.memory.list_category(self.selected_folder, self.search.get())
        if not items:
            hint = ("No encontré nada con eso." if self.search.get().strip() else
                    "Todavía está vacío 🌱\nDime «recuerda que…» o escríbelo arriba.")
            ctk.CTkLabel(self.items, text=hint, font=self.font_body, text_color=MUTED, justify="center").grid(
                row=0, column=0, pady=50)
            return
        for row, item in enumerate(items[:MAX_ITEMS_SHOWN]):
            self._item_card(item).grid(row=row, column=0, padx=4, pady=5, sticky="ew")

    def _item_card(self, item: MemoryItem) -> ctk.CTkFrame:
        card = ctk.CTkFrame(self.items, corner_radius=18, fg_color=CREAM, border_width=1, border_color=BORDER)
        card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(card, text=item.content, font=self.font_body, text_color=TEXT, wraplength=520,
                     justify="left", anchor="w").grid(row=0, column=0, padx=18, pady=(12, 2), sticky="ew")
        meta = []
        if self.selected_folder is None:
            folder = CATEGORY_BY_NAME.get(item.category)
            meta.append(f"{folder.icon} {folder.label}" if folder else item.category)
        if item.due_at:
            meta.append(f"📅 {format_date(item.due_at, with_weekday=True)}")
        meta.append(f"guardado {format_date(item.created_at)}")
        meta.append(SOURCES.get(item.source_type, item.source_type))
        ctk.CTkLabel(card, text="  ·  ".join(meta), font=self.font_small, text_color=MUTED, anchor="w").grid(
            row=1, column=0, padx=18, pady=(0, 12), sticky="ew")
        ctk.CTkButton(card, text="Borrar", width=72, height=30, corner_radius=15, font=self.font_small,
                      fg_color=CREAM, text_color=ROSE, hover_color=("#F7E0DF", "#402D2C"),
                      command=lambda i=item: self._delete_item(i)).grid(row=0, column=1, rowspan=2, padx=(4, 14))
        return card

    def _delete_item(self, item: MemoryItem) -> None:
        preview = item.content if len(item.content) <= 120 else item.content[:117] + "…"
        if messagebox.askyesno("Borrar", f"¿Borrar esto para siempre?\n\n{preview}", parent=self):
            self.memory.delete(item.memory_item_id)
            self._refresh_folders()

    def _add_item(self) -> None:
        text = self.new_text.get().strip()
        if not text:
            return
        if self.selected_folder is None:
            messagebox.showinfo("Elige una carpeta", "Primero elige en qué carpeta lo guardo.", parent=self)
            return
        due_at = None
        if self.selected_folder == "Agenda" and self.new_date.get().strip():
            due_at = normalize_due_at(self.new_date.get())
            if due_at is None:
                messagebox.showwarning("Fecha", "Escribe la fecha así: 2026-10-17 o 2026-10-17 10:00", parent=self)
                return
        self.memory.save(self.selected_folder, text, self.app.profile.name, SOURCE_MANUAL, due_at)
        self.new_text.delete(0, "end")
        self.new_date.delete(0, "end")
        self._refresh_folders()

    # ================================================================== speaker / headset

    def _on_mode(self, value: str) -> None:
        self.app.set_speaker_mode(value == MODE_SPEAKER)
        self._update_mode()

    def _update_mode(self) -> None:
        speaker = bool(self.app.echo_guard)
        wanted = MODE_SPEAKER if speaker else MODE_HEADSET
        if self.mode_switch.get() != wanted:
            self.mode_switch.set(wanted)
        self._set(self.mode_hint, "mode_hint",
                  text="Mientras hablo no te escucho, para no oírme a mí misma." if speaker
                  else "Puedes interrumpirme cuando quieras.")

    # ================================================================== polling

    def _tick(self) -> None:
        if self.closing:
            return
        try:
            if self.watchdog is not None:
                self.watchdog.beat()
            for _ in range(MAX_LINES_PER_TICK):  # a burst of lines is spread over a few refreshes
                try:
                    self._append_line(*self.events.get_nowait())
                except queue.Empty:
                    break
            self._update_status()
            self._refresh_folders()
        finally:
            self.after(150, self._tick)  # keep refreshing even if something above failed (it is logged)

    def _set(self, widget, key: str, **options) -> None:
        """configure() only when something changed: customtkinter redraws the whole widget on every
        configure(), and doing that ~7 times a second for every button kept the window busy for nothing."""
        if self._shown.get(key) != options:
            self._shown[key] = options
            widget.configure(**options)

    def _paint_button(self, button: ctk.CTkButton, key: str, enabled: bool, color, hover, **extra) -> None:
        self._set(button, key, state="normal" if enabled else "disabled", fg_color=color if enabled else DISABLED,
                  hover_color=hover, **extra)

    def _update_status(self) -> None:
        error = self.result.get("error")
        state = "error" if error else self.app.state
        title, hint, tint, dot = STATES.get(state, STATES["starting"])
        if state == "speaking":
            hint = "Habla y te escucho." if not self.app.echo_guard else "Termino y te escucho."
        self._set(self.status_card, "status_card", fg_color=tint)
        self._set(self.status_text, "status_text", text=title)
        self._set(self.status_hint, "status_hint", text=hint)
        self._set(self.status_dot, "status_dot", text_color=dot)

        running = state not in ("stopped", "error", "starting")
        muted = self.app.muted
        self._paint_button(self.mute_button, "mute", running, ROSE if muted else SAGE,
                           ROSE_DARK if muted else SAGE_DARK,
                           text="🎙  Activar micrófono" if muted else "🔇  Pausar micrófono")
        self._paint_button(self.sleep_button, "sleep", running and not self.app.sleeping, LAVENDER, LAVENDER_DARK)
        self._paint_button(self.wake_button, "wake", running and self.app.sleeping, HONEY, HONEY_DARK)
        self._update_mode()
        self._update_profile()
        voice_label = self._voice_label(self.app.settings.voice)
        if self.voice_menu.get() != voice_label:
            self.voice_menu.set(voice_label)
        self._set(self.hello, "hello", text=greeting())

        responses = self.app.usage.totals.responses
        self._set(self.usage_label, "usage", text=f"Esta sesión: {responses} respuesta{'' if responses == 1 else 's'}")
        if error and not self.error_shown:
            self.error_shown = True
            messagebox.showerror(APP_NAME, str(error), parent=self)

    def report_callback_exception(self, exc_type, exc, tb) -> None:
        """Tk callback errors would vanish without a console: log them instead."""
        log.error("Error in the window", exc_info=(exc_type, exc, tb))

    def _on_close(self) -> None:
        self.closing = True
        self.app.stop()
        self.status_text.configure(text="Hasta luego…")
        self._wait_for_worker(40)

    def _wait_for_worker(self, tries: int) -> None:
        if self.worker is None or not self.worker.is_alive() or tries <= 0:
            self.destroy()
        else:
            self.after(100, lambda: self._wait_for_worker(tries - 1))


def run_gui(app, memory: MemoryStore) -> int:
    """Start the conversation on a background thread and show the window. Returns an exit code."""
    events: queue.Queue = queue.Queue()
    ui.add_listener(lambda kind, text, stamp: events.put((kind, text, stamp)))
    result: dict = {}

    def conversation() -> None:
        try:
            asyncio.run(app.run())
        except Exception as exc:  # shown in the window (FatalBackendError, AudioError, ...)
            result["error"] = exc
            ui.error(str(exc))

    if sys.platform == "win32":  # own taskbar entry and icon, instead of being grouped under Python
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("MiAsistente.VoiceCompanion")
        except Exception:
            pass
    worker = threading.Thread(target=conversation, name="conversation", daemon=True)
    worker.start()
    watchdog = FreezeWatchdog()
    window = MainWindow(app, memory, events, worker, result, watchdog)
    watchdog.start()
    window.mainloop()
    watchdog.stop()
    app.stop()
    worker.join(timeout=5)
    return 1 if result.get("error") else 0
