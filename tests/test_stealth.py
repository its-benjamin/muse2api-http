from __future__ import annotations

import asyncio

import pytest

from muse2api import gateway
from muse2api.config import Settings, proxy_map
from muse2api.gateway import (
    CHROME_BROWSER_UA,
    FIREFOX_BROWSER_UA,
    GatewayError,
    HatchConnection,
    get_dynamic_browser_ua,
    get_ws_headers,
)
from muse2api.http import MuseHTTP, get_base_headers


def test_chrome_stealth_headers():
    headers = get_base_headers("chrome")
    assert "TE" not in headers
    assert headers["Sec-Fetch-Dest"] == "empty"
    assert headers["Sec-Fetch-Mode"] == "cors"
    assert headers["Sec-Fetch-Site"] == "same-origin"

    ws_headers = get_ws_headers("chrome")
    assert ws_headers["Sec-Fetch-Dest"] == "websocket"
    assert "Sec-Fetch-Storage-Access" not in ws_headers
    assert "Chrome" in CHROME_BROWSER_UA


def test_firefox_stealth_headers():
    headers = get_base_headers("firefox")
    assert headers.get("TE") == "trailers"

    ws_headers = get_ws_headers("firefox")
    assert ws_headers["Sec-Fetch-Dest"] == "empty"
    assert ws_headers.get("Sec-Fetch-Storage-Access") == "none"
    assert "Firefox" in FIREFOX_BROWSER_UA


def test_dynamic_browser_ua_resolution():
    chrome_ua = get_dynamic_browser_ua("chrome")
    assert "Chrome" in chrome_ua
    assert "Mozilla/5.0" in chrome_ua

    firefox_ua = get_dynamic_browser_ua("firefox")
    assert "Firefox" in firefox_ua
    assert "rv:" in firefox_ua

def test_proxy_map():
    assert proxy_map(None) is None
    assert proxy_map("") is None
    assert proxy_map("socks5://u:p@127.0.0.1:1080") == {
        "http": "socks5://u:p@127.0.0.1:1080",
        "https": "socks5://u:p@127.0.0.1:1080",
    }


def test_fingerprint_defaults_and_env_overrides(monkeypatch):
    for name in ("MUSE_HTTP_VERSION", "MUSE_PROXY", "MUSE_WS_FALLBACK"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings.from_env()
    assert settings.http_version == "2"
    assert settings.proxy is None
    assert settings.ws_fallback is False

    monkeypatch.setenv("MUSE_HTTP_VERSION", "auto")
    monkeypatch.setenv("MUSE_PROXY", "socks5h://127.0.0.1:1080")
    monkeypatch.setenv("MUSE_WS_FALLBACK", "1")
    settings = Settings.from_env()
    assert settings.http_version == "auto"
    assert settings.proxy == "socks5h://127.0.0.1:1080"
    assert settings.ws_fallback is True


def test_http_session_uses_configured_proxy():
    client = MuseHTTP(Settings(proxy="socks5://127.0.0.1:1080"),
                      cookies={"hatch_sess": "test"}).client
    assert client.proxies in (
        {"all": "socks5://127.0.0.1:1080"},
        {"http": "socks5://127.0.0.1:1080", "https": "socks5://127.0.0.1:1080"},
    )
    assert MuseHTTP(Settings(), cookies={"hatch_sess": "test"}).client.proxies == {}


class _FailingWSClient:
    async def ws_connect(self, *args, **kwargs):
        raise RuntimeError("tls blocked")


class _FakeSession:
    def websocket_url(self, **kwargs):
        return "wss://hatch.invalid/v1/noise"


def test_stdlib_ws_fallback_is_opt_in(monkeypatch):
    calls: list[tuple] = []

    async def fake_fallback(*args, **kwargs):
        calls.append(args)
        raise RuntimeError("fallback reached")

    monkeypatch.setattr(gateway, "ws_connect", fake_fallback)
    client = _FailingWSClient()

    with pytest.raises(GatewayError, match="MUSE_WS_FALLBACK"):
        asyncio.run(HatchConnection.connect(_FakeSession(), ws_client=client))
    assert calls == []

    with pytest.raises(RuntimeError, match="fallback reached"):
        asyncio.run(HatchConnection.connect(_FakeSession(), ws_client=client,
                                            ws_fallback=True))
    assert len(calls) == 1
