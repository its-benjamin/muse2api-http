"""Media extraction and download for muse agent artifacts.

Assistant turns may contain ``delta.presentation`` events with
``payload.data.images[]`` / videos. Files live in the VM workspace and are
reachable over the same Noise gateway (``/media/raw/<path>``).
"""

from __future__ import annotations

import ipaddress
import mimetypes
import os
import re
import socket
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from .gateway import HatchConnection

# Hard ceiling for any media we accept from a client or hold in memory.
MAX_MEDIA_BYTES = 150 * 1024 * 1024
_FALSE_VALUES = {"", "0", "false", "no", "off"}


def media_limit_label() -> str:
    return f"{MAX_MEDIA_BYTES // (1024 * 1024)} MiB"


class MediaFetchError(RuntimeError):
    """A remote media URL was rejected or could not be fetched safely."""


class MediaTooLargeError(RuntimeError):
    """Downloaded media exceeded the configured ceiling."""



@dataclass(slots=True)
class MediaRef:
    kind: str  # image | video
    path: str | None = None
    url: str | None = None
    mime: str | None = None
    label: str | None = None
    byte_len: int | None = None

    @property
    def identity(self) -> str:
        return self.path or self.url or f"{self.kind}:{self.label}:{self.byte_len}"

    @property
    def extension(self) -> str:
        for candidate in (self.path, self.url):
            if not candidate:
                continue
            clean = candidate.split("?", 1)[0].split("#", 1)[0]
            suffix = Path(clean).suffix.lower()
            if suffix:
                return suffix
        if self.mime:
            guessed = mimetypes.guess_extension(self.mime.split(";", 1)[0].strip())
            if guessed:
                return guessed
        return ".bin"


def _normalize_path(value: str) -> str:
    text = value.strip()
    text = text.removeprefix("sandbox://")
    text = text.replace("\\", "/")
    while "//" in text:
        text = text.replace("//", "/")
    return text.lstrip("/")


def _ref_from_node(node: dict, kind_hint: str | None) -> MediaRef | None:
    path = node.get("path") or node.get("image_path") or node.get("video_path")
    variants = node.get("variants")
    url = None
    if isinstance(variants, dict):
        original = variants.get("original")
        if isinstance(original, str):
            url = original
    if not url and isinstance(node.get("url"), str):
        url = node["url"]

    mime = node.get("mime") or node.get("mime_type") or node.get("mimeType")
    kind = node.get("kind") or kind_hint
    label = node.get("label") or node.get("prompt") or node.get("alt")

    if kind_hint is None:
        if isinstance(mime, str):
            if mime.startswith("video/"):
                kind = "video"
            elif mime.startswith("image/"):
                kind = "image"
        if kind not in {"image", "video"} and isinstance(path, str):
            clean = path.lower().split("?", 1)[0]
            if clean.endswith((".mp4", ".webm", ".mov", ".m4v")):
                kind = "video"
            elif clean.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
                kind = "image"
    if kind not in {"image", "video"}:
        return None
    if not (path or url):
        return None
    return MediaRef(
        kind=str(kind),
        path=_normalize_path(path) if isinstance(path, str) else None,
        url=url if isinstance(url, str) else None,
        mime=mime if isinstance(mime, str) else None,
        label=label if isinstance(label, str) else None,
        byte_len=node.get("byte_len") if isinstance(node.get("byte_len"), int) else None,
    )


def extract_media(events: list[dict]) -> list[MediaRef]:
    found: dict[str, MediaRef] = {}

    def add(ref: MediaRef) -> None:
        found.setdefault(ref.identity, ref)

    def walk(node: Any, kind_hint: str | None = None) -> None:
        if isinstance(node, dict):
            direct = _ref_from_node(node, kind_hint)
            if direct is not None:
                add(direct)
            kind = node.get("kind") if node.get("kind") in {"image", "video"} else kind_hint
            for key, value in node.items():
                if key in {"path", "image_path", "video_path", "url", "variants", "mime",
                           "mime_type", "mimeType", "label", "byte_len"}:
                    continue
                walk(value, kind)
        elif isinstance(node, list):
            for item in node:
                walk(item, kind_hint)
        elif isinstance(node, str):
            for match in re.finditer(r"sandbox://[^\s)\"']+", node):
                walk({"path": match.group(0)}, kind_hint)

    for event in events:
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        name = str(event.get("event_name") or event.get("event") or "")
        kind_hint = None
        if payload.get("kind") in {"image", "video"}:
            kind_hint = payload["kind"]
        if name == "delta.presentation" or "images" in payload or "videos" in payload:
            walk(payload, kind_hint)
        resources = event.get("resources")
        if resources:
            walk(resources, kind_hint)
    return list(found.values())


class MediaStore:
    def __init__(self, root: Path, public_base: str | None = None) -> None:
        self.root = root
        self.public_base = (public_base or "/v1/media").rstrip("/")
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, data: bytes, extension: str) -> str:
        name = f"{uuid.uuid4().hex}{extension if extension.startswith('.') else '.' + extension}"
        (self.root / name).write_bytes(data)
        return name

    def path_for(self, name: str) -> Path | None:
        candidate = (self.root / Path(name).name).resolve()
        if candidate.parent != self.root.resolve() or not candidate.is_file():
            return None
        return candidate

    def url_for(self, name: str) -> str:
        return f"{self.public_base}/{name}"


def _private_hosts_allowed() -> bool:
    raw = os.environ.get("MUSE2API_ALLOW_PRIVATE_URLS")
    return raw is not None and raw.strip().lower() not in _FALSE_VALUES


def assert_public_host(host: str) -> None:
    """Reject hosts that resolve to loopback/private/link-local/reserved ranges.

    Client-supplied image URLs are fetched by this process, so an unrestricted
    fetch is an SSRF pivot into the host network and cloud metadata services.
    """
    if _private_hosts_allowed():
        return
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise MediaFetchError(f"could not resolve {host}") from exc
    addresses = {info[4][0] for info in infos}
    if not addresses:
        raise MediaFetchError(f"could not resolve {host}")
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address.split("%", 1)[0])
        except ValueError as exc:
            raise MediaFetchError(f"unparseable address for {host}") from exc
        if not ip.is_global:
            raise MediaFetchError(
                f"refusing to fetch {host} -> {ip}: non-public address "
                "(set MUSE2API_ALLOW_PRIVATE_URLS=1 to allow it)"
            )


async def fetch_public_media(
    url: str,
    *,
    impersonate: str = "chrome",
    timeout: float = 90.0,
    max_bytes: int = MAX_MEDIA_BYTES,
    max_redirects: int = 5,
) -> tuple[bytes, str | None]:
    """Download a user-supplied URL with redirect, host and size guards."""
    from curl_cffi.requests import AsyncSession

    current = url
    async with AsyncSession(impersonate=impersonate, timeout=timeout) as client:
        for _ in range(max_redirects + 1):
            parsed = urlparse(current)
            if parsed.scheme not in {"http", "https"}:
                raise MediaFetchError(f"unsupported URL scheme: {parsed.scheme or '(none)'}")
            if not parsed.hostname:
                raise MediaFetchError("URL has no host")
            # Validate every hop: a public URL may redirect to 127.0.0.1.
            assert_public_host(parsed.hostname)
            response = await client.get(current, allow_redirects=False, stream=True)
            try:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise MediaFetchError("redirect without a Location header")
                    current = urljoin(current, location)
                    continue
                if not 200 <= response.status_code < 300:
                    raise MediaFetchError(f"HTTP {response.status_code} for {current}")
                declared = response.headers.get("content-length") or ""
                if declared.isdigit() and int(declared) > max_bytes:
                    raise MediaFetchError("remote media exceeds the size limit")
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_content():
                    total += len(chunk)
                    if total > max_bytes:
                        raise MediaFetchError("remote media exceeds the size limit")
                    chunks.append(chunk)
                if not chunks:
                    raise MediaFetchError("remote media is empty")
                return b"".join(chunks), response.headers.get("content-type")
            finally:
                await response.aclose()
    raise MediaFetchError("too many redirects")


async def download_media(conn: HatchConnection, ref: MediaRef, *, impersonate: str = "chrome",
                         max_bytes: int = MAX_MEDIA_BYTES) -> bytes:
    paths: list[str] = []
    if ref.path:
        clean = _normalize_path(ref.path)
        # The gateway accepts both /media/raw and /fs/raw; try the direct media
        # route first, then fall back to the filesystem route.
        paths.extend([f"/media/raw/{clean}", f"/fs/raw/{clean}"])

    def check(data: bytes) -> bytes:
        if len(data) > max_bytes:
            raise MediaTooLargeError(
                f"downloaded media exceeds {max_bytes // (1024 * 1024)} MiB"
            )
        return data

    last_error: Exception | None = None
    for path in paths:
        try:
            response = await conn.request("GET", path, {}, timeout=300.0)
            if 200 <= response.status < 300 and response.body:
                return check(response.body)
            last_error = RuntimeError(f"{path} -> HTTP {response.status}")
        except MediaTooLargeError:
            raise
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    # Signed VM URLs are not publicly reachable, but an external URL may be.
    if ref.url and ref.url.startswith(("http://", "https://")):
        try:
            from curl_cffi import CurlFollow
            from curl_cffi.requests import AsyncSession

            async with AsyncSession(
                impersonate=impersonate,
                timeout=120,
                allow_redirects=CurlFollow.SAFE,
            ) as client:
                response = await client.get(ref.url)
                if 200 <= response.status_code < 300 and response.content:
                    return check(response.content)
                last_error = RuntimeError(f"HTTP {response.status_code} for {ref.url}")
        except MediaTooLargeError:
            raise
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    raise RuntimeError(f"could not download {ref.identity}: {last_error}")
