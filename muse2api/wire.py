"""Minimal protobuf wire codec for the Hatch Noise transport.

Shapes (from the web bundle descriptors):

    NoiseTransportFrame { int64 chunk_id=1; uint32 chunk_index=2;
                          uint32 total_chunks=3; bytes payload=4; }
    ServiceRequest  { ServiceType service=1; bytes payload=2; }
    ServiceResponse { bytes payload=1; }
    ServiceFrame    { int64 stream_id=1;
                      oneof kind { ApplicationRequest request=2;
                                   ApplicationResponse response=3;
                                   BodyChunk body_chunk=4; Reset reset=5; } }
    ApplicationRequest  { string verb=1; string path=2; repeated Header headers=3;
                          bytes body=4; bool end_body=5; }
    ApplicationResponse { int32 status=1; repeated Header headers=2;
                          bytes body=3; bool end_body=4; }
    BodyChunk { bytes data=1; bool end_body=2; }
    Header    { string key=1; string value=2; }
    Reset     { Code code=1; string reason=2; }
"""

from __future__ import annotations

import os
import struct
from collections.abc import Iterator
from dataclasses import dataclass, field

MAX_NOISE_CHUNK_PAYLOAD = 48 * 1024
MAX_NOISE_CHUNKS = 256
MAX_ASSEMBLY_BYTES = 16 * 1024 * 1024


def varint(value: int) -> bytes:
    if value < 0:
        value &= (1 << 64) - 1
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def key(field_no: int, wire_type: int) -> bytes:
    return varint((field_no << 3) | wire_type)


def f_varint(field_no: int, value: int) -> bytes:
    return key(field_no, 0) + varint(value)


def f_bytes(field_no: int, value: bytes) -> bytes:
    return key(field_no, 2) + varint(len(value)) + value


def f_string(field_no: int, value: str) -> bytes:
    return f_bytes(field_no, value.encode("utf-8"))


def f_bool(field_no: int, value: bool) -> bytes:
    return f_varint(field_no, 1 if value else 0)


def read_varint(data: bytes, pos: int = 0) -> tuple[int, int]:
    value = 0
    shift = 0
    while True:
        if pos >= len(data):
            raise ValueError("truncated varint")
        b = data[pos]
        pos += 1
        value |= (b & 0x7F) << shift
        if not b & 0x80:
            return value, pos
        shift += 7
        if shift > 70:
            raise ValueError("varint too long")


def fields(data: bytes) -> Iterator[tuple[int, int, int | bytes]]:
    pos = 0
    while pos < len(data):
        tag, pos = read_varint(data, pos)
        num, wire_type = tag >> 3, tag & 7
        if wire_type == 0:
            value, pos = read_varint(data, pos)
            yield num, wire_type, value
        elif wire_type == 2:
            length, pos = read_varint(data, pos)
            end = pos + length
            if end > len(data):
                raise ValueError("truncated length-delimited field")
            yield num, wire_type, data[pos:end]
            pos = end
        elif wire_type == 1:
            end = pos + 8
            if end > len(data):
                raise ValueError("truncated fixed64")
            yield num, wire_type, data[pos:end]
            pos = end
        elif wire_type == 5:
            end = pos + 4
            if end > len(data):
                raise ValueError("truncated fixed32")
            yield num, wire_type, data[pos:end]
            pos = end
        else:
            raise ValueError(f"unsupported protobuf wire type {wire_type}")


def signed64(value: int) -> int:
    return value - (1 << 64) if value & (1 << 63) else value


@dataclass(slots=True)
class Header:
    key: str
    value: str

    def encode(self) -> bytes:
        return f_string(1, self.key) + f_string(2, self.value)


@dataclass(slots=True)
class ApplicationRequest:
    verb: str
    path: str
    headers: list[Header] = field(default_factory=list)
    body: bytes = b""
    end_body: bool = True

    def encode(self) -> bytes:
        out = bytearray()
        out += f_string(1, self.verb)
        out += f_string(2, self.path)
        for header in self.headers:
            out += f_bytes(3, header.encode())
        if self.body:
            out += f_bytes(4, self.body)
        out += f_bool(5, self.end_body)
        return bytes(out)


@dataclass(slots=True)
class ApplicationResponse:
    status: int = 0
    headers: list[Header] = field(default_factory=list)
    body: bytes = b""
    end_body: bool = False

    @classmethod
    def decode(cls, data: bytes) -> ApplicationResponse:
        status = 0
        headers: list[Header] = []
        body = b""
        end_body = False
        for num, wire_type, value in fields(data):
            if num == 1 and wire_type == 0:
                status = int(value)
            elif num == 2 and wire_type == 2:
                hkey = hvalue = ""
                for n2, w2, v2 in fields(bytes(value)):
                    if n2 == 1 and w2 == 2:
                        hkey = bytes(v2).decode("utf-8", "replace")
                    elif n2 == 2 and w2 == 2:
                        hvalue = bytes(v2).decode("utf-8", "replace")
                headers.append(Header(hkey, hvalue))
            elif num == 3 and wire_type == 2:
                body = bytes(value)
            elif num == 4 and wire_type == 0:
                end_body = bool(value)
        return cls(status=status, headers=headers, body=body, end_body=end_body)


@dataclass(slots=True)
class DecodedServiceFrame:
    stream_id: int
    kind: str
    payload: object = None


def encode_service_frame_request(stream_id: int, request: ApplicationRequest) -> bytes:
    return f_varint(1, stream_id) + f_bytes(2, request.encode())


def encode_service_frame_body(stream_id: int, data: bytes, end_body: bool = False) -> bytes:
    chunk = f_bytes(1, data) + f_bool(2, end_body)
    return f_varint(1, stream_id) + f_bytes(4, chunk)


def encode_service_frame_reset(stream_id: int, code: int = 1, reason: str = "") -> bytes:
    reset = f_varint(1, code) + f_string(2, reason)
    return f_varint(1, stream_id) + f_bytes(5, reset)


def decode_service_frame(data: bytes) -> DecodedServiceFrame:
    stream_id = 0
    kind = "unknown"
    payload: object = None
    for num, wire_type, value in fields(data):
        if num == 1 and wire_type == 0:
            stream_id = signed64(int(value))
        elif num == 2 and wire_type == 2:
            kind = "request"
            payload = bytes(value)
        elif num == 3 and wire_type == 2:
            kind = "response"
            payload = ApplicationResponse.decode(bytes(value))
        elif num == 4 and wire_type == 2:
            chunk_data = b""
            end_body = False
            for n2, w2, v2 in fields(bytes(value)):
                if n2 == 1 and w2 == 2:
                    chunk_data = bytes(v2)
                elif n2 == 2 and w2 == 0:
                    end_body = bool(v2)
            kind = "body_chunk"
            payload = (chunk_data, end_body)
        elif num == 5 and wire_type == 2:
            code = 0
            reason = ""
            for n2, w2, v2 in fields(bytes(value)):
                if n2 == 1 and w2 == 0:
                    code = int(v2)
                elif n2 == 2 and w2 == 2:
                    reason = bytes(v2).decode("utf-8", "replace")
            kind = "reset"
            payload = (code, reason)
    return DecodedServiceFrame(stream_id=stream_id, kind=kind, payload=payload)


SERVICE_DAEMON = 0
SERVICE_SENTINEL = 1
SERVICE_VAULT = 2
SERVICE_AUTHD = 3

SERVICES = {
    "daemon": SERVICE_DAEMON,
    "sentinel": SERVICE_SENTINEL,
    "vault": SERVICE_VAULT,
    "authd": SERVICE_AUTHD,
}


def encode_service_request(service: int, service_frame: bytes) -> bytes:
    return key(1, 0) + varint(service) + f_bytes(2, service_frame)


def decode_service_response(data: bytes) -> bytes:
    for num, wire_type, value in fields(data):
        if num == 1 and wire_type == 2:
            return bytes(value)
    raise ValueError("ServiceResponse missing payload")


@dataclass(slots=True)
class NoiseChunk:
    chunk_id: int
    chunk_index: int
    total_chunks: int
    payload: bytes

    def encode(self) -> bytes:
        return (
            f_varint(1, self.chunk_id)
            + f_varint(2, self.chunk_index)
            + f_varint(3, self.total_chunks)
            + f_bytes(4, self.payload)
        )

    @classmethod
    def decode(cls, data: bytes) -> NoiseChunk:
        chunk_id = 0
        chunk_index = 0
        total_chunks = 1
        payload = b""
        for num, wire_type, value in fields(data):
            if num == 1 and wire_type == 0:
                chunk_id = signed64(int(value))
            elif num == 2 and wire_type == 0:
                chunk_index = int(value)
            elif num == 3 and wire_type == 0:
                total_chunks = int(value)
            elif num == 4 and wire_type == 2:
                payload = bytes(value)
        return cls(chunk_id, chunk_index, total_chunks, payload)


def random_int64() -> int:
    return struct.unpack("<q", os.urandom(8))[0]


def split_noise_payload(data: bytes, max_payload: int = MAX_NOISE_CHUNK_PAYLOAD) -> list[bytes]:
    total = max(1, (len(data) + max_payload - 1) // max_payload)
    if total > MAX_NOISE_CHUNKS:
        raise ValueError(f"payload too large for Noise framing: {total} chunks")
    chunk_id = random_int64()
    if not data:
        return [NoiseChunk(chunk_id, 0, 1, b"").encode()]
    return [
        NoiseChunk(
            chunk_id,
            index,
            total,
            data[index * max_payload:(index + 1) * max_payload],
        ).encode()
        for index in range(total)
    ]


class NoiseReassembler:
    def __init__(self) -> None:
        self._pending: dict[int, tuple[int, dict[int, bytes], int]] = {}

    def feed(self, raw: bytes) -> bytes | None:
        chunk = NoiseChunk.decode(raw)
        if chunk.total_chunks < 1 or chunk.total_chunks > MAX_NOISE_CHUNKS:
            raise ValueError("invalid total_chunks")
        if chunk.chunk_index < 0 or chunk.chunk_index >= chunk.total_chunks:
            raise ValueError("invalid chunk_index")
        if len(chunk.payload) > MAX_NOISE_CHUNK_PAYLOAD:
            raise ValueError("noise frame payload too large")
        total, parts, size = self._pending.get(
            chunk.chunk_id, (chunk.total_chunks, {}, 0)
        )
        if total != chunk.total_chunks:
            self._pending.pop(chunk.chunk_id, None)
            raise ValueError("inconsistent total_chunks")
        if chunk.chunk_index in parts:
            self._pending.pop(chunk.chunk_id, None)
            raise ValueError("duplicate noise chunk")
        parts[chunk.chunk_index] = chunk.payload
        size += len(chunk.payload)
        if size > MAX_ASSEMBLY_BYTES:
            self._pending.pop(chunk.chunk_id, None)
            raise ValueError("noise assembly exceeded 16 MiB")
        self._pending[chunk.chunk_id] = (total, parts, size)
        if len(parts) != total:
            return None
        self._pending.pop(chunk.chunk_id, None)
        return b"".join(parts[index] for index in range(total))
