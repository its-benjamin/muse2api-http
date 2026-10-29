"""Proxy pool management and fitness registry for muse2api.

- Multiple rotation strategies: smart (fitness-aware), round-robin, random, fill-first.
- Health tracking / fitness registry: marks failing proxies unfit with a cooldown.
- Per-account proxy assignment: pins each account to a proxy to prevent IP correlation.
- Fail-open fallback: if all proxies are marked unfit, falls back to the pool.
"""
from __future__ import annotations

import hashlib
import logging
import os
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Settings

log = logging.getLogger("muse2api.proxy")

DEFAULT_UNFIT_SECONDS = 300.0


def normalize_proxy_url(url: str | None) -> str | None:
    """Normalize a proxy URL string or return None if empty/invalid."""
    if not url:
        return None
    cleaned = str(url).strip()
    if not cleaned:
        return None
    # Strip trailing slashes
    cleaned = cleaned.rstrip("/")
    if not (cleaned.startswith("http://") or cleaned.startswith("https://") or cleaned.startswith("socks5://") or cleaned.startswith("socks5h://")):
        cleaned = f"http://{cleaned}"
    return cleaned


def proxy_hash(url: str | None) -> str:
    """Stable fingerprint for a proxy URL (non-cryptographic short hash)."""
    if not url:
        return "direct"
    digest = hashlib.sha256(url.strip().encode()).hexdigest()[:8]
    return f"proxy-{digest}"


@dataclass
class ProxyFitnessEntry:
    url: str
    unfit_until: float = 0.0
    reason: str = ""
    uses: int = 0
    failures: int = 0

    @property
    def is_fit(self) -> bool:
        return time.monotonic() >= self.unfit_until


class ProxyPool:
    """Pool of egress proxies with health checks, rotation, and per-account pinning."""

    def __init__(
        self,
        proxies: list[str] | None = None,
        *,
        strategy: str = "smart",
        cooldown_seconds: float = DEFAULT_UNFIT_SECONDS,
    ) -> None:
        self.strategy = strategy.lower().strip() if strategy else "smart"
        self.cooldown_seconds = cooldown_seconds
        self._entries: dict[str, ProxyFitnessEntry] = {}
        self._order: list[str] = []
        self._index: int = 0
        self._account_map: dict[str, str] = {}

        for raw in proxies or []:
            norm = normalize_proxy_url(raw)
            if norm and norm not in self._entries:
                self._entries[norm] = ProxyFitnessEntry(url=norm)
                self._order.append(norm)

    @classmethod
    def from_settings(cls, settings: Settings) -> ProxyPool:
        """Construct a ProxyPool from configuration and environment variables."""
        collected: list[str] = []

        # 1. From proxy file if specified
        proxy_file = getattr(settings, "proxy_file", None) or os.environ.get("MUSE_PROXY_FILE")
        if proxy_file:
            path = Path(proxy_file)
            if path.is_file():
                try:
                    for line in path.read_text(encoding="utf-8").splitlines():
                        line = line.strip()
                        if line and not line.startswith("#"):
                            norm = normalize_proxy_url(line)
                            if norm:
                                collected.append(norm)
                except OSError as exc:
                    log.warning("could not read proxy file %s: %s", path, exc)

        # 2. From configured list or MUSE_PROXIES env var
        raw_list = getattr(settings, "proxies", None) or []
        if not raw_list and os.environ.get("MUSE_PROXIES"):
            raw_list = [p.strip() for p in os.environ.get("MUSE_PROXIES", "").replace(";", ",").split(",") if p.strip()]
        for p in raw_list:
            norm = normalize_proxy_url(p)
            if norm:
                collected.append(norm)

        # 3. Fallback to single legacy MUSE_PROXY if no list provided
        if not collected and settings.proxy:
            norm = normalize_proxy_url(settings.proxy)
            if norm:
                collected.append(norm)

        strategy = getattr(settings, "proxy_strategy", None) or os.environ.get("MUSE_PROXY_STRATEGY", "smart")
        cooldown = getattr(settings, "proxy_cooldown_seconds", None) or float(os.environ.get("MUSE_PROXY_COOLDOWN", str(DEFAULT_UNFIT_SECONDS)))

        return cls(collected, strategy=strategy, cooldown_seconds=cooldown)

    @property
    def is_empty(self) -> bool:
        return len(self._order) == 0

    def __len__(self) -> int:
        return len(self._order)

    def is_fit(self, proxy_url: str | None) -> bool:
        if not proxy_url:
            return True
        norm = normalize_proxy_url(proxy_url)
        entry = self._entries.get(norm)
        if entry is None:
            return True
        return entry.is_fit

    def mark_unfit(self, proxy_url: str | None, reason: str = "", cooldown: float | None = None) -> None:
        if not proxy_url:
            return
        norm = normalize_proxy_url(proxy_url)
        entry = self._entries.get(norm)
        if entry is None:
            entry = ProxyFitnessEntry(url=norm)
            self._entries[norm] = entry
            self._order.append(norm)

        cd = cooldown if cooldown is not None else self.cooldown_seconds
        entry.unfit_until = time.monotonic() + cd
        entry.reason = reason
        entry.failures += 1
        log.warning("proxy %s marked unfit for %.0fs (reason: %s)", norm, cd, reason or "unspecified")

    def mark_fit(self, proxy_url: str | None) -> None:
        if not proxy_url:
            return
        norm = normalize_proxy_url(proxy_url)
        entry = self._entries.get(norm)
        if entry is not None:
            entry.unfit_until = 0.0
            entry.reason = ""

    def get_fit_proxies(self) -> list[str]:
        return [url for url in self._order if self._entries[url].is_fit]

    def next_proxy(self, *, exclude: set[str] | None = None) -> str | None:
        """Pick a proxy using the configured rotation strategy and fitness state."""
        if not self._order:
            return None

        exclude_set = exclude or set()
        eligible = [url for url in self._order if url not in exclude_set]
        if not eligible:
            eligible = list(self._order)

        # Smart strategy filters for fit proxies first
        if self.strategy == "smart":
            fit = [url for url in eligible if self._entries[url].is_fit]
            if fit:
                eligible = fit
            else:
                # Fail-open: all eligible are unfit, reset the earliest expiring one
                earliest = min(eligible, key=lambda u: self._entries[u].unfit_until)
                self._entries[earliest].unfit_until = 0.0
                eligible = [earliest]

        if self.strategy == "fill-first":
            chosen = eligible[0]
        elif self.strategy == "random":
            chosen = random.choice(eligible)
        else:
            # Round-robin (default for smart & round-robin)
            idx = self._index % len(eligible)
            chosen = eligible[idx]
            self._index = (self._index + 1) % max(1, len(self._order))

        self._entries[chosen].uses += 1
        return chosen

    def get_for_account(self, account_name: str) -> str | None:
        """Assign or retrieve a pinned proxy for an account to prevent IP correlation."""
        if not self._order:
            return None

        current = self._account_map.get(account_name)
        # If already mapped and still fit, keep it sticky
        if current and self.is_fit(current):
            return current

        # Otherwise pick the next fit proxy and pin it to this account
        chosen = self.next_proxy()
        if chosen:
            self._account_map[account_name] = chosen
        return chosen

    def status(self) -> list[dict]:
        """Inspection snapshot for health/admin endpoints."""
        now = time.monotonic()
        return [
            {
                "url": entry.url,
                "fit": entry.is_fit,
                "unfit_remaining_s": max(0, int(entry.unfit_until - now)) if not entry.is_fit else 0,
                "reason": entry.reason,
                "uses": entry.uses,
                "failures": entry.failures,
            }
            for entry in self._entries.values()
        ]
