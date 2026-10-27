from __future__ import annotations

import asyncio
import os

from muse2api.config import Settings
from muse2api.http import MuseHTTP


def test_idle_sleep_setting_default_and_override():
    s = Settings.from_env()
    assert s.idle_sleep_seconds == 1800.0

    os.environ["MUSE_IDLE_SLEEP_SECONDS"] = "600"
    try:
        s2 = Settings.from_env()
        assert s2.idle_sleep_seconds == 600.0
    finally:
        os.environ.pop("MUSE_IDLE_SLEEP_SECONDS", None)


def test_http_client_recreated_after_close():
    settings = Settings.from_env()
    # Dummy cookies to satisfy MuseHTTP initialization
    http = MuseHTTP(settings, cookies={"hatch_sess": "dummy-token-for-test"})
    assert http._client is not None
    client_1 = http.client

    # Close the client (simulating idle sleep)
    asyncio.run(http.close())
    assert http._client is None

    # Accessing .client again lazily re-creates the session
    client_2 = http.client
    assert client_2 is not None
    assert client_2 is not client_1

    # Cleanup
    asyncio.run(http.close())


def test_cookie_renew_setting_default_and_override():
    s = Settings.from_env()
    assert s.cookie_renew_seconds == 7200.0

    os.environ["MUSE_COOKIE_RENEW_SECONDS"] = "3600"
    try:
        s2 = Settings.from_env()
        assert s2.cookie_renew_seconds == 3600.0
    finally:
        os.environ.pop("MUSE_COOKIE_RENEW_SECONDS", None)
