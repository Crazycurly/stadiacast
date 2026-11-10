"""DNS wire format (RFC 1035): enough to parse queries and answers and to build redirect responses."""

from __future__ import annotations

import socket
import struct
from typing import Any

QTYPES = {
    1: "A",
    2: "NS",
    5: "CNAME",
    6: "SOA",
    12: "PTR",
    16: "TXT",
    28: "AAAA",
    33: "SRV",
    41: "OPT",
    47: "NSEC",
    65: "HTTPS",
    255: "ANY",
}

RCODE_FORMERR = 1
RCODE_SERVFAIL = 2
RCODE_NXDOMAIN = 3


def qtype_name(t: int) -> str:
    return QTYPES.get(t, str(t))


#: RFC 1035's limits on a name: a label of 63 bytes, 255 bytes in all.  Without them a few
#: compression pointers expand a small message into megabytes of names.
MAX_LABEL = 63
MAX_NAME = 255
MAX_POINTERS = 10


def parse_name(buf: bytes, off: int) -> tuple[str, int]:
    """Decode a (possibly compressed) domain name at ``off``; returns ``(name, offset after it)``.
    A name that breaks the limits above or runs past the message is a ``ValueError``."""
    labels: list[str] = []
    after: int | None = None  # where the name ends in the message: behind its first pointer
    size = pointers = 0
    while True:
        if off >= len(buf):
            raise ValueError("name runs past end")
        length = buf[off]
        if length == 0:
            off += 1
            break
        if length & 0xC0 == 0xC0:
            pointers += 1
            if pointers > MAX_POINTERS:
                raise ValueError("compression loop")
            if off + 1 >= len(buf):
                raise ValueError("name runs past end")
            if after is None:
                after = off + 2
            off = ((length & 0x3F) << 8) | buf[off + 1]
            continue
        size += 1 + length
        if length > MAX_LABEL or size > MAX_NAME:
            raise ValueError("name too long")
        off += 1
        if off + length > len(buf):
            raise ValueError("name runs past end")
        labels.append(buf[off : off + length].decode("ascii", "replace"))
        off += length
    return ".".join(labels), (after if after is not None else off)


def parse_rr(buf: bytes, off: int) -> tuple[dict[str, Any], int]:
    """Decode one resource record; returns ``(record, offset after it)``."""
    name, off = parse_name(buf, off)
    rtype, rclass, ttl, rdlen = struct.unpack_from("!HHIH", buf, off)
    off += 10
    rdata = buf[off : off + rdlen]
    rr: dict[str, Any] = {"name": name, "type": qtype_name(rtype), "class": rclass & 0x7FFF, "ttl": ttl}
    try:
        if rtype == 1 and rdlen == 4:
            rr["data"] = socket.inet_ntop(socket.AF_INET, rdata)
        elif rtype == 28 and rdlen == 16:
            rr["data"] = socket.inet_ntop(socket.AF_INET6, rdata)
        elif rtype in (5, 12, 2):
            rr["data"] = parse_name(buf, off)[0]
        elif rtype == 33:
            pri, wt, port = struct.unpack_from("!HHH", rdata, 0)
            rr["data"] = {"priority": pri, "weight": wt, "port": port, "target": parse_name(buf, off + 6)[0]}
        elif rtype == 16:
            txts, p = [], 0
            while p < len(rdata):
                n = rdata[p]
                txts.append(rdata[p + 1 : p + 1 + n].decode("utf-8", "replace"))
                p += 1 + n
            rr["data"] = txts
        else:
            rr["data"] = rdata.hex()
    except Exception as e:  # keep going on odd records
        rr["data"] = f"<parse error {e}> {rdata.hex()}"
    return rr, off + rdlen


def parse_question(buf: bytes, off: int) -> tuple[dict[str, Any], int]:
    """Decode one question; returns ``(question, offset after it)``."""
    name, off = parse_name(buf, off)
    qtype, qclass = struct.unpack_from("!HH", buf, off)
    question = {
        "name": name,
        "type": qtype_name(qtype),
        "qtype": qtype,
        "class": qclass & 0x7FFF,
        "unicast_response": bool(qclass & 0x8000),
    }
    return question, off + 4


def parse_query(buf: bytes) -> dict[str, Any]:
    """The header and the first question of a query, which is all a server answers (a query has
    one question, RFC 9619): whatever else a message claims to hold is not walked."""
    ident, flags, qd = struct.unpack_from("!HHH", buf, 0)
    return {
        "id": ident,
        "flags": flags,
        "qr": bool(flags & 0x8000),
        "questions": [parse_question(buf, 12)[0]] if qd else [],
    }


def parse_message(buf: bytes) -> dict[str, Any]:
    """Decode a whole DNS message into a dict (header flags, questions, answers, authority, additional)."""
    (ident, flags, qd, an, ns, ar) = struct.unpack_from("!HHHHHH", buf, 0)
    off = 12
    msg: dict[str, Any] = {
        "id": ident,
        "flags": flags,
        "qr": bool(flags & 0x8000),
        "rcode": flags & 0xF,
        "questions": [],
        "answers": [],
        "authority": [],
        "additional": [],
    }
    for _ in range(qd):
        question, off = parse_question(buf, off)
        msg["questions"].append(question)
    for section, count in (("answers", an), ("authority", ns), ("additional", ar)):
        for _ in range(count):
            rr, off = parse_rr(buf, off)
            msg[section].append(rr)
    return msg


def build_a_response(query: bytes, ip: str | None, ttl: int = 30) -> bytes:
    """Answer the first question of ``query`` with one A record, or with an empty NOERROR answer
    when ``ip`` is None (used for AAAA so clients fall back to A)."""
    ident = query[:2]
    _, off = parse_name(query, 12)
    qend = off + 4
    question = query[12:qend]
    flags = 0x8180  # QR, RD, RA
    if ip is None:
        return ident + struct.pack("!HHHHH", flags, 1, 0, 0, 0) + question
    header = ident + struct.pack("!HHHHH", flags, 1, 1, 0, 0)
    answer = b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, ttl, 4) + socket.inet_aton(ip)
    return header + question + answer


def build_rcode_response(query: bytes, rcode: int) -> bytes:
    """An answer with only a response code (FORMERR, SERVFAIL, NXDOMAIN) and the echoed question."""
    ident = query[:2]
    try:
        _, off = parse_name(query, 12)
        question = query[12 : off + 4]
    except (ValueError, IndexError):
        question = b""
    return ident + struct.pack("!HHHHH", 0x8180 | rcode, 1 if question else 0, 0, 0, 0) + question
