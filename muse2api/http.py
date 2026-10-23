"""muse.ai HTTP bootstrap: cookies -> session -> VM -> admission/notary tokens.

Uses ``curl_cffi`` with Chrome impersonation because muse.ai sits behind Meta's
edge proxies that reject some stock TLS fingerprints on authenticated routes.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from pathlib import Path

from curl_cffi.requests import AsyncSession

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
BASE_HEADERS = {
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
    "TE": "trailers",
}


def _parse_http_version(value: str):
    from curl_cffi.const import CurlHttpVersion

    return {
        "1.1": CurlHttpVersion.V1_1,
        "2": CurlHttpVersion.V2_0,
        "3": CurlHttpVersion.V3,
        "auto": None,
    }.get(value, None)


class MuseAuthError(RuntimeError):
    pass


class MuseUpstreamError(RuntimeError):
    pass


class MuseHTTP:
    def __init__(self, settings: Settings, cookies: dict[str, str] | None = None) -> None:
        self.settings = settings
        self.jar = CookieJar(cookies or parse_cookie_file(settings.cookies_file))
        missing = [name for name in ("hatch_sess",) if name not in self.jar]
        if missing:
            raise CookieError(
                "missing hatch_sess cookie; export fresh cookies from a logged-in muse.ai tab"
            )
        self._http_version = _parse_http_version(settings.http_version)
        self._client: AsyncSession | None = self._create_client()
        self._cached_session: GatewaySession | None = None
        self._cached_at = 0.0

    def _create_client(self) -> AsyncSession:
        return AsyncSession(
            impersonate=self.settings.impersonate,
            timeout=self.settings.request_timeout,
            http_version=self._http_version,
            max_clients=50,
            retry=2,
            cookies=self.jar.copy(),
        )

    @property
    def client(self) -> AsyncSession:
        if self._client is None:
            self._client = self._create_client()
        return self._client

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
                    self.jar._cookies[cookie.name] = cookie.value
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
        headers = dict(BASE_HEADERS)
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
        except Exception as exc:  # noqa: BLE001 - network layer
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
        except Exception as exc:  # noqa: BLE001
            raise MuseUpstreamError(
                f"{method.upper()} {path} returned non-JSON: {response.text[:200]!r}"
            ) from exc
        if not isinstance(data, dict):
            raise MuseUpstreamError(f"{method.upper()} {path} returned {type(data).__name__}")
        return data

    # -- bootstrap ---------------------------------------------------------

    async def session_info(self) -> dict:
        return await self.request_json("GET", "/api/session", params={"vm_state": "live"})

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
                await asyncio.sleep(1.5)
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
            await asyncio.sleep(1.5)
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
            import json

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
            (self.settings.state_dir / "cookies.json").write_text(
                __import__("json").dumps(self.jar.copy(), indent=2),
                encoding="utf-8",
            )
        except Exception:  # pragma: no cover - best effort
            pass

    async def connect(self) -> tuple[GatewaySession, "HatchConnection"]:
        from .gateway import HatchConnection

        session = await self.bootstrap()
        try:
            conn = await HatchConnection.connect(
                session, ws_client=self.client, impersonate=self.settings.impersonate)
        except Exception:
            # Tokens are short-lived; force one re-bootstrap before surfacing.
            session = await self.bootstrap(force=True)
            conn = await HatchConnection.connect(
                session, ws_client=self.client, impersonate=self.settings.impersonate)
        return session, conn
