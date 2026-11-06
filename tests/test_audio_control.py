"""Controlling the headset stream: ``--audio-source`` at READY, the ``/_audio`` endpoint and the session's settings.

The sources are stood in for (no sound card, no ffmpeg): ``source_from_spec`` returns ``None``,
which starts the sender's pacer with no encoder, and the test feeds the frames.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from stadiacast import controller
from stadiacast.receiver import Receiver
from tests.fake_controller import FakeController
from tests.helpers import connect_firmware_flow, make_receiver, single, wait_for


@pytest.fixture
def sources(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every spec the controller resolves, in order; each resolves to ``None`` (pacer only)."""
    seen: list[str] = []

    def resolve(spec: str) -> None:
        seen.append(spec)

    monkeypatch.setattr(controller, "source_from_spec", resolve)
    return seen


async def call(
    fc: FakeController, rx: Receiver, method: str = "GET", body: dict[str, Any] | None = None
) -> tuple[int, Any]:
    """One request to ``/_audio`` on the plaintext port; the JSON answer, or ``None`` for an empty one."""
    raw = json.dumps(body).encode() if body is not None else b""
    st, _, out = await fc.request(
        method, "x", "/_audio", raw, "application/json", port=rx.http.http_ports[0], tls=False
    )
    return st, json.loads(out) if out else None


def test_a_volume_set_before_ready_keeps_the_autostart(tmp_path, sources) -> None:
    """The gain set before READY waits on the session; ``--audio-source`` still starts at READY, with it."""

    async def run() -> None:
        rx = make_receiver(tmp_path, audio_source="tone")
        await rx.start()
        fc = await connect_firmware_flow(rx)
        cs = single(rx)
        try:
            assert cs.display_state not in ("ready", "input")
            st, out = await call(fc, rx, "POST", {"do": "volume", "volume": 0.5})
            assert (st, out["playing"], out["volume"]) == (200, False, 0.5)
            assert "audio_started" not in rx.log.counts
            await asyncio.wait_for(fc.display_ready.wait(), 5)
            await fc.keepalive()  # the controller's READY-only traffic: READY on our side
            await wait_for(lambda: rx.log.counts.get("audio_started") == 1)
            assert sources == ["tone"]
            state = cs.audio_state()
            assert (state["playing"], state["volume"]) == (True, 0.5)
            cs.audio_feed(bytes([0xF8, 0]), marker=True)
            await wait_for(lambda: fc.audio_frames)
            # The gain outlives the stream: a stop and a new start keep it.
            assert (await call(fc, rx, "POST", {"do": "stop"}))[1]["playing"] is False
            st, out = await call(fc, rx, "POST", {"do": "start", "source": "tone"})
            assert st == 200 and cs.audio_state()["volume"] == 0.5
        finally:
            fc.close()
            await rx.stop()

    asyncio.run(run())
