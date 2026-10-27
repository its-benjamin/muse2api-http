"""Opt-in handling for muse agent permission (egress) approvals.

The upstream agent blocks tool/connector/network use until a human answers an
approval. The web client renders those prompts and POSTs a decision. An OpenAI
HTTP client has no human, so a chat can otherwise sit until ``MUSE_CHAT_TIMEOUT``.

This module only acts when ``MUSE2API_APPROVAL_POLICY`` is explicitly enabled,
and only for approvals scoped to the chat thread that is currently waiting.
Account-wide permission settings are never modified.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from .gateway import HatchConnection

log = logging.getLogger("muse2api.approvals")

POLICIES = {"off", "allow_once", "allow_session", "deny"}
_FALLBACKS = {
    "allow_once": ("allow_once",),
    "allow_session": ("allow_session", "allow_once"),
    "deny": ("deny", "deny_always"),
}


def _record(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _scope(record: dict) -> dict:
    scope = _record(record.get("scope"))
    # Current snapshots nest the scope; older ones put the fields at top level.
    return scope or record


def approval_id(record: dict) -> str | None:
    value = record.get("approval_id") or record.get("id")
    return value if isinstance(value, str) and value else None


def is_pending(record: dict) -> bool:
    status = record.get("status")
    return status is None or status == "pending"


def matches_session(record: dict, session_id: str | None) -> bool:
    if not session_id:
        return False
    scope = _scope(record)
    return session_id in {
        scope.get("thread_id") or record.get("thread_id"),
        scope.get("session_scope_id") or record.get("session_scope_id"),
    }


def offered_decisions(record: dict) -> set[str] | None:
    """Return the submittable decisions, or ``None`` when the field is absent."""
    raw = record.get("decision_options")
    if not isinstance(raw, list):
        return None
    offered: set[str] = set()
    for entry in raw:
        if isinstance(entry, str):
            offered.add(entry)
        elif isinstance(entry, dict):
            kind = entry.get("kind")
            if isinstance(kind, str):
                offered.add(kind)
    return offered


def decision_for(record: dict, policy: str) -> str | None:
    """Pick the strongest decision the server offers for ``policy``."""
    if policy not in _FALLBACKS:
        return None
    offered = offered_decisions(record)
    if offered is not None and not offered:
        # Present-but-empty is authoritative: no client action is offered.
        return None
    for candidate in _FALLBACKS[policy]:
        if offered is None or candidate in offered:
            return candidate
    return None


def pending_for_session(records: Iterable[Any], session_id: str | None) -> list[dict]:
    result: list[dict] = []
    for raw in records:
        record = _record(raw)
        if (record and is_pending(record) and matches_session(record, session_id)
                and approval_id(record)):
            result.append(record)
    return result


async def decide(
    conn: HatchConnection,
    record: dict,
    policy: str,
    *,
    reason: str = "muse2api HTTP client auto-approval",
    timeout: float = 20.0,
) -> bool:
    """Submit one decision over the existing Noise connection."""
    ident = approval_id(record)
    choice = decision_for(record, policy)
    if ident is None or choice is None:
        return False
    try:
        response = await conn.request(
            "POST",
            f"/approvals/{ident}/decide",
            {
                "approval_id": ident,
                "decision": choice,
                "reason": reason,
            },
            service="sentinel",
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 - never fail a chat over approval I/O
        log.warning("approval %s decision failed: %s", ident, exc)
        return False
    if 200 <= response.status < 300:
        log.info("approval %s -> %s", ident, choice)
        return True
    log.warning("approval %s -> HTTP %s: %s", ident, response.status, response.text()[:200])
    return False


async def handle_pending(
    conn: HatchConnection,
    session_id: str | None,
    policy: str,
) -> int:
    """Decide any pending approvals for ``session_id``; return the count handled."""
    if policy not in _FALLBACKS or not session_id:
        return 0
    try:
        response = await conn.request(
            "GET", "/approvals", {}, service="sentinel", timeout=15.0
        )
    except Exception as exc:  # noqa: BLE001 - approvals are best-effort
        log.debug("approvals poll failed: %s", exc)
        return 0
    if response.status >= 400:
        log.debug("approvals poll -> HTTP %s", response.status)
        return 0
    try:
        data = response.json() or {}
    except Exception:  # noqa: BLE001
        return 0
    if isinstance(data, dict) and isinstance(data.get("result"), dict):
        # The gateway wraps RPC payloads as {"ok": true, "result": {...}}.
        data = data["result"]
    pending = data.get("pending_approvals")
    if not isinstance(pending, list):
        pending = data.get("pending")
    if not isinstance(pending, list):
        return 0
    handled = 0
    for record in pending_for_session(pending, session_id):
        if await decide(conn, record, policy):
            handled += 1
    return handled
