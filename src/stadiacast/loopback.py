"""Windows system audio: WASAPI loopback through ``pyaudiowpatch`` (the ``audio`` extra).

ffmpeg has no WASAPI loopback input on Windows, so the capture runs here and the sender pipes the
PCM into ffmpeg's stdin. The default loopback is the default output device: whatever the speakers
play.

Two things that bite: a loopback device carries its speaker's name followed by ``[Loopback]``, and
WASAPI runs in shared mode, so the capture must use the device's mix format (48000 Hz usually,
sometimes 44100), which is why a :class:`Device` carries its rate and channels.
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from typing import Any

#: Frames per block: 20 ms at 48 kHz, one Opus frame.
BLOCK = 960


@dataclass
class Device:
    index: int
    name: str
    rate: int
    channels: int


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


def _device(info: dict[str, Any]) -> Device:
    return Device(
        index=int(info["index"]),
        name=str(info["name"]),
        rate=int(info["defaultSampleRate"]),
        channels=max(1, min(2, int(info["maxInputChannels"]))),
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
    finally:
        p.terminate()


class Capture:
    """A background thread reads PCM blocks into a queue; the sender takes them.

    A full queue drops the newest block so the stream stays real time instead of lagging.
    """

    def __init__(self, device: Device, *, block: int = BLOCK, qsize: int = 24) -> None:
        self.device = device
        self.name = device.name
        self.rate = device.rate
        self.channels = device.channels
        self.block = block
        self.error: str | None = None
        self.overflows = 0
        self._q: queue.Queue[bytes | None] = queue.Queue(maxsize=qsize)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="wasapi-loopback", daemon=True)

    def _run(self) -> None:
        stream = None
        p = _pyaudio().PyAudio()
        try:
            stream = p.open(
                format=_pyaudio().paInt16,
                channels=self.device.channels,
                rate=self.device.rate,
                input=True,
                input_device_index=self.device.index,
                frames_per_buffer=self.block,
            )
            while not self._stop.is_set():
                data = stream.read(self.block, exception_on_overflow=False)
                try:
                    self._q.put_nowait(data)
                except queue.Full:
                    self.overflows += 1  # the consumer stalled: drop rather than lag
        except Exception as e:
            self.error = repr(e)
        finally:
            if stream is not None:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass
            try:
                p.terminate()
            except Exception:
                pass
            try:
                self._q.put_nowait(None)
            except queue.Full:
                pass

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def read(self, timeout: float = 1.0) -> bytes | None:
        """One block of PCM; ``None`` once the capture ended (see ``error``), ``b""`` on a timeout."""
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return b""


__all__ = ["BLOCK", "Capture", "Device", "available", "describe", "devices"]
