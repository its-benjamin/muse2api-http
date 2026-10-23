from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(slots=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 18610
    api_key: str | None = None
    model: str = "muse-spark"
    timezone: str = "UTC"
    cookies_file: str = "cookies_muse.txt"
    state_dir: Path = field(default_factory=lambda: Path(".muse-state"))
    # "thread" keeps one muse side-chat per API session (context for free).
    # "main" uses the account's main chat via /chat/subscribe.
    session_mode: str = "thread"
    chat_timeout: float = 300.0
    request_timeout: float = 60.0
    listen_ms: int = 90_000
    cache_session_seconds: float = 45.0
    log_level: str = "info"
    # Optional confidential-VM credentials. Standard VMs ignore both.
    cvm_rv_key: str | None = None
    attestation_cmd: str | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        state_dir = Path(os.environ.get("MUSE_STATE_DIR", ".muse-state"))
        return cls(
            host=os.environ.get("MUSE2API_HOST", "127.0.0.1"),
            port=int(os.environ.get("MUSE2API_PORT", "18610")),
            api_key=os.environ.get("MUSE2API_KEY") or None,
            model=os.environ.get("MUSE_MODEL", "muse-spark"),
            timezone=os.environ.get("MUSE_TIMEZONE", "UTC"),
            cookies_file=os.environ.get("MUSE_COOKIE_FILE", "cookies_muse.txt"),
            state_dir=state_dir,
            session_mode=os.environ.get("MUSE_SESSION_MODE", "thread").strip().lower(),
            chat_timeout=float(os.environ.get("MUSE_CHAT_TIMEOUT", "300")),
            request_timeout=float(os.environ.get("MUSE_REQUEST_TIMEOUT", "60")),
            listen_ms=int(os.environ.get("MUSE_LISTEN_MS", "90000")),
            cache_session_seconds=float(os.environ.get("MUSE_SESSION_CACHE", "45")),
            log_level=os.environ.get("MUSE_LOG_LEVEL", os.environ.get("LOG_LEVEL", "info")),
            cvm_rv_key=os.environ.get("MUSE_CVM_RV_KEY") or None,
            attestation_cmd=os.environ.get("MUSE_ATTESTATION_CMD") or None,
        )

    def ensure_state_dir(self) -> Path:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        return self.state_dir
