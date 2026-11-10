#!/usr/bin/env python3
"""Pack a PEM certificate chain and its key into the credential bundle the receiver embeds.

    uv run tools/pack_credential.py --chain fullchain.pem --key privkey.pem --out identity.bin

It imports the package, so it runs from a source checkout with the project's environment:
``uv run`` as above, or ``.venv/bin/python tools/pack_credential.py``.

The output is an unencrypted PKCS#12 bundle (``openssl pkcs12 -in identity.bin -nodes -passin pass:``
reads it).  The receiver's own copy ships inside the package; a fork that wants to embed a
different credential rebuilds that file with this tool, and a deployment with its own
certificate simply passes ``--cert``/``--key`` instead.
"""

from __future__ import annotations

import argparse
import os
import sys

from stadiacast.certs import describe_certificate, load_credential, pack_credential


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--chain", required=True, help="PEM chain, leaf first (certbot's fullchain.pem)")
    ap.add_argument("--key", required=True, help="PEM private key of the leaf")
    ap.add_argument("--out", required=True, help="where to write the bundle")
    a = ap.parse_args(argv)
    with open(a.chain, "rb") as fh:
        chain_pem = fh.read()
    with open(a.key, "rb") as fh:
        key_pem = fh.read()
    blob = pack_credential(chain_pem, key_pem)
    _, chain = load_credential(blob)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)) or ".", exist_ok=True)
    with open(a.out, "wb") as fh:
        fh.write(blob)
    info = describe_certificate(chain[0])
    print(
        f"{a.out}: {len(blob)} bytes, {len(chain)} certificates, subject {info['subject']}, "
        f"issuer {info['issuer']}, serial {info['serial']}, expires {info['not_after']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
