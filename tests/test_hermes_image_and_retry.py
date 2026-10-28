from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest
from starlette.testclient import TestClient

from muse2api.config import Settings
from muse2api.gateway import GatewayError, HatchConnection
from muse2api.http import GatewaySession
from muse2api.pool import Account
from muse2api.server import (
    MODEL_ALIASES,
    create_app,
    extract_reference_images,
    image_size_to_aspect,
)


def test_image_size_to_aspect_hermes():
    # Hermes landscape formats
    assert image_size_to_aspect("landscape") == "16:9"
    assert image_size_to_aspect("landscape_16_9") == "16:9"
    assert image_size_to_aspect("1536x1024") == "16:9"
    assert image_size_to_aspect("1792x1024") == "16:9"
    assert image_size_to_aspect("1024x768") == "4:3"
    assert image_size_to_aspect("landscape_4_3") == "4:3"

    # Hermes portrait formats
    assert image_size_to_aspect("portrait") == "9:16"
    assert image_size_to_aspect("portrait_16_9") == "9:16"
    assert image_size_to_aspect("1024x1536") == "9:16"
    assert image_size_to_aspect("1024x1792") == "9:16"
    assert image_size_to_aspect("768x1024") == "3:4"
    assert image_size_to_aspect("portrait_4_3") == "3:4"

    # Hermes square formats
    assert image_size_to_aspect("square") == "1:1"
    assert image_size_to_aspect("square_hd") == "1:1"
    assert image_size_to_aspect("1024x1024") == "1:1"
    assert image_size_to_aspect("512x512") == "1:1"

    # None and empty fallback
    assert image_size_to_aspect(None) == "1:1"
    assert image_size_to_aspect("") == "1:1"


def test_extract_reference_images():
    # Single image_url from Hermes
    body = {"prompt": "test", "image_url": "https://example.com/img1.png"}
    assert extract_reference_images(body) == ["https://example.com/img1.png"]

    # Multiple reference_image_urls from Hermes
    body = {
        "prompt": "blend",
        "image_url": "https://example.com/a.png",
        "reference_image_urls": ["https://example.com/b.png", "https://example.com/c.png"],
    }
    refs = extract_reference_images(body)
    assert refs == ["https://example.com/a.png", "https://example.com/b.png", "https://example.com/c.png"]

    # Empty body
    assert extract_reference_images({"prompt": "plain"}) == []


def test_hermes_model_aliases():
    assert "muse-image-1.0" in MODEL_ALIASES
    assert "muse-image" in MODEL_ALIASES
    assert "gpt-image-2-medium" in MODEL_ALIASES
    assert "gpt-image-2.5-flare" in MODEL_ALIASES
    assert "fal-ai/flux-2/klein/9b" in MODEL_ALIASES


def test_upstream_502_cooldown_is_short():
    account = Account(name="test_acc", http=None)
    exc = GatewayError("Failed to perform, curl: (22) Refused WebSocket upgrade: 502.")
    start = time.monotonic()
    account.mark_bad(exc)

    assert account.healthy is False
    assert account.error_kind == "upstream"
    assert account.last_status == 502
    # Upstream cooldown should be ~10s, NOT 120s
    assert account.cooldown_until - start <= 15.0
    assert account.cooldown_until - start >= 8.0


def test_ws_connect_retries_on_502(monkeypatch):
    attempts = 0

    class MockWS:
        def __init__(self):
            pass

        async def send(self, data):
            pass

        async def recv(self):
            # Noise handshake message 2
            return b"\x00" * 96

        async def close(self):
            pass

        async def __aiter__(self):
            await asyncio.Event().wait()
            yield b""

    class MockAsyncSession:
        async def ws_connect(self, *args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise RuntimeError("Failed to perform, curl: (22) Refused WebSocket upgrade: 502.")
            return MockWS()

    session = GatewaySession(
        vm_id="vm-test",
        gateway_url="https://vm-test.example.com",
        auth_token="auth123",
    )

    # Patch Noise handshake steps to pass through
    monkeypatch.setattr("muse2api.gateway.NoiseXXInitiator.initialize", AsyncMock())
    monkeypatch.setattr("muse2api.gateway.NoiseXXInitiator.write_message1", lambda self, p: b"m1")
    monkeypatch.setattr("muse2api.gateway.NoiseXXInitiator.read_message2", lambda self, p: b"")
    monkeypatch.setattr("muse2api.gateway.NoiseXXInitiator.write_message3", lambda self, p: b"m3")
    monkeypatch.setattr("muse2api.gateway.NoiseXXInitiator.split", lambda self: (AsyncMock(), AsyncMock()))

    client = MockAsyncSession()

    async def _test():
        conn = await HatchConnection.connect(session, ws_client=client)
        assert attempts == 3
        assert conn is not None
        assert not conn.is_closed
        await conn.close()

    asyncio.run(_test())


def test_images_edits_json_endpoint(tmp_path):
    app = create_app(Settings(state_dir=tmp_path / ".state"))
    client = TestClient(app)

    # Missing image should return 400 invalid_image, NOT 422
    resp = client.post("/v1/images/edits", json={"prompt": "make it sunny"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_image"

    # Missing prompt should return 400
    resp = client.post("/v1/images/edits", json={"image_url": "https://example.com/a.png"})
    assert resp.status_code == 400
    assert resp.json()["error"]["message"] == "prompt is required"


def test_image_instruction_allows_search_when_unsure(tmp_path, monkeypatch):
    captured_instructions: list[str] = []

    async def mock_run_with_media(instruction, **kwargs):
        captured_instructions.append(instruction)
        raise RuntimeError("stop here")

    app = create_app(Settings(state_dir=tmp_path / ".state"))
    chat_mock = AsyncMock()
    chat_mock.run_with_media = mock_run_with_media
    app.state.chat = chat_mock
    app.state.sem = asyncio.Semaphore(2)
    client = TestClient(app)
    with pytest.raises(RuntimeError, match="stop here"):
        client.post(
            "/v1/images/generations",
            json={"prompt": "Kafka Hibino from Kaiju No. 8 fighting", "size": "landscape"},
        )

    assert len(captured_instructions) == 1
    inst = captured_instructions[0]
    assert "aspect ratio 16:9" in inst
    assert "research visual image references only" in inst
    assert "Do not research narrative lore" in inst
    assert "non-sexualized" not in inst
    assert "tasteful" not in inst
    assert "heroic" not in inst


def test_refusal_detection_vindicta():
    from muse2api.chat import MEDIA_REFUSAL_MARKERS
    from muse2api.server import classify_image_error

    refusal_text = (
        "I checked Vindicta's canonical lore before generating — she says she was a child "
        "when she was killed during the Salem Witch Trials, with that period's accusers/accused "
        "noted as young, ranging 9 to 20.\n\n"
        "Because of that, I can't make the requested version — a reclining, camera-facing portrait "
        "with her boots and feet as the prominent foreground focal point. That fetish-focused "
        "composition sexualizes a character whose canonical age isn't established as adult, "
        "regardless of the 'adult' label in the request.\n\n"
        "I can make a non-sexualized 16:9 portrait instead: Vindicta as a ghostly sniper on a "
        "rainy neon rooftop in her dark tactical outfit, rifle in hand, crow familiar nearby, "
        "in polished game concept-art style."
    )

    lowered = refusal_text.lower()
    matched = [marker for marker in MEDIA_REFUSAL_MARKERS if marker in lowered]
    assert len(matched) > 0, "Refusal text must match refusal markers"
    assert "can't make" in matched
    assert "sexualizes" in matched
    assert "fetish-focused" in matched
    assert "isn't established as adult" in matched

    code = classify_image_error(refusal_text)
    assert code == "image_policy_violation"
