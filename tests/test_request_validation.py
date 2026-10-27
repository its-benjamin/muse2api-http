from __future__ import annotations

from pathlib import Path

from starlette.testclient import TestClient

from muse2api import server
from muse2api.config import Settings
from muse2api.server import create_app


def make_client(tmp_path: Path, **settings) -> TestClient:
    return TestClient(create_app(Settings(state_dir=tmp_path / ".state", **settings)))


def test_bad_image_count_is_a_client_error(tmp_path):
    client = make_client(tmp_path)
    for value in ("abc", 0, 11, True):
        response = client.post("/v1/images/generations", json={"prompt": "x", "n": value})
        assert response.status_code == 400, (value, response.text)
        assert response.json()["error"]["type"] == "invalid_request_error"


def test_bad_video_duration_is_a_client_error(tmp_path):
    client = make_client(tmp_path)
    for value in ("soon", -3, 3600):
        response = client.post("/v1/videos", json={"prompt": "x", "duration": value})
        assert response.status_code == 400, (value, response.text)


def test_non_object_messages_are_rejected(tmp_path):
    client = make_client(tmp_path)
    response = client.post("/v1/chat/completions", json={"messages": ["hello"]})
    assert response.status_code == 400
    assert "messages[0]" in response.json()["error"]["message"]


def test_oversized_edit_upload_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "MAX_MEDIA_BYTES", 16)
    client = make_client(tmp_path)
    response = client.post(
        "/v1/images/edits",
        files={"image": ("a.png", b"x" * 64, "image/png")},
        data={"prompt": "make it blue"},
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "invalid_image"


def test_api_key_requires_constant_time_match(tmp_path):
    client = make_client(tmp_path, api_key="secret-key")
    assert client.get("/v1/models").status_code == 401
    assert client.get("/v1/models", headers={"Authorization": "Bearer nope"}).status_code == 401
    authorized = client.get("/v1/models", headers={"Authorization": "Bearer secret-key"})
    assert authorized.status_code == 200
    assert authorized.json()["object"] == "list"
