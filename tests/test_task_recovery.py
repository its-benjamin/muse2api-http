from __future__ import annotations

import json
from pathlib import Path

from muse2api.server import TaskStore, recover_interrupted_tasks


def test_interrupted_tasks_are_failed_on_recovery(tmp_path: Path):
    path = tmp_path / "tasks.json"
    store = TaskStore(path)
    queued = store.create("video")
    running = store.create("image")
    store.update(running["id"], status="running", progress=40)
    done = store.create("image")
    store.update(done["id"], status="succeeded", progress=100)

    assert recover_interrupted_tasks(store) == 2

    reloaded = TaskStore(path)
    assert reloaded.get(queued["id"])["status"] == "failed"
    assert reloaded.get(running["id"])["status"] == "failed"
    assert "restarted" in reloaded.get(queued["id"])["error"]
    assert reloaded.get(done["id"])["status"] == "succeeded"

    # Idempotent: nothing left to recover.
    assert recover_interrupted_tasks(reloaded) == 0


def test_recovery_keeps_task_file_valid(tmp_path: Path):
    path = tmp_path / "tasks.json"
    store = TaskStore(path)
    task = store.create("video")
    recover_interrupted_tasks(store)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data[task["id"]]["status"] == "failed"
