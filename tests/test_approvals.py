from __future__ import annotations

import asyncio

from muse2api.approvals import (
    decide,
    decision_for,
    handle_pending,
    matches_session,
    pending_for_session,
)

SESSION = "thread-123"


def _approval(**overrides):
    record = {
        "approval_id": "ap-1",
        "status": "pending",
        "scope": {"thread_id": SESSION},
        "decision_options": ["allow_once", "allow_session", "deny"],
    }
    record.update(overrides)
    return record


def test_matches_thread_and_session_scope_only():
    assert matches_session(_approval(), SESSION)
    assert matches_session(
        {"approval_id": "a", "status": "pending", "scope": {"session_scope_id": SESSION}},
        SESSION,
    )
    assert not matches_session(_approval(scope={"thread_id": "other"}), SESSION)
    assert not matches_session(_approval(), None)


def test_pending_filter_requires_id_and_pending_status():
    records = [
        _approval(),
        _approval(approval_id="", status="pending"),
        _approval(approval_id="done", status="approved"),
        _approval(approval_id="other", scope={"thread_id": "x"}),
    ]
    assert [r["approval_id"] for r in pending_for_session(records, SESSION)] == ["ap-1"]


def test_decision_respects_offered_options():
    assert decision_for(_approval(), "allow_once") == "allow_once"
    assert decision_for(_approval(), "allow_session") == "allow_session"
    assert decision_for(_approval(), "deny") == "deny"
    assert decision_for(_approval(decision_options=["allow_once"]), "allow_session") == "allow_once"
    assert decision_for(_approval(decision_options=["allow_only_meta"]), "allow_once") is None
    assert decision_for(_approval(decision_options=["allow_once"]), "deny") is None
    assert decision_for(_approval(decision_options=[]), "allow_once") is None


def test_legacy_approval_without_options_uses_policy_default():
    legacy = _approval()
    del legacy["decision_options"]
    assert decision_for(legacy, "allow_once") == "allow_once"
    assert decision_for(legacy, "deny") == "deny"
    assert decision_for(legacy, "off") is None


class _Response:
    def __init__(self, status=200, payload=None):
        self.status = status
        self._payload = payload if payload is not None else {}
        self.body = b"{}"

    def json(self):
        return self._payload

    def text(self):
        return "{}"


class _FakeConn:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def request(self, method, path, params=None, *, service="daemon", timeout=60.0):
        self.calls.append((method, path, params, service))
        if method == "GET":
            return _Response(payload=self.payload)
        return _Response(payload={"approval": params})


def test_handle_pending_decides_only_current_thread():
    conn = _FakeConn({
        "ok": True,
        "result": {
            "pending_approvals": [
                _approval(),
                _approval(approval_id="ap-2", scope={"thread_id": "other"}),
            ]
        },
    })
    handled = asyncio.run(handle_pending(conn, SESSION, "allow_once"))
    assert handled == 1
    assert conn.calls[0][:2] == ("GET", "/approvals")
    assert conn.calls[0][3] == "sentinel"
    method, path, params, service = conn.calls[1]
    assert (method, path) == ("POST", "/approvals/ap-1/decide")
    assert params["approval_id"] == "ap-1"
    assert params["decision"] == "allow_once"
    assert service == "sentinel"


def test_handle_pending_is_noop_when_policy_off():
    conn = _FakeConn({"pending_approvals": [_approval()]})
    assert asyncio.run(handle_pending(conn, SESSION, "off")) == 0
    assert conn.calls == []


def test_decide_rejects_unoffered_decision():
    conn = _FakeConn({})
    record = _approval(decision_options=["deny"])
    assert asyncio.run(decide(conn, record, "allow_once")) is False
    assert conn.calls == []
