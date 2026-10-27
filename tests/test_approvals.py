from __future__ import annotations

import asyncio

from muse2api.approvals import (
    POLICIES,
    decide,
    decision_for,
    decision_scope,
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


def test_allow_always_prefers_durable_grants():
    assert "allow_always" in POLICIES
    assert decision_for(_approval(decision_options=["allow_once", "allow_always"]),
                        "allow_always") == "allow_always"
    # A scoped grant submits the plain decision; the scope travels separately.
    scoped = _approval(decision_options=[
        {"kind": "allow_always_scoped", "label": {"kind": "allow_always_scoped",
                                                  "scope": "destination_domain"}},
        {"kind": "allow_once", "label": {"kind": "allow_once"}},
    ])
    assert decision_for(scoped, "allow_always") == "allow_always"
    assert decision_scope(scoped, "allow_always") == "destination_domain"
    # Falls back when the daemon does not offer a durable grant.
    assert decision_for(_approval(decision_options=["allow_session", "allow_once"]),
                        "allow_always") == "allow_session"
    assert decision_for(_approval(decision_options=["allow_for_this_task"]),
                        "allow_once") == "allow_for_this_task"
    assert decision_for(_approval(decision_options=["deny_for_this_task"]),
                        "deny") == "deny_for_this_task"
    legacy = _approval()
    del legacy["decision_options"]
    assert decision_for(legacy, "allow_always") == "allow_session"
    assert decision_scope(legacy, "allow_session") is None


def test_scoped_always_decision_sends_scope():
    conn = _FakeConn({})
    record = _approval(decision_options=[
        {"kind": "allow_always_scoped", "label": {"scope": "destination_domain"}},
    ])
    assert asyncio.run(decide(conn, record, "allow_always")) is True
    params = conn.calls[0][2]
    assert params["decision"] == "allow_always"
    assert params["always_scope"] == params["allow_always_scope"] == "destination_domain"


def test_allow_today_is_a_valid_one_shot_grant():
    record = _approval(decision_options=["allow_today"])
    assert decision_for(record, "allow_once") == "allow_today"


class _RoutingConn:
    def __init__(self, routes, pending):
        self.routes = routes
        self.pending = pending
        self.calls = []

    async def request(self, method, path, params=None, *, service="daemon", timeout=60.0):
        self.calls.append((method, path, params, service))
        if method == "GET":
            if path == "/approvals":
                return _Response(payload={"ok": True,
                                          "result": {"pending_approvals": self.pending}})
            if path in self.routes:
                return _Response(payload={"ok": True, "result": self.routes[path]})
            return _Response(status=404)
        return _Response(payload={"ok": True})


def _network_settings(conn):
    return next(c[2] for c in conn.calls if c[0] == "POST")


def test_settings_policy_allows_when_account_default_allows():
    record = _approval(host="httpbin.org", port=443, scope={"thread_id": SESSION})
    conn = _RoutingConn({"/permissions/settings": {"network_default": "auto_allow"},
                         "/permissions/active": {"items": []}}, [record])
    assert asyncio.run(handle_pending(conn, SESSION, "settings")) == 1
    assert _network_settings(conn)["decision"] == "allow_session"


def test_settings_policy_denies_denied_protocol():
    record = _approval(host="ssh.example", port=22, scope={"thread_id": SESSION})
    conn = _RoutingConn({"/permissions/settings": {
        "network_default": "auto_allow",
        "protocols": [{"key": "ssh", "ports": [22], "mode": "deny"}],
    }, "/permissions/active": {"items": []}}, [record])
    assert asyncio.run(handle_pending(conn, SESSION, "settings")) == 1
    assert _network_settings(conn)["decision"] == "deny"


def test_settings_policy_uses_ask_fallback_when_account_asks():
    record = _approval(host="unfamiliar.example", port=443, scope={"thread_id": SESSION})
    conn = _RoutingConn({"/permissions/settings": {"network_default": "always_ask"},
                         "/permissions/active": {"items": []}}, [record])
    assert asyncio.run(handle_pending(conn, SESSION, "settings", ask_policy="deny")) == 1
    assert _network_settings(conn)["decision"] == "deny"


def test_settings_policy_honours_connector_override_action():
    record = _approval(server_presentation={"permissionQuestion": {
        "kind": "connector_action", "connector": "browser", "action": "page.download",
    }}, scope={"thread_id": SESSION})
    override = {"kind": "connector_override", "connector": "browser", "mode": "allow",
                "target": {"type": "connector", "connector": {"key": "browser", "actions": [
                    {"key": "page.download", "mode": "allow"}]}}}
    conn = _RoutingConn({"/permissions/settings": {"connector_default": "always_ask"},
                         "/permissions/active": {"items": [override]}}, [record])
    assert asyncio.run(handle_pending(conn, SESSION, "settings")) == 1
    assert _network_settings(conn)["decision"] == "allow_session"


def test_settings_are_cached_per_connection():
    record = _approval(host="httpbin.org", port=443, scope={"thread_id": SESSION})
    conn = _RoutingConn({"/permissions/settings": {"network_default": "auto_allow"},
                         "/permissions/active": {"items": []}}, [record])
    for _ in range(2):
        assert asyncio.run(handle_pending(conn, SESSION, "settings")) == 1
    gets = [c[1] for c in conn.calls if c[0] == "GET"]
    assert gets.count("/permissions/settings") == 1
    assert gets.count("/permissions/active") == 1


def test_scoped_label_shape_from_live_snapshot():
    record = _approval(decision_options=[
        {"kind": "allow_once", "label": {"kind": "allow_once"}, "label_text": "Allow once"},
        {"kind": "allow_always",
         "label": {"kind": "allow_always_scoped", "scope": "destination_domain"},
         "label_text": "Always allow this site"},
    ])
    assert decision_for(record, "allow_always") == "allow_always"
    assert decision_scope(record, "allow_always") == "destination_domain"


def test_settings_reads_nested_network_payload():
    record = _approval(payload={"type": "network", "network_payload": {
        "host": "postman-echo.com", "port": 443, "scheme": "https", "method": "POST",
    }}, display={"permission_question": {"kind": "network_access",
                                         "scope": "postman-echo.com"}},
        decision_options=["allow_once", "allow_always", "deny"])
    conn = _RoutingConn({"/permissions/settings": {"network_default": "auto_allow"},
                         "/permissions/active": {"items": []}}, [record])
    assert asyncio.run(handle_pending(conn, SESSION, "settings")) == 1
    # Session grants are offered for most network approvals; this snapshot
    # only offered one-shot, so the allow mode still proceeds without hanging.
    assert _network_settings(conn)["decision"] == "allow_once"


def test_settings_denies_nested_protocol_port():
    record = _approval(payload={"network_payload": {"host": "db.example", "port": 5432}},
                       scope={"thread_id": SESSION})
    conn = _RoutingConn({"/permissions/settings": {
        "network_default": "auto_allow",
        "protocols": [{"key": "databases", "ports": [5432], "mode": "deny"}],
    }, "/permissions/active": {"items": []}}, [record])
    assert asyncio.run(handle_pending(conn, SESSION, "settings")) == 1
    assert _network_settings(conn)["decision"] == "deny"
