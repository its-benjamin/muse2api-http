"""Multi-account pool with failover.

Each account is one cookie jar / muse.ai login. A failed account is put on a
short cooldown, and new connections are routed to the next healthy one. This
mirrors the reference project's account-pool behaviour without a headless
browser.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings
from .cookies import parse_cookie_file
from .gateway import GatewaySession, HatchConnection
from .http import MuseAuthError, MuseHTTP
from .protocol import ProtocolConfig

log = logging.getLogger("muse2api.pool")


@dataclass
class Account:
    name: str
    http: MuseHTTP
    healthy: bool = True
    cooldown_until: float = 0.0
    last_error: str | None = None
    last_status: int | None = None
    error_kind: str | None = None  # auth | forbidden | rate_limit | upstream | network
    uses: int = 0
    last_used: float = field(default_factory=time.time)
    conn: HatchConnection | None = None
    session: GatewaySession | None = None

    def available(self) -> bool:
        return self.healthy or time.monotonic() >= self.cooldown_until

    def mark_bad(self, error: Exception, *, cooldown: float = 120.0) -> None:
        self.healthy = False
        self.cooldown_until = time.monotonic() + cooldown
        self.last_error = str(error)[:500]
        if self.conn is not None:
            try:
                self.conn._closed.set()
            except Exception:
                pass
            self.conn = None
            self.session = None
        # Classify the failure so callers/health endpoints can distinguish
        # auth (401) vs forbidden (403) vs rate-limit (429) vs upstream/network.
        self.last_status = None
        self.error_kind = "network"
        text = str(error).lower()
        # Try to extract an HTTP status code from the message.
        import re as _re

        m = _re.search(r"\b(401|403|429|5\d{2})\b", text)
        if m:
            self.last_status = int(m.group(1))
        if self.last_status == 401 or "auth" in text or "unauthorized" in text:
            self.error_kind = "auth"
            cooldown = max(cooldown, 300.0)
        elif self.last_status == 403 or "forbidden" in text:
            self.error_kind = "forbidden"
            cooldown = max(cooldown, 600.0)
        elif self.last_status == 429 or "rate" in text or "quota" in text:
            self.error_kind = "rate_limit"
            cooldown = max(cooldown, 180.0)
        elif self.last_status and self.last_status >= 500:
            self.error_kind = "upstream"
            cooldown = min(cooldown, 10.0)
        elif self.error_kind == "network":
            cooldown = min(cooldown, 10.0)
        self.cooldown_until = time.monotonic() + cooldown
        log.warning("account %s unhealthy for %.0fs (%s): %s",
                    self.name, cooldown, self.error_kind, error)

    def mark_good(self) -> None:
        self.healthy = True
        self.cooldown_until = 0.0
        self.uses += 1
        self.last_used = time.time()


class PoolExhausted(RuntimeError):
    """Every configured account failed or is cooling down."""


class AccountPool:
    def __init__(self, settings: Settings, accounts: list[Account] | None = None,
                 protocol: ProtocolConfig | None = None) -> None:
        self.settings = settings
        self.protocol = protocol or ProtocolConfig.load()
        if accounts is not None:
            self.accounts = accounts
        else:
            self.accounts = self._load_accounts(settings)
        if not self.accounts:
            raise ValueError("no muse.ai accounts configured")
        self._index = 0
        self._lock = asyncio.Lock()

    @staticmethod
    def _load_accounts(settings: Settings) -> list[Account]:
        raw = os.environ.get("MUSE_COOKIE_FILES") or settings.cookies_file
        files = [part.strip() for part in raw.split(",") if part.strip()]
        accounts: list[Account] = []
        for index, path in enumerate(files):
            try:
                cookies = parse_cookie_file(path)
                accounts.append(Account(name=Path(path).name or f"account-{index + 1}",
                                        http=MuseHTTP(settings, cookies=cookies,
                                                      cookie_path=path)))
            except Exception as exc:  # noqa: BLE001
                log.error("could not load cookies from %s: %s", path, exc)
        return accounts

    def _next(self) -> Account:
        count = len(self.accounts)
        for offset in range(count):
            account = self.accounts[(self._index + offset) % count]
            if account.available():
                self._index = (self._index + offset + 1) % count
                return account
        # Everyone is cooling down: reset the earliest and try it.
        account = min(self.accounts, key=lambda item: item.cooldown_until)
        account.healthy = True
        account.cooldown_until = 0.0
        self._index = (self.accounts.index(account) + 1) % count
        return account

    async def _open(self, account: Account) -> tuple[Account, GatewaySession, HatchConnection]:
        if account.conn is not None and not account.conn.is_closed and account.session is not None:
            return account, account.session, account.conn
        session = await account.http.bootstrap()
        try:
            conn = await HatchConnection.connect(
                session,
                lb_host=self.protocol.lb_host,
                ws_path=self.protocol.ws_path,
                cvm_rv_key=self.settings.cvm_rv_key,
                attestation_cmd=self.settings.attestation_cmd,
                ws_client=account.http.client,
                impersonate=self.settings.impersonate,
                user_agent=self.settings.user_agent,
                proxy=self.settings.proxy,
                ws_fallback=self.settings.ws_fallback,
            )
        except Exception as exc:
            log.warning(
                "gateway connection failed for %s (%s); retrying with fresh bootstrap",
                account.name, exc,
            )
            session = await account.http.bootstrap(force=True)
            conn = await HatchConnection.connect(
                session,
                lb_host=self.protocol.lb_host,
                ws_path=self.protocol.ws_path,
                cvm_rv_key=self.settings.cvm_rv_key,
                attestation_cmd=self.settings.attestation_cmd,
                ws_client=account.http.client,
                impersonate=self.settings.impersonate,
                user_agent=self.settings.user_agent,
                proxy=self.settings.proxy,
                ws_fallback=self.settings.ws_fallback,
            )
        account.conn = conn
        account.session = session
        return account, session, conn

    async def _retry_after_reload(self, account: Account) -> bool:
        """Pick up a re-exported cookie file once instead of cooling down."""
        try:
            return await account.http.reload_cookies()
        except Exception as exc:  # noqa: BLE001 - reload is best effort
            log.debug("cookie reload failed for %s: %s", account.name, exc)
            return False

    async def connect(self) -> tuple[Account, GatewaySession, HatchConnection]:
        last_error: Exception | None = None
        async with self._lock:
            order = [self._next() for _ in range(len(self.accounts))]
        for account in order:
            try:
                result = await self._open(account)
                account.mark_good()
                return result
            except Exception as exc:  # noqa: BLE001 - try the next login
                last_error = exc
                if isinstance(exc, MuseAuthError) and await self._retry_after_reload(account):
                    try:
                        result = await self._open(account)
                        account.mark_good()
                        return result
                    except Exception as retry_exc:  # noqa: BLE001
                        last_error = retry_exc
                account.mark_bad(last_error)
                log.warning("account %s failed: %s", account.name, last_error)
        raise PoolExhausted(f"all muse.ai accounts failed; last error: {last_error}")

    async def bootstrap(self) -> tuple[Account, GatewaySession]:
        last_error: Exception | None = None
        for account in [self._next() for _ in range(len(self.accounts))]:
            try:
                session = await account.http.bootstrap(force=True)
                account.mark_good()
                return account, session
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if isinstance(exc, MuseAuthError) and await self._retry_after_reload(account):
                    try:
                        session = await account.http.bootstrap(force=True)
                        account.mark_good()
                        return account, session
                    except Exception as retry_exc:  # noqa: BLE001
                        last_error = retry_exc
                account.mark_bad(last_error)
        raise PoolExhausted(f"all muse.ai accounts failed; last error: {last_error}")

    async def renew_cookies(self) -> int:
        """Refresh rolling cookies account by account; returns successes."""
        renewed = 0
        for account in self.accounts:
            try:
                await account.http.session_info()
            except MuseAuthError:
                if not await self._retry_after_reload(account):
                    account.mark_bad(MuseAuthError("cookie renewal rejected"))
                    continue
                try:
                    await account.http.session_info()
                except Exception as exc:  # noqa: BLE001 - keep other accounts alive
                    account.mark_bad(exc)
                    continue
            except Exception as exc:  # noqa: BLE001
                account.mark_bad(exc)
                continue
            # mark_good() would inflate the load-balancing `uses` counter.
            account.healthy = True
            account.cooldown_until = 0.0
            renewed += 1
        return renewed

    async def refresh_all(self) -> None:
        for account in self.accounts:
            try:
                await account.http.bootstrap(force=True)
                account.mark_good()
            except Exception as exc:  # noqa: BLE001
                account.mark_bad(exc)

    def status(self) -> list[dict]:
        return [
            {
                "name": account.name,
                "healthy": account.available(),
                "uses": account.uses,
                "last_error": account.last_error,
                "last_status": account.last_status,
                "error_kind": account.error_kind,
            }
            for account in self.accounts
        ]

    async def close_idle(self) -> None:
        """Close open HTTP and warm WebSocket sessions during idle sleep."""
        for account in self.accounts:
            if account.conn is not None:
                try:
                    await account.conn.close()
                except Exception:
                    pass
                account.conn = None
                account.session = None
            try:
                await account.http.close()
            except Exception:
                pass

    async def close(self) -> None:
        for account in self.accounts:
            if account.conn is not None:
                try:
                    await account.conn.close()
                except Exception:
                    pass
                account.conn = None
                account.session = None
            await account.http.close()
