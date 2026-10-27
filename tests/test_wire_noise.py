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
