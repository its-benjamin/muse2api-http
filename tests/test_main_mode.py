from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from muse2api.chat import ChatService
from muse2api.config import Settings


class FakeConnection:
    """Streams one unrelated event first, like the real gateway does."""

    def __init__(self) -> None:
        self.sent_event = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.closed = False
        self.sent: list[dict] = []

    async def stream(self, *args, **kwargs):
        # Deliver events only once the request is out, so the ordering of
        # "unrelated event" -> "text" is fixed rather than racy.
        await self.sent_event.wait()
        try:
            yield {"event": "delta.presentation", "payload": {"kind": "image"}}
            await self.release.wait()
            yield {"event": "delta.text_append", "payload": {"text": "hello "}}
            yield {"event": "delta.text_append", "payload": {"text": "world"}}
            yield {"event": "delta.message_done", "payload": {}}
        except asyncio.CancelledError:
            self.cancelled.set()
            raise

    async def send(self, method, path, body=None, **kwargs):
        self.sent.append(body or {})
        self.sent_event.set()

    async def request(self, *args, **kwargs):
        return SimpleNamespace(json=lambda: {})

    async def close(self) -> None:
        self.closed = True


class FakePool:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    async def connect(self):
        return None, None, self.connection


def test_main_mode_keeps_subscription_alive_past_unrelated_events(tmp_path: Path):
    connection = FakeConnection()
    service = ChatService(Settings(state_dir=tmp_path), FakePool(connection))

    async def scenario() -> str:
        task = asyncio.create_task(service._run_main("hi", on_delta=None, timeout=5.0))
        await asyncio.wait_for(connection.sent_event.wait(), 5.0)
        try:
            # The buggy version cancelled the subscription as soon as it saw
            # the presentation event; the fixed one keeps reading the stream.
            await asyncio.wait_for(connection.cancelled.wait(), 1.0)
            cancelled_early = True
        except TimeoutError:
            cancelled_early = False
        connection.release.set()
        result = await asyncio.wait_for(task, 5.0)
        assert cancelled_early is False
        return result.text

    assert asyncio.run(scenario()) == "hello world"
    assert connection.closed is True
    assert connection.sent and connection.sent[0]["message"] == "hi"
