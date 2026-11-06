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

Both kinds share one shape: a :class:`CaptureDevice` says what PCM format the device delivers
(what ffmpeg's ``pipe:0`` input is told) and makes a fresh :class:`BlockCapture`, which hands
over 20 ms blocks through a bounded queue that the sender reads from a worker thread.
"""

from __future__ import annotations

import abc
import contextlib
import os
import queue
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Any

from stadiacast.errors import SetupError

#: Fragments that mark a virtual input carrying the computer's own output.
LOOPBACK_HINTS = ("blackhole", "loopback", "soundflower", "virtual", "monitor")

#: Frames per block: 20 ms at 48 kHz, one Opus frame.
BLOCK = 960

#: Blocks a capture holds for a reader that fell behind: about half a second.
QUEUE_BLOCKS = 24


class BlockCapture(abc.ABC):
    """A running capture as the sender sees it: interleaved int16 blocks from a bounded queue.

    A subclass opens its device in :meth:`start`, hands every block to :meth:`_push` from
    whatever thread delivers it, and calls :meth:`_mark_end` once no more will come. A full queue
    drops the newest block and counts it in ``overflows``: the stream stays real time instead of
    lagging behind a reader that stalled. One capture per ffmpeg run, since none restarts.
    """

    def __init__(self, name: str, rate: int, channels: int) -> None:
        self.name = name
        self.rate = rate
        self.channels = channels
        #: Why the capture ended, when it failed.
        self.error: str | None = None
        #: Blocks lost to a full queue or reported lost by the device.
        self.overflows = 0
        self._q: queue.Queue[bytes | None] = queue.Queue(maxsize=QUEUE_BLOCKS)
        self._ended = False

    @abc.abstractmethod
    def start(self) -> None:
        """Open the device and start delivering blocks."""

    @abc.abstractmethod
    def stop(self) -> None:
        """Close the device; :meth:`read` then returns what is still queued, then ``None``."""

    def read(self, timeout: float = 1.0) -> bytes | None:
        """The oldest block; ``b""`` when none came within ``timeout``, ``None`` once the capture ended."""
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None if self._ended else b""

    def _push(self, block: bytes) -> None:
        try:
            self._q.put_nowait(block)
        except queue.Full:
            self.overflows += 1  # the reader stalled: drop rather than lag

    def _mark_end(self) -> None:
        """No more blocks: :meth:`read` returns ``None`` once the queue is empty, and the marker
        wakes a reader that is waiting."""
        self._ended = True
        with contextlib.suppress(queue.Full):
            self._q.put_nowait(None)


@dataclass(frozen=True)
class CaptureDevice(abc.ABC):
    """A capture device and the PCM format it delivers: the rate and the one or two channels used."""

    name: str
    rate: int
    channels: int

    @abc.abstractmethod
    def capture(self) -> BlockCapture:
        """A fresh, not yet started capture of this device."""


def _sounddevice() -> Any:
    try:
        import sounddevice
    except ImportError as e:
        raise SetupError("system audio capture needs the audio extra: pip install 'stadiacast[audio]'") from e
    except OSError as e:  # the binding is there but not the library it wraps
        raise SetupError(f"PortAudio is missing ({e}); on Linux install libportaudio2") from e
    return sounddevice


@dataclass(frozen=True)
class PortAudioDevice(CaptureDevice):
    """A PortAudio input, opened by its name."""

    def capture(self) -> PortAudioCapture:
        return PortAudioCapture(self)


class PortAudioCapture(BlockCapture):
    """A PortAudio input stream: its callback, on PortAudio's own thread, pushes each block."""

    def __init__(self, device: PortAudioDevice) -> None:
        super().__init__(device.name, device.rate, device.channels)
        self._stream: Any = None

    def _callback(self, indata: Any, _frames: int, _time: Any, status: Any) -> None:
        if status and status.input_overflow:
            self.overflows += 1
        self._push(bytes(indata))

    def start(self) -> None:
        sd = _sounddevice()
        self._stream = sd.RawInputStream(
            samplerate=self.rate,
            blocksize=BLOCK,
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
        self._mark_end()


def portaudio_devices() -> list[PortAudioDevice]:
    """The PortAudio input devices, in index order, each with its default rate and up to two channels."""
    sd = _sounddevice()
    return [
        PortAudioDevice(str(d["name"]), int(d["default_samplerate"]), min(2, int(d["max_input_channels"])))
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
        raise SetupError("pactl not found: name a capture device with device:NAME (see --audio-devices)")
    run = subprocess.run([exe, "get-default-sink"], capture_output=True, text=True, timeout=10, check=False)
    sink = run.stdout.strip()
    if run.returncode or not sink:
        raise SetupError("pactl found no default sink: name a capture device with device:NAME")
    return f"{sink}.monitor"


def open_portaudio(name: str | None) -> PortAudioDevice:
    """A capture device by a fragment of its name (exact match first), or the platform's loopback when ``None``."""
    devices = portaudio_devices()
    names = [d.name for d in devices]
    if name is None:
        if sys.platform.startswith("linux"):
            # PortAudio's pulse device reads the source PULSE_SOURCE names: the default output's monitor.
            os.environ["PULSE_SOURCE"] = linux_default_monitor()
            name = "pulse" if "pulse" in names else "default"
        else:
            name = pick_loopback(names)
            if name is None:
                raise SetupError(
                    f"no loopback device among the audio inputs ({', '.join(names) or 'none'}): install BlackHole "
                    "(brew install blackhole-2ch) and choose it as the sound output, or name an input with device:NAME"
                )
    key = name.lower()
    match = next((d for d in devices if key == d.name.lower()), None) or next(
        (d for d in devices if key in d.name.lower()), None
    )
    if match is None:
        raise SetupError(f"no capture device matches {name!r}; available: {names}")
    return match


def list_devices() -> list[str]:
    """The capture devices ``--audio-source device:NAME`` accepts on this platform."""
    if sys.platform == "win32":
        from stadiacast import loopback

        return [d.name for d in loopback.devices()]
    return [d.name for d in portaudio_devices()]


__all__ = [
    "BLOCK",
    "LOOPBACK_HINTS",
    "QUEUE_BLOCKS",
    "BlockCapture",
    "CaptureDevice",
    "PortAudioCapture",
    "PortAudioDevice",
    "linux_default_monitor",
    "list_devices",
    "open_portaudio",
    "pick_loopback",
    "portaudio_devices",
]
