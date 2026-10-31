"""muse.ai HTTP bootstrap: cookies -> session -> VM -> admission/notary tokens.

Uses ``curl_cffi`` with Chrome impersonation because muse.ai sits behind Meta's
edge proxies that reject some stock TLS fingerprints on authenticated routes.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from curl_cffi.requests import AsyncSession

if TYPE_CHECKING:
    from .gateway import HatchConnection
from .config import Settings
from .cookies import CookieError, CookieJar, parse_cookie_file
from .gateway import GatewaySession

log = logging.getLogger("muse2api.http")

BASE = "https://muse.ai"
# Only used by the stdlib-WebSocket fallback; curl_cffi sets the real UA from
# the impersonation profile for both HTTP and WS.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)
# Copied from the captured Firefox XHRs. curl_cffi owns TLS/HTTP2,
# Accept-Encoding, Host and Connection; these are the application headers the
# web client actually sends.
def get_base_headers(impersonate: str = "chrome") -> dict[str, str]:
    headers = {
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9,en-GB;q=0.8",
        "Origin": BASE,
        "Referer": BASE + "/",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "Priority": "u=4",
        "Pragma": "no-cache",
        "Cache-Control": "no-cache",
    }
    if "firefox" in (impersonate or "").lower():
        headers["TE"] = "trailers"
    return headers


BASE_HEADERS = get_base_headers("chrome")


def _parse_http_version(value: str):
    from curl_cffi.const import CurlHttpVersion

    return {
        "1.1": CurlHttpVersion.V1_1,
        "2": CurlHttpVersion.V2_0,
        "3": CurlHttpVersion.V3,
        "auto": None,
    }.get(value)


class MuseAuthError(RuntimeError):
    pass


class MuseUpstreamError(RuntimeError):
    pass


def _cookie_cache_path(state_dir: Path, source: Path) -> Path:
    try:
        key = str(source.resolve())
    except OSError:  # pragma: no cover - missing drive/permission
        key = str(source)
    digest = hashlib.sha256(key.encode()).hexdigest()[:10]
    return state_dir / f"cookies-{digest}.json"


class MuseHTTP:
    def __init__(self, settings: Settings, cookies: dict[str, str] | None = None,
                 cookie_path: str | Path | None = None,
                 proxy: str | None = None) -> None:
        self.settings = settings
        self.proxy = proxy or settings.proxy
        self._cookie_source = Path(cookie_path) if cookie_path else Path(settings.cookies_file)
        self._cache_path = _cookie_cache_path(settings.state_dir, self._cookie_source)
        if cookies is None:
            try:
                cookies = parse_cookie_file(self._cookie_source)
            except (OSError, CookieError):
                cookies = {}
        self.jar = CookieJar(cookies)
        # Prefer the last rotated jar over a stale export, unless the operator
        # just wrote a newer cookie file.
        self._restore_cached_cookies()
        missing = [name for name in ("hatch_sess",) if name not in self.jar]
        if missing:
            raise CookieError(
                "missing hatch_sess cookie; export fresh cookies from a logged-in muse.ai tab"
            )
        self._persisted = self.jar.copy()
        self._last_persist = 0.0
        self._http_version = _parse_http_version(settings.http_version)
        self._client: AsyncSession | None = self._create_client()
        self._cached_session: GatewaySession | None = None
        self._cached_at = 0.0

    # -- cookie persistence ------------------------------------------------

    def _restore_cached_cookies(self) -> None:
        try:
            if not self._cache_path.is_file():
                return
            source_mtime = (
                self._cookie_source.stat().st_mtime if self._cookie_source.exists() else 0.0
            )
            if self._cache_path.stat().st_mtime < source_mtime:
                return  # the operator exported fresh cookies; the source wins
            data = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.debug("could not restore rotated cookies: %s", exc)
            return
        if not isinstance(data, dict):
            return
        self.jar.merge({str(k): str(v) for k, v in data.items() if isinstance(v, str)})
        log.info("restored rotated muse.ai cookies from %s", self._cache_path)

    def _persist_cookies(self, *, force: bool = False) -> None:
        """Atomically save rotated cookies so restarts reuse the live session."""
        now = time.monotonic()
        if not force and now - self._last_persist < 30.0:
            return
        cookies = self.jar.copy()
        if cookies == self._persisted:
            self._last_persist = now
            return
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            temp = self._cache_path.with_suffix(".tmp")
            temp.write_text(json.dumps(cookies, indent=2), encoding="utf-8")
            temp.replace(self._cache_path)
        except OSError as exc:  # pragma: no cover - best effort
            log.debug("could not persist rotated cookies: %s", exc)
            return
        self._persisted = cookies
        self._last_persist = now

    async def reload_cookies(self) -> bool:
        """Pick up a freshly exported cookie file without a restart."""
        try:
            fresh = parse_cookie_file(self._cookie_source)
            source_mtime = self._cookie_source.stat().st_mtime
        except (OSError, CookieError) as exc:
            log.debug("could not reload cookies from %s: %s", self._cookie_source, exc)
            return False
        try:
            cache_mtime = self._cache_path.stat().st_mtime
        except OSError:
            cache_mtime = 0.0
        if cache_mtime and source_mtime <= cache_mtime:
            # The cached jar is the fresher one; never roll back a live session
            # because the original export is still lying around.
            return False
        if not any(self.jar.get(name) != value for name, value in fresh.items()):
            return False
        self.jar.merge(fresh)
        await self.close()
        self._cached_session = None
        self._persist_cookies(force=True)
        log.info("reloaded muse.ai cookies from %s", self._cookie_source)
        return True

    def _create_client(self) -> AsyncSession:
        return AsyncSession(
            impersonate=self.settings.impersonate,
            timeout=self.settings.request_timeout,
            http_version=self._http_version,
            max_clients=50,
            retry=2,
            cookies=self.jar.copy(),
            **({"proxy": self.proxy or self.settings.proxy} if (self.proxy or self.settings.proxy) else {}),
        )

    @property
    def client(self) -> AsyncSession:
        if self._client is None:
            self._client = self._create_client()
        return self._client

    async def set_proxy(self, proxy: str | None) -> None:
        """Update egress proxy, gracefully closing the existing client session."""
        if proxy != self.proxy:
            self.proxy = proxy
            await self.close()
    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:
                pass
            self._client = None

    # -- low level ---------------------------------------------------------

    def _merge_cookies(self) -> None:
        if self._client is None:
            return
        try:
            for cookie in self._client.cookies.jar:
                if cookie.name and cookie.value:
                    self.jar.merge({cookie.name: cookie.value})
            self._persist_cookies()
        except Exception:  # pragma: no cover - jar internals differ by version
            pass

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        params: dict | None = None,
        timeout: float | None = None,
        require_ok: bool = True,
    ) -> dict:
        headers = get_base_headers(self.settings.impersonate)
        # Let the impersonation profile set the UA unless the operator pins one.
        if self.settings.user_agent:
            headers["User-Agent"] = self.settings.user_agent
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        try:
            response = await self.client.request(
                method.upper(),
                BASE + path,
                headers=headers,
                json=json_body,
                params=params,
                timeout=timeout or self.settings.request_timeout,
            )
        except Exception as exc:
            raise MuseUpstreamError(f"{method.upper()} {path} failed: {exc}") from exc
        finally:
            self._merge_cookies()

        if response.status_code in (401, 403):
            # 403 also happens when hatch_vml is stale; callers can retry once
            # after refreshing /api/session.
            raise MuseAuthError(
                f"{method.upper()} {path} -> HTTP {response.status_code}: "
                f"{response.text[:200]}"
            )
        if require_ok and not 200 <= response.status_code < 300:
            raise MuseUpstreamError(
                f"{method.upper()} {path} -> HTTP {response.status_code}: {response.text[:300]}"
            )
        try:
            data = response.json()
        except Exception as exc:
            raise MuseUpstreamError(
                f"{method.upper()} {path} returned non-JSON: {response.text[:200]!r}"
            ) from exc
        if not isinstance(data, dict):
            raise MuseUpstreamError(f"{method.upper()} {path} returned {type(data).__name__}")
        return data

    # -- bootstrap ---------------------------------------------------------

    async def session_info(self) -> dict:
        info = await self.request_json("GET", "/api/session", params={"vm_state": "live"})
        # /api/session rotates the long-lived cookies; save immediately so a
        # restart never falls back to a stale export.
        self._persist_cookies(force=True)
        return info

    async def _wake_vm(self, vm_id: str) -> None:
        await self.request_json(
            "POST",
            "/api/hatch/vm/wake",
            json_body={
                "vm_id": vm_id,
                "retry_count": 1,
                "connect_attempt_id": str(uuid.uuid4()),
            },
            require_ok=False,
        )

    async def _wait_running(self, vm_id: str, deadline: float) -> dict:
        last: dict = {}
        while time.monotonic() < deadline:
            try:
                info = await self.session_info()
            except MuseAuthError:
                raise
            except Exception as exc:  # noqa: BLE001 - retry transient upstream
                log.debug("session poll failed: %s", exc)
                await asyncio.sleep(1.2 + random.uniform(0.1, 0.4))
                continue
            last = info
            status = str(info.get("status") or "")
            state = str(info.get("vm_state") or "")
            if status == "assigned" and state.upper() == "RUNNING" and info.get("vm_id"):
                return info
            if status in {"unavailable", "pending"} or state.upper() in {"STARTING", "WAKING"}:
                await asyncio.sleep(1.5)
                continue
            if info.get("vm_id") and state.upper() in {"RUNNING"}:
                return info
            await asyncio.sleep(1.2 + random.uniform(0.1, 0.4))
        raise MuseUpstreamError(
            f"VM {vm_id} did not become RUNNING: {last.get('status')} / {last.get('vm_state')}"
            if last else f"VM {vm_id} did not become RUNNING before timeout"
        )

    async def bootstrap(self, *, force: bool = False) -> GatewaySession:
        now = time.monotonic()
        if not force and self._cached_session and now - self._cached_at < self.settings.cache_session_seconds:
            return self._cached_session

        info = await self.session_info()
        vm_id = info.get("vm_id")
        endpoint = info.get("endpoint_url")
        state = str(info.get("vm_state") or "")

        if not vm_id or not endpoint:
            raise MuseUpstreamError(
                f"muse.ai did not assign a VM (status={info.get('status')!r}, "
                f"issue={info.get('vm_resolution_issue')!r})"
            )

        if state.upper() != "RUNNING":
            log.info("waking VM %s (state=%s)", vm_id, state or "unknown")
            await self._wake_vm(vm_id)
            info = await self._wait_running(vm_id, time.monotonic() + 45.0)
            vm_id = info.get("vm_id", vm_id)
            endpoint = info.get("endpoint_url", endpoint)
            # Settle window after waking so the VM's noise listener binds before upgrade
            await asyncio.sleep(1.2 + random.uniform(0.1, 0.4))

        try:
            token_resp = await self.request_json(
                "POST",
                "/api/hatch/token",
                json_body={"vmAddress": endpoint, "vmName": vm_id},
            )
        except MuseAuthError:
            # A stale hatch_vml cookie is the usual cause; refresh once.
            await self.session_info()
            token_resp = await self.request_json(
                "POST",
                "/api/hatch/token",
                json_body={"vmAddress": endpoint, "vmName": vm_id},
            )
        auth_token = token_resp.get("token")
        if not isinstance(auth_token, str) or not auth_token.strip():
            raise MuseUpstreamError("hatch/token response is missing 'token'")

        notary: str | None = None
        try:
            notary_resp = await self.request_json(
                "POST",
                "/api/hatch/noise-notary-token",
                json_body={"vmName": vm_id},
            )
            raw_notary = notary_resp.get("notaryToken")
            if isinstance(raw_notary, str) and raw_notary.strip():
                notary = raw_notary.strip()
        except Exception as exc:  # noqa: BLE001 - notary is optional for some accounts
            log.warning("noise notary token unavailable (%s); continuing without it", exc)

        session = GatewaySession(
            vm_id=vm_id,
            gateway_url=endpoint,
            auth_token=auth_token,
            notary_token=notary,
        )
        self._cached_session = session
        self._cached_at = time.monotonic()
        self._persist_session(session)
        log.info("gateway session ready: vm=%s", vm_id)
        return session

    def _persist_session(self, session: GatewaySession) -> None:
        try:
            self.settings.ensure_state_dir()
            (self.settings.state_dir / "gateway.json").write_text(
                json.dumps(
                    {
                        "vm_id": session.vm_id,
                        "gateway_url": session.gateway_url,
                        "saved_at": int(time.time()),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            self._persist_cookies(force=True)
        except Exception:  # pragma: no cover - best effort
            pass

    async def connect(self) -> tuple[GatewaySession, HatchConnection]:
        from .gateway import HatchConnection

        session = await self.bootstrap()
        try:
            conn = await HatchConnection.connect(
                session,
                ws_client=self.client,
                impersonate=self.settings.impersonate,
                proxy=self.proxy or self.settings.proxy,
                ws_fallback=self.settings.ws_fallback,
            )
        except Exception:
            # Tokens are short-lived; force one re-bootstrap before surfacing.
            session = await self.bootstrap(force=True)
            conn = await HatchConnection.connect(
                session,
                ws_client=self.client,
                impersonate=self.settings.impersonate,
                proxy=self.proxy or self.settings.proxy,
                ws_fallback=self.settings.ws_fallback,
            )
        return session, conn
