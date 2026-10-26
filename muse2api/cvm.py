"""Confidential-VM (RV) handshake support.

Standard `hatch` VMs send an empty Noise message 2 and use an empty message 3.
Confidential VMs instead put a `hatch.noise.NoiseHandshakeMessage2` challenge in
message 2 and require a notarised message 3 carrying either the fresh RV key or
an HMAC proof of the recovery key. This module ports the client side of
`noiseRvAuth.ts` byte-for-byte.

Policy: fail closed. A challenge without a configured RV key, without a
successful attestation verdict, or with a mismatched VM proof is an error, never
a silent downgrade to the standard handshake.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import shlex
import subprocess
from dataclasses import dataclass
from typing import Any

PROOF_KEY_DOMAIN = b"hatch.noise-auth.key.v1"
VM_PROOF_KEY_LABEL = b"vm-proof-key"
CLIENT_PROOF_KEY_LABEL = b"client-proof-key"
VM_PROOF_DOMAIN = b"hatch.noise-auth.vm-proof.v1"
CLIENT_PROOF_DOMAIN = b"hatch.noise-auth.client-proof.v1"
CLIENT_NONCE_LEN = 32
STATE_UNPROVISIONED = 1
STATE_PROVISIONED = 2


class CvmError(Exception):
    pass


@dataclass(slots=True)
class RvChallenge:
    state: int
    attestation: bytes
    vm_nonce: bytes
    vm_proof: bytes | None = None
    chip_binding: bytes | None = None

    @property
    def provisioned(self) -> bool:
        return self.state == STATE_PROVISIONED


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if offset >= len(data) or shift > 63:
            raise CvmError("truncated protobuf varint")
        byte = data[offset]
        offset += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, offset
        shift += 7


def decode_challenge(payload: bytes) -> RvChallenge | None:
    """Decode the strict RV challenge shape; return None for any other payload."""
    offset = 0
    attestation: bytes | None = None
    state: int | None = None
    vm_nonce: bytes | None = None
    vm_proof: bytes | None = None
    chip_binding: bytes | None = None
    while offset < len(payload):
        tag, offset = _read_varint(payload, offset)
        field, wire = tag >> 3, tag & 7
        if field == 0:
            return None
        if wire == 0:
            value, offset = _read_varint(payload, offset)
            if field != 2 or state is not None:
                return None
            state = value
            continue
        if wire == 2:
            length, offset = _read_varint(payload, offset)
            if offset + length > len(payload):
                return None
            value = payload[offset:offset + length]
            offset += length
            if field == 1 and attestation is None:
                attestation = value
            elif field == 3 and vm_nonce is None:
                vm_nonce = value
            elif field == 4 and vm_proof is None:
                vm_proof = value
            elif field == 5 and chip_binding is None:
                chip_binding = value
            else:
                return None
            continue
        return None
    if attestation is None or vm_nonce is None or len(vm_nonce) != 32:
        return None
    if state not in (STATE_UNPROVISIONED, STATE_PROVISIONED):
        return None
    if state == STATE_UNPROVISIONED and vm_proof is not None:
        return None
    if state == STATE_PROVISIONED and (vm_proof is None or len(vm_proof) != 32):
        return None
    return RvChallenge(state=state, attestation=attestation, vm_nonce=vm_nonce,
                       vm_proof=vm_proof, chip_binding=chip_binding)


def parse_rv_key(value: str) -> bytes:
    raw = value.strip()
    candidates = []
    try:
        candidates.append(bytes.fromhex(raw))
    except ValueError:
        pass
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            candidates.append(decoder(raw + "=" * (-len(raw) % 4)))
        except (ValueError, TypeError):
            continue
    for candidate in candidates:
        if len(candidate) == 32:
            return candidate
    raise CvmError("MUSE_CVM_RV_KEY must be a 32-byte hex or base64 key")


def _proof_key(rv_key: bytes, label: bytes) -> bytes:
    return hmac.new(rv_key, PROOF_KEY_DOMAIN + label, hashlib.sha256).digest()


def create_vm_proof(rv_key: bytes, message1_transcript: bytes, vm_nonce: bytes,
                    client_nonce: bytes) -> bytes:
    key = _proof_key(rv_key, VM_PROOF_KEY_LABEL)
    return hmac.new(key, VM_PROOF_DOMAIN + message1_transcript + client_nonce + vm_nonce,
                    hashlib.sha256).digest()


def create_client_proof(rv_key: bytes, message2_transcript: bytes, vm_nonce: bytes,
                        client_nonce: bytes) -> bytes:
    key = _proof_key(rv_key, CLIENT_PROOF_KEY_LABEL)
    return hmac.new(key, CLIENT_PROOF_DOMAIN + message2_transcript + vm_nonce + client_nonce,
                    hashlib.sha256).digest()


def _encode_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        part = value & 0x7F
        value >>= 7
        if value:
            out.append(part | 0x80)
        else:
            out.append(part)
            return bytes(out)


def encode_message3(notary_token: str, payload: bytes, *, fresh: bool) -> bytes:
    token = notary_token.encode("utf-8")
    out = bytearray()
    out += _encode_varint(10)  # field 1, wire type 2
    out += _encode_varint(len(token))
    out += token
    out += _encode_varint(18 if fresh else 26)
    out += _encode_varint(len(payload))
    out += payload
    return bytes(out)


def build_message3(challenge: RvChallenge, rv_key: bytes, notary_token: str,
                   message1_transcript: bytes, message2_transcript: bytes,
                   client_nonce: bytes, verdict: str | None) -> bytes:
    if not notary_token.strip():
        raise CvmError("confidential VM notary token is missing")
    if len(rv_key) != 32 or len(message1_transcript) != 32 or len(message2_transcript) != 32:
        raise CvmError("RV key and transcript hashes must be 32 bytes")
    if challenge.state == STATE_UNPROVISIONED:
        if verdict not in ("accepted", "accepted_chip_pinned"):
            raise CvmError("attestation was not accepted; refusing to send the RV key")
        return encode_message3(notary_token, rv_key, fresh=True)
    assert challenge.vm_proof is not None
    expected = create_vm_proof(rv_key, message1_transcript, challenge.vm_nonce, client_nonce)
    if not hmac.compare_digest(expected, challenge.vm_proof):
        raise CvmError("confidential VM proof mismatch (wrong RV key)")
    proof = create_client_proof(rv_key, message2_transcript, challenge.vm_nonce, client_nonce)
    return encode_message3(notary_token, proof, fresh=False)


def run_attestation_verifier(command: str, *, attestation: bytes, server_static_key: bytes,
                             handshake_hash: bytes, client_nonce: bytes,
                             chip_binding: bytes | None) -> str:
    """Run an external verifier (EJS-style) and return its verdict.

    The command receives one JSON object on stdin and must print
    ``{"verdict": "accepted"|"accepted_chip_pinned"|"rejected", ...}``.
    """
    if not command:
        raise CvmError(
            "confidential VM attestation requires MUSE_ATTESTATION_CMD; refusing to trust "
            "an unverified CVM"
        )
    request = {
        "attestation": base64.b64encode(attestation).decode(),
        "server_static_key": base64.b64encode(server_static_key).decode(),
        "handshake_hash": base64.b64encode(handshake_hash).decode(),
        "client_nonce": base64.b64encode(client_nonce).decode(),
        "chip_binding": base64.b64encode(chip_binding).decode() if chip_binding else None,
    }
    try:
        completed = subprocess.run(
            shlex.split(command, posix=os.name != "nt"),
            input=json.dumps(request).encode(),
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CvmError(f"attestation verifier failed to run: {exc}") from exc
    if completed.returncode != 0:
        raise CvmError(
            f"attestation verifier exited {completed.returncode}: "
            f"{completed.stderr.decode('utf-8', 'replace')[:400]}"
        )
    try:
        result: Any = json.loads(completed.stdout.decode("utf-8"))
    except ValueError as exc:
        raise CvmError("attestation verifier returned invalid JSON") from exc
    verdict = result.get("verdict") if isinstance(result, dict) else None
    if verdict not in ("accepted", "accepted_chip_pinned", "rejected"):
        raise CvmError("attestation verifier returned an unknown verdict")
    if verdict == "rejected":
        raise CvmError(f"attestation rejected: {result.get('reason', 'no reason')}")
    return verdict
