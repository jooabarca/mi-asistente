"""Microphone capture and interruptible playback with sounddevice + numpy.

Both providers use 16-bit signed little-endian mono PCM: OpenAI wants 24 kHz in and
out, Gemini wants 16 kHz in and sends 24 kHz out. If a device can't be opened at the
needed rate (common with some Bluetooth drivers), the stream is opened at the device's
own rate and resampled with soxr.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable

import numpy as np
import sounddevice as sd

SAMPLE_RATE = 24000
DTYPE = "int16"


class AudioError(Exception):
    """Audio device problem, with a message meant for the user."""


# --------------------------------------------------------------------------- devices


def describe_devices() -> str:
    devices = sd.query_devices()
    hostapis = sd.query_hostapis()
    default_in, default_out = sd.default.device
    lines = ["Input devices (microphones):"]
    for idx, dev in enumerate(devices):
        if dev["max_input_channels"] > 0:
            mark = "*" if idx == default_in else " "
            lines.append(f"  {mark}{idx:>3}  {dev['name']}  [{hostapis[dev['hostapi']]['name']}]")
    lines.append("\nOutput devices (speakers / earbuds):")
    for idx, dev in enumerate(devices):
        if dev["max_output_channels"] > 0:
            mark = "*" if idx == default_out else " "
            lines.append(f"  {mark}{idx:>3}  {dev['name']}  [{hostapis[dev['hostapi']]['name']}]")
    lines.append("\n* = current Windows default. Prefer entries marked [MME] or [Windows WASAPI].")
    return "\n".join(lines)


def resolve_device(spec, kind: str) -> int | None:
    """Turn a settings value (None, index, or part of a name) into a device index."""
    if spec is None or str(spec).strip() == "":
        return None
    channels_key = "max_input_channels" if kind == "input" else "max_output_channels"
    devices = sd.query_devices()
    if isinstance(spec, int) or str(spec).strip().isdigit():
        idx = int(spec)
        if 0 <= idx < len(devices) and devices[idx][channels_key] > 0:
            return idx
        raise AudioError(f"{kind}_device {idx} is not a valid {kind} device. Run: python main.py --list-devices")
    wanted = str(spec).lower()
    for idx, dev in enumerate(devices):  # first match wins (MME entries come first on Windows)
        if dev[channels_key] > 0 and wanted in dev["name"].lower():
            return idx
    raise AudioError(f"No {kind} device matches '{spec}'. Run: python main.py --list-devices")


def _negotiate(device: int | None, kind: str, preferred_rate: int = SAMPLE_RATE) -> tuple[int, int]:
    """Find a (sample rate, channels) the device accepts, preferring preferred_rate mono."""
    try:
        info = sd.query_devices(device, kind)
    except Exception as exc:
        raise AudioError(f"No default {kind} device found ({exc}). Check Windows sound settings.") from exc
    check = sd.check_input_settings if kind == "input" else sd.check_output_settings
    max_channels = int(info["max_input_channels" if kind == "input" else "max_output_channels"])
    rates = [preferred_rate]
    if int(info["default_samplerate"]) != preferred_rate:
        rates.append(int(info["default_samplerate"]))
    last_error: Exception | None = None
    for rate in rates:
        for channels in dict.fromkeys((1, min(2, max_channels))):
            try:
                check(device=device, samplerate=rate, channels=channels, dtype=DTYPE)
                return rate, channels
            except Exception as exc:
                last_error = exc
    raise AudioError(f"Can't open {kind} device '{info['name']}': {last_error}")


def _make_resampler(in_rate: int, out_rate: int):
    if in_rate == out_rate:
        return None
    import soxr  # only needed when the device can't do 24 kHz

    return soxr.ResampleStream(in_rate, out_rate, 1, dtype=DTYPE)


# --------------------------------------------------------------------------- microphone


class Microphone:
    """Calls on_audio(bytes) with PCM16 mono chunks at target_rate, from the audio thread."""

    def __init__(self, device: int | None, block_ms: int, on_audio: Callable[[bytes], None],
                 target_rate: int = SAMPLE_RATE):
        self.device = device
        self.block_ms = block_ms
        self.on_audio = on_audio
        self.target_rate = target_rate
        self.stream: sd.InputStream | None = None
        self.peak = 0  # loudest sample seen, for the audio test

    def start(self) -> None:
        rate, channels = _negotiate(self.device, "input", preferred_rate=self.target_rate)
        self._resampler = _make_resampler(rate, self.target_rate)
        self.stream = sd.InputStream(
            device=self.device,
            samplerate=rate,
            channels=channels,
            dtype=DTYPE,
            blocksize=int(rate * self.block_ms / 1000),
            callback=self._callback,
        )
        self.stream.start()
        self.description = f"{sd.query_devices(self.stream.device)['name']} @ {rate} Hz"

    def _callback(self, indata, frames, time_info, status) -> None:
        mono = np.ascontiguousarray(indata[:, 0])
        if self._resampler is not None:
            mono = self._resampler.resample_chunk(mono)
        if mono.size:
            self.peak = max(self.peak, int(np.abs(mono.astype(np.int32)).max()))
            self.on_audio(mono.tobytes())

    def stop(self) -> None:
        if self.stream is not None:
            self.stream.stop()
            self.stream.close()
            self.stream = None


# --------------------------------------------------------------------------- playback


class Player:
    """Plays queued 24 kHz PCM16 chunks and remembers how much of each item was heard.

    That "played" amount is what conversation.item.truncate needs after an interruption.
    """

    def __init__(self, device: int | None):
        self.device = device
        self.stream: sd.OutputStream | None = None
        self._lock = threading.Lock()
        self._queue: deque[tuple[str, np.ndarray]] = deque()
        self._played: dict[str, int] = {}  # item_id -> samples played (device rate)
        self.rate = SAMPLE_RATE
        self.device_name = ""
        self.last_output_time = 0.0  # time.monotonic() of the last callback that played sound

    def start(self) -> None:
        self.rate, self.channels = _negotiate(self.device, "output")
        self._resampler = _make_resampler(SAMPLE_RATE, self.rate)
        self.stream = sd.OutputStream(
            device=self.device,
            samplerate=self.rate,
            channels=self.channels,
            dtype=DTYPE,
            latency="low",
            callback=self._callback,
        )
        self.stream.start()
        self.device_name = sd.query_devices(self.stream.device)["name"]
        self.description = f"{self.device_name} @ {self.rate} Hz"

    def stop(self) -> None:
        if self.stream is not None:
            self.stream.stop()
            self.stream.close()
            self.stream = None

    @property
    def is_playing(self) -> bool:
        return bool(self._queue)

    def recently_played(self, tail_s: float) -> bool:
        """True while playing, and for tail_s seconds after (speaker echo still in the room)."""
        return self.is_playing or time.monotonic() - self.last_output_time < tail_s

    def enqueue(self, item_id: str, pcm: bytes) -> None:
        samples = np.frombuffer(pcm, dtype=np.int16)
        if self._resampler is not None:
            samples = self._resampler.resample_chunk(samples)
        if not samples.size:
            return
        with self._lock:
            self._queue.append((item_id, samples))
            self._played.setdefault(item_id, 0)

    def interrupt(self) -> tuple[str, int] | None:
        """Stop immediately. Returns (item_id, milliseconds heard) of the item cut off, if any."""
        with self._lock:
            if not self._queue:
                return None
            item_id = self._queue[0][0]
            played_ms = int(self._played.get(item_id, 0) * 1000 / self.rate)
            self._queue.clear()
        if self._resampler is not None:
            self._resampler = _make_resampler(SAMPLE_RATE, self.rate)
        return item_id, played_ms

    def wait_until_done(self, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while self._queue and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.2)  # let the device buffer drain

    def _callback(self, outdata, frames, time_info, status) -> None:
        buf = np.zeros(frames, dtype=np.int16)
        filled = 0
        with self._lock:
            while filled < frames and self._queue:
                item_id, chunk = self._queue[0]
                n = min(frames - filled, len(chunk))
                buf[filled:filled + n] = chunk[:n]
                self._played[item_id] = self._played.get(item_id, 0) + n
                if n == len(chunk):
                    self._queue.popleft()
                else:
                    self._queue[0] = (item_id, chunk[n:])
                filled += n
        if filled:
            self.last_output_time = time.monotonic()
        outdata[:] = buf[:, None]


# Output device names that mean the sound goes into the user's ears, not the room.
_HEADPHONE_WORDS = ("headphone", "headset", "earbud", "earphone", "buds", "airpods", "hands-free", "auricular")


def looks_like_headphones(device_name: str) -> bool:
    name = device_name.lower()
    return any(word in name for word in _HEADPHONE_WORDS)


# --------------------------------------------------------------------------- self-test


def run_audio_test(input_device: int | None, output_device: int | None, seconds: float = 4.0) -> None:
    """Tone -> record -> play back, using exactly the format sent to the API."""
    player = Player(output_device)
    player.start()
    print(f"Output: {player.description}")
    t = np.arange(int(SAMPLE_RATE * 0.6)) / SAMPLE_RATE
    tone = (np.sin(2 * np.pi * 440 * t) * 0.2 * 32767).astype(np.int16)
    print("1) Playing a short beep. You should hear it in your earbuds...")
    player.enqueue("tone", tone.tobytes())
    player.wait_until_done()

    chunks: list[bytes] = []
    mic = Microphone(input_device, 40, chunks.append)
    mic.start()
    print(f"Input:  {mic.description}")
    print(f"2) Recording {seconds:.0f} seconds. Say something now...")
    time.sleep(seconds)
    mic.stop()
    recording = b"".join(chunks)
    level = mic.peak / 32767
    print(f"   Captured {len(recording) / 2 / SAMPLE_RATE:.1f} s of audio, peak level {level:.0%}.")
    if level < 0.02:
        print("   WARNING: the microphone seems silent. Check the input device and Windows mic privacy settings.")

    print("3) Playing your recording back...")
    player.enqueue("recording", recording)
    player.wait_until_done(seconds + 5)
    player.stop()
    print("Audio test finished.")
