"""stadiacast.ble_provisioner without a radio: the pure parts, and the whole provisioning flow against a
fake BLE stack (device discovery, the Wi-Fi scan, the RSA-encrypted credential write)."""

from __future__ import annotations

import asyncio
import base64
import gzip
import json
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from stadiacast import ble_provisioner as ble


def test_connect_payload_and_redaction():
    payload = ble.build_connect_payload("home", "c2VjcmV0", 7, 4, bssid="aa:bb", frequency=2412)
    assert payload == {
        "ssid": "home", "wpa_auth": 7, "wpa_cipher": 4, "bssid": "aa:bb", "frequency": 2412, "enc_passwd": "c2VjcmV0",
    }  # fmt: skip
    assert "enc_passwd" not in ble.build_connect_payload("open", None, 1, 1)
    assert ble.redact('{"ssid":"home","enc_passwd":"c2VjcmV0"}') == '{"ssid":"home","enc_passwd": "<redacted>"}'


def test_window_payload_decompresses_gzip_only():
    assert ble.window_payload(gzip.compress(b'{"a":1}')) == b'{"a":1}'
    assert ble.window_payload(b"plain") == b"plain"
    assert ble.window_payload(b"\x1f\x8btruncated") == b"\x1f\x8btruncated"


def test_window_payload_survives_a_torn_gzip_stream():
    # gzip raises EOFError for a stream cut short and zlib.error for corrupt data, neither an OSError
    torn = gzip.compress(b'{"ssid": "net"}')[:-4]
    assert ble.window_payload(torn) == torn
    corrupt = b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\x03" + b"\xff" * 8  # a gzip header, then an invalid block
    assert ble.window_payload(corrupt) == corrupt


@pytest.mark.usefixtures("fake_ble")  # the fixture also fast-forwards asyncio.sleep
def test_window_read_walks_pages_until_a_short_read():
    class Paging:
        def __init__(self) -> None:
            self.requested: list[int] = []

        async def write_gatt_char(self, char: str, data: bytes, response: bool = True) -> None:  # noqa: ARG002
            self.requested.append(int.from_bytes(data, "little"))

        async def read_gatt_char(self, char: str) -> bytes:  # noqa: ARG002
            page = self.requested[-1]
            if page == 0:
                return b"a" * 512  # full page: keep going
            if page == 1:
                return b"b" * 300  # short page: last one
            raise AssertionError("read past the short page")

    client = Paging()
    out = asyncio.run(ble.window_read(client, ble.WINDOW_CRYPTO))
    assert out == b"a" * 512 + b"b" * 300
    assert client.requested == [0, 1]  # the default page budget is not spent on a two-page payload


def test_cli_requires_a_subcommand_and_knows_the_match_option():
    parser = ble.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])
    args = parser.parse_args(["provision", "-a", "ADDR", "-s", "net", "--service-data-match", "7AC0"])
    assert (args.cmd, args.address, args.ssid, args.service_data_match) == ("provision", "ADDR", "net", "7AC0")
    args = parser.parse_args(
        ["provision", "-a", "ADDR", "-s", "net", "--skip-scan", "--wpa-auth", "7", "--wpa-cipher", "4"]
    )
    assert args.skip_scan and (args.wpa_auth, args.wpa_cipher) == (7, 4)
    assert ble.STATE_NAMES[0x3C] == "connected"


def test_advertiser_detection():
    fast_pair = SimpleNamespace(local_name=None, service_data={ble.FAST_PAIR_SERVICE_DATA_UUID: b"\x7a\xc0"})
    assert ble.is_stadia_advertiser(SimpleNamespace(name=None), fast_pair)
    assert ble.is_stadia_advertiser(
        SimpleNamespace(name="Stadia Setup"), SimpleNamespace(local_name=None, service_data={})
    )
    assert not ble.is_stadia_advertiser(SimpleNamespace(name="Kettle"), SimpleNamespace(local_name="", service_data={}))


def test_main_reports_a_missing_ble_extra(monkeypatch, capsys):
    monkeypatch.setattr(ble, "BLEAK_AVAILABLE", False)
    assert ble.main(["scan"]) == 2
    assert "stadiacast[ble]" in capsys.readouterr().err


# ---- the provisioning flow against a fake controller --------------------------------------

PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
SCAN_RESULTS = [
    {"ssid": "neighbour", "bssid": "11:22:33:44:55:66", "signal_level": -80, "wpa_auth": 7, "wpa_cipher": 4},
    {
        "ssid": "hotspot",
        "bssid": "aa:bb:cc:dd:ee:ff",
        "signal_level": -40,
        "wpa_auth": 5,
        "wpa_cipher": 3,
        "frequency": 2437,
    },
]


def crypto_bundle() -> bytes:
    spki = PRIVATE_KEY.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return json.dumps({"device_info": {"public_key": base64.b64encode(spki).decode()}}).encode()


def test_public_key_and_password_encryption_round_trip():
    key = ble.load_public_key(crypto_bundle())
    assert key.key_size == 2048
    enc = ble.encrypt_password("hunter2", key)
    assert PRIVATE_KEY.decrypt(base64.b64decode(enc), padding.PKCS1v15()) == b"hunter2"


class FakeClient:
    """The GATT surface the provisioner touches, with the controller's windowed reads."""

    mtu_size = 517  # what the controller negotiates: the credential JSON is one ATT write
    instances: ClassVar[list[FakeClient]] = []
    torn: ClassVar[set[tuple[str, str]]] = set()  # windows that arrive cut short

    def __init__(self, device: Any, timeout: float | None = None) -> None:  # noqa: ARG002
        self.device = device
        self.writes: list[tuple[str, bytes]] = []
        self.notify: dict[str, Any] = {}
        self.state = 0x14
        self.pages: dict[str, int] = {}
        FakeClient.instances.append(self)

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def start_notify(self, char: str, callback: Any) -> None:
        self.notify[char] = callback

    async def write_gatt_char(self, char: str, data: bytes, response: bool = True) -> None:  # noqa: ARG002
        self.writes.append((char, bytes(data)))
        if char == ble.CHAR_SETTINGS:
            self.state = ble.STATE_SCAN_DONE
        elif char == ble.CHAR_CONNECT:
            self.state = 0x3C
            self.notify[ble.CHAR_STATE](None, bytearray([0x1E, 0x3C]))
        else:
            self.pages[char] = int.from_bytes(data, "little")

    async def read_gatt_char(self, char: str) -> bytearray:
        if char == ble.CHAR_STATE:
            return bytearray([self.state])
        if char == ble.CHAR_IP:
            return bytearray(b"\xc0\xa8\x02\x03")
        windows = {ble.WINDOW_SCAN: json.dumps(SCAN_RESULTS).encode(), ble.WINDOW_CRYPTO: crypto_bundle()}
        for pair, payload in windows.items():
            data_char, control_char = pair
            if char == data_char:
                blob = gzip.compress(payload)
                if pair in self.torn:
                    blob = blob[: len(blob) // 2]
                return bytearray(blob if self.pages.get(control_char, 0) == 0 else b"")
        raise AssertionError(f"unexpected read of {char}")


class FakeScanner:
    advertisement = SimpleNamespace(
        local_name="Stadia Setup", service_data={ble.FAST_PAIR_SERVICE_DATA_UUID: b"\x7a\xc0\x01"}
    )
    device = SimpleNamespace(address="11111111-2222-3333-4444-555555555555", name="Stadia Setup")

    @classmethod
    async def find_device_by_filter(cls, match: Any, timeout: float = 0) -> Any:  # noqa: ARG003
        return cls.device if match(cls.device, cls.advertisement) else None


@pytest.fixture
def fake_ble(monkeypatch):
    async def instant_sleep(_seconds: float) -> None:
        await real_sleep(0)

    real_sleep = asyncio.sleep
    monkeypatch.setattr(ble.asyncio, "sleep", instant_sleep)
    monkeypatch.setattr(ble, "BleakClient", FakeClient)
    monkeypatch.setattr(ble, "BleakScanner", FakeScanner)
    monkeypatch.setattr(FakeClient, "torn", set())
    FakeClient.instances.clear()
    return FakeClient


def test_provision_scans_copies_the_security_enums_and_writes_encrypted_credentials(fake_ble, capsys):
    asyncio.run(ble.provision(FakeScanner.device.address, "hotspot", "hunter2", wait=0.01))
    (client,) = fake_ble.instances
    chars = [c for c, _ in client.writes]
    assert chars[0] == ble.WINDOW_CRYPTO[1]  # the key read comes before the Wi-Fi rescan
    assert ble.CHAR_SETTINGS in chars  # the scan is still triggered, after the key
    assert chars[-1] == ble.CHAR_CONNECT
    payload = json.loads(client.writes[-1][1])
    assert payload["ssid"] == "hotspot"
    assert (payload["wpa_auth"], payload["wpa_cipher"]) == (5, 3)  # the device's own scan entry, not a guess
    assert (payload["bssid"], payload["frequency"]) == ("aa:bb:cc:dd:ee:ff", 2437)
    assert PRIVATE_KEY.decrypt(base64.b64decode(payload["enc_passwd"]), padding.PKCS1v15()) == b"hunter2"
    out = capsys.readouterr().out
    assert "wpa_auth=5, wpa_cipher=3" in out
    assert "<redacted>" in out and "hunter2" not in out
    assert "STATE -> 0x3c (connected)" in out
    assert "Controller IP: 192.168.2.3" in out


def test_provision_matches_by_service_data_and_overrides(fake_ble):
    asyncio.run(
        ble.provision(
            "unknown-address", "hotspot", None, wpa_auth=1, wpa_cipher=1, service_data_match="7AC0", wait=0.01
        )
    )
    (client,) = fake_ble.instances
    payload = json.loads(client.writes[-1][1])
    assert (payload["wpa_auth"], payload["wpa_cipher"]) == (1, 1)
    assert "enc_passwd" not in payload  # an open network: nothing to encrypt, no key read
    assert not any(c == ble.WINDOW_CRYPTO[1] for c, _ in client.writes)


def test_provision_skip_scan_never_touches_the_scan_and_drops_bssid(fake_ble):
    asyncio.run(
        ble.provision(
            FakeScanner.device.address, "hotspot", "hunter2", wpa_auth=7, wpa_cipher=4, wait=0.01, skip_scan=True
        )
    )
    (client,) = fake_ble.instances
    chars = [c for c, _ in client.writes]
    assert ble.CHAR_SETTINGS not in chars  # no settings write: no Wi-Fi scan, the link survives
    assert ble.WINDOW_SCAN[1] not in chars
    payload = json.loads(client.writes[-1][1])
    assert (payload["wpa_auth"], payload["wpa_cipher"]) == (7, 4)  # supplied, not scan-copied
    assert "bssid" not in payload and "frequency" not in payload  # the controller joins without them
    assert PRIVATE_KEY.decrypt(base64.b64decode(payload["enc_passwd"]), padding.PKCS1v15()) == b"hunter2"


@pytest.mark.usefixtures("fake_ble")
def test_provision_skip_scan_demands_the_security_enums():
    with pytest.raises(ble.ProvisioningError, match="--skip-scan needs"):
        asyncio.run(ble.provision(FakeScanner.device.address, "hotspot", "x", wait=0.01, skip_scan=True))


@pytest.mark.usefixtures("fake_ble")
def test_provision_errors_are_reported_not_raised_as_tracebacks():
    with pytest.raises(ble.ProvisioningError, match="not advertising"):
        asyncio.run(ble.provision("unknown-address", "hotspot", "x", wait=0.01))
    with pytest.raises(ble.ProvisioningError, match="not found in scan results"):
        asyncio.run(ble.provision(FakeScanner.device.address, "elsewhere", "x", wait=0.01))


@pytest.mark.parametrize(("window", "what"), [(ble.WINDOW_CRYPTO, "public key"), (ble.WINDOW_SCAN, "scan results")])
def test_provision_reports_a_window_cut_short_and_writes_nothing(fake_ble, window, what):
    fake_ble.torn = {window}
    with pytest.raises(ble.ProvisioningError, match=f"{what} could not be read"):
        asyncio.run(ble.provision(FakeScanner.device.address, "hotspot", "hunter2", wait=0.01))
    (client,) = fake_ble.instances
    assert ble.CHAR_CONNECT not in [c for c, _ in client.writes]
