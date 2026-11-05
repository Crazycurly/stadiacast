"""stadiacast.certs: the lab certificate, the start-up check and the clock pin."""

from __future__ import annotations

import datetime
import itertools
import os
import ssl

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization

from stadiacast import certs
from stadiacast.certs import (
    GOOGLE_SANS,
    create_self_signed,
    describe_certificate,
    ensure_certificate,
    load_credential,
    pack_credential,
    resolve_clock_pin,
)
from stadiacast.config import Config
from stadiacast.log import EventLog


def test_self_signed_pair_covers_the_google_names_and_keeps_the_key_private(tmp_path):
    cert_path, key_path = str(tmp_path / "sub" / "lab.crt"), str(tmp_path / "sub" / "lab.key")
    create_self_signed(cert_path, key_path, days=10)
    with open(cert_path, "rb") as fh:
        cert = x509.load_pem_x509_certificate(fh.read())
    sans = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
    assert sans == GOOGLE_SANS
    assert cert.issuer == cert.subject
    assert (cert.not_valid_after_utc - cert.not_valid_before_utc).days == 11
    assert oct(os.stat(key_path).st_mode & 0o777) == "0o600"


def test_ensure_certificate_creates_a_lab_pair_only_when_asked(tmp_path):
    log = EventLog(None, echo=False)
    cfg = Config(cert=str(tmp_path / "c.pem"), key=str(tmp_path / "k.pem"), cert_given=True)
    with pytest.raises(FileNotFoundError, match="--self-signed"):
        ensure_certificate(cfg, log)
    cfg.self_signed = True
    ensure_certificate(cfg, log)
    assert os.path.exists(cfg.cert) and os.path.exists(cfg.key)
    assert log.counts == {"self_signed_certificate_created": 1}
    ensure_certificate(cfg, log)  # already there: nothing happens
    assert log.counts == {"self_signed_certificate_created": 1}


def test_clock_pin_dates_and_the_certificate_midpoint(tmp_path):
    cfg = Config(cert=str(tmp_path / "c.pem"), key=str(tmp_path / "k.pem"), self_signed=True)
    cfg.clock_pin = "2026-06-01T12:00:00Z"
    noon = datetime.datetime(2026, 6, 1, 12, tzinfo=datetime.UTC).timestamp()
    assert resolve_clock_pin(cfg) == noon
    cfg.clock_pin = "2026-06-01T12:00:00"  # naive = UTC
    assert resolve_clock_pin(cfg) == noon
    cfg.clock_pin = "2026-06-01T14:00:00+02:00"
    assert resolve_clock_pin(cfg) == noon
    cfg.clock_pin = "not a date"
    with pytest.raises(ValueError):
        resolve_clock_pin(cfg)
    create_self_signed(cfg.cert, cfg.key, days=20)
    cfg.clock_pin = "auto"
    with open(cfg.cert, "rb") as fh:
        cert = x509.load_pem_x509_certificate(fh.read())
    lo, hi = cert.not_valid_before_utc.timestamp(), cert.not_valid_after_utc.timestamp()
    assert abs(resolve_clock_pin(cfg) - (lo + hi) / 2) < 1


def _packed_lab_credential(directory) -> bytes:
    cert_path, key_path = str(directory / "lab.crt"), str(directory / "lab.key")
    create_self_signed(cert_path, key_path, days=10)
    with open(cert_path, "rb") as cert_fh, open(key_path, "rb") as key_fh:
        return pack_credential(cert_fh.read(), key_fh.read())


def test_pack_credential_round_trips_without_pem_markers(tmp_path):
    blob = _packed_lab_credential(tmp_path / "one")
    assert b"PRIVATE KEY" not in blob and b"BEGIN" not in blob
    key, chain = load_credential(blob)
    assert len(chain) == 1
    spki = (serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    assert key.public_key().public_bytes(*spki) == chain[0].public_key().public_bytes(*spki)
    create_self_signed(str(tmp_path / "two.crt"), str(tmp_path / "two.key"), days=10)
    with (
        open(tmp_path / "one" / "lab.crt", "rb") as cert_fh,
        open(tmp_path / "two.key", "rb") as key_fh,
        pytest.raises(ValueError, match="does not match"),
    ):
        pack_credential(cert_fh.read(), key_fh.read())


def test_builtin_credential_is_the_default_when_nothing_is_configured(tmp_path, monkeypatch):
    blob_path = tmp_path / "identity.bin"
    blob_path.write_bytes(_packed_lab_credential(tmp_path / "src"))
    monkeypatch.setattr(certs, "BUILTIN_CREDENTIAL", str(blob_path))
    log = EventLog(None, echo=False)
    missing_cert, missing_key = str(tmp_path / "missing" / "c.pem"), str(tmp_path / "missing" / "k.pem")

    cfg = Config(cert=missing_cert, key=missing_key)
    ensure_certificate(cfg, log)
    assert cfg.cert != missing_cert and os.path.exists(cfg.cert) and os.path.exists(cfg.key)
    assert oct(os.stat(cfg.key).st_mode & 0o777) == "0o600"
    assert log.counts == {"builtin_credential": 1}
    assert log.records[-1]["subject"] == "cloudcast-pa.googleapis.com" and log.records[-1]["chain"] == 1
    ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(cfg.cert, cfg.key)

    explicit = Config(cert=missing_cert, key=missing_key, cert_given=True)
    with pytest.raises(FileNotFoundError, match="--cert"):
        ensure_certificate(explicit, log)

    monkeypatch.setattr(certs, "BUILTIN_CREDENTIAL", str(tmp_path / "absent.bin"))
    with pytest.raises(FileNotFoundError, match="--self-signed"):
        ensure_certificate(Config(cert=missing_cert, key=missing_key), log)


def test_shipped_credential_is_a_signed_chain_to_a_gts_root_for_its_own_key():
    with open(certs.BUILTIN_CREDENTIAL, "rb") as fh:
        blob = fh.read()
    assert b"PRIVATE KEY" not in blob and b"BEGIN" not in blob
    key, chain = load_credential(blob)
    spki = (serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    assert key.public_key().public_bytes(*spki) == chain[0].public_key().public_bytes(*spki)
    for cert, issuer in itertools.pairwise(chain):
        cert.verify_directly_issued_by(issuer)  # raises when the chain is not contiguous or not signed
    issuers = [describe_certificate(cert)["issuer"] for cert in chain]
    assert any(name.startswith("GTS Root R") for name in issuers), issuers
    assert describe_certificate(chain[0])["subject"] not in GOOGLE_SANS  # issued to its own name, not Google's
