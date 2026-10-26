from __future__ import annotations

from muse2api.gateway import (
    CHROME_BROWSER_UA,
    FIREFOX_BROWSER_UA,
    get_ws_headers,
)
from muse2api.http import get_base_headers


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
