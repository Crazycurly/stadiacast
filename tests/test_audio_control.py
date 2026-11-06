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
from stadiacast.errors import SetupError
from stadiacast.receiver import Receiver
from tests.fake_controller import FakeController
from tests.helpers import connect_firmware_flow, make_receiver, single, wait_for


class Sources:
    """Stands in for ``source_from_spec``: every spec resolves to ``None`` (the pacer alone), or
    raises what ``fail`` maps it to.  ``resolved`` records each spec and whether it was resolved
    on the event loop's own thread."""

    def __init__(self) -> None:
        self.resolved: list[tuple[str, bool]] = []
        self.fail: dict[str, Exception] = {}

    def __call__(self, spec: str) -> None:
        try:
            asyncio.get_running_loop()
            on_loop = True
        except RuntimeError:  # a worker thread has no running loop
            on_loop = False
        self.resolved.append((spec, on_loop))
        if spec in self.fail:
            raise self.fail[spec]

    @property
    def specs(self) -> list[str]:
        return [spec for spec, _ in self.resolved]


@pytest.fixture
def sources(monkeypatch: pytest.MonkeyPatch) -> Sources:
    stand_in = Sources()
    monkeypatch.setattr(controller, "source_from_spec", stand_in)
    return stand_in


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
            assert sources.specs == ["tone"]
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


def test_sources_resolve_off_the_event_loop_and_a_setup_error_is_reported(tmp_path, sources) -> None:
    """Resolving a capture enumerates devices or runs pactl: never on the loop every controller shares.

    A capture this computer cannot open is ``audio_source_invalid`` at READY and a 400 on ``/_audio``.
    """
    sources.fail["system"] = SetupError("system audio capture needs the audio extra")

    async def run() -> None:
        rx = make_receiver(tmp_path, audio_source="system")
        await rx.start()
        fc = await connect_firmware_flow(rx)
        cs = single(rx)
        try:
            await asyncio.wait_for(fc.display_ready.wait(), 5)
            await fc.keepalive()
            await wait_for(lambda: rx.log.counts.get("audio_source_invalid") == 1)
            rec = [r for r in rx.log.records if r["event"] == "audio_source_invalid"][-1]
            assert rec["spec"] == "system" and "audio extra" in rec["error"]
            fc.send_input_rpc(buttons=1)
            await wait_for(lambda: cs.display_state == "input")
            assert sources.specs == ["system"]  # once per boot: the input milestone does not retry it
            st, out = await call(fc, rx, "POST", {"do": "start", "source": "system"})
            assert st == 400 and "audio extra" in out["error"]
            st, out = await call(fc, rx, "POST", {"do": "start", "source": "tone"})
            assert (st, out["playing"]) == (200, True)
            assert sources.resolved == [("system", False), ("system", False), ("tone", False)]
        finally:
            fc.close()
            await rx.stop()

    asyncio.run(run())


@pytest.mark.usefixtures("sources")
def test_get_reads_the_stream_and_only_post_changes_it(tmp_path) -> None:
    async def run() -> None:
        rx = make_receiver(tmp_path)
        await rx.start()
        fc = await connect_firmware_flow(rx)
        cs = single(rx)
        try:
            await asyncio.wait_for(fc.display_ready.wait(), 5)
            await fc.keepalive()
            st, out = await call(fc, rx, "POST", {"do": "start", "source": "tone"})
            assert (st, out["playing"]) == (200, True)
            st, out = await call(fc, rx, "GET", {"do": "stop"})  # an action in a GET body is ignored
            assert (st, out["playing"], out["controller"]) == (200, True, "c0")
            for method in ("PUT", "DELETE"):
                assert await call(fc, rx, method, {"do": "stop"}) == (405, None)
            assert cs.audio_state()["playing"] is True
            st, out = await call(fc, rx, "POST")  # a POST has to say what to do
            assert st == 400 and out["actions"] == ["start", "stop", "volume", "mode"]
            st, out = await call(fc, rx, "POST", {"do": "stop"})
            assert (st, out["playing"]) == (200, False)
        finally:
            fc.close()
            await rx.stop()

    asyncio.run(run())
