"""Windows system audio: WASAPI loopback through ``pyaudiowpatch`` (the ``audio`` extra).

ffmpeg has no WASAPI loopback input on Windows, so the capture runs here and the sender pipes the
PCM into ffmpeg's stdin. The default loopback is the default output device: whatever the speakers
play.

Three things that bite: a loopback device carries its speaker's name followed by ``[Loopback]``;
WASAPI runs in shared mode, so the capture must use the device's mix format (48000 Hz usually,
sometimes 44100), which is why a :class:`Device` carries its rate and channels; and a loopback
delivers data only while some program renders on its speaker, so a silent computer would feed
the headset nothing. The capture therefore plays silence on the speaker for as long as it runs,
which keeps the audio engine, and with it the loopback, going.
"""

from __future__ import annotations

import contextlib
import threading
from dataclasses import dataclass
from typing import Any

from stadiacast.capture import BLOCK, BlockCapture, CaptureDevice
from stadiacast.errors import SetupError


def _pyaudio() -> Any:
    try:
        import pyaudiowpatch
    except ImportError as e:
        raise SetupError("system audio on Windows needs the audio extra: pip install 'stadiacast[audio]'") from e
    return pyaudiowpatch


def available() -> bool:
    try:
        _pyaudio()
    except SetupError:
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
            raise SetupError(f"no loopback device matches {name!r}; available: {[d.name for d in devices()]}")
        return found[0]
    p = pa.PyAudio()
    try:
        return _device(p.get_default_wasapi_loopback())
    except Exception as e:  # no default output device: take the first loopback there is
        found = devices()
        if not found:
            raise SetupError(f"no WASAPI loopback device (no output device? {e})") from e
        return found[0]
    finally:
        p.terminate()


def _speaker(p: Any, loopback: dict[str, Any]) -> dict[str, Any] | None:
    """The output device the loopback ``loopback`` mirrors: on the same host API, with the longest
    name the loopback's name contains (``pyaudiowpatch`` names a loopback after its speaker)."""
    best: dict[str, Any] | None = None
    for i in range(p.get_device_count()):
        info = p.get_device_info_by_index(i)
        if info.get("isLoopbackDevice") or int(info.get("maxOutputChannels", 0)) < 1:
            continue
        if info.get("hostApi") != loopback.get("hostApi") or str(info["name"]) not in str(loopback["name"]):
            continue
        if best is None or len(str(info["name"])) > len(str(best["name"])):
            best = info
    return best


def _open_silence(pa: Any, p: Any, speaker: dict[str, Any]) -> Any:
    """A stream of zeros rendering on ``speaker`` until it is closed."""
    channels = max(1, min(2, int(speaker["maxOutputChannels"])))

    def silence(_in: Any, frames: int, _time: Any, _status: Any) -> tuple[bytes, Any]:
        return bytes(frames * channels * 2), pa.paContinue

    return p.open(
        format=pa.paInt16,
        channels=channels,
        rate=int(speaker["defaultSampleRate"]),
        output=True,
        output_device_index=int(speaker["index"]),
        frames_per_buffer=BLOCK,
        stream_callback=silence,
    )


class Capture(BlockCapture):
    """The loopback stream, read blocking by a background thread that pushes each block, next to a
    silence stream on the speaker that keeps the loopback delivering while the computer is quiet."""

    def __init__(self, device: Device) -> None:
        super().__init__(device.name, device.rate, device.channels)
        self._index = device.index
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="wasapi-loopback", daemon=True)
        self._p: Any = None
        self._stream: Any = None
        self._silence: Any = None
        #: The speaker the silence plays on, or why it does not play: ``details()`` reports it.
        self.keepalive = "not started"

    def start(self) -> None:
        """Open the silence stream and the loopback, then read the loopback on the thread."""
        pa = _pyaudio()
        self._p = pa.PyAudio()
        try:
            speaker = _speaker(self._p, self._p.get_device_info_by_index(self._index))
            if speaker is None:
                self.keepalive = f"failed: no output device matches the loopback {self.name!r}"
            else:
                try:
                    self._silence = _open_silence(pa, self._p, speaker)
                    self.keepalive = str(speaker["name"])
                except Exception as e:  # the loopback still works while something plays
                    self.keepalive = f"failed: {e!r}"
            self._stream = self._p.open(
                format=pa.paInt16,
                channels=self.channels,
                rate=self.rate,
                input=True,
                input_device_index=self._index,
                frames_per_buffer=BLOCK,
            )
        except Exception:
            self._close()
            self._mark_end()
            raise
        self._thread.start()

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                self._push(self._stream.read(BLOCK, exception_on_overflow=False))
        except Exception as e:  # an unplugged or reconfigured device ends the capture; the sender restarts it
            self.error = repr(e)
        finally:
            self._close()
            self._mark_end()

    def _close(self) -> None:
        # A failing close has nothing left to do; the reader is woken either way.
        for stream in (self._stream, self._silence):
            if stream is not None:
                with contextlib.suppress(Exception):
                    stream.stop_stream()
                    stream.close()
        self._stream = self._silence = None
        if self._p is not None:
            with contextlib.suppress(Exception):
                self._p.terminate()
            self._p = None

    def stop(self) -> None:
        """Ask the thread to finish: it closes both streams after the block it is reading."""
        self._stop.set()

    def details(self) -> dict[str, Any]:
        return {"keepalive": self.keepalive}


__all__ = ["Capture", "Device", "available", "describe", "devices"]
