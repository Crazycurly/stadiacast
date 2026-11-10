"""stadiacast.signaling: the address a remote ICE candidate names."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from stadiacast.signaling import candidate_address


def candidate(ip: str = "", port: int = 0, raw: str = "") -> SimpleNamespace:
    return SimpleNamespace(ice_candidate=SimpleNamespace(local_endpoint_ip=ip, local_endpoint_port=port), raw_value=raw)


def test_candidate_address_from_the_fields_and_from_the_sdp_line():
    assert candidate_address(candidate("192.168.2.2", 49153)) == ("192.168.2.2", 49153)
    assert candidate_address(candidate("fd00::2", 49153)) == ("fd00::2", 49153)
    line = "candidate:1 1 udp 2122260223 192.168.2.2 49153 typ host generation 0"
    assert candidate_address(candidate(raw=line)) == ("192.168.2.2", 49153)
    assert candidate_address(candidate()) is None


@pytest.mark.parametrize(
    ("ip", "port"),
    [
        pytest.param("192.168.2.2", 70000, id="a port beyond 65535"),
        pytest.param("192.168.2.2", 0, id="port 0"),
        pytest.param("controller.local", 49153, id="a name, which the socket would resolve"),
        pytest.param("192.168.2", 49153, id="not an address"),
    ],
)
def test_a_candidate_no_datagram_can_be_sent_to_is_none(ip, port):
    """asyncio closes a UDP socket for good on a port out of range, and resolves a name while
    every controller's link waits."""
    assert candidate_address(candidate(ip, port)) is None
    line = f"candidate:1 1 udp 2122260223 {ip} {port} typ host"
    assert candidate_address(candidate(raw=line)) is None
