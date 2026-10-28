"""Hatch gateway: WebSocket + Noise XX + HTTP-over-Noise RPC.

One ``HatchConnection`` multiplexes many request/subscription streams over a
single encrypted WebSocket, exactly like the web client.
"""

from __future__ import annotations

import asyncio
import codecs
import json
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from websockets.asyncio.client import connect as ws_connect

from .cvm import (
    build_message3,
    decode_challenge,
    parse_rv_key,
    run_attestation_verifier,
)
from .noise import EMPTY_AD, NoiseXXInitiator, make_message1_payload
from .wire import (
    SERVICES,
    ApplicationRequest,
    ApplicationResponse,
    Header,
    NoiseReassembler,
    decode_service_frame,
    decode_service_response,
    encode_service_frame_request,
    encode_service_request,
    split_noise_payload,
)

log = logging.getLogger("muse2api.gateway")

CHROME_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)
FIREFOX_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:136.0) "
    "Gecko/20100101 Firefox/136.0"
)
BROWSER_UA = CHROME_BROWSER_UA


def get_ws_headers(impersonate: str = "chrome") -> dict[str, str]:
    """Headers for the gateway WebSocket upgrade matching the browser profile."""
    headers = {
        "Origin": "https://muse.ai",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9,en-GB;q=0.8",
        "Sec-Fetch-Mode": "websocket",
        "Sec-Fetch-Site": "cross-site",
        "Pragma": "no-cache",
        "Cache-Control": "no-cache",
    }
    if "firefox" in (impersonate or "").lower():
        headers["Sec-Fetch-Dest"] = "empty"
        headers["Sec-Fetch-Storage-Access"] = "none"
    else:
        headers["Sec-Fetch-Dest"] = "websocket"
    return headers


WS_HEADERS = get_ws_headers("chrome")


class GatewayError(RuntimeError):
    pass


class GatewayClosed(GatewayError):
    pass


class RpcError(GatewayError):
    def __init__(self, message: str, *, status: int = 0, body: bytes = b"") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


@dataclass(slots=True)
class GatewaySession:
    vm_id: str
    gateway_url: str
    auth_token: str
    notary_token: str | None = None
    app_id: str = "hatch-web"

    def websocket_url(self, *, lb_host: str = "hatch.metaaivm.com", path: str = "/v1/noise") -> str:
        params: dict[str, str] = {
            "vm_id": self.vm_id,
            "auth_token": self.auth_token,
            "app_id": self.app_id,
            "request_id": str(uuid.uuid4()),
        }
        if self.notary_token:
            params["notary_token"] = self.notary_token
        return f"wss://{lb_host}{path}?{urlencode(params)}"


@dataclass(slots=True)
class RpcResponse:
    status: int
    headers: list[Header]
    body: bytes

    def json(self) -> Any:
        if not self.body:
            return None
        return json.loads(self.body.decode("utf-8"))

    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def raise_for_status(self) -> RpcResponse:
        if self.status >= 400:
            raise RpcError(
                f"hatch RPC HTTP {self.status}: {self.text()[:500]}",
                status=self.status,
                body=self.body,
            )
        return self


class _Stream:
    """One stream_id: unary request or long-lived subscription."""

    def __init__(self, stream_id: int) -> None:
        self.id = stream_id
        self.queue: asyncio.Queue[Any] = asyncio.Queue()
        self.status = 0
        self.headers: list[Header] = []
        self.body = bytearray()
        self.responded = False
        self._buffer = ""
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._done = False

    def feed(self, frame) -> None:
        if frame.kind == "response":
            response: ApplicationResponse = frame.payload
            self.responded = True
            self.status = response.status
            self.headers = response.headers
            if response.body:
                self.body += response.body
                self._parse(response.body)
            if response.end_body:
                self._finish()
        elif frame.kind == "body_chunk":
            data, end_body = frame.payload
            if data:
                self.body += data
                self._parse(data)
            if end_body:
                self._finish()
        elif frame.kind == "reset":
            code, reason = frame.payload
            self._finish(f"stream reset ({code}): {reason}")
        else:
            # Server-initiated request frames are not part of this client.
            pass

    def _parse(self, data: bytes) -> None:
        text = self._decoder.decode(data)
        if not text:
            return
        self._buffer += text
        while "\n" in self._buffer:
            line, _, self._buffer = self._buffer.partition("\n")
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            self.queue.put_nowait(obj)
        # Chat chunks are usually one self-contained JSON object without newline.
        tail = self._buffer.strip()
        if tail.startswith("{") or tail.startswith("["):
            try:
                obj = json.loads(tail)
            except json.JSONDecodeError:
                return
            self._buffer = ""
            self.queue.put_nowait(obj)

    def _finish(self, error: str | None = None) -> None:
        if self._done:
            return
        self._done = True
        if self._buffer.strip():
            try:
                self.queue.put_nowait(json.loads(self._buffer))
            except json.JSONDecodeError:
                pass
            self._buffer = ""
        self.queue.put_nowait(("complete", error))


class HatchConnection:
    def __init__(self, ws, tx, rx, *, locale: str = "en-US") -> None:
        self.ws = ws
        self.tx = tx
        self.rx = rx
        self.locale = locale
        self._send_lock = asyncio.Lock()
        self._streams: dict[int, _Stream] = {}
        self._next_stream_id = 1
        self._closed = asyncio.Event()
        self._fatal: BaseException | None = None
        self._receiver = asyncio.create_task(self._receive_loop(), name="hatch-receiver")

    @property
    def is_closed(self) -> bool:
        return self._closed.is_set()

    @classmethod
    async def connect(
        cls,
        session: GatewaySession,
        *,
        lb_host: str = "hatch.metaaivm.com",
        ws_path: str = "/v1/noise",
        locale: str = "en-US",
        timeout: float = 20.0,
        cvm_rv_key: str | None = None,
        attestation_cmd: str | None = None,
        ws_client=None,
        impersonate: str = "chrome",
        user_agent: str | None = None,
        proxy: str | None = None,
        ws_fallback: bool = False,
    ) -> HatchConnection:
        url = session.websocket_url(lb_host=lb_host, path=ws_path)
        log.debug("connecting %s", url.split("?")[0])
        ws = None
        own_client = None
        if ws_client is None:
            from curl_cffi import AsyncSession

            own_client = AsyncSession(
                impersonate=impersonate,
                max_clients=2,
                **({"proxy": proxy} if proxy else {}),
            )
            ws_client = own_client
        max_attempts = 4
        backoff = 1.0
        for attempt in range(1, max_attempts + 1):
            try:
                from curl_cffi import WebSocketRetryStrategy

                ws_headers = get_ws_headers(impersonate)
                if user_agent:
                    ws_headers["User-Agent"] = user_agent
                ws = await ws_client.ws_connect(
                    url,
                    headers=ws_headers,
                    timeout=timeout,
                    impersonate=impersonate,
                    max_message_size=16 * 1024 * 1024,
                    ws_retry=WebSocketRetryStrategy(retry=True, count=3),
                    **({"proxy": proxy} if proxy else {}),
                )
                log.debug("websocket via curl_cffi (%s fingerprint)", impersonate)
                break
            except Exception as exc:  # noqa: BLE001 - stdlib fallback is opt-in
                is_transient = (
                    any(code in str(exc) for code in ("502", "503", "504", "Bad Gateway", "Refused WebSocket upgrade"))
                    or isinstance(exc, (ConnectionResetError, ConnectionRefusedError))
                )
                if attempt < max_attempts and is_transient:
                    log.info(
                        "curl_cffi websocket upgrade returned %s (attempt %d/%d); retrying in %.1fs",
                        exc, attempt, max_attempts, backoff,
                    )
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 1.5, 3.0)
                    continue

                if not ws_fallback:
                    if own_client is not None:
                        await own_client.close()
                    raise GatewayError(
                        "curl_cffi websocket failed; refusing the stdlib fallback "
                        f"because it leaks a different TLS fingerprint (set "
                        f"MUSE_WS_FALLBACK=1 to allow it): {exc}"
                    ) from exc
                log.warning("curl_cffi websocket failed (%s); using websockets", exc)
                fallback_ua = user_agent or (
                    FIREFOX_BROWSER_UA if "firefox" in (impersonate or "").lower() else CHROME_BROWSER_UA
                )
                ws_headers = get_ws_headers(impersonate)
                ws = await ws_connect(
                    url,
                    origin=ws_headers["Origin"],
                    user_agent_header=fallback_ua,
                    additional_headers={
                        key: value for key, value in ws_headers.items()
                        if key not in ("Origin", "User-Agent")
                    },
                    max_size=None,
                    open_timeout=timeout,
                    ping_interval=20,
                    ping_timeout=20,
                )
                break
        if own_client is not None:
            # The connection owns the dedicated session only when the caller
            # did not supply a shared one; keep a reference for cleanup.
            log.debug("websocket uses a dedicated curl_cffi session")
        try:
            noise = NoiseXXInitiator()
            await noise.initialize()
            client_nonce, payload1 = make_message1_payload()
            await ws.send(noise.write_message1(payload1))
            if hasattr(ws, "flush"):
                await ws.flush()
            message1_hash = noise.handshake_hash
            message2 = await asyncio.wait_for(ws.recv(), timeout)
            if isinstance(message2, tuple):  # curl_cffi returns (payload, flags)
                message2 = message2[0]
            if not isinstance(message2, (bytes, bytearray)):
                await ws.close()
                raise GatewayError("expected binary Noise handshake message 2")
            payload2 = noise.read_message2(bytes(message2))
            message3_payload = b""
            challenge = decode_challenge(payload2) if payload2 else None
            if payload2 and challenge is None:
                # Standard VMs send an attestation blob here, not an RV challenge.
                log.debug("standard message 2 payload (%d bytes)", len(payload2))
            if challenge is not None:
                if not cvm_rv_key:
                    await ws.close()
                    raise GatewayError(
                        "confidential VM challenge received but MUSE_CVM_RV_KEY is not set; "
                        "refusing to downgrade the handshake"
                    )
                message2_hash = noise.handshake_hash
                verdict = run_attestation_verifier(
                    attestation_cmd or "",
                    attestation=challenge.attestation,
                    server_static_key=noise.remote_static_public_key or b"",
                    handshake_hash=message2_hash,
                    client_nonce=client_nonce,
                    chip_binding=challenge.chip_binding,
                )
                message3_payload = build_message3(
                    challenge,
                    parse_rv_key(cvm_rv_key),
                    session.notary_token or "",
                    message1_hash,
                    message2_hash,
                    client_nonce,
                    verdict,
                )
            await ws.send(noise.write_message3(message3_payload))
            if hasattr(ws, "flush"):
                await ws.flush()
            tx, rx = noise.split()
            log.debug("noise handshake complete")
            conn = cls(ws, tx, rx, locale=locale)
            conn._ws_client = own_client
            return conn
        except BaseException:
            if ws is not None:
                try:
                    await ws.close()
                except Exception:
                    pass
            if own_client is not None:
                try:
                    await own_client.close()
                except Exception:
                    pass
            raise
        return conn

    # -- receive -----------------------------------------------------------

    async def _receive_loop(self) -> None:
        reassembler = NoiseReassembler()
        try:
            async for message in self.ws:
                if not isinstance(message, (bytes, bytearray)):
                    continue
                try:
                    plaintext = self.rx.decrypt_with_ad(EMPTY_AD, bytes(message))
                    assembled = reassembler.feed(plaintext)
                    if assembled is None:
                        continue
                    service_payload = decode_service_response(assembled)
                    frame = decode_service_frame(service_payload)
                except Exception as exc:  # pragma: no cover - protocol corruption
                    log.warning("dropping bad noise frame: %s", exc)
                    continue
                stream = self._streams.get(frame.stream_id)
                if stream is not None:
                    stream.feed(frame)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - surface as fatal
            self._fatal = exc
            log.warning("gateway receive loop ended: %s", exc)
        finally:
            self._closed.set()
            for stream in list(self._streams.values()):
                stream._finish("connection closed")

    # -- send --------------------------------------------------------------

    def _headers(self, *, json_body: bool) -> list[Header]:
        headers = [
            Header("x-request-id", str(uuid.uuid4())),
            Header("x-app-id", "hatch-web"),
            Header("Accept-Language", self.locale),
        ]
        if json_body:
            headers.insert(0, Header("Content-Type", "application/json"))
        return headers

    async def _send_stream_frames(self, stream_id: int, *, method: str, path: str,
                                  body: bytes, json_body: bool,
                                  service: str) -> None:
        request = ApplicationRequest(method, path, self._headers(json_body=json_body), body, True)
        service_frame = encode_service_frame_request(stream_id, request)
        envelope = encode_service_request(SERVICES[service], service_frame)
        async with self._send_lock:
            if self._closed.is_set():
                raise GatewayClosed(f"gateway closed: {self._fatal!r}")
            for frame in split_noise_payload(envelope):
                await self.ws.send(self.tx.encrypt_with_ad(EMPTY_AD, frame))
            if hasattr(self.ws, "flush"):
                await self.ws.flush()

    def _new_stream(self) -> _Stream:
        stream_id = self._next_stream_id
        self._next_stream_id += 1
        stream = _Stream(stream_id)
        self._streams[stream_id] = stream
        return stream

    def _drop_stream(self, stream_id: int) -> None:
        self._streams.pop(stream_id, None)

    async def _send_reset(self, stream_id: int, reason: str) -> None:
        if self._closed.is_set():
            return
        try:
            from .wire import encode_service_frame_reset

            envelope = encode_service_request(
                SERVICES["daemon"],
                encode_service_frame_reset(stream_id, 1, reason),
            )
            async with self._send_lock:
                for frame in split_noise_payload(envelope):
                    await self.ws.send(self.tx.encrypt_with_ad(EMPTY_AD, frame))
                if hasattr(self.ws, "flush"):
                    await self.ws.flush()
        except Exception:  # pragma: no cover - best effort
            pass

    # -- public API --------------------------------------------------------

    async def request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        *,
        service: str = "daemon",
        timeout: float = 60.0,
    ) -> RpcResponse:
        if method.upper() == "GET":
            query = urlencode({k: v for k, v in (params or {}).items() if v is not None}, doseq=True)
            if query:
                path = f"{path}{'&' if '?' in path else '?'}{query}"
            body = b""
            json_body = False
        else:
            body = json.dumps(params or {}, ensure_ascii=False, separators=(",", ":")).encode()
            json_body = True

        stream = self._new_stream()
        try:
            await self._send_stream_frames(
                stream.id, method=method.upper(), path=path, body=body,
                json_body=json_body, service=service,
            )
            deadline = asyncio.get_running_loop().time() + timeout
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise GatewayError(f"RPC timeout after {timeout:.0f}s: {method} {path}")
                item = await asyncio.wait_for(stream.queue.get(), remaining)
                if isinstance(item, tuple) and item[0] == "complete":
                    if item[1]:
                        raise GatewayError(item[1])
                    return RpcResponse(stream.status, stream.headers, bytes(stream.body))
        finally:
            if not stream._done:
                await self._send_reset(stream.id, "client aborted")
            self._drop_stream(stream.id)

    async def send(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        *,
        service: str = "daemon",
    ) -> int:
        """Fire-and-forget request; returns stream_id without waiting for a reply."""
        body = json.dumps(params or {}, ensure_ascii=False, separators=(",", ":")).encode()
        stream = self._new_stream()
        try:
            await self._send_stream_frames(
                stream.id, method=method.upper(), path=path, body=body,
                json_body=True, service=service,
            )
        except BaseException:
            self._drop_stream(stream.id)
            raise
        return stream.id

    async def stream(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        *,
        service: str = "daemon",
        timeout: float | None = None,
        reset_reason: str = "client done",
    ) -> AsyncIterator[dict]:
        """Yield JSON records from a long-lived (or NDJSON) response stream."""
        body = json.dumps(params or {}, ensure_ascii=False, separators=(",", ":")).encode()
        stream = self._new_stream()
        try:
            await self._send_stream_frames(
                stream.id, method=method.upper(), path=path, body=body,
                json_body=True, service=service,
            )
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout if timeout else None
            while True:
                wait = 1.0
                if deadline is not None:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        return
                    wait = min(wait, remaining)
                try:
                    item = await asyncio.wait_for(stream.queue.get(), wait)
                except TimeoutError:
                    if deadline is not None and loop.time() >= deadline:
                        return
                    continue
                if isinstance(item, tuple) and item[0] == "complete":
                    return
                yield item
        finally:
            await self._send_reset(stream.id, reset_reason)
            self._drop_stream(stream.id)

    async def ack(self, method: str, path: str, params: dict | None = None, *,
                  service: str = "daemon", timeout: float = 20.0) -> dict:
        """Open a stream, read the first JSON record (the ack), then cancel it."""
        gen = self.stream(method, path, params, service=service, reset_reason="ack only")
        try:
            async with asyncio.timeout(timeout):
                async for record in gen:
                    return record
        finally:
            await gen.aclose()
        return {}

    async def close(self) -> None:
        if not self._closed.is_set():
            try:
                await self.ws.close()
            except Exception:  # pragma: no cover
                pass
            self._closed.set()
        own_client = getattr(self, "_ws_client", None)
        if own_client is not None:
            try:
                await own_client.close()
            except Exception:  # pragma: no cover
                pass
        if not self._receiver.done():
            self._receiver.cancel()
            try:
                await self._receiver
            except asyncio.CancelledError:
                pass
