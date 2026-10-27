"""OpenAI-compatible HTTP surface for muse.ai.

Endpoints
---------
GET  /v1/models
POST /v1/chat/completions          (stream + non-stream)
POST /v1/responses                 (Responses API, stream + non-stream)
POST /v1/images/generations
POST /v1/images/edits              (multipart)
POST /v1/videos                    (async task)
GET  /v1/videos/{task_id}
GET  /v1/media/{filename}
GET  /healthz
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from . import __version__
from .chat import ChatResult, ChatService, session_key
from .config import Settings
from .gateway import GatewayError
from .http import MuseAuthError, MuseUpstreamError
from .media import (
    MAX_MEDIA_BYTES,
    MediaStore,
    download_media,
    fetch_public_media,
    media_limit_label,
)
from .pool import AccountPool, PoolExhausted
from .protocol import ProtocolConfig
from .tool_bridge import (
    apply_tool_bridge,
    build_tool_instructions,
    build_tool_reminder,
    render_tool_transcript,
    tools_active,
)

log = logging.getLogger("muse2api.server")

_VIDEO_REGION_HINTS = (
    "not available for your account",
    "available for your account in your country or region",
    "country or region",
    "lasting restriction",
)
_VIDEO_QUOTA_HINTS = ("quota", "credits", "limit reached")
_VIDEO_REFUSAL_MARKERS = _VIDEO_REGION_HINTS + _VIDEO_QUOTA_HINTS


VIDEO_ERROR_MESSAGES = {
    "video_unavailable_region": (
        "Video generation is not available for this account or region. "
        "The restriction is permanent; text and image generation still work."
    ),
    "video_quota_exhausted": (
        "Video generation quota is exhausted for this account. "
        "Add credits or configure another account."
    ),
    "video_generation_failed": "Video generation failed.",
}


def video_failure(code: str, upstream: str | None) -> dict[str, str]:
    """Stable, client-facing failure fields; keep model prose out of `error`."""
    payload = {"error": VIDEO_ERROR_MESSAGES.get(code, VIDEO_ERROR_MESSAGES["video_generation_failed"]),
               "code": code}
    if upstream and upstream.strip():
        payload["upstream_message"] = upstream.strip()[:1000]
    return payload


def load_video_block(path: Path) -> float:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        until = float(data.get("until", 0))
    except Exception:  # noqa: BLE001 - a corrupt cache must not block video
        return 0.0
    return until if until > time.time() else 0.0


def save_video_block(path: Path, until: float) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"until": until, "reason": "video_unavailable_region"}),
            encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001 - best effort
        log.debug("could not persist video block: %s", exc)


def classify_video_error(text: str) -> str:
    """Turn the model's prose refusal into a stable API error code."""
    lowered = text.lower()
    if any(hint in lowered for hint in _VIDEO_REGION_HINTS):
        return "video_unavailable_region"
    if "quota" in lowered or "credits" in lowered or "limit reached" in lowered:
        return "video_quota_exhausted"
    return "video_generation_failed"

MODEL_ALIASES = {
    "muse-spark",
    "muse",
    "gpt-4o",
    "gpt-4o-mini",
    "gpt-4.1",
    "gpt-5",
    "claude-sonnet-4",
    "claude-3-5-sonnet",
    "deepseek-chat",
}


class ApiError(Exception):
    def __init__(self, message: str, *, status: int = 400, type_: str = "invalid_request_error",
                 code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.type = type_
        self.code = code


def error_response(message: str, *, status: int = 400, type_: str = "invalid_request_error",
                   code: str | None = None) -> JSONResponse:
    return JSONResponse(
        {"error": {"message": message, "type": type_, "code": code}},
        status_code=status,
    )


_DATA_URI_HEADER = "data:"


def int_field(body: dict[str, Any], *names: str, default: int, minimum: int,
              maximum: int) -> int:
    """Read an integer body field, rejecting bad types and out-of-range values."""
    for name in names:
        if body.get(name) is None:
            continue
        value = body[name]
        if isinstance(value, bool):
            raise ApiError(f"{name} must be an integer")
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise ApiError(f"{name} must be an integer") from None
        if not minimum <= number <= maximum:
            raise ApiError(f"{name} must be between {minimum} and {maximum}")
        return number
    return default


def require_messages(messages: Any) -> list[dict[str, Any]]:
    if not isinstance(messages, list) or not messages:
        raise ApiError("messages must be a non-empty array")
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise ApiError(f"messages[{index}] must be an object")
    return messages


async def reference_image_item(value: Any) -> dict[str, Any] | None:
    """Normalize an image reference (URL, data URI, base64 or dict) to an item."""
    url: str | None = None
    encoded: str | None = None
    mime: str | None = None
    if isinstance(value, dict):
        raw_url = value.get("url")
        if isinstance(raw_url, str) and raw_url.startswith(("http://", "https://")):
            url = raw_url
        raw_data = value.get("b64_json") or value.get("base64") or value.get("data")
        if isinstance(raw_data, str):
            encoded = raw_data
        raw_mime = value.get("mime_type") or value.get("mime")
        if isinstance(raw_mime, str):
            mime = raw_mime
    elif isinstance(value, str):
        text = value.strip()
        if text.startswith(("http://", "https://")):
            url = text
        elif text.startswith(_DATA_URI_HEADER):
            header, _, payload = text.partition(",")
            if ";base64" not in header:
                raise ApiError("image data URI must be base64 encoded")
            mime = header[len(_DATA_URI_HEADER):].split(";", 1)[0] or "image/png"
            encoded = payload
        else:
            encoded = text
    if url is None and encoded is None:
        return None
    if url is not None:
        try:
            data, fetched_mime = await fetch_public_media(url)
            mime = fetched_mime or mime
        except Exception as exc:  # network, TLS, redirect or SSRF-guard rejection
            raise ApiError(
                f"could not download reference image: {exc}",
                status=400, code="invalid_image",
            ) from exc
    else:
        try:
            data = base64.b64decode(encoded or "", validate=True)
        except Exception as exc:
            raise ApiError("reference image is not valid base64",
                           code="invalid_image") from exc
    if not data:
        raise ApiError("reference image is empty", code="invalid_image")
    if len(data) > MAX_MEDIA_BYTES:
        raise ApiError(f"reference image exceeds {media_limit_label()}", code="invalid_image")
    mime = (mime or mimetypes.guess_type(url or "")[0] or "image/png").split(";", 1)[0]
    if not mime.startswith("image/"):
        raise ApiError("reference image must be an image", code="invalid_image")
    return {
        "type": "image",
        "mime_type": mime,
        "data_base64": base64.b64encode(data).decode("ascii"),
        "filename": "reference" + (mimetypes.guess_extension(mime) or ".png"),
    }


async def first_video_reference(body: dict) -> dict[str, Any] | None:
    for key in ("reference_image", "image_url", "image"):
        if body.get(key) is not None:
            item = await reference_image_item(body[key])
            if item is not None:
                return item
    return None


def content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                for key in ("text", "content", "input_text"):
                    if isinstance(item.get(key), str):
                        parts.append(item[key])
                        break
        return "".join(parts)
    if isinstance(value, dict):
        for key in ("text", "content", "input_text"):
            if key in value:
                return content_text(value[key])
    return ""


def flatten_messages(messages: list[dict]) -> str:
    lines: list[str] = []
    for message in messages:
        rendered = render_tool_transcript([message])
        if rendered:
            lines.extend(rendered)
            continue
        role = str(message.get("role") or "user")
        text = content_text(message.get("content"))
        if not text.strip():
            continue
        lines.append(f"{role}: {text.strip()}")
    return "\n\n".join(lines)


async def latest_user_images(messages: list[dict]) -> list[dict]:
    """Convert image parts of the newest user turn into attachment items."""
    for message in reversed(messages):
        if str(message.get("role") or "") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            return []
        items: list[dict] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            kind = part.get("type")
            if kind == "image_url":
                value = part.get("image_url")
                if isinstance(value, dict):
                    value = value.get("url")
            elif kind == "input_image":
                value = part.get("image_url") or part.get("url")
            else:
                continue
            if value is None:
                continue
            item = await reference_image_item(value)
            if item is not None:
                items.append(item)
            if len(items) >= 8:
                return items
        return items
    return []


def tool_prompt(body: dict, prompt: str) -> str:
    if not tools_active(body):
        return prompt
    return (
        f"{build_tool_instructions(body)}\n\n{prompt}\n\n{build_tool_reminder()}"
    )


def last_user_text(messages: list[dict]) -> str:
    for message in reversed(messages):
        if str(message.get("role") or "") == "user":
            text = content_text(message.get("content")).strip()
            if text:
                return text
    return ""


def build_prompt(messages: list[dict], state) -> str:
    """Send only the new turn when the client repeats a known conversation."""
    system_parts = [content_text(m.get("content")).strip()
                    for m in messages if str(m.get("role") or "") == "system"]
    system_parts = [part for part in system_parts if part]
    system_text = "\n\n".join(system_parts)
    system_hash = hashlib.sha256(system_text.encode()).hexdigest()[:16]
    user_count = sum(1 for m in messages if str(m.get("role") or "") == "user")
    latest = last_user_text(messages)
    if not latest:
        raise ApiError("no non-empty user message")

    if system_hash != state.system_hash or user_count != state.last_user_count + 1:
        # New conversation or a rewritten transcript: give the agent the whole context.
        transcript = flatten_messages(messages)
        if system_text and not any(str(m.get("role")) == "system" for m in messages[:1]):
            transcript = f"System: {system_text}\n\n{transcript}"
        return transcript
    if system_text and not state.last_system_sent:
        return f"System: {system_text}\n\n{latest}"
    return latest


def openai_model(model: Any, default: str) -> str:
    return model.strip() if isinstance(model, str) and model.strip() else default


def record_turn(state, messages: list[dict[str, Any]]) -> None:
    """Remember the transcript shape so the next turn can send only the delta."""
    state.last_user_count = sum(1 for m in messages if str(m.get("role") or "") == "user")
    system_text = "\n\n".join(
        content_text(m.get("content")).strip()
        for m in messages if str(m.get("role") or "") == "system"
    )
    state.system_hash = hashlib.sha256(system_text.encode()).hexdigest()[:16]
    state.last_system_sent = True


async def conversation_prompt(messages: list[dict[str, Any]], state,
                              empty_message: str = "no non-empty user message"):
    """Build the outbound prompt and the newest user turn's image attachments."""
    images = await latest_user_images(messages)
    if last_user_text(messages):
        return build_prompt(messages, state), images
    if images:
        system_text = "\n\n".join(
            content_text(m.get("content")).strip()
            for m in messages if str(m.get("role") or "") == "system"
            and content_text(m.get("content")).strip()
        )
        fallback = "user: Describe the attached image."
        prompt = f"System: {system_text}\n\n{fallback}" if system_text else fallback
        return prompt, images
    raise ApiError(empty_message)


def sse(data: Any) -> str:
    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return f"data: {payload}\n\n"


def image_size_to_aspect(size: str | None) -> str:
    if not size:
        return "1:1"
    if ":" in size and "x" not in size:
        return size
    mapping = {
        "1024x1024": "1:1",
        "512x512": "1:1",
        "256x256": "1:1",
        "1792x1024": "16:9",
        "1024x1792": "9:16",
        "1344x768": "16:9",
        "768x1344": "9:16",
    }
    return mapping.get(size, size if ":" in size else "1:1")


class TaskStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.tasks: dict[str, dict] = {}
        if path is not None:
            self._load()

    def _load(self) -> None:
        try:
            if self.path and self.path.is_file():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self.tasks = {k: v for k, v in data.items() if isinstance(v, dict)}
        except Exception:  # noqa: BLE001
            self.tasks = {}

    def save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps(self.tasks, indent=2), encoding="utf-8")
            temp.replace(self.path)
        except Exception:  # noqa: BLE001
            pass

    def create(self, kind: str) -> dict:
        task_id = "task_" + uuid.uuid4().hex
        task = {
            "id": task_id,
            "object": "video" if kind == "video" else ("image_task" if kind == "image" else "task"),
            "kind": kind,
            "status": "queued",
            "progress": 0,
            "created_at": int(time.time()),
            "result": None,
            "error": None,
            "text": "",
        }
        self.tasks[task_id] = task
        self.save()
        return task

    def get(self, task_id: str) -> dict | None:
        return self.tasks.get(task_id)

    def update(self, task_id: str, **fields: Any) -> None:
        task = self.tasks.get(task_id)
        if task is None:
            return
        task.update(fields)
        self.save()


def recover_interrupted_tasks(store: TaskStore) -> int:
    """Fail tasks that were mid-flight when the previous process stopped.

    Video/image task ids are persisted, but nothing can resume an interrupted
    generation, so reporting them as queued/running forever misleads pollers.
    """
    stale = [task for task in store.tasks.values()
             if isinstance(task, dict) and task.get("status") in {"queued", "running"}]
    for task in stale:
        task["status"] = "failed"
        task["error"] = "server restarted before the task completed"
    if stale:
        store.save()
    return len(stale)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    media_root = Path(settings.state_dir.parent) / "data" / "media"
    media = MediaStore(media_root, public_base=settings.public_base or "/v1/media")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.ensure_state_dir()
        protocol = ProtocolConfig.load()
        log.info("protocol: lb=%s ws=%s source=%s", protocol.lb_host,
                 protocol.ws_path, protocol.source)
        pool = AccountPool(settings, protocol=protocol)
        app.state.pool = pool
        app.state.http = pool.accounts[0].http
        app.state.chat = ChatService(settings, pool, protocol=protocol)
        app.state.media = media
        app.state.tasks = TaskStore(settings.state_dir / "tasks.json")
        app.state.image_tasks = {}
        for task in app.state.tasks.tasks.values():
            if isinstance(task, dict) and task.get("kind") == "image":
                app.state.image_tasks[task["id"]] = task
                key = task.get("idempotency_key")
                if key:
                    app.state.image_tasks[key] = task
        recover_interrupted_tasks(app.state.tasks)
        app.state.sem = asyncio.Semaphore(int(os.environ.get("MUSE2API_CONCURRENCY", "2")))
        app.state.locks = {}
        app.state.started_at = time.time()
        app.state.last_active_at = time.time()
        app.state.is_sleeping = False
        app.state.last_renewal_at = None
        keepalive = asyncio.create_task(_keepalive(app))

        try:
            await pool.bootstrap()
        except Exception as exc:  # noqa: BLE001 - server still starts; health reports it
            log.error("initial muse.ai bootstrap failed: %s", exc)

        try:
            yield
        finally:
            keepalive.cancel()
            # keepalive task handles both ping and 6h cookie renewal
            await pool.close()

    app = FastAPI(title="muse2api-http", version=__version__, lifespan=lifespan)
    now_init = time.time()
    app.state.started_at = now_init
    app.state.last_active_at = now_init
    app.state.is_sleeping = False
    app.state.last_renewal_at = None
    video_block_file = settings.state_dir / "video_block.json"
    app.state.video_block_file = video_block_file
    app.state.video_block_until = load_video_block(video_block_file)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def _track_activity(request: Request, call_next):
        if request.url.path.startswith("/v1/"):
            app.state.last_active_at = time.time()
            app.state.is_sleeping = False
        return await call_next(request)

    async def _keepalive(app: FastAPI) -> None:
        last_refresh = time.time()
        last_cookie_renew = time.time()
        while True:
            await asyncio.sleep(60)
            now = time.time()

            # Always renew cookies via lightweight GET /api/session every 6 hours, even during idle sleep
            if now - last_cookie_renew >= 6 * 3600:
                last_cookie_renew = now
                try:
                    renewed = await app.state.pool.renew_cookies()
                except Exception as exc:  # noqa: BLE001 - never kill the loop
                    log.warning("keepalive cookie renewal failed: %s", exc)
                else:
                    if renewed:
                        app.state.last_renewal_at = now
                        log.info(
                            "keepalive: renewed muse.ai session cookies for %d account(s)",
                            renewed,
                        )
                    else:
                        log.warning("keepalive: no account could renew its cookies")

            idle_threshold = settings.idle_sleep_seconds
            if idle_threshold > 0:
                idle_time = now - getattr(app.state, "last_active_at", app.state.started_at)
                if idle_time >= idle_threshold:
                    if not getattr(app.state, "is_sleeping", False):
                        app.state.is_sleeping = True
                        log.info(
                            "idle sleep: no activity for %ds (threshold %ds); pausing WebSocket keepalive",
                            int(idle_time), int(idle_threshold),
                        )
                        try:
                            await app.state.pool.close_idle()
                        except Exception as exc:  # noqa: BLE001
                            log.debug("error closing idle pool: %s", exc)
                    continue
            # Awake: refresh if it's been 30m since last refresh
            if now - last_refresh >= 30 * 60:
                last_refresh = now
                try:
                    await app.state.pool.refresh_all()
                    log.info("keepalive: refreshed muse.ai sessions")
                except Exception as exc:  # noqa: BLE001
                    log.warning("keepalive failed: %s", exc)

    def check_auth(request: Request) -> None:
        if not settings.api_key:
            return
        header = request.headers.get("authorization") or ""
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        # Constant-time compare: a plain != leaks the key prefix to timing probes.
        if not hmac.compare_digest(token, settings.api_key):
            raise ApiError("invalid API key", status=401, type_="authentication_error")

    def key_for(request: Request) -> str:
        return session_key(
            request.headers.get("x-muse-session"),
            request.headers.get("authorization") if not settings.api_key else None,
        )

    def lock_for(key: str) -> asyncio.Lock:
        lock = app.state.locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            app.state.locks[key] = lock
        return lock

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError):
        return error_response(str(exc), status=exc.status, type_=exc.type, code=exc.code)

    @app.exception_handler(MuseAuthError)
    async def _auth_error(_: Request, exc: MuseAuthError):
        return error_response(
            f"muse.ai authentication failed: {exc}",
            status=401,
            type_="authentication_error",
            code="muse_auth",
        )

    @app.exception_handler(MuseUpstreamError)
    async def _upstream_error(_: Request, exc: MuseUpstreamError):
        return error_response(str(exc), status=502, type_="upstream_error", code="muse_upstream")

    @app.exception_handler(GatewayError)
    async def _gateway_error(_: Request, exc: GatewayError):
        return error_response(
            str(exc) or "muse gateway error",
            status=502,
            type_="upstream_error",
            code="muse_gateway",
        )

    @app.exception_handler(PoolExhausted)
    async def _pool_exhausted(_: Request, exc: PoolExhausted):
        return error_response(
            str(exc),
            status=503,
            type_="unavailable_error",
            code="muse_unavailable",
        )

    # ------------------------------------------------------------------ misc

    @app.get("/")
    async def index():
        return {
            "name": "muse2api-http",
            "version": __version__,
            "mode": "noise-http (no browser)",
            "endpoints": [
                "/v1/models", "/v1/chat/completions", "/v1/responses",
                "/v1/images/generations", "/v1/images/edits",
                "/v1/videos", "/v1/media/{name}", "/healthz",
            ],
        }

    @app.get("/healthz")
    async def healthz():
        pool: AccountPool | None = getattr(app.state, "pool", None)
        primary = getattr(app.state, "http", None)
        now = time.time()
        started = getattr(app.state, "started_at", now)
        last_active = getattr(app.state, "last_active_at", started)
        idle_s = int(now - last_active)
        is_sleeping = getattr(app.state, "is_sleeping", False)
        return {
            "ok": bool(primary and primary._cached_session is not None),
            "mode": settings.session_mode,
            "vm_id": primary._cached_session.vm_id if (primary and primary._cached_session) else None,
            "accounts": pool.status() if pool else [],
            "uptime_s": int(now - started),
            "idle_s": idle_s,
            "sleeping": is_sleeping,
            "started_at": started,
            "last_renewal_at": getattr(app.state, "last_renewal_at", None),
        }

    @app.get("/v1/models")
    async def models(request: Request):
        check_auth(request)
        ids = sorted(MODEL_ALIASES | {settings.model})
        return {
            "object": "list",
            "data": [
                {"id": model_id, "object": "model", "created": int(app.state.started_at),
                 "owned_by": "muse2api-http"}
                for model_id in ids
            ],
        }

    @app.post("/v1/session/refresh")
    async def refresh_session(request: Request):
        check_auth(request)
        _account, session = await app.state.pool.bootstrap()
        return {"ok": True, "vm_id": session.vm_id}

    @app.post("/v1/session/reset")
    async def reset_session(request: Request):
        check_auth(request)
        state = app.state.chat.sessions.reset(key_for(request))
        return {"ok": True, "session_id": state.session_id, "node_id": state.node_id}

    # ------------------------------------------------------------------ chat

    async def _chat_once(request: Request, prompt: str, timeout: float,
                         on_delta=None, items: list[dict] | None = None) -> ChatResult:
        key = key_for(request)
        async with app.state.sem, lock_for(key):
            return await app.state.chat.run(
                prompt, session_key_value=key, timeout=timeout, on_delta=on_delta,
                items=items,
            )

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        check_auth(request)
        try:
            body = await request.json()
        except Exception:
            raise ApiError("invalid JSON body") from None
        messages = require_messages(body.get("messages"))
        model = openai_model(body.get("model"), settings.model)
        stream = body.get("stream") is True

        key = key_for(request)
        state = app.state.chat.sessions.get(key)
        prompt, images = await conversation_prompt(messages, state)
        prompt = tool_prompt(body, prompt)
        bridge_active = tools_active(body)

        if not stream:
            result = await _chat_once(
                request, prompt, settings.chat_timeout, items=images or None
            )
            record_turn(state, messages)
            app.state.chat.sessions.save()
            message: dict[str, Any] = {"role": "assistant", "content": result.text}
            finish_reason = "stop"
            if bridge_active:
                repaired = apply_tool_bridge(result.text, body)
                if repaired["applied"]:
                    message["content"] = repaired["content"] or None
                    message["tool_calls"] = repaired["tool_calls"]
                    finish_reason = "tool_calls"
            return {
                "id": f"chatcmpl-{result.message_id}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [{
                    "index": 0,
                    "message": message,
                    "finish_reason": finish_reason,
                }],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }

        completion_id = "chatcmpl-" + uuid.uuid4().hex
        created = int(time.time())

        async def event_stream() -> AsyncIterator[str]:
            queue: asyncio.Queue[Any] = asyncio.Queue()

            def push(chunk: str) -> None:
                if not bridge_active:
                    queue.put_nowait(("delta", chunk))

            async def runner() -> None:
                try:
                    result = await _chat_once(
                        request, prompt, settings.chat_timeout,
                        None if bridge_active else push, items=images or None,
                    )
                    record_turn(state, messages)
                    app.state.chat.sessions.save()
                    await queue.put(("done", result))
                except Exception as exc:  # noqa: BLE001
                    await queue.put(("error", exc))
            task = asyncio.create_task(runner())

            yield sse({
                "id": completion_id, "object": "chat.completion.chunk", "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""},
                             "finish_reason": None}],
            })
            finish_reason = "stop"
            try:
                final: ChatResult | None = None
                while True:
                    kind, value = await queue.get()
                    if kind == "delta":
                        yield sse({
                            "id": completion_id, "object": "chat.completion.chunk",
                            "created": created, "model": model,
                            "choices": [{"index": 0, "delta": {"content": value},
                                         "finish_reason": None}],
                        })
                    elif kind == "done":
                        final = value
                        break
                    elif kind == "error":
                        raise value
                if bridge_active and final is not None:
                    repaired = apply_tool_bridge(final.text, body)
                    if repaired["applied"]:
                        for index, call in enumerate(repaired["tool_calls"]):
                            yield sse({
                                "id": completion_id,
                                "object": "chat.completion.chunk",
                                "created": created, "model": model,
                                "choices": [{
                                    "index": 0,
                                    "delta": {"tool_calls": [{
                                        "index": index,
                                        "id": call["id"],
                                        "type": "function",
                                        "function": call["function"],
                                    }]},
                                    "finish_reason": None,
                                }],
                            })
                        finish_reason = "tool_calls"
                    else:
                        text = final.text
                        for start in range(0, len(text), 400):
                            yield sse({
                                "id": completion_id,
                                "object": "chat.completion.chunk",
                                "created": created, "model": model,
                                "choices": [{"index": 0,
                                             "delta": {"content": text[start:start + 400]},
                                             "finish_reason": None}],
                            })
                yield sse({
                    "id": completion_id, "object": "chat.completion.chunk", "created": created,
                    "model": model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
                })
                options = body.get("stream_options")
                if isinstance(options, dict) and options.get("include_usage") is True:
                    yield sse({
                        "id": completion_id, "object": "chat.completion.chunk",
                        "created": created, "model": model, "choices": [],
                        "usage": {"prompt_tokens": 0, "completion_tokens": 0,
                                  "total_tokens": 0},
                    })
                yield sse("[DONE]")
            except Exception as exc:  # noqa: BLE001
                yield sse({"error": {"message": str(exc), "type": "server_error"}})
                yield sse("[DONE]")
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ------------------------------------------------------------------ responses API

    @app.post("/v1/responses")
    async def responses(request: Request):
        check_auth(request)
        try:
            body = await request.json()
        except Exception:
            raise ApiError("invalid JSON body") from None
        raw_input = body.get("input")
        messages: list[dict] = []
        if isinstance(raw_input, str):
            messages = [{"role": "user", "content": raw_input}]
        elif isinstance(raw_input, list):
            call_names: dict[str, str] = {}
            for item in raw_input:
                if not isinstance(item, dict):
                    continue
                kind = item.get("type")
                if kind == "message":
                    messages.append({"role": item.get("role"), "content": item.get("content")})
                elif kind == "function_call":
                    call_id = str(item.get("call_id") or item.get("id") or "")
                    name = str(item.get("name") or "")
                    if call_id:
                        call_names[call_id] = name
                    messages.append({
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{
                            "id": call_id or "call_" + uuid.uuid4().hex,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": item.get("arguments") or "{}",
                            },
                        }],
                    })
                elif kind == "function_call_output":
                    call_id = str(item.get("call_id") or "")
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call_id,
                        "name": call_names.get(call_id),
                        "content": item.get("output") if isinstance(item.get("output"), str)
                        else json.dumps(item.get("output"), ensure_ascii=False),
                    })
                elif "role" in item:
                    messages.append(item)
        instructions = body.get("instructions")
        if isinstance(instructions, str) and instructions.strip():
            messages.insert(0, {"role": "system", "content": instructions})
        if not messages:
            raise ApiError("input must contain at least one message")
        model = openai_model(body.get("model"), settings.model)
        stream = body.get("stream") is True
        key = key_for(request)
        state = app.state.chat.sessions.get(key)
        prompt, images = await conversation_prompt(
            messages, state, empty_message="input must contain a non-empty user message"
        )
        prompt = tool_prompt(body, prompt)
        bridge_active = tools_active(body)

        if not stream:
            result = await _chat_once(
                request, prompt, settings.chat_timeout, items=images or None
            )
            record_turn(state, messages)
            app.state.chat.sessions.save()
            text = result.text
            tool_calls = None
            if bridge_active:
                repaired = apply_tool_bridge(result.text, body)
                if repaired["applied"]:
                    text = repaired["content"]
                    tool_calls = repaired["tool_calls"]
            return _responses_payload(
                text, model, body.get("id") or "resp_" + uuid.uuid4().hex,
                tool_calls=tool_calls,
            )

        response_id = "resp_" + uuid.uuid4().hex

        async def stream_responses() -> AsyncIterator[str]:
            created = int(time.time())
            message_item_id = "msg_" + uuid.uuid4().hex
            queue: asyncio.Queue[Any] = asyncio.Queue()

            def push(chunk: str) -> None:
                if not bridge_active:
                    queue.put_nowait(("delta", chunk))

            async def runner() -> None:
                try:
                    result = await _chat_once(
                        request, prompt, settings.chat_timeout,
                        None if bridge_active else push, items=images or None,
                    )
                    record_turn(state, messages)
                    app.state.chat.sessions.save()
                    await queue.put(("done", result))
                except Exception as exc:  # noqa: BLE001
                    await queue.put(("error", exc))
            task = asyncio.create_task(runner())

            def ev(event: str, data: dict) -> str:
                return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

            def message_events(text: str, output_index: int) -> list[str]:
                events = [
                    ev("response.output_item.added", {
                        "type": "response.output_item.added",
                        "output_index": output_index,
                        "item": {"id": message_item_id, "type": "message",
                                 "role": "assistant", "status": "in_progress", "content": []},
                    }),
                    ev("response.content_part.added", {
                        "type": "response.content_part.added",
                        "item_id": message_item_id, "output_index": output_index,
                        "content_index": 0,
                        "part": {"type": "output_text", "text": "", "annotations": []},
                    }),
                ]
                for start in range(0, len(text), 400):
                    events.append(ev("response.output_text.delta", {
                        "type": "response.output_text.delta",
                        "item_id": message_item_id, "output_index": output_index,
                        "content_index": 0, "delta": text[start:start + 400],
                    }))
                events.extend([
                    ev("response.output_text.done", {
                        "type": "response.output_text.done",
                        "item_id": message_item_id, "output_index": output_index,
                        "content_index": 0, "text": text,
                    }),
                    ev("response.output_item.done", {
                        "type": "response.output_item.done",
                        "output_index": output_index,
                        "item": {"id": message_item_id, "type": "message",
                                 "role": "assistant", "status": "completed",
                                 "content": [{"type": "output_text", "text": text,
                                              "annotations": []}]},
                    }),
                ])
                return events

            base = {
                "id": response_id, "object": "response", "created_at": created,
                "status": "in_progress", "model": model, "output": [],
            }
            yield ev("response.created", {"type": "response.created", "response": base})
            if not bridge_active:
                yield ev("response.output_item.added", {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {"id": message_item_id, "type": "message", "role": "assistant",
                             "status": "in_progress", "content": []},
                })
                yield ev("response.content_part.added", {
                    "type": "response.content_part.added",
                    "item_id": message_item_id, "output_index": 0, "content_index": 0,
                    "part": {"type": "output_text", "text": "", "annotations": []},
                })
            full = ""
            try:
                final: ChatResult | None = None
                while True:
                    kind, value = await queue.get()
                    if kind == "delta":
                        full += value
                        if not bridge_active:
                            yield ev("response.output_text.delta", {
                                "type": "response.output_text.delta",
                                "item_id": message_item_id, "output_index": 0,
                                "content_index": 0, "delta": value,
                            })
                    elif kind == "done":
                        final = value
                        break
                    elif kind == "error":
                        raise value

                text = final.text if final is not None else full
                tool_calls: list[dict] | None = None
                if bridge_active and final is not None:
                    repaired = apply_tool_bridge(text, body)
                    if repaired["applied"]:
                        text = repaired["content"]
                        tool_calls = repaired["tool_calls"]
                    if text.strip() or not tool_calls:
                        for event in message_events(text, 0):
                            yield event
                else:
                    yield ev("response.output_text.done", {
                        "type": "response.output_text.done",
                        "item_id": message_item_id, "output_index": 0, "content_index": 0,
                        "text": text,
                    })
                    yield ev("response.output_item.done", {
                        "type": "response.output_item.done",
                        "output_index": 0,
                        "item": {"id": message_item_id, "type": "message",
                                 "role": "assistant", "status": "completed",
                                 "content": [{"type": "output_text", "text": text,
                                              "annotations": []}]},
                    })

                output_base = 1 if (bridge_active and text.strip()) else 0
                for index, call in enumerate(tool_calls or []):
                    fn = call.get("function") or {}
                    args = fn.get("arguments") or "{}"
                    fc_id = "fc_" + uuid.uuid4().hex
                    output_index = output_base + index
                    yield ev("response.output_item.added", {
                        "type": "response.output_item.added",
                        "output_index": output_index,
                        "item": {"id": fc_id, "type": "function_call",
                                 "call_id": call.get("id"), "name": fn.get("name") or "",
                                 "arguments": "", "status": "in_progress"},
                    })
                    yield ev("response.function_call_arguments.delta", {
                        "type": "response.function_call_arguments.delta",
                        "item_id": fc_id, "output_index": output_index, "delta": args,
                    })
                    yield ev("response.function_call_arguments.done", {
                        "type": "response.function_call_arguments.done",
                        "item_id": fc_id, "output_index": output_index, "arguments": args,
                    })
                    yield ev("response.output_item.done", {
                        "type": "response.output_item.done",
                        "output_index": output_index,
                        "item": {"id": fc_id, "type": "function_call",
                                 "call_id": call.get("id"), "name": fn.get("name") or "",
                                 "arguments": args, "status": "completed"},
                    })

                yield ev("response.completed", {
                    "type": "response.completed",
                    "response": _responses_payload(
                        text, model, response_id, created, tool_calls=tool_calls
                    ),
                })
            except Exception as exc:  # noqa: BLE001
                yield ev("response.failed", {
                    "type": "response.failed",
                    "response": {**base, "status": "failed",
                                 "error": {"message": str(exc), "type": "server_error"}},
                })
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        return StreamingResponse(
            stream_responses(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ------------------------------------------------------------------ images

    async def _generate_images(prompt: str, n: int, size: str, response_format: str,
                               model: str, timeout: float,
                               reference_image: str | None = None) -> dict:
        aspect = image_size_to_aspect(size)
        instruction = (
            f"Generate {max(1, n)} image(s) with aspect ratio {aspect} for the following request. "
            "Use your image generation tool now, do not ask questions.\n\n" + prompt
        )
        items = None
        if reference_image:
            instruction = (
                "Edit/transform the attached reference image according to this request. "
                "Use your image editing tool now, do not ask questions.\n\n" + prompt
            )
            item = await reference_image_item(reference_image)
            if item is None:
                raise ApiError("reference image is empty", code="invalid_image")
            items = [item, {"type": "text", "text": instruction}]
        async with app.state.sem:
            result = await app.state.chat.run_with_media(
                instruction, kind="image", timeout=timeout, items=items
            )
        images = [ref for ref in result.media if ref.kind == "image"]
        if not images:
            raise ApiError(
                result.text or "muse did not return an image",
                status=502, type_="upstream_error", code="no_image",
            )

        _account, _session, conn = await app.state.pool.connect()
        data: list[dict] = []
        try:
            for ref in images[: max(1, n)]:
                try:
                    raw = await download_media(conn, ref)
                except Exception as exc:  # noqa: BLE001
                    log.warning("image download failed: %s", exc)
                    continue
                name = app.state.media.save(raw, ref.extension)
                entry: dict[str, Any] = {"revised_prompt": ref.label or prompt,
                                         "kind": "image"}
                entry["b64_json"] = base64.b64encode(raw).decode("ascii")
                entry["url"] = app.state.media.url_for(name)
                entry["bytes"] = len(raw)
                data.append(entry)
        finally:
            await conn.close()
        if not data:
            raise ApiError("generated image could not be downloaded", status=502,
                           type_="upstream_error", code="media_download")
        return {"created": int(time.time()), "data": data}

    @app.post("/v1/images/generations")
    async def images_generations(request: Request):
        check_auth(request)
        try:
            body = await request.json()
        except Exception:
            raise ApiError("invalid JSON body") from None
        prompt = body.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ApiError("prompt is required")
        n = int_field(body, "n", default=1, minimum=1, maximum=10)
        size = str(body.get("size") or "1024x1024")
        response_format = str(body.get("response_format") or "url")
        reference_image = body.get("reference_image")
        return await _generate_images(
            prompt.strip(), n, size, response_format,
            openai_model(body.get("model"), settings.model),
            float(os.environ.get("MUSE_IMAGE_TIMEOUT", "240")),
            reference_image=reference_image if isinstance(reference_image, str) else None,
        )

    @app.post("/v1/images/edits")
    async def images_edits(
        request: Request,
        image: UploadFile = File(...),
        prompt: str = Form(...),
        size: str = Form("1024x1024"),
        response_format: str = Form("url"),
        model: str = Form(""),
        n: int = Form(1),
    ):
        check_auth(request)
        raw = await image.read(MAX_MEDIA_BYTES + 1)
        if not raw:
            raise ApiError("image is empty")
        if len(raw) > MAX_MEDIA_BYTES:
            raise ApiError(
                f"image exceeds {media_limit_label()}",
                status=413,
                code="invalid_image",
            )
        mime = image.content_type or mimetypes.guess_type(image.filename or "")[0] or "image/png"
        item = {
            "type": "image",
            "mime_type": mime,
            "data_base64": base64.b64encode(raw).decode("ascii"),
            "filename": image.filename or "image.png",
        }
        instruction = (
            "Edit the attached image according to this request. Use your image editing tool now, "
            "do not ask questions.\n\n" + prompt.strip()
        )
        async with app.state.sem:
            result = await app.state.chat.run_with_media(
                instruction,
                kind="image",
                timeout=float(os.environ.get("MUSE_IMAGE_TIMEOUT", "240")),
                items=[item, {"type": "text", "text": instruction}],
            )
        images = [ref for ref in result.media if ref.kind == "image"]
        if not images:
            raise ApiError(result.text or "muse did not return an edited image", status=502,
                           type_="upstream_error", code="no_image")
        _account, _session, conn = await app.state.pool.connect()
        try:
            ref = images[-1]
            raw_out = await download_media(conn, ref)
        finally:
            await conn.close()
        name = app.state.media.save(raw_out, ref.extension)
        entry: dict[str, Any] = {"revised_prompt": ref.label or prompt, "kind": "image",
                                 "bytes": len(raw_out)}
        entry["b64_json"] = base64.b64encode(raw_out).decode("ascii")
        entry["url"] = app.state.media.url_for(name)
        return {"created": int(time.time()), "data": [entry]}

    # ------------------------------------------------------------------ videos

    async def _run_video_task(task: dict, prompt: str, duration: int, size: str,
                              items: list[dict] | None = None) -> None:
        task["status"] = "running"
        task["progress"] = 5
        aspect = image_size_to_aspect(size)
        framing = (
            "Use the attached image as the first frame. "
            if items else ""
        )
        instruction = (
            f"{framing}Generate a {duration} second video with aspect ratio {aspect} for the "
            "following request. Use your video generation tool now, do not ask questions.\n\n"
            + prompt
        )
        try:
            async with app.state.sem:
                result = await app.state.chat.run_with_media(
                    instruction, kind="video",
                    timeout=float(os.environ.get("MUSE_VIDEO_TIMEOUT", "600")),
                    items=([*items, {"type": "text", "text": instruction}] if items else None),
                    refusal_markers=_VIDEO_REFUSAL_MARKERS,
                )
            task["progress"] = 70
            task["text"] = result.text
            videos = [ref for ref in result.media if ref.kind == "video"]
            if not videos:
                code = classify_video_error(result.text or "")
                task["status"] = "failed"
                task.update(video_failure(code, result.text))
                if code == "video_unavailable_region":
                    app.state.video_block_until = time.time() + float(
                        os.environ.get("MUSE_VIDEO_BLOCK_SECONDS", "21600")
                    )
                    save_video_block(app.state.video_block_file, app.state.video_block_until)
                return
            _account, _session, conn = await app.state.pool.connect()
            try:
                ref = videos[-1]
                raw = await download_media(conn, ref)
            finally:
                await conn.close()
            name = app.state.media.save(raw, ref.extension or ".mp4")
            task["status"] = "succeeded"
            task["progress"] = 100
            task["result"] = {"url": app.state.media.url_for(name),
                              "bytes": len(raw), "revised_prompt": ref.label or prompt}
        except Exception as exc:  # noqa: BLE001
            code = classify_video_error(str(exc))
            task["status"] = "failed"
            task.update(video_failure(code, str(exc)))
            if code == "video_unavailable_region":
                app.state.video_block_until = time.time() + float(
                    os.environ.get("MUSE_VIDEO_BLOCK_SECONDS", "21600")
                )
                save_video_block(app.state.video_block_file, app.state.video_block_until)
        app.state.tasks.save()

    @app.post("/v1/videos")
    async def create_video(request: Request):
        check_auth(request)
        try:
            body = await request.json()
        except Exception:
            raise ApiError("invalid JSON body") from None
        prompt = body.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ApiError("prompt is required")
        if time.time() < getattr(app.state, "video_block_until", 0.0):
            raise ApiError(
                VIDEO_ERROR_MESSAGES["video_unavailable_region"],
                status=503,
                type_="upstream_error",
                code="video_unavailable_region",
            )
        duration = int_field(body, "duration", "seconds", default=5, minimum=1, maximum=60)
        size = str(body.get("size") or body.get("aspect_ratio") or "16:9")
        reference = await first_video_reference(body)
        reference_items = [reference] if reference else []
        task = app.state.tasks.create("video")
        asyncio.create_task(
            _run_video_task(task, prompt.strip(), duration, size, reference_items)
        )
        return {"id": task["id"], "object": task["object"], "status": "queued",
                "created_at": task["created_at"]}

    @app.get("/v1/videos/{task_id}")
    async def get_video(task_id: str, request: Request):
        check_auth(request)
        task = app.state.tasks.get(task_id)
        if task is None:
            raise ApiError("video task not found", status=404, code="not_found")
        return task

    # ------------------------------------------------------------------ async images (idempotent tasks)
    def _image_task_key(request: Request, body: dict) -> str | None:
        key = request.headers.get("idempotency-key") or body.get("idempotency_key")
        return str(key).strip() if key else None

    @app.post("/v1/images/tasks")
    async def create_image_task(request: Request):
        check_auth(request)
        try:
            body = await request.json()
        except Exception:
            raise ApiError("invalid JSON body") from None
        prompt = body.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ApiError("prompt is required")
        n = int_field(body, "n", default=1, minimum=1, maximum=10)
        size = str(body.get("size") or "1024x1024")
        response_format = str(body.get("response_format") or "url")
        model = openai_model(body.get("model"), settings.model)
        timeout = float(os.environ.get("MUSE_IMAGE_TIMEOUT", "240"))
        key = _image_task_key(request, body)
        if key and key in app.state.image_tasks:
            reused = app.state.image_tasks[key]
            return {"id": reused["id"], "object": "image_task",
                    "status": reused["status"], "created_at": reused["created_at"],
                    "reused": True}
        task = app.state.tasks.create("image")
        if key:
            task["idempotency_key"] = key
        app.state.image_tasks[task["id"]] = task
        if key:
            app.state.image_tasks[key] = task
        app.state.tasks.save()
        async def _run():
            task["status"] = "running"
            task["progress"] = 5
            try:
                reference_image = body.get("reference_image")
                result = await _generate_images(prompt.strip(), n, size, response_format, model, timeout,
                                               reference_image=reference_image if isinstance(reference_image, str) else None)
                task["status"] = "succeeded"
                task["progress"] = 100
                task["result"] = result
                app.state.tasks.save()
            except Exception as exc:  # noqa: BLE001
                task["status"] = "failed"
                task["error"] = str(exc)
                app.state.tasks.save()
        asyncio.create_task(_run())
        return {"id": task["id"], "object": "image_task", "status": "queued",
                "created_at": task["created_at"]}

    @app.get("/v1/images/tasks/{task_id}")
    async def get_image_task(task_id: str, request: Request):
        check_auth(request)
        task = app.state.tasks.get(task_id)
        if task is None or task.get("kind") != "image":
            raise ApiError("image task not found", status=404, code="not_found")
        return task

    # ------------------------------------------------------------------ sessions / history

    @app.get("/v1/sessions")
    async def list_sessions(request: Request):

        check_auth(request)
        _account, _session, conn = await app.state.pool.connect()
        try:
            resp = await conn.request("GET", app.state.chat.protocol.route("sessions.list").path)
            return resp.json() if hasattr(resp, "json") else {"raw": resp.text()}
        finally:
            await conn.close()

    @app.get("/v1/sessions/{session_id}/history")
    async def session_history(session_id: str, request: Request):
        check_auth(request)
        _account, _session, conn = await app.state.pool.connect()
        try:
            resp = await conn.request(
                "GET", app.state.chat.protocol.route("chat.history").path,
                {"session_id": session_id},
            )
            return resp.json() if hasattr(resp, "json") else {"raw": resp.text()}
        finally:
            await conn.close()


    # ------------------------------------------------------------------ media

    @app.get("/v1/media/{name}")
    @app.get("/media/{name}")
    async def get_media(name: str):
        path = app.state.media.path_for(name)
        if path is None:
            raise ApiError("media not found", status=404, code="not_found")
        return FileResponse(path)

    return app


def _responses_payload(text: str, model: str, response_id: str, created: int | None = None,
                       tool_calls: list[dict] | None = None) -> dict:
    output: list[dict] = []
    if text.strip() or not tool_calls:
        output.append({
            "id": "msg_" + uuid.uuid4().hex,
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        })
    for call in tool_calls or []:
        fn = call.get("function") or {}
        output.append({
            "id": "fc_" + uuid.uuid4().hex,
            "type": "function_call",
            "call_id": call.get("id") or "call_" + uuid.uuid4().hex,
            "name": fn.get("name") or "",
            "arguments": fn.get("arguments") or "{}",
            "status": "completed",
        })
    return {
        "id": response_id,
        "object": "response",
        "created_at": created or int(time.time()),
        "status": "completed",
        "model": model,
        "output": output,
        "output_text": text,
        "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
    }
