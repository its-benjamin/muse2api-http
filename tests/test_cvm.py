from __future__ import annotations

import base64
import hashlib
import hmac
import os

import pytest

from muse2api.cvm import (
    CLIENT_PROOF_DOMAIN,
    CLIENT_PROOF_KEY_LABEL,
    PROOF_KEY_DOMAIN,
    STATE_PROVISIONED,
    STATE_UNPROVISIONED,
    CvmError,
    build_message3,
    create_vm_proof,
    decode_challenge,
    parse_rv_key,
)


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        part = value & 0x7F
        value >>= 7
        if value:
            out.append(part | 0x80)
        else:
            out.append(part)
            return bytes(out)


def _field(number: int, payload: bytes) -> bytes:
    return _varint((number << 3) | 2) + _varint(len(payload)) + payload


def _state(value: int) -> bytes:
    return _varint(2 << 3) + _varint(value)


def challenge_payload(state: int, *, vm_proof: bytes | None = None) -> bytes:
    payload = _field(1, b"attestation-blob") + _state(state) + _field(3, b"v" * 32)
    if vm_proof is not None:
        payload += _field(4, vm_proof)
    return payload


def test_decode_standard_empty_payload():
    assert decode_challenge(b"") is None


def test_decode_unprovisioned_and_provisioned():
    unprovisioned = decode_challenge(challenge_payload(STATE_UNPROVISIONED))
    assert unprovisioned is not None and unprovisioned.state == STATE_UNPROVISIONED
    provisioned = decode_challenge(challenge_payload(STATE_PROVISIONED, vm_proof=b"p" * 32))
    assert provisioned is not None and provisioned.provisioned
    assert provisioned.vm_proof == b"p" * 32


def test_rejects_malformed_shapes():
    assert decode_challenge(_field(1, b"x")) is None  # no nonce/state
    assert decode_challenge(challenge_payload(STATE_PROVISIONED)) is None  # missing proof
    assert decode_challenge(challenge_payload(STATE_UNPROVISIONED, vm_proof=b"p" * 32)) is None


def test_fresh_key_requires_accepted_attestation():
    challenge = decode_challenge(challenge_payload(STATE_UNPROVISIONED))
    key = os.urandom(32)
    with pytest.raises(CvmError, match="attestation"):
        build_message3(challenge, key, "notary", b"1" * 32, b"2" * 32, b"3" * 32, None)
    message = build_message3(challenge, key, "notary", b"1" * 32, b"2" * 32, b"3" * 32,
                             "accepted_chip_pinned")
    assert b"notary" in message and key in message


def test_provisioned_proof_roundtrip():
    rv_key = os.urandom(32)
    m1, m2, client_nonce = os.urandom(32), os.urandom(32), os.urandom(32)
    vm_nonce = b"n" * 32
    vm_proof = create_vm_proof(rv_key, m1, vm_nonce, client_nonce)
    payload = _field(1, b"attestation") + _state(STATE_PROVISIONED) + _field(3, vm_nonce) + _field(4, vm_proof)
    challenge = decode_challenge(payload)
    message3 = build_message3(challenge, rv_key, "notary", m1, m2, client_nonce, "accepted")

    assert hmac.compare_digest(vm_proof, create_vm_proof(rv_key, m1, vm_nonce, client_nonce))
    expected_client = hmac.new(
        hmac.new(rv_key, PROOF_KEY_DOMAIN + CLIENT_PROOF_KEY_LABEL, hashlib.sha256).digest(),
        CLIENT_PROOF_DOMAIN + m2 + vm_nonce + client_nonce,
        hashlib.sha256,
    ).digest()
    assert expected_client in message3


def test_provisioned_wrong_key_fails_closed():
    challenge = decode_challenge(challenge_payload(STATE_PROVISIONED, vm_proof=b"p" * 32))
    with pytest.raises(CvmError, match="proof mismatch"):
        build_message3(challenge, os.urandom(32), "notary", b"1" * 32, b"2" * 32, b"3" * 32,
                       "accepted")


def test_parse_rv_key_hex_and_base64():
    key = os.urandom(32)
    assert parse_rv_key(key.hex()) == key
    assert parse_rv_key(base64.b64encode(key).decode()) == key
    with pytest.raises(CvmError):
        parse_rv_key("short")
