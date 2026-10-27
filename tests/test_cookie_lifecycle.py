from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from muse2api.config import Settings
from muse2api.http import MuseAuthError, MuseHTTP
from muse2api.pool import Account, AccountPool
from muse2api.protocol import ProtocolConfig

FAR_FUTURE = 4102444800


def write_jar(path: Path, value: str) -> None:
    path.write_text(
        "# Netscape HTTP Cookie File\n"
        f".muse.ai\tTRUE\t/\tFALSE\t{FAR_FUTURE}\thatch_sess\t{value}\n",
        encoding="utf-8",
    )


class FakeCookie:
    def __init__(self, name: str, value: str) -> None:
        self.name = name
        self.value = value


class FakeClient:
    def __init__(self, rotated: str) -> None:
        self.cookies = SimpleNamespace(jar=[FakeCookie("hatch_sess", rotated)])

    async def request(self, *args, **kwargs):
        return SimpleNamespace(status_code=200, text="{}", json=lambda: {"vm_id": "vm-1"})

    async def close(self) -> None:
        pass


def make_settings(tmp_path: Path) -> Settings:
    source = tmp_path / "cookies.txt"
    write_jar(source, "old")
    return Settings(cookies_file=str(source), state_dir=tmp_path / ".state")


def test_rotated_cookies_persist_and_survive_restart(tmp_path: Path):
    settings = make_settings(tmp_path)
    http = MuseHTTP(settings)
    http._client = FakeClient("rotated")

    asyncio.run(http.session_info())

    saved = json.loads(http._cache_path.read_text(encoding="utf-8"))
    assert saved["hatch_sess"] == "rotated"

    restarted = MuseHTTP(settings)
    try:
        assert restarted.jar.get("hatch_sess") == "rotated"
    finally:
        asyncio.run(restarted.close())


def test_newer_export_wins_over_cached_cookies(tmp_path: Path):
    settings = make_settings(tmp_path)
    http = MuseHTTP(settings)
    http._client = FakeClient("rotated")
    asyncio.run(http.session_info())

    source = Path(settings.cookies_file)
    write_jar(source, "fresh-export")
    future = http._cache_path.stat().st_mtime + 60
    os.utime(source, (future, future))

    restarted = MuseHTTP(settings)
    try:
        assert restarted.jar.get("hatch_sess") == "fresh-export"
    finally:
        asyncio.run(restarted.close())


def test_reload_cookies_uses_new_export_and_drops_client(tmp_path: Path):
    settings = make_settings(tmp_path)
    http = MuseHTTP(settings)
    http._client = FakeClient("rotated")

    write_jar(Path(settings.cookies_file), "re-exported")
    assert asyncio.run(http.reload_cookies()) is True
    assert http.jar.get("hatch_sess") == "re-exported"
    assert http._client is None
    assert asyncio.run(http.reload_cookies()) is False


class FailingHTTP:
    def __init__(self, error: Exception, recovered: bool) -> None:
        self.error = error
        self.recovered = recovered
        self.calls = 0
        self.reloads = 0
        self.client = None

    async def bootstrap(self, force: bool = False):
        self.calls += 1
        if self.recovered and self.calls > 1:
            return SimpleNamespace(vm_id="vm-1")
        raise self.error

    async def reload_cookies(self) -> bool:
        self.reloads += 1
        return self.recovered


def make_pool(tmp_path: Path, http: FailingHTTP) -> AccountPool:
    settings = Settings(state_dir=tmp_path / ".state")
    return AccountPool(
        settings,
        accounts=[Account(name="a", http=http)],
        protocol=ProtocolConfig.load(),
    )


def test_pool_reloads_cookies_once_after_auth_failure(tmp_path: Path, monkeypatch):
    async def fake_connect(*args, **kwargs):
        return "connection"

    monkeypatch.setattr("muse2api.pool.HatchConnection.connect", fake_connect)
    http = FailingHTTP(MuseAuthError("HTTP 401"), recovered=True)
    account, _session, connection = asyncio.run(make_pool(tmp_path, http).connect())

    assert connection == "connection"
    assert http.reloads == 1
    assert http.calls == 2
    assert account.healthy is True


def test_pool_does_not_reload_on_transient_errors(tmp_path: Path, monkeypatch):
    async def fake_connect(*args, **kwargs):
        return "connection"

    monkeypatch.setattr("muse2api.pool.HatchConnection.connect", fake_connect)
    http = FailingHTTP(RuntimeError("network down"), recovered=False)

    with pytest.raises(RuntimeError):
        asyncio.run(make_pool(tmp_path, http).connect())
    assert http.reloads == 0


def test_reload_ignores_export_older_than_the_live_cache(tmp_path: Path):
    settings = make_settings(tmp_path)
    http = MuseHTTP(settings)
    http._client = FakeClient("rotated")
    asyncio.run(http.session_info())

    source = Path(settings.cookies_file)
    write_jar(source, "stale-export")
    past = http._cache_path.stat().st_mtime - 60
    os.utime(source, (past, past))

    assert asyncio.run(http.reload_cookies()) is False
    assert http.jar.get("hatch_sess") == "rotated"


class RenewHTTP:
    def __init__(self, recovered: bool) -> None:
        self.recovered = recovered
        self.calls = 0
        self.reloads = 0

    async def session_info(self):
        self.calls += 1
        if self.calls == 1:
            raise MuseAuthError("HTTP 401")
        return {}

    async def reload_cookies(self) -> bool:
        self.reloads += 1
        return self.recovered


def test_renew_cookies_recovers_after_reloading_export(tmp_path: Path):
    http = RenewHTTP(recovered=True)
    pool = make_pool(tmp_path, http)

    assert asyncio.run(pool.renew_cookies()) == 1
    assert http.reloads == 1
    assert pool.accounts[0].healthy is True
    assert pool.accounts[0].uses == 0


def test_renew_cookies_marks_account_bad_without_fresh_export(tmp_path: Path):
    http = RenewHTTP(recovered=False)
    pool = make_pool(tmp_path, http)

    assert asyncio.run(pool.renew_cookies()) == 0
    assert pool.accounts[0].healthy is False
