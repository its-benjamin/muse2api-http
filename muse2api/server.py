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
import json
import logging
import mimetypes
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from .chat import ChatResult, ChatService, session_key
from .config import Settings
from .http import MuseAuthError, MuseHTTP, MuseUpstreamError
from .media import MediaRef, MediaStore, download_media
from .pool import AccountPool
from .protocol import ProtocolConfig

log = logging.getLogger("muse2api.server")

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
        role = str(message.get("role") or "user")
        text = content_text(message.get("content"))
        if not text.strip():
            continue
        lines.append(f"{role}: {text.strip()}")
    return "\n\n".join(lines)


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


def openai_model(model: str | None, default: str) -> str:
    if not model:
        return default
    return model


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
    def __init__(self) -> None:
        self.tasks: dict[str, dict] = {}

    def create(self, kind: str) -> dict:
        task_id = "task_" + uuid.uuid4().hex
        task = {
            "id": task_id,
            "object": "video" if kind == "video" else "task",
            "kind": kind,
            "status": "queued",
            "progress": 0,
            "created_at": int(time.time()),
            "result": None,
            "error": None,
            "text": "",
        }
        self.tasks[task_id] = task
        return task

    def get(self, task_id: str) -> dict | None:
        return self.tasks.get(task_id)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    media_root = Path(settings.state_dir.parent) / "data" / "media"
    media = MediaStore(media_root, public_base="/v1/media")

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
        app.state.tasks = TaskStore()
        app.state.sem = asyncio.Semaphore(int(__import__("os").environ.get("MUSE2API_CONCURRENCY", "2")))
        app.state.locks = {}
        app.state.started_at = time.time()
        keepalive = asyncio.create_task(_keepalive(app))

        try:
            await pool.bootstrap()
        except Exception as exc:  # noqa: BLE001 - server still starts; health reports it
            log.error("initial muse.ai bootstrap failed: %s", exc)

        try:
            yield
        finally:
            keepalive.cancel()
            try:
                await keepalive
            except asyncio.CancelledError:
                pass
            await pool.close()

    app = FastAPI(title="muse2api-http", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    async def _keepalive(app: FastAPI) -> None:
        while True:
            await asyncio.sleep(30 * 60)
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
        if token != settings.api_key:
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

    # ------------------------------------------------------------------ misc

    @app.get("/")
    async def index():
        return {
            "name": "muse2api-http",
            "version": "0.1.0",
            "mode": "noise-http (no browser)",
            "endpoints": [
                "/v1/models", "/v1/chat/completions", "/v1/responses",
                "/v1/images/generations", "/v1/images/edits",
                "/v1/videos", "/v1/media/{name}", "/healthz",
            ],
        }

    @app.get("/healthz")
    async def healthz():
        pool: AccountPool = app.state.pool
        primary = app.state.http
        return {
            "ok": primary._cached_session is not None,
            "mode": settings.session_mode,
            "vm_id": primary._cached_session.vm_id if primary._cached_session else None,
            "accounts": pool.status(),
            "uptime_s": int(time.time() - app.state.started_at),
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
                         on_delta=None) -> ChatResult:
        key = key_for(request)
        async with app.state.sem, lock_for(key):
            return await app.state.chat.run(
                prompt, session_key_value=key, timeout=timeout, on_delta=on_delta
            )

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        check_auth(request)
        try:
            body = await request.json()
        except Exception:
            raise ApiError("invalid JSON body")
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            raise ApiError("messages must be a non-empty array")
        model = openai_model(body.get("model"), settings.model)
        stream = body.get("stream") is True

        key = key_for(request)
        state = app.state.chat.sessions.get(key)
        try:
            prompt = build_prompt(messages, state)
        except ApiError:
            raise

        if not stream:
            result = await _chat_once(request, prompt, settings.chat_timeout)
            state.last_user_count = sum(1 for m in messages if str(m.get("role")) == "user")
            state.system_hash = hashlib.sha256(
                "\n\n".join(content_text(m.get("content")).strip()
                            for m in messages if str(m.get("role")) == "system").encode()
            ).hexdigest()[:16]
            state.last_system_sent = True
            app.state.chat.sessions._save()
            return {
                "id": f"chatcmpl-{result.message_id}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": result.text},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }

        completion_id = "chatcmpl-" + uuid.uuid4().hex
        created = int(time.time())

        async def event_stream() -> AsyncIterator[str]:
            queue: asyncio.Queue[Any] = asyncio.Queue()

            def push(chunk: str) -> None:
                queue.put_nowait(("delta", chunk))

            async def runner() -> None:
                try:
                    result = await _chat_once(request, prompt, settings.chat_timeout, push)
                    state.last_user_count = sum(1 for m in messages if str(m.get("role")) == "user")
                    state.system_hash = hashlib.sha256(
                        "\n\n".join(content_text(m.get("content")).strip()
                                    for m in messages if str(m.get("role")) == "system").encode()
                    ).hexdigest()[:16]
                    state.last_system_sent = True
                    app.state.chat.sessions._save()
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
            try:
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
                        break
                    elif kind == "error":
                        raise value
                yield sse({
                    "id": completion_id, "object": "chat.completion.chunk", "created": created,
                    "model": model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
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
            raise ApiError("invalid JSON body")
        raw_input = body.get("input")
        messages: list[dict] = []
        if isinstance(raw_input, str):
            messages = [{"role": "user", "content": raw_input}]
        elif isinstance(raw_input, list):
            for item in raw_input:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "message":
                    messages.append({"role": item.get("role"), "content": item.get("content")})
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
        prompt = build_prompt(messages, state)

        if not stream:
            result = await _chat_once(request, prompt, settings.chat_timeout)
            state.last_user_count = sum(1 for m in messages if str(m.get("role")) == "user")
            state.system_hash = hashlib.sha256(
                "\n\n".join(content_text(m.get("content")).strip()
                            for m in messages if str(m.get("role")) == "system").encode()
            ).hexdigest()[:16]
            state.last_system_sent = True
            app.state.chat.sessions._save()
            return _responses_payload(result.text, model, body.get("id") or "resp_" + uuid.uuid4().hex)

        response_id = "resp_" + uuid.uuid4().hex

        async def stream_responses() -> AsyncIterator[str]:
            created = int(time.time())
            item_id = "msg_" + uuid.uuid4().hex
            queue: asyncio.Queue[Any] = asyncio.Queue()

            async def runner() -> None:
                try:
                    result = await _chat_once(request, prompt, settings.chat_timeout)
                    state.last_user_count = sum(1 for m in messages if str(m.get("role")) == "user")
                    state.system_hash = hashlib.sha256(
                        "\n\n".join(content_text(m.get("content")).strip()
                                    for m in messages if str(m.get("role")) == "system").encode()
                    ).hexdigest()[:16]
                    state.last_system_sent = True
                    app.state.chat.sessions._save()
                    await queue.put(("done", result))
                except Exception as exc:  # noqa: BLE001
                    await queue.put(("error", exc))
            task = asyncio.create_task(runner())

            def ev(event: str, data: dict) -> str:
                return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

            base = {
                "id": response_id, "object": "response", "created_at": created,
                "status": "in_progress", "model": model, "output": [],
            }
            yield ev("response.created", {"type": "response.created", "response": base})
            yield ev("response.output_item.added", {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {"id": item_id, "type": "message", "role": "assistant",
                         "status": "in_progress", "content": []},
            })
            yield ev("response.content_part.added", {
                "type": "response.content_part.added",
                "item_id": item_id, "output_index": 0, "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []},
            })
            full = ""
            try:
                while True:
                    kind, value = await queue.get()
                    if kind == "done":
                        full = value.text
                    elif kind == "error":
                        raise value
                    break
                for start in range(0, len(full), 400):
                    chunk = full[start:start + 400]
                    yield ev("response.output_text.delta", {
                        "type": "response.output_text.delta",
                        "item_id": item_id, "output_index": 0, "content_index": 0,
                        "delta": chunk,
                    })
                yield ev("response.output_text.done", {
                    "type": "response.output_text.done",
                    "item_id": item_id, "output_index": 0, "content_index": 0,
                    "text": full,
                })
                yield ev("response.output_item.done", {
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": {"id": item_id, "type": "message", "role": "assistant",
                             "status": "completed",
                             "content": [{"type": "output_text", "text": full, "annotations": []}]},
                })
                yield ev("response.completed", {
                    "type": "response.completed",
                    "response": _responses_payload(full, model, response_id, created),
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
                               model: str, timeout: float) -> dict:
        aspect = image_size_to_aspect(size)
        instruction = (
            f"Generate {max(1, n)} image(s) with aspect ratio {aspect} for the following request. "
            "Use your image generation tool now, do not ask questions.\n\n" + prompt
        )
        async with app.state.sem:
            result = await app.state.chat.run_with_media(
                instruction, kind="image", timeout=timeout
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
                if response_format == "b64_json":
                    entry["b64_json"] = base64.b64encode(raw).decode("ascii")
                else:
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
            raise ApiError("invalid JSON body")
        prompt = body.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ApiError("prompt is required")
        n = int(body.get("n") or 1)
        size = str(body.get("size") or "1024x1024")
        response_format = str(body.get("response_format") or "url")
        return await _generate_images(
            prompt.strip(), n, size, response_format,
            openai_model(body.get("model"), settings.model),
            float(__import__("os").environ.get("MUSE_IMAGE_TIMEOUT", "240")),
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
        raw = await image.read()
        if not raw:
            raise ApiError("image is empty")
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
        state = app.state.chat.sessions.get(f"media:edit:{uuid.uuid4()}")
        async with app.state.sem:
            result = await app.state.chat.run_with_media(
                instruction,
                kind="image",
                timeout=float(__import__("os").environ.get("MUSE_IMAGE_TIMEOUT", "240")),
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
        if response_format == "b64_json":
            entry["b64_json"] = base64.b64encode(raw_out).decode("ascii")
        else:
            entry["url"] = app.state.media.url_for(name)
        return {"created": int(time.time()), "data": [entry]}

    # ------------------------------------------------------------------ videos

    async def _run_video_task(task: dict, prompt: str, duration: int, size: str) -> None:
        task["status"] = "running"
        task["progress"] = 5
        aspect = image_size_to_aspect(size)
        instruction = (
            f"Generate a {duration} second video with aspect ratio {aspect} for the following "
            "request. Use your video generation tool now, do not ask questions.\n\n" + prompt
        )
        try:
            async with app.state.sem:
                result = await app.state.chat.run_with_media(
                    instruction, kind="video",
                    timeout=float(__import__("os").environ.get("MUSE_VIDEO_TIMEOUT", "600")),
                )
            task["progress"] = 70
            task["text"] = result.text
            videos = [ref for ref in result.media if ref.kind == "video"]
            if not videos:
                task["status"] = "failed"
                task["error"] = result.text or "muse did not return a video"
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
            task["status"] = "failed"
            task["error"] = str(exc)

    @app.post("/v1/videos")
    async def create_video(request: Request):
        check_auth(request)
        try:
            body = await request.json()
        except Exception:
            raise ApiError("invalid JSON body")
        prompt = body.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ApiError("prompt is required")
        duration = int(body.get("duration") or body.get("seconds") or 5)
        size = str(body.get("size") or "16:9")
        task = app.state.tasks.create("video")
        asyncio.create_task(_run_video_task(task, prompt.strip(), duration, size))
        return {"id": task["id"], "object": task["object"], "status": "queued",
                "created_at": task["created_at"]}

    @app.get("/v1/videos/{task_id}")
    async def get_video(task_id: str, request: Request):
        check_auth(request)
        task = app.state.tasks.get(task_id)
        if task is None:
            raise ApiError("video task not found", status=404, code="not_found")
        return task

    # ------------------------------------------------------------------ media

    @app.get("/v1/media/{name}")
    @app.get("/media/{name}")
    async def get_media(name: str):
        path = app.state.media.path_for(name)
        if path is None:
            raise ApiError("media not found", status=404, code="not_found")
        return FileResponse(path)

    return app


def _responses_payload(text: str, model: str, response_id: str, created: int | None = None) -> dict:
    return {
        "id": response_id,
        "object": "response",
        "created_at": created or int(time.time()),
        "status": "completed",
        "model": model,
        "output": [{
            "id": "msg_" + uuid.uuid4().hex,
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        }],
        "output_text": text,
        "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
    }
