from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from muse2api.config import Settings
from muse2api.pool import Account, AccountPool
from muse2api.protocol import ProtocolConfig
from muse2api.proxy import ProxyPool, normalize_proxy_url, proxy_hash


def test_proxy_normalization():
    assert normalize_proxy_url("127.0.0.1:8080") == "http://127.0.0.1:8080"
    assert normalize_proxy_url("http://127.0.0.1:8080/") == "http://127.0.0.1:8080"
    assert normalize_proxy_url("socks5://user:pass@proxy.local:1080") == "socks5://user:pass@proxy.local:1080"
    assert normalize_proxy_url(None) is None
    assert normalize_proxy_url("") is None


def test_proxy_hash():
    assert proxy_hash(None) == "direct"
    assert proxy_hash("http://127.0.0.1:8080").startswith("proxy-")


def test_proxy_pool_rotation_and_strategies():
    proxies = ["http://proxy1:8080", "http://proxy2:8080", "http://proxy3:8080"]
    pool = ProxyPool(proxies, strategy="round-robin")
    assert len(pool) == 3

    p1 = pool.next_proxy()
    p2 = pool.next_proxy()
    p3 = pool.next_proxy()
    assert [p1, p2, p3] == proxies
    # wraps around
    assert pool.next_proxy() == proxies[0]

    # fill-first always returns the first
    fill_pool = ProxyPool(proxies, strategy="fill-first")
    assert fill_pool.next_proxy() == proxies[0]
    assert fill_pool.next_proxy() == proxies[0]


def test_proxy_pool_fitness_and_circuit_breaking():
    proxies = ["http://proxy1:8080", "http://proxy2:8080"]
    pool = ProxyPool(proxies, strategy="smart", cooldown_seconds=60.0)

    assert pool.is_fit(proxies[0]) is True
    assert pool.is_fit(proxies[1]) is True

    # Mark proxy1 as unfit
    pool.mark_unfit(proxies[0], reason="connection timeout")
    assert pool.is_fit(proxies[0]) is False

    # Smart selection should now exclusively select proxy2
    assert pool.next_proxy() == proxies[1]
    assert pool.next_proxy() == proxies[1]

    # Manually restore proxy1
    pool.mark_fit(proxies[0])
    assert pool.is_fit(proxies[0]) is True

    # Fail-open test: when all proxies are marked unfit, does not crash and selects one
    pool.mark_unfit(proxies[0], reason="err1")
    pool.mark_unfit(proxies[1], reason="err2")
    chosen = pool.next_proxy()
    assert chosen in proxies


def test_proxy_pool_per_account_pinning():
    proxies = ["http://proxy1:8080", "http://proxy2:8080"]
    pool = ProxyPool(proxies, strategy="smart")

    acc1_proxy = pool.get_for_account("account-1")
    acc2_proxy = pool.get_for_account("account-2")

    # Sticky pinning: subsequent calls return the same proxy
    assert pool.get_for_account("account-1") == acc1_proxy
    assert pool.get_for_account("account-2") == acc2_proxy

    # If pinned proxy becomes unfit, it automatically rotates to a fit one
    pool.mark_unfit(acc1_proxy, reason="blocked")
    new_acc1_proxy = pool.get_for_account("account-1")
    assert new_acc1_proxy != acc1_proxy
    assert new_acc1_proxy == acc2_proxy


def test_proxy_pool_from_settings(tmp_path: Path):
    proxy_file = tmp_path / "proxies.txt"
    proxy_file.write_text("http://pfile1:8080\n# comment\nhttp://pfile2:8080\n", encoding="utf-8")

    settings = Settings(proxy_file=str(proxy_file))
    pool = ProxyPool.from_settings(settings)
    assert len(pool) == 2
    assert pool.next_proxy() == "http://pfile1:8080"

    # From proxies list
    settings2 = Settings(proxies=["http://p1:8080", "http://p2:8080"])
    pool2 = ProxyPool.from_settings(settings2)
    assert len(pool2) == 2

    # From legacy single proxy
    settings3 = Settings(proxy="http://single:8080")
    pool3 = ProxyPool.from_settings(settings3)
    assert len(pool3) == 1
    assert pool3.next_proxy() == "http://single:8080"


def test_smart_concurrency_scaling():
    protocol = ProtocolConfig.load()

    # 1 account, auto concurrency -> 2
    acc1 = Account(name="acc1", http=MagicMock())
    s1 = Settings(concurrency="auto", concurrency_per_account=2)
    pool1 = AccountPool(s1, protocol=protocol, accounts=[acc1])
    assert pool1.compute_concurrency() == 2

    # 3 accounts, auto concurrency -> 6
    acc2 = Account(name="acc2", http=MagicMock())
    acc3 = Account(name="acc3", http=MagicMock())
    pool3 = AccountPool(s1, protocol=protocol, accounts=[acc1, acc2, acc3])
    assert pool3.compute_concurrency() == 6

    # 3 accounts, custom 3 per account -> 9
    s_custom = Settings(concurrency="auto", concurrency_per_account=3)
    pool_custom = AccountPool(s_custom, protocol=protocol, accounts=[acc1, acc2, acc3])
    assert pool_custom.compute_concurrency() == 9

    # Explicit fixed cap override
    s_fixed = Settings(concurrency=10)
    pool_fixed = AccountPool(s_fixed, protocol=protocol, accounts=[acc1, acc2, acc3])
    assert pool_fixed.compute_concurrency() == 10


def test_least_loaded_account_selection():
    protocol = ProtocolConfig.load()
    acc1 = Account(name="acc1", http=MagicMock(), active_requests=2)
    acc2 = Account(name="acc2", http=MagicMock(), active_requests=0)
    acc3 = Account(name="acc3", http=MagicMock(), active_requests=1)

    pool = AccountPool(Settings(), protocol=protocol, accounts=[acc1, acc2, acc3])

    # Account 2 has the lowest active requests (0), so _next() must pick acc2
    chosen = pool._next()
    assert chosen.name == "acc2"


def test_connection_lease_tracks_active_requests():
    import asyncio

    async def _test():
        protocol = ProtocolConfig.load()
        acc = Account(name="acc1", http=MagicMock(), active_requests=0)
        mock_conn = MagicMock()
        mock_conn.is_closed = False
        acc.conn = mock_conn
        acc.session = MagicMock()

        pool = AccountPool(Settings(), protocol=protocol, accounts=[acc])
        assert acc.active_requests == 0

        async with pool.connection() as (leased_acc, _session, _conn):
            assert leased_acc.active_requests == 1

        # Decremented on normal exit
        assert acc.active_requests == 0

        # Decremented even on exception
        with pytest.raises(ValueError):
            async with pool.connection():
                assert acc.active_requests == 1
                raise ValueError("simulated error")
        assert acc.active_requests == 0

    asyncio.run(_test())

def test_healthz_exposes_concurrency_and_proxy_pool(tmp_path: Path):
    from starlette.testclient import TestClient

    from muse2api.server import create_app

    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text("hatch_sess=token123\n", encoding="utf-8")

    settings = Settings(
        cookies_file=str(cookie_file),
        state_dir=tmp_path / ".state",
        proxies=["http://proxy1:8080", "http://proxy2:8080"],
        concurrency="auto",
        concurrency_per_account=3,
    )
    app = create_app(settings)

    with TestClient(app) as client:
        resp = client.get("/healthz")
        assert resp.status_code == 200
        data = resp.json()

        assert "concurrency" in data
        assert "proxy_pool" in data
        assert isinstance(data["proxy_pool"], list)
        assert len(data["proxy_pool"]) == 2
