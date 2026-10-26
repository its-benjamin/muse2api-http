"""Discover the current Hatch gateway protocol from the public web bundle.

This is the maintenance path for server-side renames: the muse.ai Next.js
bundle (and the source maps it publishes) contains the live gateway host,
WebSocket path, app id and the daemon HTTP route table. The result is written
to ``muse2api/protocol.json`` and loaded at startup.

Usage::

    python -m muse2api discover
    python -m muse2api discover --offline capture/maps
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path

from .protocol import DEFAULT_APP_ID, DEFAULT_LB_HOST, DEFAULT_ROUTES, DEFAULT_WS_PATH

log = logging.getLogger("muse2api.discover")

CHUNK_RE = re.compile(r'(?:/_next/)?static/chunks/[A-Za-z0-9_\-./~]+?\.js')
MAP_RE = re.compile(r'sourceMappingURL=([A-Za-z0-9_\-./~]+?\.map)')
ROUTE_METHOD_RE = re.compile(r'method:"([a-z0-9_.\-]+)"', re.IGNORECASE)
ROUTE_FIELD_RE = {
    key: re.compile(rf'{key}:"([^"]*)"')
    for key in ("httpMethod", "path", "service", "streamMode")
}


def _get(url: str, timeout: float = 30.0) -> bytes:
    from curl_cffi import requests as curl_requests

    response = curl_requests.get(url, impersonate="firefox", timeout=timeout)
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code} for {url}")
    return response.content


def _flatten_map(data: dict) -> list[str]:
    sources: list[str] = []
    sections = data.get("sections")
    if sections:
        for section in sections:
            sources.extend(_flatten_map(section.get("map") or {}))
        return sources
    for content in data.get("sourcesContent") or []:
        if content:
            sources.append(content)
    return sources


def _extract_routes(text: str) -> dict[str, dict]:
    routes: dict[str, dict] = {}
    for match in ROUTE_METHOD_RE.finditer(text):
        method = match.group(1)
        if method not in DEFAULT_ROUTES and "." not in method:
            continue
        window = text[match.end():match.end() + 600]
        spec: dict[str, str] = {}
        for key, pattern in ROUTE_FIELD_RE.items():
            found = pattern.search(window)
            if found:
                spec[key] = found.group(1)
        if spec.get("path"):
            routes[method] = spec
    return routes


def _extract_constant(text: str, name: str) -> str | None:
    for pattern in (rf'{name}\s*[:=]\s*"([^"]+)"', rf'"{name}"\s*:\s*"([^"]+)"'):
        found = re.search(pattern, text)
        if found:
            return found.group(1)
    return None


def _cache_fresh(path: Path, hours: float) -> bool:
    if hours <= 0 or not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        generated = datetime.fromisoformat(str(data.get("generated_at")))
        if generated.tzinfo is None:
            generated = generated.replace(tzinfo=UTC)
        return (datetime.now(UTC) - generated).total_seconds() < hours * 3600
    except (OSError, ValueError, TypeError):
        return False


def _fetch_js(url: str) -> tuple[str, str | None]:
    try:
        return url, _get(url).decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 - best effort discovery
        log.debug("chunk fetch failed %s: %s", url, exc)
        return url, None


def discover(*, offline: Path | None = None, write: bool = True, force: bool = False,
             cache_hours: float = 24.0, include_maps: bool = False,
             workers: int = 12) -> dict:
    from concurrent.futures import ThreadPoolExecutor
    from urllib.parse import urljoin

    from .protocol import CONFIG_PATH

    if offline is None and write and not force and _cache_fresh(CONFIG_PATH, cache_hours):
        cached = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        cached["cached"] = True
        log.info("using cached protocol from %s", CONFIG_PATH)
        return cached

    raw_text = ""
    map_sources: list[str] = []

    if offline is not None:
        offline = Path(offline)
        for path in sorted(offline.glob("*.js.map")):
            try:
                map_sources.extend(_flatten_map(json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, ValueError) as exc:
                log.debug("skipping %s: %s", path, exc)
    else:
        html = _get("https://muse.ai/").decode("utf-8", "replace")
        queue = sorted(set(CHUNK_RE.findall(html)))
        seen: set[str] = set()
        chunk_urls: list[str] = []
        while queue and len(seen) < 300:
            batch: list[str] = []
            for chunk in queue:
                chunk = "/_next" + chunk if chunk.startswith("/static/") else chunk
                url = urljoin("https://muse.ai/", chunk)
                if url not in seen:
                    seen.add(url)
                    batch.append(url)
            queue = []
            if not batch:
                break
            with ThreadPoolExecutor(max_workers=min(workers, len(batch))) as pool:
                for url, js in pool.map(_fetch_js, batch):
                    if js is None:
                        continue
                    raw_text += js
                    chunk_urls.append(url)
                    for reference in CHUNK_RE.findall(js):
                        reference = "/_next" + reference if reference.startswith("/static/") else reference
                        if urljoin("https://muse.ai/", reference) not in seen:
                            queue.append(reference)
            log.info("scanned %d chunks (%d queued)", len(seen), len(queue))
        log.info("fetched %d script chunks", len(chunk_urls))

        if include_maps:
            map_urls = {urljoin(url, name) for url in chunk_urls
                        for name in set(MAP_RE.findall(raw_text))}
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for map_url, content in pool.map(
                        lambda url: (url, _fetch_js(url)[1]), sorted(map_urls)):
                    if content is None:
                        continue
                    try:
                        map_sources.extend(_flatten_map(json.loads(content)))
                    except ValueError:
                        log.debug("bad source map %s", map_url)

    corpus = raw_text + "\n" + "\n".join(map_sources)
    routes = {**DEFAULT_ROUTES, **_extract_routes(corpus)}
    result = {
        "lb_host": _extract_constant(corpus, "HATCH_SHARED_LB_HOST") or DEFAULT_LB_HOST,
        "ws_path": _extract_constant(corpus, "NOISE_WS_PATH") or DEFAULT_WS_PATH,
        "app_id": _extract_constant(corpus, "HATCH_APP_ID") or DEFAULT_APP_ID,
        "routes": routes,
        "generated_at": datetime.now(UTC).isoformat(),
        "source": f"offline:{offline}" if offline is not None else "https://muse.ai bundle",
    }
    if include_maps and not map_sources:
        log.warning("no source maps parsed; route news may be incomplete")

    if write:
        CONFIG_PATH.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        log.info("wrote %s", CONFIG_PATH)
    return result


def summarize(result: dict) -> str:
    changed = []
    for method, spec in result["routes"].items():
        default = DEFAULT_ROUTES.get(method)
        if default and default.get("path") != spec.get("path"):
            changed.append(f"{method}: {default.get('path')} -> {spec.get('path')}")
    lines = [
        f"lb_host: {result['lb_host']}",
        f"ws_path: {result['ws_path']}",
        f"app_id: {result['app_id']}",
        f"routes: {len(result['routes'])}",
    ]
    lines.extend(changed or ["no route changes vs built-in defaults"])
    return "\n".join(lines)
