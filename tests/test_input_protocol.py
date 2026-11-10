"""stadiacast.input_protocol: the compressed form of an input series."""

from __future__ import annotations

import gzip
import time
import zlib

import pytest

from stadiacast.input_protocol import MAX_SERIES_BYTES, InputDecoder, decompress_series
from stadiacast.protos import Protos


def raw_deflate(data: bytes) -> bytes:
    deflater = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    return deflater.compress(data) + deflater.flush()


@pytest.mark.parametrize("compress", [gzip.compress, zlib.compress, raw_deflate], ids=["gzip", "zlib", "raw deflate"])
def test_a_series_decompresses_in_each_framing(compress):
    assert decompress_series(compress(b"series" * 50)) == b"series" * 50
    assert decompress_series(compress(b"x" * MAX_SERIES_BYTES)) == b"x" * MAX_SERIES_BYTES


@pytest.mark.parametrize("compress", [gzip.compress, zlib.compress, raw_deflate], ids=["gzip", "zlib", "raw deflate"])
def test_a_series_that_expands_beyond_the_limit_is_refused_without_inflating_it(compress):
    bomb = compress(b"\0" * (64 * 1024 * 1024))  # 64 MiB in about 64 KiB
    started = time.perf_counter()
    with pytest.raises(ValueError, match="expands beyond"):
        decompress_series(bomb)
    assert time.perf_counter() - started < 0.5
    with pytest.raises(ValueError, match="expands beyond"):
        decompress_series(compress(b"x" * (MAX_SERIES_BYTES + 1)))


@pytest.mark.parametrize("data", [b"", b"not compressed", gzip.compress(b"series" * 50)[:-6], zlib.compress(b"s")[:-2]])
def test_what_is_not_a_whole_compressed_stream_is_a_value_error(data):
    with pytest.raises(ValueError, match="neither zlib/gzip nor raw deflate"):
        decompress_series(data)


def test_the_decoder_reads_a_compressed_series_and_refuses_an_oversized_one():
    protos = Protos()
    series = protos.InputMessageSeries()
    series.session_id, series.sequence_number = "seven", 3
    plain = series.SerializeToString()
    msg = protos.InputProtocolMessage()
    msg.compressed_series, msg.uncompressed_size = gzip.compress(plain), len(plain)
    out = InputDecoder(protos).decode(msg.SerializeToString())
    assert (out["compressed"], out["session_id"], out["sequence_number"]) == (True, "seven", 3)
    msg.compressed_series = gzip.compress(b"\0" * (MAX_SERIES_BYTES + 1))
    with pytest.raises(ValueError, match="expands beyond"):
        InputDecoder(protos).decode(msg.SerializeToString())
