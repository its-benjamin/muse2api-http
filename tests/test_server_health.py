from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from starlette.testclient import TestClient

from muse2api.config import Settings
from muse2api.server import TaskStore, create_app


def test_task_store_persistence(tmp_path: Path):
    path = tmp_path / "tasks.json"
    store = TaskStore(path)
    task = store.create("image")
    assert task["id"].startswith("task_")
    assert task["kind"] == "image"
    assert task["object"] == "image_task"
    assert path.is_file()

    # Verify atomic update
    store.update(task["id"], status="succeeded", progress=100)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data[task["id"]]["status"] == "succeeded"
    assert data[task["id"]]["progress"] == 100

    # Verify reloading in fresh store
    store2 = TaskStore(path)
    loaded = store2.get(task["id"])
    assert loaded is not None
    assert loaded["status"] == "succeeded"


def test_healthz_endpoint(tmp_path: Path):
    # Dummy cookies file so settings can initialize
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text("hatch_sess=test-token-123\n", encoding="utf-8")

    settings = Settings(
        cookies_file=str(cookie_file),
        state_dir=tmp_path / ".state",
    )
    app = create_app(settings)

    # Mock pool and http so we don't hit live network
    app.state.pool = MagicMock()
    app.state.pool.status.return_value = [
        {"name": "test-account", "healthy": True, "uses": 0, "last_error": None, "last_status": None, "error_kind": None}
    ]
    app.state.http = MagicMock()
    app.state.http._cached_session = None

    client = TestClient(app)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    data = resp.json()

    # Must contain rich health contract fields
    assert "ok" in data
    assert "mode" in data
    assert "vm_id" in data
    assert "accounts" in data
    assert "uptime_s" in data
    assert "idle_s" in data
    assert "sleeping" in data
    assert "started_at" in data
    assert "last_renewal_at" in data
    assert len(data["accounts"]) == 1
    assert data["accounts"][0]["name"] == "test-account"


def test_image_tasks_idempotency_across_restart(tmp_path: Path):
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text("hatch_sess=test-token-123\n", encoding="utf-8")
    state_dir = tmp_path / ".state"

    settings = Settings(
        cookies_file=str(cookie_file),
        state_dir=state_dir,
    )

    # Simulate first app run creating an image task with an idempotency key
    tasks_file = state_dir / "tasks.json"
    store1 = TaskStore(tasks_file)
    task = store1.create("image")
    task["idempotency_key"] = "test-idem-key-999"
    store1.update(task["id"], idempotency_key="test-idem-key-999", status="succeeded")

    # Create second app instance (simulating restart)
    app2 = create_app(settings)
    # Initialize lifespan state
    app2.state.tasks = TaskStore(tasks_file)
    app2.state.image_tasks = {}
    for t in app2.state.tasks.tasks.values():
        if isinstance(t, dict) and t.get("kind") == "image":
            app2.state.image_tasks[t["id"]] = t
            k = t.get("idempotency_key")
            if k:
                app2.state.image_tasks[k] = t

    client2 = TestClient(app2)
    # Posting with same idempotency key returns reused task without re-running
    resp = client2.post(
        "/v1/images/tasks",
        headers={"idempotency-key": "test-idem-key-999"},
        json={"prompt": "draw a cat"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["reused"] is True
    assert body["id"] == task["id"]
    assert body["status"] == "succeeded"
