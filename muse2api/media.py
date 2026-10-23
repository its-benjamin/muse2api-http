"""Media extraction and download for muse agent artifacts.

Assistant turns may contain ``delta.presentation`` events with
``payload.data.images[]`` / videos. Files live in the VM workspace and are
reachable over the same Noise gateway (``/media/raw/<path>``).
"""

from __future__ import annotations

import mimetypes
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .gateway import HatchConnection


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
    if text.startswith("sandbox://"):
        text = text[len("sandbox://"):]
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


async def download_media(conn: HatchConnection, ref: MediaRef) -> bytes:
    paths: list[str] = []
    if ref.path:
        clean = _normalize_path(ref.path)
        # The gateway accepts both /media/raw and /fs/raw; try the direct media
        # route first, then fall back to the filesystem route.
        paths.extend([f"/media/raw/{clean}", f"/fs/raw/{clean}"])
    last_error: Exception | None = None
    for path in paths:
        try:
            response = await conn.request("GET", path, {}, timeout=300.0)
            if 200 <= response.status < 300 and response.body:
                return response.body
            last_error = RuntimeError(f"{path} -> HTTP {response.status}")
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    # Signed VM URLs are not publicly reachable, but an external URL may be.
    if ref.url and ref.url.startswith(("http://", "https://")):
        try:
            from curl_cffi.requests import AsyncSession

            async with AsyncSession(impersonate="chrome", timeout=120) as client:
                response = await client.get(ref.url)
                if 200 <= response.status_code < 300 and response.content:
                    return response.content
                last_error = RuntimeError(f"HTTP {response.status_code} for {ref.url}")
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    raise RuntimeError(f"could not download {ref.identity}: {last_error}")
