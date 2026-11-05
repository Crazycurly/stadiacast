"""Where system audio comes from: the ``system`` and ``device:NAME`` sources.

The capture runs in this process and the sender pipes the PCM into ffmpeg's stdin, on every
platform, because ffmpeg's own device inputs are not reliable for this: its AVFoundation input
dropped a tenth of the samples on a Mac (measured, and audible as a choppy stream).

- macOS and Linux capture through PortAudio (the ``sounddevice`` package, part of the ``audio``
  extra). macOS has no loopback of its own: what the Mac plays is reached through a virtual
  output such as BlackHole, chosen as the sound output or inside a Multi-Output Device; the first
  input whose name looks like one is the default. On Linux the default output's PulseAudio
  monitor is used (written from the documentation, not yet tried on a real machine).
- Windows captures the default output's WASAPI loopback (:mod:`stadiacast.loopback`).
"""

from __future__ import annotations

import os
import queue
import shutil
import subprocess
import sys
from collections.abc import Callable
from typing import Any

#: Fragments that mark a virtual input carrying the computer's own output.
LOOPBACK_HINTS = ("blackhole", "loopback", "soundflower", "virtual", "monitor")

#: Frames per block: 20 ms at 48 kHz, one Opus frame.
BLOCK = 960


def _sounddevice() -> Any:
    try:
        import sounddevice
    except ImportError as e:
        raise RuntimeError("system audio capture needs the audio extra: pip install 'stadiacast[audio]'") from e
    except OSError as e:  # the binding is there but not the library it wraps
        raise RuntimeError(f"PortAudio is missing ({e}); on Linux install libportaudio2") from e
    return sounddevice


def portaudio_devices() -> list[tuple[str, int, int]]:
    """The PortAudio input devices: ``(name, default sample rate, input channels)``, in index order."""
    sd = _sounddevice()
    return [
        (str(d["name"]), int(d["default_samplerate"]), int(d["max_input_channels"]))
        for d in sd.query_devices()
        if int(d["max_input_channels"]) > 0
    ]


def pick_loopback(names: list[str]) -> str | None:
    """The first device that looks like a loopback of the computer's output, if any."""
    for name in names:
        if any(hint in name.lower() for hint in LOOPBACK_HINTS):
            return name
    return None


def linux_default_monitor() -> str:
    """The PulseAudio monitor of the default sink: what the computer plays."""
    exe = shutil.which("pactl")
    if exe is None:
        raise RuntimeError("pactl not found: name a capture device with device:NAME (see --audio-devices)")
    run = subprocess.run([exe, "get-default-sink"], capture_output=True, text=True, timeout=10, check=False)
    sink = run.stdout.strip()
    if run.returncode or not sink:
        raise RuntimeError("pactl found no default sink: name a capture device with device:NAME")
    return f"{sink}.monitor"


class PortAudioCapture:
    """A PortAudio input stream read in 20 ms int16 blocks; the sender takes them from a queue.

    A full queue drops the newest block so the stream stays real time instead of lagging.
    """

    def __init__(self, device: str, rate: int, channels: int, *, block: int = BLOCK, qsize: int = 24) -> None:
        self.name = device
        self.rate = rate
        self.channels = channels
        self.block = block
        self.error: str | None = None
        self.overflows = 0
        self._q: queue.Queue[bytes | None] = queue.Queue(maxsize=qsize)
        self._stream: Any = None

    def _callback(self, indata: Any, _frames: int, _time: Any, status: Any) -> None:
        if status and status.input_overflow:
            self.overflows += 1
        try:
            self._q.put_nowait(bytes(indata))
        except queue.Full:
            self.overflows += 1  # the consumer stalled: drop rather than lag

    def start(self) -> None:
        sd = _sounddevice()
        self._stream = sd.RawInputStream(
            samplerate=self.rate,
            blocksize=self.block,
            channels=self.channels,
            dtype="int16",
            device=self.name,
            # "low" asks CoreAudio for 20 ms I/O buffers: the default 85 ms ones hand over four
            # blocks at once, which the sender's pacer would have to trim (measured on a Mac).
            latency="low",
            callback=self._callback,
        )
        self._stream.start()

    def stop(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception as e:  # a failing close has nothing left to do
                self.error = repr(e)
        try:
            self._q.put_nowait(None)
        except queue.Full:
            pass

    def read(self, timeout: float = 1.0) -> bytes | None:
        """One block of PCM; ``None`` once the capture ended, ``b""`` on a timeout."""
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return b""


def open_portaudio(name: str | None) -> tuple[str, int, int, Callable[[], Any]]:
    """Resolve a capture device (``None`` = the platform's loopback) to its name, rate, channels
    and a factory for a fresh :class:`PortAudioCapture`."""
    devices = portaudio_devices()
    if name is None:
        if sys.platform.startswith("linux"):
            # PortAudio's pulse device reads the source PULSE_SOURCE names: the default output's monitor.
            os.environ["PULSE_SOURCE"] = linux_default_monitor()
            name = next((n for n, _r, _c in devices if n == "pulse"), None) or "default"
        else:
            name = pick_loopback([n for n, _r, _c in devices])
            if name is None:
                raise RuntimeError(
                    "no loopback device among the audio inputs "
                    f"({', '.join(n for n, _r, _c in devices) or 'none'}): install BlackHole "
                    "(brew install blackhole-2ch) and choose it as the sound output, or name an input with device:NAME"
                )
    key = name.lower()
    match = next(((n, r, c) for n, r, c in devices if key == n.lower()), None) or next(
        ((n, r, c) for n, r, c in devices if key in n.lower()), None
    )
    if match is None:
        raise RuntimeError(f"no capture device matches {name!r}; available: {[n for n, _r, _c in devices]}")
    dev_name, rate, channels = match
    channels = max(1, min(2, channels))
    return dev_name, rate, channels, lambda: PortAudioCapture(dev_name, rate, channels)


def list_devices() -> list[str]:
    """The capture devices ``--audio-source device:NAME`` accepts on this platform."""
    if sys.platform == "win32":
        from stadiacast import loopback

        return [d.name for d in loopback.devices()]
    return [n for n, _r, _c in portaudio_devices()]


__all__ = [
    "BLOCK",
    "LOOPBACK_HINTS",
    "PortAudioCapture",
    "linux_default_monitor",
    "list_devices",
    "open_portaudio",
    "pick_loopback",
    "portaudio_devices",
]
