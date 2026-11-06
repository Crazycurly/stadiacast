"""Windows system audio: WASAPI loopback through ``pyaudiowpatch`` (the ``audio`` extra).

ffmpeg has no WASAPI loopback input on Windows, so the capture runs here and the sender pipes the
PCM into ffmpeg's stdin. The default loopback is the default output device: whatever the speakers
play.

Two things that bite: a loopback device carries its speaker's name followed by ``[Loopback]``, and
WASAPI runs in shared mode, so the capture must use the device's mix format (48000 Hz usually,
sometimes 44100), which is why a :class:`Device` carries its rate and channels.
"""

from __future__ import annotations

import contextlib
import threading
from dataclasses import dataclass
from typing import Any

from stadiacast.capture import BLOCK, BlockCapture, CaptureDevice


def _pyaudio() -> Any:
    try:
        import pyaudiowpatch
    except ImportError as e:
        raise RuntimeError("system audio on Windows needs the audio extra: pip install 'stadiacast[audio]'") from e
    return pyaudiowpatch


def available() -> bool:
    try:
        _pyaudio()
    except RuntimeError:
        return False
    return True


@dataclass(frozen=True)
class Device(CaptureDevice):
    """A loopback device, which PyAudio opens by its index."""

    index: int

    def capture(self) -> Capture:
        return Capture(self)


def _device(info: dict[str, Any]) -> Device:
    return Device(
        name=str(info["name"]),
        rate=int(info["defaultSampleRate"]),
        channels=max(1, min(2, int(info["maxInputChannels"]))),
        index=int(info["index"]),
    )


def devices() -> list[Device]:
    """Every loopback device: one per output endpoint."""
    pa = _pyaudio()
    p = pa.PyAudio()
    try:
        return [
            _device(p.get_device_info_by_index(i))
            for i in range(p.get_device_count())
            if p.get_device_info_by_index(i).get("isLoopbackDevice")
        ]
    finally:
        p.terminate()


def describe(name: str | None = None) -> Device:
    """A loopback device by a fragment of its name, or the default output's loopback."""
    pa = _pyaudio()
    if name:
        key = name.lower()
        found = [d for d in devices() if key in d.name.lower()]
        if not found:
            raise RuntimeError(f"no loopback device matches {name!r}; available: {[d.name for d in devices()]}")
        return found[0]
    p = pa.PyAudio()
    try:
        return _device(p.get_default_wasapi_loopback())
    except Exception as e:  # no default output device: take the first loopback there is
        found = devices()
        if not found:
            raise RuntimeError(f"no WASAPI loopback device (no output device? {e})") from e
        return found[0]
    finally:
        p.terminate()


class Capture(BlockCapture):
    """A background thread reads the loopback stream, blocking, and pushes each block."""

    def __init__(self, device: Device) -> None:
        super().__init__(device.name, device.rate, device.channels)
        self._index = device.index
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="wasapi-loopback", daemon=True)

    def _run(self) -> None:
        p: Any = None
        stream: Any = None
        try:
            pa = _pyaudio()
            p = pa.PyAudio()
            stream = p.open(
                format=pa.paInt16,
                channels=self.channels,
                rate=self.rate,
                input=True,
                input_device_index=self._index,
                frames_per_buffer=BLOCK,
            )
            while not self._stop.is_set():
                self._push(stream.read(BLOCK, exception_on_overflow=False))
        except Exception as e:  # an unplugged or reconfigured device ends the capture; the sender restarts it
            self.error = repr(e)
        finally:
            # A failing close has nothing left to do; the reader is woken either way.
            if stream is not None:
                with contextlib.suppress(Exception):
                    stream.stop_stream()
                    stream.close()
            if p is not None:
                with contextlib.suppress(Exception):
                    p.terminate()
            self._mark_end()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        """Ask the thread to finish: it closes the stream after the block it is reading."""
        self._stop.set()


__all__ = ["Capture", "Device", "available", "describe", "devices"]
