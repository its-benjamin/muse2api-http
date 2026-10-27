"""Chat orchestration over the Hatch gateway.

Two modes:

* ``thread`` (default): one server-side muse side-chat per API session. The
  reply is read from ``/chat/history``. This is the reliable path: live
  subscription deltas do not cover thread replies and race thread creation.
* ``main``: the account's main chat, read live from ``/chat/subscribe``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .approvals import POLICIES as APPROVAL_POLICIES
from .approvals import handle_pending
from .config import Settings
from .gateway import GatewayError, HatchConnection
from .media import MediaRef, extract_media
from .pool import AccountPool
from .protocol import ProtocolConfig

log = logging.getLogger("muse2api.chat")

CAPABILITIES = [
    "chat_cancel",
    "delta_stream",
    "custom_reactions",
    "custom_reactions_facebook_thumbs_up_v1",
]

DEFAULT_SESSION_KEY = "default"
# The upstream history can pause between assistant deltas; require a quiet
# window before declaring a reply complete, otherwise long turns truncate.
TEXT_SETTLE_SECONDS = 2.0


@dataclass(slots=True)
class ChatSession:
    session_id: str
    node_id: str
    last_used: float = field(default_factory=time.time)
    last_user_count: int = 0
    system_hash: str = ""
    last_system_sent: bool = False


class SessionStore:
    def __init__(self, path: Path, *, ttl: float = 24 * 3600, max_entries: int = 500) -> None:
        self.path = path
        self.ttl = ttl
        self.max_entries = max_entries
        self._items: dict[str, ChatSession] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        now = time.time()
        for key, value in raw.items():
            if not isinstance(value, dict):
                continue
            sid, nid = value.get("session_id"), value.get("node_id")
            if not isinstance(sid, str) or not isinstance(nid, str):
                continue
            last = float(value.get("last_used") or 0)
            if now - last <= self.ttl:
                self._items[key] = ChatSession(
                    sid, nid, last,
                    int(value.get("last_user_count") or 0),
                    str(value.get("system_hash") or ""),
                    bool(value.get("last_system_sent")),
                )

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                key: {
                    "session_id": value.session_id,
                    "node_id": value.node_id,
                    "last_used": value.last_used,
                    "last_user_count": value.last_user_count,
                    "system_hash": value.system_hash,
                    "last_system_sent": value.last_system_sent,
                }
                for key, value in self._items.items()
            }
            self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError as exc:  # pragma: no cover - best effort
            log.debug("could not persist chat sessions: %s", exc)

    def get(self, key: str) -> ChatSession:
        now = time.time()
        state = self._items.get(key)
        if state is None:
            state = ChatSession(str(uuid.uuid4()), str(uuid.uuid4()))
            self._items[key] = state
        state.last_used = now
        self._sweep(now)
        self.save()
        return state

    def reset(self, key: str) -> ChatSession:
        self._items.pop(key, None)
        return self.get(key)

    def _sweep(self, now: float) -> None:
        stale = [k for k, v in self._items.items() if now - v.last_used > self.ttl]
        for key in stale:
            self._items.pop(key, None)
        if len(self._items) > self.max_entries:
            for key, _ in sorted(self._items.items(), key=lambda kv: kv[1].last_used)[
                : len(self._items) - self.max_entries
            ]:
                self._items.pop(key, None)


def session_key(header_value: str | None, authorization: str | None) -> str:
    if header_value and header_value.strip():
        return "h:" + header_value.strip()
    if authorization and authorization.strip():
        digest = hashlib.sha256(authorization.strip().encode()).hexdigest()[:16]
        return "a:" + digest
    return DEFAULT_SESSION_KEY


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    if isinstance(value, dict):
        for key in ("text", "display_text", "content"):
            if key in value:
                return _as_text(value[key])
    return ""


def event_text(event: dict) -> str:
    for key in ("display_text", "text"):
        text = _as_text(event.get(key))
        if text:
            return text
    payload = event.get("payload")
    if isinstance(payload, dict):
        for key in ("display_text", "text", "content", "delta"):
            text = _as_text(payload.get(key))
            if text:
                return text
    return ""


def history_events(payload: Any) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    node = payload.get("result", payload)
    if not isinstance(node, dict):
        return []
    for key in ("chat_events", "events", "history"):
        events = node.get(key)
        if isinstance(events, list):
            return [event for event in events if isinstance(event, dict)]
    return []


@dataclass(slots=True)
class ChatResult:
    text: str
    message_id: str
    events: list[dict] = field(default_factory=list)
    media: list[MediaRef] = field(default_factory=list)


class ChatService:
    def __init__(self, settings: Settings, pool: AccountPool,
                 protocol: ProtocolConfig | None = None) -> None:
        self.settings = settings
        self.pool = pool
        self.protocol = protocol or ProtocolConfig.load()
        self.sessions = SessionStore(settings.state_dir / "chat_sessions.json")
        self.approval_policy = (
            settings.approval_policy
            if settings.approval_policy in APPROVAL_POLICIES
            else "off"
        )
        if settings.approval_policy not in APPROVAL_POLICIES:
            log.warning(
                "unknown approval policy %r; permission prompts stay unhandled",
                settings.approval_policy,
            )
        self.approval_ask = (
            settings.approval_ask if settings.approval_ask in APPROVAL_POLICIES
            else "allow_once"
        )
        if self.approval_ask in {"off", "settings"}:
            self.approval_ask = "allow_once"

    async def run(
        self,
        prompt: str,
        *,
        on_delta: Callable[[str], None] | None = None,
        session_key_value: str = DEFAULT_SESSION_KEY,
        timeout: float | None = None,
        want_media: bool = False,
        items: list[dict] | None = None,
    ) -> ChatResult:
        if not prompt.strip():
            raise ValueError("prompt is empty")
        timeout = timeout or self.settings.chat_timeout
        if self.settings.session_mode == "main":
            return await self._run_main(
                prompt, on_delta=on_delta, timeout=timeout, items=items
            )
        state = self.sessions.get(session_key_value)
        return await self._run_thread(
            prompt, state, on_delta=on_delta, timeout=timeout, want_media=want_media,
            items=items,
        )

    async def run_with_media(
        self,
        prompt: str,
        *,
        kind: str = "image",
        timeout: float | None = None,
        items: list[dict] | None = None,
        refusal_markers: tuple[str, ...] = (),
    ) -> ChatResult:
        """Generate media in a throwaway thread and collect the artifacts."""
        state = self.sessions.get(f"media:{kind}:{uuid.uuid4()}")
        return await self._run_thread(
            prompt,
            state,
            on_delta=None,
            timeout=timeout or self.settings.chat_timeout,
            want_media=True,
            items=items,
            refusal_markers=refusal_markers,
        )

    # -- thread mode -------------------------------------------------------

    async def _run_thread(
        self,
        prompt: str,
        state: ChatSession,
        *,
        on_delta: Callable[[str], None] | None,
        timeout: float,
        want_media: bool = False,
        items: list[dict] | None = None,
        refusal_markers: tuple[str, ...] = (),
    ) -> ChatResult:
        _account, _session, conn = await self.pool.connect()
        try:
            await self._register_node(conn, state.node_id)
            body = {
                "items": items or [{"type": "text", "text": prompt}],
                "message": prompt,
                "node_id": state.node_id,
                "capabilities": CAPABILITIES,
                "session_id": state.session_id,
                "timezone": self.settings.timezone,
                "metadata": {"thread_is_dictation_used": False},
            }
            message_id = ""
            try:
                ack = await conn.ack("POST", self.protocol.route("chat.stream").path, body, timeout=20.0)
                raw_id = ack.get("message_id") or ack.get("id")
                if isinstance(raw_id, str):
                    message_id = raw_id
            except (TimeoutError, GatewayError) as exc:
                log.debug("chat.stream ack unavailable (%s); falling back to history search", exc)

            reply, reply_id, events, media = await self._poll_thread_reply(
                conn,
                state.session_id,
                message_id=message_id,
                prompt=prompt,
                on_delta=on_delta,
                timeout=timeout,
                want_media=want_media,
                refusal_markers=refusal_markers,
            )
            if not reply and not media:
                raise GatewayError("muse returned an empty reply")
            return ChatResult(
                text=reply,
                message_id=reply_id or message_id or str(uuid.uuid4()),
                events=events,
                media=media,
            )
        except Exception:
            # If the turn failed unexpectedly on this connection, mark closed so next turn reconnects
            conn._closed.set()
            raise

    async def _poll_thread_reply(
        self,
        conn: HatchConnection,
        session_id: str,
        *,
        message_id: str,
        prompt: str,
        on_delta: Callable[[str], None] | None,
        timeout: float,
        want_media: bool = False,
        refusal_markers: tuple[str, ...] = (),
    ) -> tuple[str, str, list[dict], list[MediaRef]]:
        deadline = time.monotonic() + timeout
        last_text: str | None = None
        emitted = 0
        best = ""
        best_id = ""
        started = time.monotonic()
        polls = 0
        captured: list[dict] = []
        media: list[MediaRef] = []
        media_signature: tuple[str, ...] | None = None
        media_stable_at: float | None = None
        text_stable_at: float | None = None
        next_approval_check = 0.0

        async def approval_hold() -> bool:
            """Answer prompts before declaring the turn finished."""
            if self.approval_policy == "off":
                return False
            handled = await handle_pending(
                conn, session_id, self.approval_policy, ask_policy=self.approval_ask
            )
            if handled:
                log.info("answered %d permission prompt(s) for %s", handled, session_id)
            return handled > 0

        while time.monotonic() < deadline:
            polls += 1
            if self.approval_policy != "off" and time.monotonic() >= next_approval_check:
                next_approval_check = time.monotonic() + 4.0
                handled = await handle_pending(
                    conn, session_id, self.approval_policy, ask_policy=self.approval_ask
                )
                if handled:
                    log.info("answered %d permission prompt(s) for %s", handled, session_id)
            try:
                response = await conn.request(
                    "GET",
                    self.protocol.route("chat.history").path,
                    {"limit": 60, "session_id": session_id},
                    timeout=min(20.0, max(5.0, deadline - time.monotonic())),
                )
                data = response.json() or {}
            except (TimeoutError, GatewayError) as exc:
                log.debug("history poll failed: %s", exc)
                if getattr(conn, "is_closed", False):
                    log.info("gateway connection closed mid-turn; reconnecting to recover thread %s", session_id)
                    try:
                        _, _, conn = await self.pool.connect()
                    except Exception as rec_exc:
                        log.warning("gateway reconnection failed: %s", rec_exc)
                await asyncio.sleep(1.0)
                continue

            events = history_events(data)
            if not events:
                await asyncio.sleep(0.8)
                continue

            user_index = -1
            if message_id:
                for index, event in enumerate(events):
                    if event.get("message_id") == message_id:
                        user_index = index
                        break
            if user_index < 0:
                needle = prompt.strip()
                for index, event in enumerate(events):
                    name = str(event.get("event_name") or event.get("event") or "")
                    if name != "message.user":
                        continue
                    text = event_text(event).strip()
                    if text and (text == needle or needle[:80] in text):
                        user_index = index
            if user_index < 0:
                await asyncio.sleep(0.8)
                continue

            after = events[user_index + 1:]
            captured = after
            current_text = ""
            current_id = ""
            for event in after:
                name = str(event.get("event_name") or event.get("event") or "")
                if name != "message.assistant":
                    continue
                payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
                if event.get("reply_to_message_id") or payload.get("reply_to_message_id"):
                    continue
                text = event_text(event)
                if not text:
                    continue
                current_text = text
                raw_id = event.get("message_id") or payload.get("message_id")
                current_id = raw_id if isinstance(raw_id, str) else current_id

            if want_media:
                media = extract_media(after)

            if current_text:
                best = current_text
                best_id = current_id or best_id
                if on_delta and len(current_text) > emitted:
                    fresh = current_text[emitted:]
                    for start in range(0, len(fresh), 240):
                        on_delta(fresh[start:start + 240])
                    emitted = len(current_text)
                if last_text == current_text:
                    if text_stable_at is None:
                        text_stable_at = time.monotonic()
                else:
                    last_text = current_text
                    text_stable_at = None
            elif last_text is not None:
                last_text = None
                text_stable_at = None

            if (refusal_markers and not media and current_text
                    and any(marker in current_text.lower() for marker in refusal_markers)):
                # The upstream model already refused; do not wait out the
                # artifact grace period for a file that will never arrive.
                return best, best_id, captured, media

            if want_media:
                signature = tuple(sorted(ref.identity for ref in media))
                if media and signature == media_signature:
                    if media_stable_at is None:
                        media_stable_at = time.monotonic()
                    settled = time.monotonic() - media_stable_at > 25.0
                    text_settled = (
                        text_stable_at is not None
                        and time.monotonic() - text_stable_at > TEXT_SETTLE_SECONDS
                    )
                    if text_settled or settled:
                        if await approval_hold():
                            text_stable_at = None
                            media_stable_at = None
                            continue
                        return best, best_id, captured, media
                elif media:
                    media_signature = signature
                    media_stable_at = time.monotonic()
                else:
                    media_stable_at = None
                    if text_stable_at is not None and time.monotonic() - text_stable_at > 25.0:
                        # Text settled but no artifact arrived; surface what we have.
                        return best, best_id, captured, media
            elif (text_stable_at is not None
                  and time.monotonic() - text_stable_at > TEXT_SETTLE_SECONDS):
                if await approval_hold():
                    text_stable_at = None
                    last_text = None
                    continue
                return best, best_id, captured, media

            # Ignore a false "stable" on the first snapshot.
            if polls == 1 and time.monotonic() - started < 1.0:
                text_stable_at = None

            poll_interval = 0.35 if (current_text and text_stable_at is None) else 0.6
            await asyncio.sleep(poll_interval)

        return best, best_id, captured, media

    # -- main-chat mode ----------------------------------------------------

    async def _run_main(
        self,
        prompt: str,
        *,
        on_delta: Callable[[str], None] | None,
        timeout: float,
        want_media: bool = False,
        items: list[dict] | None = None,
    ) -> ChatResult:
        _account, _session, conn = await self.pool.connect()
        client_id = str(uuid.uuid4())
        queue: asyncio.Queue[Any] = asyncio.Queue()

        try:
            await self._register_node(conn, client_id)
            await self._register_capabilities(conn, client_id)

            async def pump() -> None:
                try:
                    async for event in conn.stream(
                        "POST",
                        self.protocol.route("chat.subscribe").path,
                        {
                            "after_stream_seq": 0,
                            "after_chat_event_seq": 0,
                            "capabilities": CAPABILITIES,
                        },
                        reset_reason="chat done",
                    ):
                        await queue.put(event)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    log.debug("subscribe stream ended: %s", exc)

            pump_task = asyncio.create_task(pump())
            await asyncio.sleep(0.4)  # let the subscribe attach

            send_start_ms = int(time.time() * 1000)
            await conn.send(
                "POST",
                self.protocol.route("chat.stream").path,
                {
                    "message": prompt,
                    "items": items or [{"type": "text", "text": prompt}],
                    "node_id": client_id,
                    "capabilities": CAPABILITIES,
                    "timezone": self.settings.timezone,
                },
            )

            parts: list[str] = []
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), min(5.0, remaining))
                except TimeoutError:
                    continue
                if not isinstance(event, dict):
                    continue
                ts = event.get("ts_ms")
                if isinstance(ts, (int, float)) and ts < send_start_ms:
                    continue
                name = str(event.get("event") or event.get("event_name") or "")
                payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
                if name == "delta.text_append":
                    text = _as_text(payload.get("text") or payload.get("delta"))
                    if text:
                        parts.append(text)
                        if on_delta:
                            on_delta(text)
                elif name == "delta.message_done":
                    if not parts:
                        transcript = payload.get("transcript")
                        if isinstance(transcript, dict):
                            for message in transcript.get("messages") or []:
                                for content in message.get("content") or []:
                                    if content.get("type") == "text" and isinstance(content.get("text"), str):
                                        parts.append(content["text"])
                                        if on_delta:
                                            on_delta(content["text"])
                    break
                elif name == "message.assistant":
                    if not parts:
                        text = event_text(event)
                        if text:
                            parts.append(text)
                            if on_delta:
                                on_delta(text)
                    break
            text = "".join(parts)
            if not text:
                raise GatewayError(f"muse returned an empty reply within {timeout:.0f}s")
            return ChatResult(text=text, message_id=str(uuid.uuid4()))
        finally:
            try:
                pump_task.cancel()  # type: ignore[possibly-undefined]
            except Exception:
                pass
            await conn.close()

    # -- preamble helpers --------------------------------------------------

    async def _register_node(self, conn: HatchConnection, node_id: str) -> None:
        try:
            await conn.request(
                "POST",
                self.protocol.route("nodes.register").path,
                {
                    "node_id": node_id,
                    "display_name": "Web",
                    "platform": "web",
                    "commands_v2": {"ping": {"description": "Connection liveness check"}},
                },
                timeout=15.0,
            )
        except Exception as exc:  # noqa: BLE001 - optional routing hint
            log.debug("node registration failed: %s", exc)

    async def _register_capabilities(self, conn: HatchConnection, client_id: str) -> None:
        try:
            await conn.request(
                "POST",
                self.protocol.route("client.register_capabilities").path,
                {
                    "client_id": client_id,
                    "platform": "web",
                    "display_name": "Muse Web",
                    "version": "0.0.0",
                    "capabilities": {
                        "data_sources": {},
                        "device_commands": {},
                        "hatch_app_commands": {},
                        "rendering": {
                            "supported_presentations": ["text_with_button", "generic_list"],
                            "supported_inline_presentations": ["option"],
                            "supported_text_entities": [],
                        },
                    },
                },
                timeout=15.0,
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("capability registration failed: %s", exc)
