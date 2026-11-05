"""Headset audio: the source spellings, the pacer's numbering, and what a fake controller receives."""

from __future__ import annotations

import asyncio
import json
import sys
import types

import pytest

from stadiacast.audio import SAMPLES_PER_FRAME, AudioSender, Source, ffmpeg_path, source_from_spec
from stadiacast.rtc.rtp import parse_rtp
from tests.helpers import connect_firmware_flow, make_receiver, single, wait_for


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


# ---------------------------------------------------------------- system audio capture


def test_system_and_device_spellings_follow_the_platform(monkeypatch) -> None:
    from stadiacast import capture, loopback

    inputs = [("MacBook Pro Microphone", 48000, 1), ("BlackHole 2ch", 48000, 2)]
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(capture, "portaudio_devices", lambda: inputs)
    system = source_from_spec("system")
    assert system.args == ["-f", "s16le", "-ar", "48000", "-ac", "2", "-i", "pipe:0"]
    assert system.pcm and system.label == "capture BlackHole 2ch" and system.open_capture is not None
    mic = source_from_spec("device:macbook")  # a fragment, case-insensitive, picks one channel
    assert mic.args == ["-f", "s16le", "-ar", "48000", "-ac", "1", "-i", "pipe:0"]
    assert mic.open_capture is not None and mic.open_capture().name == "MacBook Pro Microphone"
    monkeypatch.setattr(capture, "portaudio_devices", lambda: inputs[:1])
    with pytest.raises(RuntimeError, match="BlackHole"):
        source_from_spec("system")
    with pytest.raises(RuntimeError, match="no capture device matches"):
        source_from_spec("device:nothing")

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(capture, "portaudio_devices", lambda: [("pulse", 44100, 32), ("default", 44100, 32)])
    monkeypatch.setattr(capture, "linux_default_monitor", lambda: "alsa_output.pci.monitor")
    monitor = source_from_spec("system")
    assert monitor.args == ["-f", "s16le", "-ar", "44100", "-ac", "2", "-i", "pipe:0"]
    assert monitor.label == "capture pulse"

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(loopback, "describe", lambda _name: loopback.Device(3, "Speakers [Loopback]", 44100, 2))
    win = source_from_spec("system")
    assert win.args == ["-f", "s16le", "-ar", "44100", "-ac", "2", "-i", "pipe:0"]
    assert win.pcm and win.open_capture is not None and win.label == "capture Speakers [Loopback]"

    with pytest.raises(ValueError, match="device: needs a name"):
        source_from_spec("device:")


def test_the_loopback_hints_pick_the_virtual_output() -> None:
    from stadiacast.capture import pick_loopback

    assert pick_loopback(["MacBook Pro Microphone", "BlackHole 2ch"]) == "BlackHole 2ch"
    assert pick_loopback(["Monitor of Built-in Audio"]) == "Monitor of Built-in Audio"
    assert pick_loopback(["MacBook Pro Microphone"]) is None


def _loopback_input_present() -> bool:
    try:
        from stadiacast import capture

        return capture.pick_loopback([n for n, _r, _c in capture.portaudio_devices()]) is not None
    except RuntimeError:
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


class _SineCapture:
    """A stand-in for the Windows loopback: 20 ms blocks of a 440 Hz sine, stereo int16 at 48 kHz."""

    name, rate, channels, error = "fake loopback", 48000, 2, None

    def __init__(self) -> None:
        self.n = 0
        self.stopped = False

    def start(self) -> None:
        pass

    def stop(self) -> None:
        self.stopped = True

    def read(self, _timeout: float = 1.0) -> bytes | None:
        import math
        import struct
        import time as _t

        _t.sleep(0.02)  # real time, like a sound card
        start, self.n = self.n, self.n + 960
        block = [int(12000 * math.sin(2 * math.pi * 440 * i / 48000)) for i in range(start, start + 960)]
        return struct.pack(f"<{2 * 960}h", *[v for s in block for v in (s, s)])


@pytest.mark.skipif(ffmpeg_path() is None, reason="needs ffmpeg (with libopus) on PATH")
def test_a_pcm_source_is_piped_into_ffmpeg() -> None:
    """The Windows loopback path without Windows: PCM blocks on ffmpeg's stdin come out as Opus frames."""

    async def run() -> None:
        ep = _StubEndpoint()
        sender = AudioSender(ep, srtp=False)
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
        assert cap.stopped
        assert sender.stats.level > 0.3  # the sine's peak reached ffmpeg
        assert all(parse_rtp(p).payload for p in ep.sent[:5])

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


def test_a_missing_portaudio_library_is_a_clear_error(tmp_path, monkeypatch) -> None:
    """Linux without libportaudio2: the binding imports but raises OSError; the receiver says what to install."""
    from stadiacast import capture

    (tmp_path / "sounddevice.py").write_text("raise OSError('PortAudio library not found')\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "sounddevice", raising=False)
    with pytest.raises(RuntimeError, match="libportaudio2"):
        capture.portaudio_devices()
    monkeypatch.setitem(sys.modules, "sounddevice", None)  # not installed at all: the extra is named
    with pytest.raises(RuntimeError, match="audio extra"):
        capture.portaudio_devices()
