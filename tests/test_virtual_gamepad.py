"""The virtual gamepad output: one pad per controller, XInput fields in, the game's rumble back."""

from __future__ import annotations

import asyncio
import sys
from typing import ClassVar

import pytest

from stadiacast.config import Config
from stadiacast.log import EventLog
from stadiacast.virtual_gamepad import BUTTON_BITS, MOTOR_SCALE, VirtualGamepads
from tests.helpers import connect_firmware_flow, fake, make_receiver, single, wait_for


class _FakePad:
    """A pad that records what the receiver drives, in place of ViGEm."""

    created: ClassVar[list[_FakePad]] = []

    def __init__(self, label: str) -> None:
        self.label = label
        self.slot = len(_FakePad.created)
        self.applied: list[dict[str, int]] = []
        self.resets = 0
        self.closed = False
        self.rumble_cb = None
        _FakePad.created.append(self)

    def apply(self, xinput: dict[str, int]) -> None:
        self.applied.append(xinput)

    def reset(self) -> None:
        self.resets += 1

    def on_rumble(self, callback) -> None:
        self.rumble_cb = callback

    def close(self) -> None:
        self.closed = True


def _with_fake_pads(rx):
    _FakePad.created.clear()
    rx.cfg.gamepad_deadzone = True
    rx.gamepads = VirtualGamepads(rx.cfg, rx.log, rx, _FakePad)
    return rx


def test_input_reaches_the_pad_and_rumble_comes_back(tmp_path) -> None:
    async def run() -> None:
        rx = _with_fake_pads(make_receiver(tmp_path, start_game="auto", start_game_delay=0.05))
        await rx.start()
        fc = await connect_firmware_flow(rx)
        cs = single(rx)
        rumbles: list[tuple[int, int]] = []
        cs.game.set_rumble = lambda left, right, _ms=None, _framing=None: rumbles.append((left, right)) or 7  # type: ignore[method-assign]
        try:
            await asyncio.wait_for(fc.display_ready.wait(), 5)
            await fc.keepalive()
            assert fc.send_input_rpc(buttons=1 << 12, lx=20000, ly=-20000, lt=255)  # A, left stick, left trigger
            await wait_for(lambda: _FakePad.created and _FakePad.created[0].applied)
            pad = _FakePad.created[0]
            assert pad.label == "c0" and rx.log.counts["gamepad_created"] == 1
            x = pad.applied[-1]
            assert x["wButtons"] == 1 << 12 and x["bLeftTrigger"] == 255 and x["bRightTrigger"] == 0
            assert x["sThumbLX"] > 0 and x["sThumbLY"] < 0  # past the deadzone, no sign flip
            assert fc.send_input_rpc(buttons=0, lx=1000, ly=1000)  # inside XInput's left deadzone
            await wait_for(lambda: len(pad.applied) >= 2)
            assert pad.applied[-1]["wButtons"] == 0 and (pad.applied[-1]["sThumbLX"], pad.applied[-1]["sThumbLY"]) == (
                0,
                0,
            )
            assert rx.status()["gamepads"] == {"c0": {"slot": 0, "frames": 2, "rumbles": 0}}

            assert pad.rumble_cb is not None
            pad.rumble_cb(255, 128)  # the game's motors, as ViGEm reports them
            await wait_for(lambda: rumbles)
            assert rumbles == [(255 * MOTOR_SCALE, 128 * MOTOR_SCALE)] == [(65535, 32896)]
            assert rx.log.counts["gamepad_rumble"] == 1

            cs.reset_boot_state()  # a reboot lets go of everything
            assert pad.resets == 1
        finally:
            fc.close()
            await rx.stop()
        assert pad.closed

    asyncio.run(run())


def test_two_controllers_get_two_pads(tmp_path) -> None:
    async def run() -> None:
        rx = _with_fake_pads(make_receiver(tmp_path, display_rpc_delay=0.05, display_rpc_interval=0.1))
        await rx.start()
        fa, fb = fake(rx, "fake-a"), fake(rx, "fake-b")
        try:
            await asyncio.gather(fa.boot(), fb.boot())
            await asyncio.gather(fa.start_udp(), fb.start_udp())
            await asyncio.gather(fa.discover(), fb.discover())
            await asyncio.gather(fa.poll_gmsg(), fb.poll_gmsg())
            await asyncio.gather(fa.poll_gmsg(), fb.poll_gmsg())
            fa.connect()
            fb.connect()
            await asyncio.gather(fa.wait_connected(10), fb.wait_connected(10))
            await asyncio.gather(
                asyncio.wait_for(fa.display_ready.wait(), 5), asyncio.wait_for(fb.display_ready.wait(), 5)
            )
            await asyncio.gather(fa.keepalive(), fb.keepalive())
            assert fa.send_input_rpc(buttons=1 << 13) and fb.send_input_rpc(buttons=1 << 15)
            await wait_for(lambda: len(_FakePad.created) == 2 and all(p.applied for p in _FakePad.created))
            by_label = {p.label: p.applied[-1]["wButtons"] for p in _FakePad.created}
            a, b = rx.find("fake-a"), rx.find("fake-b")
            assert a is not None and b is not None
            assert by_label == {a.label: 1 << 13, b.label: 1 << 15}
        finally:
            fa.close()
            fb.close()
            await rx.stop()

    asyncio.run(run())


def test_the_vigem_backend_is_windows_only_and_the_flags_parse() -> None:
    from stadiacast.cli import parse_args

    cfg = parse_args(["--gamepad", "vigem", "--gamepad-deadzone", "off"])
    assert (cfg.gamepad, cfg.gamepad_deadzone) == ("vigem", False)
    assert parse_args([]).gamepad == "off" and parse_args([]).gamepad_deadzone is True
    if sys.platform != "win32":
        with pytest.raises(RuntimeError, match="Windows only"):
            VirtualGamepads.create(Config(cert="x", key="y", gamepad="vigem"), EventLog(None, echo=False), None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unknown --gamepad"):
        VirtualGamepads.create(Config(cert="x", key="y", gamepad="bogus"), EventLog(None, echo=False), None)  # type: ignore[arg-type]
    assert set(BUTTON_BITS) == {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 13, 14, 15}


def test_a_missing_driver_or_extra_is_a_clear_error(tmp_path, monkeypatch) -> None:
    """On Windows, importing vgamepad connects to the ViGEm bus: without the driver the import itself
    raises. Both that and a missing package end in a message saying what to install."""
    monkeypatch.setattr(sys, "platform", "win32")
    (tmp_path / "vgamepad.py").write_text("raise RuntimeError('Error: VIGEM_ERROR_BUS_NOT_FOUND')\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "vgamepad", raising=False)
    cfg = Config(cert="x", key="y", gamepad="vigem")
    with pytest.raises(RuntimeError, match=r"ViGEmBus driver installed .*BUS_NOT_FOUND"):
        VirtualGamepads.create(cfg, EventLog(None, echo=False), None)  # type: ignore[arg-type]
    monkeypatch.setitem(sys.modules, "vgamepad", None)
    with pytest.raises(RuntimeError, match="gamepad extra"):
        VirtualGamepads.create(cfg, EventLog(None, echo=False), None)  # type: ignore[arg-type]
