"""The helper processes as the receiver runs them: one that exits is reported with what it wrote, one
stopped with the receiver is not, and a helper whose port is taken logs who holds it and exits."""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from stadiacast import receiver as receiver_module
from stadiacast.helpers import DNS_SINK, SNTP_SERVER, helper_command
from tests.helpers import make_receiver, wait_for

SRC = str(Path(__file__).resolve().parents[1] / "src")


def _python(code: str):
    return lambda _name, _args: [sys.executable, "-c", code]


def test_a_helper_that_exits_is_logged_with_its_last_lines(tmp_path, monkeypatch) -> None:
    exits = _python("import sys; print('UDP port 123 is taken'); sys.exit(3)")
    monkeypatch.setattr(receiver_module, "helper_command", exits)

    async def run() -> None:
        rx = make_receiver(tmp_path, sntp=True)
        await rx.start()
        try:
            await wait_for(lambda: rx.log.counts.get("helper_exited", 0) >= 1, timeout=30)
        finally:
            await rx.stop()
        record = next(r for r in rx.log.records if r["event"] == "helper_exited")
        assert (record["helper"], record["returncode"]) == ("sntp_server", 3)
        assert record["output"] == ["UDP port 123 is taken"]

    asyncio.run(run())


def test_a_helper_stopped_with_the_receiver_is_not_reported(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(receiver_module, "helper_command", _python("import time; time.sleep(60)"))

    async def run() -> None:
        rx = make_receiver(tmp_path, sntp=True)
        await rx.start()
        await asyncio.sleep(0.2)
        await rx.stop()
        assert rx._sntp_proc is not None and rx._sntp_proc.returncode is not None
        assert rx.log.counts.get("helper_exited", 0) == 0

    asyncio.run(run())


@pytest.mark.parametrize("name", [SNTP_SERVER, DNS_SINK])
def test_a_helper_whose_port_is_taken_says_so_and_exits(tmp_path, name) -> None:
    holder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    if sys.platform == "win32":  # Windows lets a second socket share a port unless the first forbids it
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    holder.bind(("127.0.0.1", 0))
    port = holder.getsockname()[1]
    log = tmp_path / "helper.jsonl"
    args = ["--listen", "127.0.0.1", "--port", str(port), "--log", str(log)]
    if name == DNS_SINK:
        args += ["--redirect-ip", "127.0.0.1"]
    try:
        run = subprocess.run(
            helper_command(name, args),
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": SRC},
            timeout=60,
            check=False,
        )
    finally:
        holder.close()
    assert run.returncode == 1, run.stdout + run.stderr
    assert f"bind_failed listen=127.0.0.1:{port} error=" in run.stdout and f"port {port} is taken" in run.stdout
    assert '"event": "bind_failed"' in log.read_text()
