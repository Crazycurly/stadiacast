"""Headset audio: the source spellings, the pacer's numbering, the capture plumbing and what a fake
controller receives."""

from __future__ import annotations

import asyncio
import json
import math
import os
import struct
import sys
import threading
import time
import types
from typing import Any

import pytest

from stadiacast import capture, loopback
from stadiacast.audio import NOTES_KEPT, SAMPLES_PER_FRAME, AudioSender, Source, ffmpeg_path, source_from_spec
from stadiacast.capture import BLOCK, QUEUE_BLOCKS, BlockCapture
from stadiacast.errors import SetupError
from stadiacast.rtc.rtp import parse_rtp
from tests.helpers import connect_firmware_flow, make_receiver, single, wait_for

#: One 20 ms stereo int16 block at 48 kHz, in bytes.
BLOCK_BYTES = BLOCK * 2 * 2


class _StubEndpoint:
    """Just enough of ``RtcEndpoint`` for ``AudioSender``: no SRTP, records the RTP it is handed."""

    def __init__(self) -> None:
        self.dtls = types.SimpleNamespace(handshake_complete=True)
        self.srtp_out = None
        self.sent: list[bytes] = []

    def send_rtp_packet(self, packet: bytes, *, protect: bool = True) -> bool:
        assert not protect  # the stub has no SRTP context, so the caller must not ask for one
        self.sent.append(packet)
        return True


def test_source_spec_spellings() -> None:
    tone = source_from_spec("tone")
    assert tone.args[:3] == ["-re", "-f", "lavfi"]
    assert source_from_spec("file:/tmp/a.ogg").args[-2:] == ["-i", "/tmp/a.ogg"]
    for bad in ("", "bogus", "file:"):
        with pytest.raises(ValueError):
            source_from_spec(bad)


def test_file_source_puts_stream_loop_before_the_input() -> None:
    src = Source.from_file("music.ogg", duration=2.0)
    assert src.args.index("-stream_loop") < src.args.index("-i")  # after -i it does nothing
    assert src.args[-2:] == ["-t", "2.0"]
    assert src.label == "file music.ogg"


def test_sender_numbers_frames_on_its_own_grid() -> None:
    async def run() -> None:
        ep = _StubEndpoint()
        sender = AudioSender(ep, srtp=False)
        await sender.start(None)  # no encoder: the frames come from the test
        try:
            for i in range(3):
                sender.enqueue(bytes([0xF8, i]), marker=(i == 0))
            await wait_for(lambda: len(ep.sent) >= 3, timeout=2)
        finally:
            await sender.stop()

        packets = [parse_rtp(pkt) for pkt in ep.sent]
        assert [p.payload_type for p in packets] == [111] * 3
        assert [p.marker for p in packets] == [True, False, False]
        assert {p.ssrc for p in packets} == {sender.ssrc}
        assert packets[1].seq == (packets[0].seq + 1) & 0xFFFF
        assert packets[2].seq == (packets[1].seq + 1) & 0xFFFF
        # Timestamps follow the slot index, so a hole stays a hole instead of shifting the stream.
        assert packets[1].timestamp - packets[0].timestamp == SAMPLES_PER_FRAME
        assert packets[2].timestamp - packets[1].timestamp == SAMPLES_PER_FRAME
        assert [p.payload for p in packets] == [bytes([0xF8, 0]), bytes([0xF8, 1]), bytes([0xF8, 2])]

    asyncio.run(run())


def test_a_burst_keeps_only_the_newest_frames() -> None:
    """ffmpeg bursts at start-up; frames past the queue target go oldest first, so latency stays put."""

    async def run() -> None:
        ep = _StubEndpoint()
        sender = AudioSender(ep, srtp=False)
        await sender.start(None)
        try:
            for i in range(10):
                sender.enqueue(bytes([0xF8, i]), marker=False)
            await wait_for(lambda: len(ep.sent) >= 6, timeout=2)
        finally:
            await sender.stop()
        assert [parse_rtp(p).payload[1] for p in ep.sent] == [4, 5, 6, 7, 8, 9]
        stats = sender.stats.snapshot()
        assert (stats["trimmed"], stats["max_queue"]) == (4, sender.state()["queue_target"])

    asyncio.run(run())


def test_the_notes_keep_only_the_latest() -> None:
    """A failure that repeats on every frame must not grow the stats without bound."""

    class _FailingEndpoint:
        def __init__(self) -> None:
            self.calls = 0

        def send_rtp_packet(self, _packet: bytes, **_kwargs: Any) -> bool:
            self.calls += 1
            raise OSError(f"send {self.calls}")

    async def run() -> None:
        sender = AudioSender(_FailingEndpoint(), srtp=False)
        failures = NOTES_KEPT + 4
        await sender.start(None)
        try:
            for i in range(failures):
                sender.enqueue(bytes([0xF8, i]), marker=False)
                await wait_for(lambda n=i: sender.stats.send_failed > n, timeout=2)
        finally:
            await sender.stop()
        notes = sender.stats.snapshot()["notes"]
        assert notes == [f"send: OSError('send {n}')" for n in range(failures - NOTES_KEPT + 1, failures + 1)]

    asyncio.run(run())


def test_audio_reaches_the_controller(tmp_path) -> None:
    async def run() -> None:
        rx = make_receiver(tmp_path)
        await rx.start()
        fc = await connect_firmware_flow(rx)
        cs = single(rx)
        try:
            assert cs.audio_state() == {"playing": False}
            # The display link has to be past OnConnected, or the pad drops RPCs on the floor.
            await wait_for(lambda: fc.dc_state == "ready", timeout=6)
            await cs.audio_start(None)  # real SRTP: the fake protects/decrypts like the pad does
            for i in range(5):
                cs.audio_feed(bytes([0xF8, i]), marker=(i == 0))
            await wait_for(lambda: len(fc.audio_frames) >= 5, timeout=3)
            frames = fc.audio_frames[:5]
            assert [f.payload for f in frames] == [bytes([0xF8, i]) for i in range(5)]
            assert [f.seq for f in frames] == [(frames[0].seq + i) & 0xFFFF for i in range(5)]
            # The firmware only plays the stream once SetAudioMode(mode=2) opened the path.
            modes = [
                r["payload"].set_audio_mode
                for r in fc.rpc_in
                if r["kind"] == "request" and r["payload"] is not None and r["payload"].HasField("set_audio_mode")
            ]
            assert [(m.mode, m.f3) for m in modes] == [(2, True)]
            # The audio stream must not ride on the endpoint's own SSRC: input ACKs use that one.
            assert frames[0].ssrc != cs.endpoint.local_ssrc
            assert cs.audio_state()["playing"] is True
        finally:
            await cs.audio_stop()
            fc.close()
            await rx.stop()

    asyncio.run(run())


@pytest.mark.skipif(ffmpeg_path() is None, reason="needs ffmpeg (with libopus) on PATH")
def test_ffmpeg_tone_comes_out_of_the_encoder(tmp_path) -> None:
    """The whole chain with a real encoder: ffmpeg -> local RTP -> pacer -> SRTP/UDP."""

    async def run() -> None:
        rx = make_receiver(tmp_path)
        await rx.start()
        fc = await connect_firmware_flow(rx)
        cs = single(rx)
        try:
            await cs.audio_start(Source.test_tone(duration=0.5))
            await wait_for(lambda: len(fc.audio_frames) >= 5, timeout=10)
            frames = fc.audio_frames[:5]
            assert {f.payload_type for f in frames} == {111}
            assert all(f.payload for f in frames)  # real Opus, not empty keep-alives
            assert [f.seq for f in frames] == [(frames[0].seq + i) & 0xFFFF for i in range(5)]
        finally:
            await cs.audio_stop()
            fc.close()
            await rx.stop()

    asyncio.run(run())


def test_a_missing_ffmpeg_is_a_setup_error(monkeypatch) -> None:
    from stadiacast import audio

    monkeypatch.setattr(audio, "ffmpeg_path", lambda: None)

    async def run() -> None:
        sender = AudioSender(_StubEndpoint(), srtp=False)
        with pytest.raises(SetupError, match="ffmpeg not found"):
            await sender.start(Source.test_tone())
        assert not sender.playing

    asyncio.run(run())


def test_the_opus_application_and_the_volume_follower_are_constructor_choices() -> None:
    music = AudioSender(None, application="audio", follow_system_volume=False)
    cmd = music._ffmpeg_cmd("ffmpeg", Source.test_tone())
    assert cmd[cmd.index("-application") + 1] == "audio"
    state = music.state()
    assert (state["application"], state["follow_system_volume"]) == ("audio", False)
    default = AudioSender(None)
    assert (default.application, default.follow_system_volume) == ("lowdelay", True)


# ---------------------------------------------------------------- system audio capture


class _FakeSounddevice:
    """Just enough of ``sounddevice``: a device list, and an input stream whose ``start`` hands the
    given blocks to the capture's callback at once, the way PortAudio's thread does when the reader
    has stalled."""

    def __init__(self, devices: list[tuple[str, int, int]], blocks: list[bytes] | None = None) -> None:
        self.devices = devices
        self.blocks = blocks or []
        self.streams: list[_FakeInputStream] = []

    def query_devices(self) -> list[dict[str, Any]]:
        return [{"name": n, "default_samplerate": float(r), "max_input_channels": c} for n, r, c in self.devices]

    def RawInputStream(self, **kwargs: Any) -> _FakeInputStream:  # noqa: N802 (sounddevice's name)
        stream = _FakeInputStream(self.blocks, **kwargs)
        self.streams.append(stream)
        return stream


class _FakeInputStream:
    def __init__(self, blocks: list[bytes], *, callback: Any, **kwargs: Any) -> None:
        self.blocks, self.callback, self.kwargs = blocks, callback, kwargs
        self.closed = False

    def start(self) -> None:
        overrun = types.SimpleNamespace(input_overflow=True)  # PortAudio reports one lost buffer
        for i, block in enumerate(self.blocks):
            self.callback(block, BLOCK, None, overrun if i == 0 else None)

    def stop(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class _FakePyAudio:
    """Just enough of ``pyaudiowpatch``: a speaker and its loopback twin. One object plays the module
    and the PortAudio session; the stream it opens reads the given blocks, then fails like an
    unplugged device (or keeps repeating the last block when ``endless``)."""

    paInt16 = 8  # noqa: N815 (pyaudiowpatch's name)
    paContinue = 0  # noqa: N815 (pyaudiowpatch's name)

    def __init__(
        self,
        blocks: list[bytes] | None = None,
        *,
        default: bool = True,
        endless: bool = False,
        output_fails: bool = False,
    ) -> None:
        self.infos: list[dict[str, Any]] = [
            {
                "index": 0,
                "name": "Speakers",
                "defaultSampleRate": 48000.0,
                "maxInputChannels": 0,
                "maxOutputChannels": 2,
            },
            {
                "index": 1,
                "name": "Speakers [Loopback]",
                "defaultSampleRate": 44100.0,
                "maxInputChannels": 2,
                "maxOutputChannels": 0,
                "isLoopbackDevice": True,
            },
        ]
        self.blocks = list(blocks or [])
        self.default, self.endless, self.output_fails = default, endless, output_fails
        self.opened: list[dict[str, Any]] = []
        self.closed = False

    def PyAudio(self) -> _FakePyAudio:  # noqa: N802 (pyaudiowpatch's name)
        return self

    def get_device_count(self) -> int:
        return len(self.infos)

    def get_device_info_by_index(self, i: int) -> dict[str, Any]:
        return self.infos[i]

    def get_default_wasapi_loopback(self) -> dict[str, Any]:
        if not self.default:
            raise OSError("no default output device")
        return self.infos[1]

    def open(self, **kwargs: Any) -> _FakePyAudio:
        if kwargs.get("output") and self.output_fails:
            raise OSError("the speaker is in exclusive use")
        self.opened.append(kwargs)
        return self

    def read(self, _frames: int, exception_on_overflow: bool = True) -> bytes:
        assert not exception_on_overflow  # an overrun must not end the capture
        time.sleep(0.001)
        if self.endless and len(self.blocks) == 1:
            return self.blocks[0]
        if not self.blocks:
            raise OSError("device unplugged")
        return self.blocks.pop(0)

    def stop_stream(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    def terminate(self) -> None:
        pass


def _blocks(n: int) -> list[bytes]:
    return [bytes([i]) * BLOCK_BYTES for i in range(n)]


def test_system_and_device_spellings_follow_the_platform(monkeypatch) -> None:
    inputs = [("Mac mini Speakers", 48000, 0), ("MacBook Pro Microphone", 48000, 1), ("BlackHole 2ch", 48000, 2)]
    monkeypatch.setitem(sys.modules, "sounddevice", _FakeSounddevice(inputs))
    monkeypatch.setattr(sys, "platform", "darwin")
    system = source_from_spec("system")
    assert system.args == ["-f", "s16le", "-ar", "48000", "-ac", "2", "-i", "pipe:0"]
    assert system.pcm and system.label == "capture BlackHole 2ch" and system.open_capture is not None
    mic = source_from_spec("device:macbook")  # a fragment, case-insensitive, picks one channel
    assert mic.args == ["-f", "s16le", "-ar", "48000", "-ac", "1", "-i", "pipe:0"]
    assert mic.open_capture is not None and mic.open_capture().name == "MacBook Pro Microphone"
    assert capture.list_devices() == ["MacBook Pro Microphone", "BlackHole 2ch"]  # inputs only
    monkeypatch.setitem(sys.modules, "sounddevice", _FakeSounddevice(inputs[:2]))
    with pytest.raises(SetupError, match="BlackHole"):
        source_from_spec("system")
    with pytest.raises(SetupError, match="no capture device matches"):
        source_from_spec("device:nothing")

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("PULSE_SOURCE", "")  # restored after the test: the source sets it
    monkeypatch.setitem(sys.modules, "sounddevice", _FakeSounddevice([("pulse", 44100, 32), ("default", 44100, 32)]))
    monkeypatch.setattr(capture, "linux_default_monitor", lambda: "alsa_output.pci.monitor")
    monitor = source_from_spec("system")
    assert monitor.args == ["-f", "s16le", "-ar", "44100", "-ac", "2", "-i", "pipe:0"]
    assert monitor.label == "capture pulse"
    assert monitor.open_capture is not None and isinstance(monitor.open_capture(), capture.PortAudioCapture)
    assert os.environ["PULSE_SOURCE"] == "alsa_output.pci.monitor"

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "pyaudiowpatch", _FakePyAudio())
    win = source_from_spec("system")
    assert win.args == ["-f", "s16le", "-ar", "44100", "-ac", "2", "-i", "pipe:0"]
    assert win.pcm and win.open_capture is not None and win.label == "capture Speakers [Loopback]"
    assert isinstance(win.open_capture(), loopback.Capture)
    assert capture.list_devices() == ["Speakers [Loopback]"]

    with pytest.raises(ValueError, match="device: needs a name"):
        source_from_spec("device:")


def test_the_loopback_hints_pick_the_virtual_output() -> None:
    from stadiacast.capture import pick_loopback

    assert pick_loopback(["MacBook Pro Microphone", "BlackHole 2ch"]) == "BlackHole 2ch"
    assert pick_loopback(["Monitor of Built-in Audio"]) == "Monitor of Built-in Audio"
    assert pick_loopback(["MacBook Pro Microphone"]) is None


def test_a_portaudio_capture_drops_blocks_rather_than_lag(monkeypatch) -> None:
    blocks = _blocks(QUEUE_BLOCKS + 2)
    sd = _FakeSounddevice([("BlackHole 2ch", 48000, 2)], blocks)
    monkeypatch.setitem(sys.modules, "sounddevice", sd)
    cap = capture.open_portaudio("blackhole 2ch").capture()
    cap.start()  # the fake delivers every block at once: a reader that stalled
    (stream,) = sd.streams
    assert stream.kwargs == {
        "samplerate": 48000,
        "blocksize": BLOCK,
        "channels": 2,
        "dtype": "int16",
        "device": "BlackHole 2ch",
        "latency": "low",
    }
    assert cap.overflows == 3  # two blocks found the queue full, and PortAudio reported one overrun
    assert cap.read(0.1) == blocks[0]  # oldest first: it is the newest that were dropped
    cap.stop()
    assert stream.closed
    assert [cap.read(0.1) for _ in range(QUEUE_BLOCKS - 1)] == blocks[1:QUEUE_BLOCKS]
    assert cap.read(0.1) is None  # stopped and drained
    assert cap.error is None


def test_the_windows_loopback_is_found_and_read_without_windows(monkeypatch) -> None:
    """WASAPI loopback on the fake: the default output's loopback in its mix format, silence rendered
    on its speaker so the loopback keeps delivering, its blocks on the shared queue, and an unplugged
    device ending the capture with the reason."""
    blocks = _blocks(3)
    pa = _FakePyAudio(blocks)
    monkeypatch.setitem(sys.modules, "pyaudiowpatch", pa)
    assert loopback.available()
    dev = loopback.describe(None)
    assert (dev.name, dev.rate, dev.channels, dev.index) == ("Speakers [Loopback]", 44100, 2, 1)
    assert loopback.describe("speakers") == dev and loopback.devices() == [dev]
    with pytest.raises(SetupError, match="no loopback device matches 'headphones'"):
        loopback.describe("headphones")

    cap = dev.capture()
    cap.start()
    assert cap.details() == {"keepalive": "Speakers"}
    assert [cap.read(1.0) for _ in blocks] == blocks
    assert cap.read(1.0) is None  # the device went away: the reader is told, and why
    assert cap.error is not None and "unplugged" in cap.error
    assert pa.closed
    silence, capture = pa.opened
    render = silence.pop("stream_callback")
    assert render(None, 480, None, 0) == (bytes(480 * 2 * 2), _FakePyAudio.paContinue)
    assert silence == {
        "format": _FakePyAudio.paInt16,
        "channels": 2,
        "rate": 48000,
        "output": True,
        "output_device_index": 0,
        "frames_per_buffer": BLOCK,
    }
    assert capture == {
        "format": _FakePyAudio.paInt16,
        "channels": 2,
        "rate": 44100,
        "input": True,
        "input_device_index": 1,
        "frames_per_buffer": BLOCK,
    }

    # The speaker refuses a second stream, or has no device of its own: the capture runs without
    # the keepalive and says so.
    busy = _FakePyAudio(_blocks(1), output_fails=True)
    monkeypatch.setitem(sys.modules, "pyaudiowpatch", busy)
    cap = loopback.describe(None).capture()
    cap.start()
    assert cap.details()["keepalive"] == "failed: OSError('the speaker is in exclusive use')"
    assert [kw["input"] for kw in busy.opened] == [True]
    lonely = _FakePyAudio(_blocks(1), default=False)
    lonely.infos = [dict(lonely.infos[1], index=0)]
    monkeypatch.setitem(sys.modules, "pyaudiowpatch", lonely)
    cap = loopback.describe(None).capture()
    cap.start()
    assert cap.details()["keepalive"] == "failed: no output device matches the loopback 'Speakers [Loopback]'"

    # No default output device: the first loopback there is, and with none at all a setup error.
    monkeypatch.setitem(sys.modules, "pyaudiowpatch", _FakePyAudio(default=False))
    assert loopback.describe(None) == dev
    bare = _FakePyAudio(default=False)
    bare.infos = bare.infos[:1]
    monkeypatch.setitem(sys.modules, "pyaudiowpatch", bare)
    with pytest.raises(SetupError, match="no WASAPI loopback device"):
        loopback.describe(None)


def test_a_stopped_loopback_hands_over_its_queue_then_ends(monkeypatch) -> None:
    """Stopping with a full queue: the reader still gets every queued block, then the end."""
    monkeypatch.setitem(sys.modules, "pyaudiowpatch", _FakePyAudio(_blocks(1), endless=True))
    cap = loopback.describe(None).capture()
    cap.start()
    deadline = time.monotonic() + 5
    while cap.overflows == 0 and time.monotonic() < deadline:  # nobody reads: the queue fills up
        time.sleep(0.01)
    cap.stop()
    cap._thread.join(timeout=2)  # it closes the stream after the block it is reading
    queued = []
    while (block := cap.read(0.2)) is not None:
        queued.append(block)
    assert len(queued) == QUEUE_BLOCKS and cap.error is None


def _loopback_input_present() -> bool:
    try:
        return capture.pick_loopback([d.name for d in capture.portaudio_devices()]) is not None
    except SetupError:
        return False


@pytest.mark.skipif(
    not _loopback_input_present() or ffmpeg_path() is None, reason="needs PortAudio, a loopback input and ffmpeg"
)
def test_the_system_source_keeps_real_time() -> None:
    """The whole capture chain on a machine with a loopback device: no sample is dropped."""

    async def run() -> None:
        ep = _StubEndpoint()
        sender = AudioSender(ep, srtp=False)
        await sender.start(Source.system())
        try:
            await asyncio.sleep(4.0)
        finally:
            state = await sender.stop()
        stats = state["stats"]
        assert stats["frames"] >= 150 and stats["underruns"] <= 5, stats  # 4 s = 200 frames, minus the start-up

    asyncio.run(run())


def test_volume_becomes_an_ffmpeg_filter() -> None:
    sender = AudioSender(_StubEndpoint(), srtp=False)
    assert sender._af_args() == []
    sender.volume = 0.5
    assert sender._af_args() == ["-af", "volume=0.5000"]
    sender.volume = 2.0
    assert sender._af_args() == ["-af", "volume=2.0000,alimiter=limit=0.98"]
    assert sender._scale_pcm(b"\x00\x40\x00\xc0", 0.5) == b"\x00\x20\x00\xe0"  # 0x4000 -> 0x2000, -0x4000 -> -0x2000
    assert sender._scale_pcm(b"\x00\x40", 1.0) == b"\x00\x40"
    # The extremes stay in range, a half sample at the end is dropped, and mute is silence.
    assert sender._scale_pcm(struct.pack("<2h", -32768, 32767) + b"\x01", 0.5) == struct.pack("<2h", -16384, 16383)
    assert sender._scale_pcm(struct.pack("<2h", -32768, 32767), 0.0) == bytes(4)


def test_the_windows_volume_is_followed_unless_turned_off(monkeypatch) -> None:
    from stadiacast import sysvolume

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sysvolume, "master_scalar", lambda: 0.25)
    monkeypatch.setattr(sysvolume, "muted", lambda: False)
    assert AudioSender(None)._system_gain() == 0.25
    assert AudioSender(None, follow_system_volume=False)._system_gain() == 1.0
    monkeypatch.setattr(sysvolume, "muted", lambda: True)
    assert AudioSender(None)._system_gain() == 0.0  # mute is a switch of its own: the slider can stay up
    monkeypatch.setattr(sysvolume, "master_scalar", lambda: None)
    unreadable = AudioSender(None)
    assert unreadable._system_gain() == 1.0
    assert unreadable.stats.snapshot()["notes"] == ["the Windows volume could not be read; streaming at full volume"]


class _SineCapture(BlockCapture):
    """A stand-in for a sound card: a thread pushes 20 ms blocks of a 440 Hz sine, stereo int16 at 48 kHz."""

    def __init__(self) -> None:
        super().__init__("fake loopback", 48000, 2)
        self.stopped = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        n = 0
        while not self.stopped.wait(0.02):  # real time, like a sound card
            block = [int(12000 * math.sin(2 * math.pi * 440 * i / 48000)) for i in range(n, n + BLOCK)]
            self._push(struct.pack(f"<{2 * BLOCK}h", *[v for s in block for v in (s, s)]))
            n += BLOCK
        self._mark_end()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self.stopped.set()


@pytest.mark.skipif(ffmpeg_path() is None, reason="needs ffmpeg (with libopus) on PATH")
@pytest.mark.parametrize(("gain", "application"), [(1.0, "lowdelay"), (0.5, "audio")])
def test_a_pcm_source_is_piped_into_ffmpeg(monkeypatch, gain: float, application: str) -> None:
    """The capture path without a sound card: PCM blocks on ffmpeg's stdin come out as Opus frames,
    scaled by the system volume in the worker thread that reads them, never on the event loop."""
    scaled_on: set[int] = set()
    scale_pcm = AudioSender._scale_pcm

    def spy(chunk: bytes, g: float) -> bytes:
        scaled_on.add(threading.get_ident())
        return scale_pcm(chunk, g)

    monkeypatch.setattr(AudioSender, "_scale_pcm", staticmethod(spy))

    async def run() -> None:
        ep = _StubEndpoint()
        sender = AudioSender(ep, srtp=False, application=application)
        monkeypatch.setattr(sender, "_system_gain", lambda: gain)  # what Windows' slider would say
        cap = _SineCapture()
        source = Source(
            ["-f", "s16le", "-ar", "48000", "-ac", "2", "-i", "pipe:0"],
            "fake loopback",
            pcm=True,
            open_capture=lambda: cap,
        )
        await sender.start(source)
        try:
            await wait_for(lambda: len(ep.sent) >= 5, timeout=10)
        finally:
            await sender.stop()
        assert cap.stopped.is_set()
        assert sender.stats.level == pytest.approx(gain * 12000 / 32768, abs=0.01)  # the sine's peak reached ffmpeg
        assert all(parse_rtp(p).payload for p in ep.sent[:5])
        assert scaled_on and threading.get_ident() not in scaled_on

    asyncio.run(run())


class _BusyCapture(BlockCapture):
    """A device that refuses to open, the way a busy or just unplugged one does."""

    def __init__(self) -> None:
        super().__init__("busy", 48000, 2)

    def start(self) -> None:
        raise OSError("device busy")

    def stop(self) -> None:
        pass


@pytest.mark.skipif(ffmpeg_path() is None, reason="needs ffmpeg (with libopus) on PATH")
def test_a_capture_that_fails_to_start_is_tried_again() -> None:
    """ffmpeg is restarted with a fresh capture instead of waiting forever on a pipe nobody writes."""

    async def run() -> None:
        ep = _StubEndpoint()
        sender = AudioSender(ep, srtp=False)
        captures = iter([_BusyCapture(), _SineCapture()])
        source = Source(
            ["-f", "s16le", "-ar", "48000", "-ac", "2", "-i", "pipe:0"],
            "flaky device",
            pcm=True,
            open_capture=lambda: next(captures),
        )
        await sender.start(source)
        try:
            await wait_for(lambda: len(ep.sent) >= 5, timeout=10)
        finally:
            await sender.stop()
        stats = sender.stats.snapshot()
        assert stats["ffmpeg_restarts"] == 1
        assert stats["notes"][0] == "capture failed: OSError('device busy')"

    asyncio.run(run())


def test_the_audio_endpoint(tmp_path) -> None:
    async def run() -> None:
        rx = make_receiver(tmp_path)
        await rx.start()
        fc = await connect_firmware_flow(rx)
        port = rx.http.http_ports[0]

        async def call(body: dict | None = None):
            raw = json.dumps(body).encode() if body is not None else b""
            st, _, out = await fc.request(
                "POST" if body else "GET", "x", "/_audio", raw, "application/json", port=port, tls=False
            )
            return st, json.loads(out)

        try:
            await asyncio.wait_for(fc.display_ready.wait(), 5)
            await fc.keepalive()
            assert await call() == (200, {"playing": False, "controller": "c0"})
            assert (await call({"do": "bogus"}))[0] == 400
            st, out = await call({"do": "start", "source": "nonsense"})
            assert st == 400 and "unknown audio source" in out["error"]
            st, out = await call({"do": "volume", "volume": 0.5})
            assert (st, out["volume"], out["playing"]) == (200, 0.5, False)
            st, out = await call({"do": "mode", "mode": 2})
            assert st == 200 and out["ok"] is True
            await wait_for(
                lambda: any(
                    r["payload"].HasField("set_audio_mode")
                    for r in fc.rpc_in
                    if r["kind"] == "request" and r["payload"] is not None
                )
            )
            st, out = await call({"do": "stop"})
            assert st == 200 and out["playing"] is False
        finally:
            fc.close()
            await rx.stop()

    asyncio.run(run())


def test_the_capture_modules_say_what_to_install(tmp_path, monkeypatch) -> None:
    """Linux without libportaudio2: the binding imports but raises OSError; the receiver says what to
    install. Without the extra (PortAudio or the Windows loopback), it names the extra."""
    (tmp_path / "sounddevice.py").write_text("raise OSError('PortAudio library not found')\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "sounddevice", raising=False)
    with pytest.raises(SetupError, match="libportaudio2"):
        capture.portaudio_devices()
    monkeypatch.setitem(sys.modules, "sounddevice", None)  # not installed at all: the extra is named
    with pytest.raises(SetupError, match="audio extra"):
        capture.portaudio_devices()
    monkeypatch.setitem(sys.modules, "pyaudiowpatch", None)
    assert not loopback.available()
    with pytest.raises(SetupError, match="audio extra"):
        loopback.devices()
