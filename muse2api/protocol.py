"""Protocol configuration with a discovered-override layer.

The web bundle is the source of truth. ``scripts/discover_protocol.py`` walks
the public Next.js chunks (and their source maps), extracts the gateway
constants and the HTTP route table, and writes ``protocol.json`` next to this
module. At runtime that file overrides these built-in defaults, so a server-side
rename can be absorbed without editing code.

Environment overrides always win: ``MUSE_LB_HOST``, ``MUSE_WS_PATH``,
``MUSE_APP_ID``.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("muse2api.protocol")

DEFAULT_LB_HOST = "hatch.metaaivm.com"
DEFAULT_WS_PATH = "/v1/noise"
DEFAULT_APP_ID = "hatch-web"

CONFIG_PATH = Path(__file__).with_name("protocol.json")
STATE_PATH = Path(".muse-state/protocol.json")


@dataclass(frozen=True, slots=True)
class Route:
    method: str
    http_method: str
    path: str
    service: str = "daemon"
    stream_mode: str | None = None

    @classmethod
    def from_dict(cls, method: str, data: dict) -> "Route":
        return cls(
            method=method,
            http_method=str(data.get("httpMethod") or data.get("http_method") or "POST").upper(),
            path=str(data.get("path") or ""),
            service=str(data.get("service") or "daemon"),
            stream_mode=data.get("streamMode") or data.get("stream_mode"),
        )


DEFAULT_ROUTES: dict[str, dict] = {
    "connection.ping": {"httpMethod": "POST", "path": "/api/ping"},
    "model.get": {"httpMethod": "GET", "path": "/model"},
    "sessions.list": {"httpMethod": "GET", "path": "/api/session/list"},
    "nodes.register": {"httpMethod": "POST", "path": "/api/nodes/register"},
    "client.register_capabilities": {"httpMethod": "POST", "path": "/client/register-capabilities"},
    "chat.stream": {"httpMethod": "POST", "path": "/chat/stream", "streamMode": "subscription"},
    "chat.subscribe": {"httpMethod": "POST", "path": "/chat/subscribe", "streamMode": "subscription"},
    "chat.history": {"httpMethod": "GET", "path": "/chat/history"},
}


@dataclass(slots=True)
class ProtocolConfig:
    lb_host: str = DEFAULT_LB_HOST
    ws_path: str = DEFAULT_WS_PATH
    app_id: str = DEFAULT_APP_ID
    routes: dict[str, Route] = field(default_factory=dict)
    source: str = "defaults"
    generated_at: str | None = None

    @classmethod
    def load(cls, path: Path | None = None) -> "ProtocolConfig":
        data: dict = {}
        source = "defaults"
        for candidate in (path, CONFIG_PATH, STATE_PATH):
            if candidate is None or not candidate.is_file():
                continue
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
                source = str(candidate)
                break
            except (OSError, ValueError) as exc:
                log.warning("could not read %s: %s", candidate, exc)
        routes_raw = {**DEFAULT_ROUTES, **(data.get("routes") or {})}
        routes: dict[str, Route] = {}
        for method, spec in routes_raw.items():
            if not isinstance(spec, dict) or not spec.get("path"):
                continue
            routes[method] = Route.from_dict(method, spec)
        return cls(
            lb_host=os.environ.get("MUSE_LB_HOST") or data.get("lb_host") or DEFAULT_LB_HOST,
            ws_path=os.environ.get("MUSE_WS_PATH") or data.get("ws_path") or DEFAULT_WS_PATH,
            app_id=os.environ.get("MUSE_APP_ID") or data.get("app_id") or DEFAULT_APP_ID,
            routes=routes,
            source=source,
            generated_at=data.get("generated_at"),
        )

    def route(self, method: str) -> Route:
        route = self.routes.get(method)
        if route is None:
            spec = DEFAULT_ROUTES.get(method)
            if spec is None:
                raise KeyError(f"no route for {method}")
            route = Route.from_dict(method, spec)
        return route

    def websocket_url(self, vm_id: str, auth_token: str, notary_token: str | None,
                      request_id: str) -> str:
        from urllib.parse import urlencode

        params = {
            "vm_id": vm_id,
            "auth_token": auth_token,
            "app_id": self.app_id,
            "request_id": request_id,
        }
        if notary_token:
            params["notary_token"] = notary_token
        return f"wss://{self.lb_host}{self.ws_path}?{urlencode(params)}"
