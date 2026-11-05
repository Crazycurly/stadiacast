"""The Windows-only paths against the real drivers: the ViGEm pad read back through XInput, the game's
rumble through ViGEm's notification, and the WASAPI loopback capture. Runs on a Windows machine with
the ViGEmBus driver, when STADIACAST_WINDOWS_LAB is set (the Windows lab workflow); skipped elsewhere."""

from __future__ import annotations

import asyncio
import ctypes
import os
import sys

import pytest

from tests.helpers import connect_firmware_flow, make_receiver, single, wait_for

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or not os.environ.get("STADIACAST_WINDOWS_LAB"),
    reason="needs Windows, the ViGEmBus driver and STADIACAST_WINDOWS_LAB=1",
)


class _XInputGamepad(ctypes.Structure):
    _fields_ = [
        ("wButtons", ctypes.c_ushort),
        ("bLeftTrigger", ctypes.c_ubyte),
        ("bRightTrigger", ctypes.c_ubyte),
        ("sThumbLX", ctypes.c_short),
        ("sThumbLY", ctypes.c_short),
        ("sThumbRX", ctypes.c_short),
        ("sThumbRY", ctypes.c_short),
    ]


class _XInputState(ctypes.Structure):
    _fields_ = [("dwPacketNumber", ctypes.c_ulong), ("Gamepad", _XInputGamepad)]


class _XInputVibration(ctypes.Structure):
    _fields_ = [("wLeftMotorSpeed", ctypes.c_ushort), ("wRightMotorSpeed", ctypes.c_ushort)]


def _xinput():
    lib = ctypes.WinDLL("xinput1_4.dll")  # type: ignore[attr-defined]
    lib.XInputGetState.argtypes = [ctypes.c_ulong, ctypes.POINTER(_XInputState)]
    lib.XInputGetState.restype = ctypes.c_ulong
    lib.XInputSetState.argtypes = [ctypes.c_ulong, ctypes.POINTER(_XInputVibration)]
    lib.XInputSetState.restype = ctypes.c_ulong
    return lib


def _read(lib, slot: int) -> _XInputGamepad:
    state = _XInputState()
    assert lib.XInputGetState(slot, ctypes.byref(state)) == 0, f"no XInput pad on slot {slot}"
    return state.Gamepad


def test_a_real_vigem_pad_follows_the_controller_and_reports_the_rumble(tmp_path) -> None:
    async def run() -> None:
        rx = make_receiver(tmp_path, gamepad="vigem", start_game="auto", start_game_delay=0.05)
        await rx.start()
        fc = await connect_firmware_flow(rx)
        try:
            await asyncio.wait_for(fc.display_ready.wait(), 5)
            await fc.keepalive()
            assert fc.send_input_rpc(buttons=1 << 12, lx=20000, ly=-20000, lt=255)  # A, left stick, left trigger
            await wait_for(lambda: rx.status()["gamepads"].get("c0", {}).get("frames", 0) >= 1, timeout=20)
            pad = rx.status()["gamepads"]["c0"]
            assert pad["slot"] is not None, "ViGEm created the pad but XInput never showed it"
            lib = _xinput()
            await asyncio.sleep(0.3)
            seen = _read(lib, pad["slot"])
            assert seen.wButtons & 0x1000 and seen.bLeftTrigger == 255 and seen.sThumbLX > 0 and seen.sThumbLY < 0
            assert fc.send_input_rpc(buttons=0)
            await wait_for(lambda: rx.status()["gamepads"]["c0"]["frames"] >= 2)
            await asyncio.sleep(0.3)
            assert _read(lib, pad["slot"]).wButtons == 0

            vibration = _XInputVibration(65535, 30000)  # what a game does
            assert lib.XInputSetState(pad["slot"], ctypes.byref(vibration)) == 0
            await wait_for(lambda: rx.log.counts.get("gamepad_rumble", 0) >= 1, timeout=10)
            record = next(r for r in rx.log.records if r["event"] == "gamepad_rumble")
            assert record["left"] == 65535 and record["right"] > 0
        finally:
            fc.close()
            await rx.stop()

    asyncio.run(run())


def test_the_windows_loopback_reaches_the_controller(tmp_path) -> None:
    from stadiacast import loopback

    if not loopback.available():
        pytest.skip("pyaudiowpatch is not installed")
    try:
        device = loopback.describe(None)
    except Exception as e:  # no render device on this machine
        pytest.skip(f"no WASAPI loopback device: {e!r}")

    async def run() -> None:
        rx = make_receiver(tmp_path, audio_source="system", audio_mode=2)
        await rx.start()
        fc = await connect_firmware_flow(rx)
        try:
            await asyncio.wait_for(fc.display_ready.wait(), 5)
            await fc.keepalive()
            await wait_for(lambda: rx.log.counts.get("audio_capture", 0) >= 1, timeout=15)
            await wait_for(lambda: len(fc.audio_frames) >= 25, timeout=15)  # half a second of Opus
            state = single(rx).audio_state()
            assert state["playing"] and state["source"] == f"capture {device.name}"
            assert state["stats"]["send_failed"] == 0
        finally:
            fc.close()
            await rx.stop()

    asyncio.run(run())
