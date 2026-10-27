"""Opt-in handling for muse agent permission (egress) approvals.

The upstream agent blocks tool/connector/network use until a human answers an
approval. The web client renders those prompts and POSTs a decision. An OpenAI
HTTP client has no human, so a chat can otherwise sit until ``MUSE_CHAT_TIMEOUT``.

Policies (``MUSE2API_APPROVAL_POLICY``):

``off``
    Never answer (default).
``allow_once`` / ``allow_session`` / ``allow_always``
    Answer each prompt with the strongest matching grant the daemon offers.
    ``allow_always`` prefers the durable grant (scoped when the option carries
    a scope), so the same destination/entity stops prompting.
``settings``
    Read the account's own permission settings (``/permissions/settings`` and
    ``/permissions/active``) and follow them: ``allow`` becomes a session grant,
    ``deny`` a refusal, and ``ask`` (which cannot reach a human here) the
    ``MUSE2API_APPROVAL_ASK`` fallback.
``deny``
    Refuse, letting the assistant report the refusal.

Only approvals whose scope (``thread_id`` / ``session_scope_id``) matches the
waiting chat are touched. Account-wide settings are read, never modified.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from typing import Any

from .gateway import HatchConnection

log = logging.getLogger("muse2api.approvals")

POLICIES = {"off", "allow_once", "allow_session", "allow_always", "settings", "deny"}
SETTINGS_POLICY = "settings"
# Permission settings are account-wide; a short cache keeps the 4s approval
# poll from hammering the gateway.
PERMISSIONS_TTL = 60.0

# One-shot grants differ per approval type (browser task, artifact, feed,
# schedule, "today"); any of them ends the current block.
_ONE_SHOT = (
    "allow_once",
    "allow",
    "allow_for_this_task",
    "allow_all_web_access_for_this_task",
    "allow_today",
    "allow_for_this_scheduled_task",
    "allow_for_this_artifact",
    "allow_for_your_feed",
)
_DENY = (
    "deny",
    "deny_always",
    "deny_for_this_task",
    "deny_for_this_scheduled_task",
    "deny_for_this_artifact",
)
_FALLBACKS = {
    "allow_once": _ONE_SHOT,
    "allow_session": ("allow_session",) + _ONE_SHOT,
    "allow_always": ("allow_always", "allow_always_scoped", "allow_session") + _ONE_SHOT,
    "deny": _DENY,
}
# Kinds used when a permission setting resolves to allow / ask / deny.
_MODE_KINDS = {
    "allow": ("allow_session",) + _ONE_SHOT,
    "ask": _ONE_SHOT,
    "deny": _DENY,
}
# A scoped "always" option submits the plain decision plus the scope it carries.
_WIRE_DECISION = {"allow_always_scoped": "allow_always"}
# Servers that predate ``decision_options`` only know the original three kinds.
_LEGACY_DECISION = {
    "allow_once": "allow_once",
    "allow_session": "allow_session",
    "allow_always": "allow_session",
    "deny": "deny",
}
_DEFAULT_MODES = {
    "auto_allow": "allow",
    "always_ask": "ask",
    "allow": "allow",
    "ask": "ask",
    "deny": "deny",
    "off": "deny",
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


def _chosen_option(record: dict, policy: str) -> str | None:
    """Return the offered option kind to submit, or ``None`` when unavailable."""
    if policy not in _FALLBACKS:
        return None
    offered = offered_decisions(record)
    if offered is None or not offered:
        # Absent: legacy server (handled by the caller). Empty: nothing offered.
        return None
    for candidate in _FALLBACKS[policy]:
        if candidate in offered:
            return candidate
    return None


def _wire(kind: str | None) -> str | None:
    if kind is None:
        return None
    return _WIRE_DECISION.get(kind, kind)


def decision_for(record: dict, policy: str) -> str | None:
    """Pick the strongest decision the server offers for ``policy``."""
    if policy not in _FALLBACKS:
        return None
    chosen = _chosen_option(record, policy)
    if chosen is not None:
        return _wire(chosen)
    if offered_decisions(record) is not None:
        return None
    return _LEGACY_DECISION[policy]


def decision_for_kinds(record: dict, kinds: tuple[str, ...]) -> str | None:
    """Pick the first offered kind from ``kinds`` (wire decision)."""
    offered = offered_decisions(record)
    if offered is None:
        return None
    if not offered:
        return None
    for candidate in kinds:
        if candidate in offered:
            return _wire(candidate)
    return None


def decision_scope(record: dict, decision: str | None) -> str | None:
    """Scope submitted alongside a scoped ``allow_always`` decision.

    The daemon offers ``{"kind": "allow_always", "label": {"kind":
    "allow_always_scoped", "scope": "destination_domain"}}``; older payloads
    put the scoped kind directly on the option.
    """
    if decision != "allow_always":
        return None
    for entry in record.get("decision_options") or []:
        option = _record(entry)
        kind = option.get("kind")
        if kind not in {"allow_always", "allow_always_scoped"}:
            continue
        label = _record(option.get("label"))
        if (label.get("kind") or kind) != "allow_always_scoped":
            continue
        scope = label.get("scope") or option.get("scope")
        if isinstance(scope, str) and scope:
            return scope
    return None


def pending_for_session(records: Iterable[Any], session_id: str | None) -> list[dict]:
    result: list[dict] = []
    for raw in records:
        record = _record(raw)
        if (record and is_pending(record) and matches_session(record, session_id)
                and approval_id(record)):
            result.append(record)
    return result


async def submit(
    conn: HatchConnection,
    record: dict,
    decision: str | None,
    *,
    reason: str | None = None,
    timeout: float = 20.0,
) -> bool:
    """Submit one decision over the existing Noise connection."""
    ident = approval_id(record)
    if ident is None or not decision:
        return False
    payload: dict[str, Any] = {
        "approval_id": ident,
        "decision": decision,
    }
    if reason:
        payload["reason"] = reason
    scope = decision_scope(record, decision)
    if scope:
        # The web client sends both keys; newer daemons read the second one.
        payload["always_scope"] = scope
        payload["allow_always_scope"] = scope
    try:
        response = await conn.request(
            "POST",
            f"/approvals/{ident}/decide",
            payload,
            service="sentinel",
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 - never fail a chat over approval I/O
        log.warning("approval %s decision failed: %s", ident, exc)
        return False
    if 200 <= response.status < 300:
        log.info("approval %s -> %s", ident, decision)
        return True
    log.warning("approval %s -> HTTP %s: %s", ident, response.status, response.text()[:200])
    return False


async def decide(
    conn: HatchConnection,
    record: dict,
    policy: str,
    *,
    reason: str | None = None,
    timeout: float = 20.0,
) -> bool:
    """Submit one decision over the existing Noise connection."""
    return await submit(conn, record, decision_for(record, policy),
                        reason=reason, timeout=timeout)


# -- account permission settings -------------------------------------------

def _question(record: dict) -> dict:
    """Approval question; older records nest it under a presentation block."""
    for holder in (record.get("server_presentation"), record.get("serverPresentation"),
                   record.get("display")):
        block = _record(holder)
        for key in ("permissionQuestion", "permission_question"):
            question = block.get(key)
            if isinstance(question, dict):
                return question
    return {}


def _network_fields(record: dict) -> tuple[str | None, int | None]:
    """Host/port for network approvals, from either record shape."""
    network = _record(_record(record.get("payload")).get("network_payload"))
    host = record.get("host") or network.get("host")
    port = record.get("port") or network.get("port")
    return (host if isinstance(host, str) else None,
            port if isinstance(port, int) else None)


def _mode(value: Any) -> str:
    return _DEFAULT_MODES.get(str(value or "").strip().lower(), "ask")


def _connector_mode(active: list[dict], connector: str, action: str | None) -> str | None:
    for item in active:
        target = _record(item.get("target"))
        info = _record(target.get("connector"))
        key = item.get("connector") or info.get("key")
        if key != connector:
            continue
        if action:
            for entry in info.get("actions") or []:
                row = _record(entry)
                if row.get("key") == action and isinstance(row.get("mode"), str):
                    return _mode(row["mode"])
        if isinstance(item.get("mode"), str):
            return _mode(item["mode"])
    return None


def _protocol_mode(settings: dict, port: int | None) -> str | None:
    if port is None:
        return None
    for entry in settings.get("protocols") or []:
        row = _record(entry)
        ports = row.get("ports") or []
        if port in ports:
            return _mode(row.get("mode") or row.get("default_mode"))
    return None


def _network_rule_mode(active: list[dict], host: Any) -> str | None:
    if not isinstance(host, str) or not host:
        return None
    for item in active:
        if item.get("kind") not in {"network_rule", "transient_network_grant"}:
            continue
        network = _record(_record(item.get("target")).get("network"))
        for entry in network.get("endpoints") or []:
            endpoint = _record(entry)
            if endpoint.get("host") == host and isinstance(item.get("mode"), str):
                return _mode(item["mode"])
    return None


def resolve_mode(record: dict, settings: dict, active: list[dict]) -> str:
    """Map one approval to the account's effective permission mode."""
    question = _question(record)
    host, port = _network_fields(record)
    connector = question.get("connector") or record.get("connector") or record.get("connector_key")
    action = question.get("action")
    if isinstance(connector, str) and connector:
        mode = _connector_mode(active, connector, action if isinstance(action, str) else None)
        return mode or _mode(settings.get("connector_default"))
    kind = question.get("kind") or record.get("approval_type")
    log.debug("resolve kind=%r host=%r port=%r", kind, host, port)
    if host is not None or kind in {
        "network_access", "network_turn_grant", "space_reader_web_read", "cron_web_read",
    }:
        mode = _protocol_mode(settings, port)
        if mode:
            return mode
        rule = _network_rule_mode(active, host)
        if rule:
            return rule
        return _mode(settings.get("network_default"))
    return "ask"


async def _get_json(conn: HatchConnection, path: str, timeout: float = 15.0) -> dict | None:
    try:
        response = await conn.request("GET", path, {}, service="sentinel", timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - approvals are best-effort
        log.debug("permissions %s failed: %s", path, exc)
        return None
    if response.status >= 400:
        log.debug("permissions %s -> HTTP %s", path, response.status)
        return None
    try:
        data = response.json() or {}
    except Exception:  # noqa: BLE001
        return None
    if isinstance(data, dict) and isinstance(data.get("result"), dict):
        data = data["result"]
    return data if isinstance(data, dict) else None


async def permissions(conn: HatchConnection) -> tuple[dict, list[dict]]:
    """Account permission settings + active overrides (per-connection cache)."""
    cached = getattr(conn, "_muse_permissions", None)
    if cached and time.monotonic() - cached[0] < PERMISSIONS_TTL:
        return cached[1], cached[2]
    settings = await _get_json(conn, "/permissions/settings") or {}
    active = await _get_json(conn, "/permissions/active") or {}
    items = active.get("items")
    active_items = [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []
    try:
        conn._muse_permissions = (time.monotonic(), settings, active_items)
    except Exception:  # pragma: no cover - exotic connection stand-ins
        pass
    return settings, active_items


async def handle_pending(
    conn: HatchConnection,
    session_id: str | None,
    policy: str,
    *,
    ask_policy: str = "allow_once",
) -> int:
    """Decide any pending approvals for ``session_id``; return the count handled."""
    if policy not in POLICIES or policy == "off" or not session_id:
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
    fallback = ask_policy if ask_policy in _FALLBACKS else "allow_once"
    handled = 0
    for record in pending_for_session(pending, session_id):
        if policy == SETTINGS_POLICY:
            settings, active = await permissions(conn)
            log.debug("permission defaults connector=%r network=%r protocols=%d active=%d",
                      settings.get("connector_default"), settings.get("network_default"),
                      len(settings.get("protocols") or []), len(active))
            mode = resolve_mode(record, settings, active)
            if mode == "allow":
                decision = decision_for_kinds(record, _MODE_KINDS["allow"])
            elif mode == "deny":
                decision = decision_for_kinds(record, _MODE_KINDS["deny"])
            else:
                log.info("approval %s is ask-scoped; using %s fallback",
                         approval_id(record), fallback)
                decision = decision_for(record, fallback)
            log.info("approval %s settings mode=%s decision=%s options=%s",
                     approval_id(record), mode, decision, offered_decisions(record))
        else:
            decision = decision_for(record, policy)
        if await submit(conn, record, decision):
            handled += 1
    return handled
