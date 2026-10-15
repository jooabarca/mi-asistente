"""Mi Asistente (VoiceCompanion): live voice conversations with Gemini Live or the OpenAI Realtime API.

Usage:
    python main.py                        open the Mi Asistente window (provider and profile from settings.yaml)
    python main.py --console              run in this terminal without the window
    python main.py --provider openai      use the other provider for this run
    python main.py --profile EnglishCoach use another profile for this run
    python main.py --list-devices         show microphones and speakers
    python main.py --list-profiles        show available profiles
    python main.py --test-audio           beep, record 4 s, play it back (no API calls)
    python main.py --check-connection     verify the API key, model and session settings (no audio)
    python main.py --debug                also print every server event type
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from voicecompanion import ui
from voicecompanion.audio import AudioError, describe_devices, resolve_device, run_audio_test
from voicecompanion.config import (
    APP_NAME, PROVIDERS, ConfigError, find_profile, load_api_key, load_profiles, load_settings,
)
from voicecompanion.errors import BackendError, FatalBackendError
from voicecompanion.memory import MemoryStore


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=f"{APP_NAME}: live voice companion (Gemini Live or OpenAI).")
    parser.add_argument("--console", action="store_true", help="Run in the terminal, without the window.")
    parser.add_argument("--provider", choices=PROVIDERS, help="gemini or openai (overrides settings.yaml).")
    parser.add_argument("--profile", help="Profile name or alias (overrides settings.yaml).")
    parser.add_argument("--model", help="Model name for the active provider (overrides settings.yaml).")
    parser.add_argument("--settings", type=Path, help="Path to an alternative settings.yaml.")
    parser.add_argument("--list-devices", action="store_true", help="List audio devices and exit.")
    parser.add_argument("--list-profiles", action="store_true", help="List profiles and exit.")
    parser.add_argument("--test-audio", action="store_true", help="Test microphone and earbuds, then exit.")
    parser.add_argument("--check-connection", action="store_true", help="Test the API connection, then exit.")
    parser.add_argument("--debug", action="store_true", help="Print server event types.")
    return parser.parse_args(argv)


def show_startup_error(message: str, gui: bool) -> None:
    ui.error(message)
    if gui:  # started from a shortcut there is no console to read the error in
        try:
            from tkinter import Tk, messagebox

            root = Tk()
            root.withdraw()
            messagebox.showerror(APP_NAME, message)
            root.destroy()
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    one_shot = args.list_devices or args.list_profiles or args.test_audio or args.check_connection
    gui = not args.console and not one_shot
    if not one_shot:
        from voicecompanion.logs import setup_logging

        setup_logging()  # logs/mi-asistente.log: the only trace of problems when started from the shortcut
    memory = None
    try:
        settings = load_settings(args.settings)
        if args.provider:
            settings.provider = args.provider
        if args.model:
            settings.model = args.model

        if args.list_devices:
            print(describe_devices())
            return 0

        profiles = load_profiles()
        if args.list_profiles:
            for profile in profiles.values():
                marker = "*" if find_profile(profiles, settings.active_profile) is profile else " "
                print(f" {marker} {profile.name:<14} {profile.description}  (say: {', '.join(profile.aliases[:2])})")
            return 0

        input_device = resolve_device(settings.audio.input_device, "input")
        output_device = resolve_device(settings.audio.output_device, "output")
        if args.test_audio:
            run_audio_test(input_device, output_device)
            return 0

        wanted = args.profile or settings.active_profile
        profile = find_profile(profiles, wanted)
        if profile is None:
            raise ConfigError(f"Profile '{wanted}' not found. Available: {', '.join(profiles)}")

        api_key = load_api_key(settings.provider)
        memory = MemoryStore(settings.db_path)

        from voicecompanion.app import VoiceCompanionApp

        app = VoiceCompanionApp(settings, api_key, profiles, profile, memory,
                                input_device, output_device, debug=args.debug, console_keys=not gui)
        if args.check_connection:
            return 0 if asyncio.run(app.check_connection()) else 1

        ui.info(f"{APP_NAME} | {settings.provider} {settings.model} | profile {profile.name} | "
                f"{memory.count()} saved memories | "
                f"quiet hours {'ON' if settings.quiet_hours.is_active() else 'off'}")
        if gui:
            from voicecompanion.gui import run_gui

            return run_gui(app, memory)
        asyncio.run(app.run())
        ui.info("Bye.")
        return 0
    except KeyboardInterrupt:
        ui.info("Stopped.")
        return 0
    except (ConfigError, AudioError, FatalBackendError) as exc:
        show_startup_error(str(exc), gui)
        return 1
    except BackendError as exc:
        show_startup_error(f"Connection problem: {exc}", gui)
        return 1
    finally:
        if memory is not None:
            memory.close()


if __name__ == "__main__":
    sys.exit(main())
