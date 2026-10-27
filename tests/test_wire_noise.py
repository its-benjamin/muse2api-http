from __future__ import annotations

import pytest

from muse2api.wire import (
    MAX_NOISE_CHUNK_PAYLOAD,
    NoiseChunk,
    NoiseReassembler,
    split_noise_payload,
)


def test_split_payload_roundtrips_through_reassembler():
    payload = b"x" * (300 * 1024)
    frames = split_noise_payload(payload, 48 * 1024)
    assert len(frames) > 1
    reassembler = NoiseReassembler()
    out = None
    for frame in frames:
        out = reassembler.feed(frame)
    assert out == payload


def test_single_large_gateway_chunk_is_accepted():
    # The gateway sends approval/permission payloads as one big Noise chunk.
    payload = b"y" * (256 * 1024)
    out = NoiseReassembler().feed(NoiseChunk(7, 0, 1, payload).encode())
    assert out == payload


def test_oversized_single_chunk_is_rejected():
    payload = b"z" * (MAX_NOISE_CHUNK_PAYLOAD + 1)
    with pytest.raises(ValueError, match="payload too large"):
        NoiseReassembler().feed(NoiseChunk(7, 0, 1, payload).encode())


def test_reassembler_evicts_abandoned_streams_to_bound_memory():
    reassembler = NoiseReassembler()
    # Feed 35 incomplete chunk streams (each sends 1 of 2 chunks)
    for cid in range(1, 36):
        out = reassembler.feed(NoiseChunk(cid, 0, 2, b"part1").encode())
        assert out is None
    # Verify pending dict is bounded to 32 entries max
    assert len(reassembler._pending) == 32
    # Verify oldest streams (1, 2, 3) were evicted
    assert 1 not in reassembler._pending
    assert 2 not in reassembler._pending
    assert 3 not in reassembler._pending
    assert 35 in reassembler._pending


def test_stream_parse_handles_split_json_and_trailing_objects():
    from muse2api.gateway import _Stream

    stream = _Stream(1)
    # Feed partial chunk 1
    stream._parse(b'{"key": "val')
    assert stream.queue.empty()
    # Feed rest of chunk 1 + newline and trailing object
    stream._parse(b'ue"}\n{"second": 123}')
    assert stream.queue.get_nowait() == {"key": "value"}
    assert stream.queue.get_nowait() == {"second": 123}
    assert stream.queue.empty()
