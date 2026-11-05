"""TLS certificate handling: the built-in credential, your own chain, a lab pair and the clock pin.

The controller validates the server chain against its built-in Google Trust Services roots and
checks neither the hostname, nor the validity dates, nor revocation (all confirmed on a real
controller), so any GTS-issued chain works for every name it connects to and never needs
renewing.  The receiver ships one such chain with its key, the *built-in credential*: a
certificate GTS issued for a domain registered for this project and revoked at our request
before the key was published, which makes it useless to every ordinary TLS client and exactly
enough for the controller.  ``--cert``/``--key``, or a pair under ``certs/``, replace it with
your own; see ``docs/setup.md``.
"""

from __future__ import annotations

import atexit
import datetime
import os
import shutil
import tempfile

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.asymmetric.types import PrivateKeyTypes
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID

from stadiacast import SERVER_NAME
from stadiacast.config import Config
from stadiacast.log import EventLog

#: Subject alternative names of the lab (self-signed) certificate.
GOOGLE_SANS = [
    "*.googleapis.com",
    "googleapis.com",
    "*.sandbox.googleapis.com",
    "*.google.com",
    "*.gstatic.com",
    "localhost",
]


#: The built-in credential: an unencrypted PKCS#12 bundle (leaf, chain, key) shipped with the package.
BUILTIN_CREDENTIAL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "identity.bin")


def resolve_clock_pin(cfg: Config) -> float:
    """The Unix epoch to pin the controller's clock to.

    ``"auto"`` is the midpoint of the served certificate's validity window; anything else is an
    ISO 8601 date-time (naive values are UTC).  The controller sets its clock from our ``Date``
    header and ``GetCurrentTime`` replies and judges the certificate's expiry against it.
    """
    pin = cfg.clock_pin or ""
    if pin == "auto":
        with open(cfg.cert, "rb") as fh:
            cert = x509.load_pem_x509_certificate(fh.read())
        lo = cert.not_valid_before_utc.timestamp()
        hi = cert.not_valid_after_utc.timestamp()
        if not hi > lo:
            raise ValueError(f"certificate has no validity window: {cfg.cert}")
        return lo + (hi - lo) / 2
    dt = datetime.datetime.fromisoformat(pin)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.UTC)
    return dt.timestamp()


def ensure_certificate(cfg: Config, log: EventLog) -> None:
    """Make sure ``cfg.cert`` / ``cfg.key`` exist before the HTTPS listener starts.

    Existing files win.  Otherwise ``cfg.self_signed`` creates a lab pair, and a configuration
    that named no files (``cfg.cert_given`` false) falls back to the built-in credential, which
    is unpacked into a private temporary directory that ``cfg.cert`` / ``cfg.key`` are pointed
    at.  Anything else raises :class:`FileNotFoundError`.
    """
    if os.path.exists(cfg.cert) and os.path.exists(cfg.key):
        return
    if cfg.self_signed:
        create_self_signed(cfg.cert, cfg.key)
        log(
            "self_signed_certificate_created",
            cert=cfg.cert,
            key=cfg.key,
            note="the controller rejects this with TLS alert unknown_ca; use a GTS-issued certificate for the device",
        )
        return
    if not cfg.cert_given and os.path.exists(BUILTIN_CREDENTIAL):
        with open(BUILTIN_CREDENTIAL, "rb") as fh:
            key, chain = load_credential(fh.read())
        cfg.cert, cfg.key = materialize_credential(key, chain)
        info = describe_certificate(chain[0])
        log(
            "builtin_credential",
            subject=info["subject"],
            issuer=info["issuer"],
            serial=info["serial"],
            not_after=info["not_after"],
            chain=len(chain),
            note="a Google Trust Services chain revoked before publication; the controller checks only the chain",
        )
        return
    raise FileNotFoundError(
        f"certificate/key not found: {cfg.cert} / {cfg.key} "
        "(use --cert/--key, put the pair under certs/, or --self-signed for a lab pair)"
    )


def create_self_signed(cert_path: str, key_path: str, days: int = 3650) -> None:
    """Write an ECDSA P-256 self-signed certificate for the Google names (tests and the fake controller)."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "cloudcast-pa.googleapis.com"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, f"{SERVER_NAME} (self-signed)"),
        ]
    )
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=days))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in GOOGLE_SANS]), critical=False)
        .sign(key, hashes.SHA256())
    )
    os.makedirs(os.path.dirname(os.path.abspath(cert_path)) or ".", exist_ok=True)
    with open(cert_path, "wb") as fh:
        fh.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key_path, "wb") as fh:
        fh.write(
            key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
            )
        )
    os.chmod(key_path, 0o600)


def pack_credential(chain_pem: bytes, key_pem: bytes) -> bytes:
    """Bundle a PEM chain (leaf first) and its key as an unencrypted PKCS#12 blob.

    This is the format of the built-in credential; ``tools/pack_credential.py`` writes it and
    ``openssl pkcs12 -in FILE -nodes -passin pass:`` reads it.
    """
    chain = x509.load_pem_x509_certificates(chain_pem)
    key = serialization.load_pem_private_key(key_pem, password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey | rsa.RSAPrivateKey):
        raise ValueError("the key must be an EC or RSA key")
    spki = (serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    if key.public_key().public_bytes(*spki) != chain[0].public_key().public_bytes(*spki):
        raise ValueError("the key does not match the leaf certificate")
    return pkcs12.serialize_key_and_certificates(None, key, chain[0], chain[1:] or None, serialization.NoEncryption())


def load_credential(blob: bytes) -> tuple[PrivateKeyTypes, list[x509.Certificate]]:
    """The key and the chain (leaf first) of a credential written by :func:`pack_credential`."""
    key, leaf, extra = pkcs12.load_key_and_certificates(blob, None)
    if key is None or leaf is None:
        raise ValueError("the credential holds no key or no certificate")
    return key, [leaf, *extra]


def materialize_credential(key: PrivateKeyTypes, chain: list[x509.Certificate]) -> tuple[str, str]:
    """Write a credential as PEM files in a private temporary directory, removed at exit.

    ``ssl.SSLContext.load_cert_chain`` wants paths, so the built-in credential is unpacked once
    per process: the chain as ``chain.pem`` and the key as ``key.pem`` (mode 0600).
    """
    tmp = tempfile.mkdtemp(prefix="stadiacast-")
    atexit.register(shutil.rmtree, tmp, True)
    cert_path, key_path = os.path.join(tmp, "chain.pem"), os.path.join(tmp, "key.pem")
    with open(cert_path, "wb") as fh:
        fh.write(b"".join(c.public_bytes(serialization.Encoding.PEM) for c in chain))
    with os.fdopen(os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as fh:
        fh.write(
            key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
            )
        )
    return cert_path, key_path


def describe_certificate(cert: x509.Certificate) -> dict[str, str]:
    """Subject, issuer, serial and expiry of a certificate, for the log."""

    def cn(name: x509.Name) -> str:
        attrs = name.get_attributes_for_oid(NameOID.COMMON_NAME)
        return str(attrs[0].value) if attrs else name.rfc4514_string()

    return {
        "subject": cn(cert.subject),
        "issuer": cn(cert.issuer),
        "serial": f"{cert.serial_number:x}",
        "not_after": cert.not_valid_after_utc.isoformat(),
    }
