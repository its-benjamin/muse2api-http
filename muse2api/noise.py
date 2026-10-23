"""Noise_XX_25519_AESGCM_SHA256 initiator.

Mirrors the muse.ai web client's hand-written Noise implementation:
XX pattern, SHA-256 HKDF, AES-GCM with the standard 4+8 byte big-endian nonce.
"""

from __future__ import annotations

import hashlib
import hmac
import os

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

PROTOCOL = b"Noise_XX_25519_AESGCM_SHA256"
EMPTY_AD = b""


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def hkdf_noise(chaining_key: bytes, input_key_material: bytes, outputs: int = 2) -> tuple[bytes, ...]:
    temp = hmac.new(chaining_key, input_key_material, hashlib.sha256).digest()
    out1 = hmac.new(temp, b"\x01", hashlib.sha256).digest()
    out2 = hmac.new(temp, out1 + b"\x02", hashlib.sha256).digest()
    if outputs == 2:
        return out1, out2
    if outputs == 3:
        out3 = hmac.new(temp, out2 + b"\x03", hashlib.sha256).digest()
        return out1, out2, out3
    raise ValueError("Noise HKDF supports 2 or 3 outputs")


def nonce12(counter: int) -> bytes:
    if counter < 0 or counter >= 1 << 64:
        raise ValueError("nonce counter out of range")
    return b"\x00" * 4 + counter.to_bytes(8, "big")


def _pub(key: X25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def _dh(private: X25519PrivateKey, public: bytes) -> bytes:
    if len(public) != 32:
        raise ValueError("x25519: invalid public key length")
    return private.exchange(X25519PublicKey.from_public_bytes(public))


class CipherState:
    def __init__(self, key: bytes | None = None) -> None:
        self._key = bytes(key) if key is not None else None
        self._nonce = 0

    @property
    def has_key(self) -> bool:
        return self._key is not None

    def initialize_key(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("AES-GCM key must be 32 bytes")
        self._key = bytes(key)
        self._nonce = 0

    def encrypt_with_ad(self, ad: bytes, plaintext: bytes) -> bytes:
        if not self._key:
            return plaintext
        nonce = nonce12(self._nonce)
        self._nonce += 1
        return AESGCM(self._key).encrypt(nonce, plaintext, ad)

    def decrypt_with_ad(self, ad: bytes, ciphertext: bytes) -> bytes:
        if not self._key:
            return ciphertext
        nonce = nonce12(self._nonce)
        self._nonce += 1
        return AESGCM(self._key).decrypt(nonce, ciphertext, ad)


class SymmetricState:
    def __init__(self) -> None:
        self.h = b""
        self.ck = b""
        self.cipher = CipherState()

    def initialize(self) -> None:
        # Noise InitializeSymmetric: h and ck both start as the zero-padded
        # protocol name; the first MixHash(prologue) then hashes h.
        name = PROTOCOL.ljust(32, b"\x00")
        self.ck = name
        self.h = name
        self.mix_hash(b"")

    def mix_hash(self, data: bytes) -> None:
        self.h = sha256(self.h + data)

    def mix_key(self, ikm: bytes) -> None:
        self.ck, temp_k = hkdf_noise(self.ck, ikm)
        self.cipher.initialize_key(temp_k)

    def encrypt_and_hash(self, plaintext: bytes) -> bytes:
        ciphertext = self.cipher.encrypt_with_ad(self.h, plaintext)
        self.mix_hash(ciphertext)
        return ciphertext

    def decrypt_and_hash(self, ciphertext: bytes) -> bytes:
        plaintext = self.cipher.decrypt_with_ad(self.h, ciphertext)
        self.mix_hash(ciphertext)
        return plaintext

    def split(self) -> tuple[CipherState, CipherState]:
        k1, k2 = hkdf_noise(self.ck, b"")
        first, second = CipherState(k1), CipherState(k2)
        return first, second


class NoiseProtocolError(RuntimeError):
    pass


class NoiseXXInitiator:
    def __init__(self) -> None:
        self.symmetric = SymmetricState()
        self.ephemeral: X25519PrivateKey | None = None
        self.static: X25519PrivateKey | None = None
        self.remote_ephemeral: bytes | None = None
        self.remote_static: bytes | None = None
        self.phase = 0

    async def initialize(self) -> None:
        self.symmetric.initialize()
        self.phase = 1

    def write_message1(self, payload: bytes = b"") -> bytes:
        if self.phase != 1:
            raise NoiseProtocolError("write_message1 in wrong phase")
        self.ephemeral = X25519PrivateKey.generate()
        e_pub = _pub(self.ephemeral)
        self.symmetric.mix_hash(e_pub)
        encrypted = self.symmetric.encrypt_and_hash(payload)
        self.phase = 2
        return e_pub + encrypted

    def read_message2(self, message: bytes) -> bytes:
        if self.phase != 2:
            raise NoiseProtocolError("read_message2 in wrong phase")
        if len(message) < 96 or self.ephemeral is None:
            raise NoiseProtocolError(f"message 2 too short ({len(message)} < 96)")
        self.remote_ephemeral = message[0:32]
        self.symmetric.mix_hash(self.remote_ephemeral)
        self.symmetric.mix_key(_dh(self.ephemeral, self.remote_ephemeral))
        self.remote_static = self.symmetric.decrypt_and_hash(message[32:80])
        if len(self.remote_static) != 32:
            raise NoiseProtocolError("message 2 static key is not 32 bytes")
        self.symmetric.mix_key(_dh(self.ephemeral, self.remote_static))
        payload = self.symmetric.decrypt_and_hash(message[80:])
        self.phase = 3
        return payload

    def write_message3(self, payload: bytes = b"") -> bytes:
        if self.phase != 3 or self.remote_ephemeral is None:
            raise NoiseProtocolError("write_message3 in wrong phase")
        self.static = X25519PrivateKey.generate()
        encrypted_static = self.symmetric.encrypt_and_hash(_pub(self.static))
        self.symmetric.mix_key(_dh(self.static, self.remote_ephemeral))
        encrypted_payload = self.symmetric.encrypt_and_hash(payload)
        self.phase = 4
        return encrypted_static + encrypted_payload

    def split(self) -> tuple[CipherState, CipherState]:
        if self.phase != 4:
            raise NoiseProtocolError("split in wrong phase")
        self.phase = 5
        return self.symmetric.split()

    @property
    def handshake_hash(self) -> bytes:
        return bytes(self.symmetric.h)

    @property
    def remote_static_public_key(self) -> bytes | None:
        return self.remote_static


def encode_client_nonce_message1(nonce: bytes) -> bytes:
    if len(nonce) != 32:
        raise ValueError("client nonce must be exactly 32 bytes")
    return b"\x0a\x20" + nonce


def make_message1_payload() -> tuple[bytes, bytes]:
    nonce = os.urandom(32)
    return nonce, encode_client_nonce_message1(nonce)
