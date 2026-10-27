from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from muse2api.media import (
    MAX_MEDIA_BYTES,
    MediaFetchError,
    MediaRef,
    MediaTooLargeError,
    assert_public_host,
    download_media,
    fetch_public_media,
)
from muse2api.server import ApiError, reference_image_item


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/image.png",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/image.png",
        "http://[::1]/image.png",
        "http://0.0.0.0/image.png",
    ],
)
def test_private_reference_urls_are_refused_before_any_request(url):
    with pytest.raises(MediaFetchError):
        asyncio.run(fetch_public_media(url))


def test_reference_image_reports_guard_rejection_as_client_error():
    with pytest.raises(ApiError) as excinfo:
        asyncio.run(reference_image_item("http://127.0.0.1/image.png"))
    assert excinfo.value.status == 400
    assert excinfo.value.code == "invalid_image"


def test_private_hosts_can_be_opted_in(monkeypatch):
    monkeypatch.setenv("MUSE2API_ALLOW_PRIVATE_URLS", "1")
    assert_public_host("127.0.0.1")


def test_guard_is_skipped_only_when_explicitly_enabled(monkeypatch):
    monkeypatch.setenv("MUSE2API_ALLOW_PRIVATE_URLS", "0")
    with pytest.raises(MediaFetchError):
        assert_public_host("127.0.0.1")


class _FakeConn:
    def __init__(self, body: bytes) -> None:
        self.body = body

    async def request(self, *args, **kwargs):
        return SimpleNamespace(status=200, body=self.body)


def test_media_ceiling_is_150_mib():
    assert MAX_MEDIA_BYTES == 150 * 1024 * 1024


def test_download_media_enforces_the_ceiling():
    conn = _FakeConn(b"x" * 64)
    ref = MediaRef(kind="image", path="generated.png")
    with pytest.raises(MediaTooLargeError):
        asyncio.run(download_media(conn, ref, max_bytes=32))
    assert asyncio.run(download_media(conn, ref, max_bytes=1024)) == b"x" * 64
