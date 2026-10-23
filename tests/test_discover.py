from __future__ import annotations

from muse2api.discover import _extract_constant, _extract_routes
from muse2api.protocol import DEFAULT_ROUTES, Route


def test_extracts_minified_route_specs():
    js = (
        'foo={method:"chat.stream",service:"daemon",httpMethod:"POST",path:"/chat/stream",'
        'streamMode:"subscription"},bar={method:"chat.history",httpMethod:"GET",path:"/chat/history"}'
    )
    routes = _extract_routes(js)
    assert routes["chat.stream"]["path"] == "/chat/stream"
    assert routes["chat.stream"]["streamMode"] == "subscription"
    assert routes["chat.history"]["httpMethod"] == "GET"


def test_extracts_constants_from_assignments_and_objects():
    text = 'const HATCH_SHARED_LB_HOST="hatch.metaaivm.com";x={"NOISE_WS_PATH":"/v1/noise"}'
    assert _extract_constant(text, "HATCH_SHARED_LB_HOST") == "hatch.metaaivm.com"
    assert _extract_constant(text, "NOISE_WS_PATH") == "/v1/noise"


def test_routes_merge_with_defaults():
    spec = {**DEFAULT_ROUTES, "chat.stream": {"httpMethod": "POST", "path": "/v2/chat/stream"}}
    route = Route.from_dict("chat.stream", spec["chat.stream"])
    assert route.path == "/v2/chat/stream"
    assert route.http_method == "POST"
