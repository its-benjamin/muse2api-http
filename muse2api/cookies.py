"""Cookie loading and a small in-memory jar.

Accepts the three formats users actually have:

* Netscape ``cookies.txt`` (curl / browser extension export)
* a JSON object ``{"hatch_sess": "...", ...}`` or ``{"cookies": {...}}``
* a raw ``Cookie:`` header (``a=b; c=d``)
"""

from __future__ import annotations

import json
import time
from pathlib import Path


class CookieError(ValueError):
    pass


def parse_cookie_file(path: str | Path) -> dict[str, str]:
    raw = Path(path).read_text(encoding="utf-8", errors="replace").strip()
    return parse_cookies(raw)


def parse_cookies(raw: str) -> dict[str, str]:
    raw = raw.strip()
    if not raw:
        raise CookieError("cookie input is empty")
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CookieError(f"invalid cookie JSON: {exc}") from None
        if isinstance(data, dict) and isinstance(data.get("cookies"), dict):
            data = data["cookies"]
        if isinstance(data, dict):
            out = {str(k): str(v) for k, v in data.items() if isinstance(v, str)}
            if out:
                return out
        raise CookieError("cookie JSON has no string values")

    cookies: dict[str, str] = {}
    # Netscape jar: seven tab-separated columns, name in 5 and value in 6.
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        cols = line.split("\t")
        if len(cols) >= 7:
            name, value = cols[5].strip(), cols[6].strip()
            if not name:
                continue
            expires = cols[4].strip()
            if expires.isdigit() and int(expires) and int(expires) < time.time():
                continue
            cookies[name] = value
    if cookies:
        return cookies

    # Raw header.
    for part in raw.split(";"):
        part = part.strip()
        if not part:
            continue
        name, sep, value = part.partition("=")
        if sep and name.strip():
            cookies[name.strip()] = value.strip()
    if not cookies:
        raise CookieError("could not parse cookies (no Netscape columns or key=value pairs)")
    return cookies


ESSENTIAL_COOKIES = ("hatch_sess", "hatch_vml", "hatch_native_auth_device")


class CookieJar:
    def __init__(self, cookies: dict[str, str]) -> None:
        self._cookies = dict(cookies)
        self._extra: dict[str, str] = {}

    def header(self) -> str:
        merged = {**self._cookies, **self._extra}
        return "; ".join(f"{k}={v}" for k, v in merged.items())

    def update_from_set_cookie(self, values: list[str]) -> None:
        for raw in values:
            first, _, attrs = raw.partition(";")
            name, sep, value = first.partition("=")
            if not sep:
                continue
            name, value = name.strip(), value.strip()
            # Ignore deletion cookies (Max-Age=0) so we keep the last live value.
            attrs_l = attrs.lower()
            if "max-age=0" in attrs_l:
                if name in self._cookies:
                    self._extra.pop(name, None)
                    self._cookies.pop(name, None)
                continue
            self._cookies[name] = value
            self._extra.pop(name, None)

    def merge(self, values: dict[str, str]) -> None:
        """Overlay ``values`` onto the jar (later values win)."""
        for name, value in values.items():
            if name and value:
                self._cookies[name] = value

    def get(self, name: str) -> str | None:
        return self._cookies.get(name)

    def __contains__(self, name: str) -> bool:
        return name in self._cookies

    def copy(self) -> dict[str, str]:
        return dict(self._cookies)


def ensure_cookie_file(path: str | Path, is_interactive: bool | None = None) -> bool:
    """Prompt for cookies interactively if ``path`` is missing or empty.

    Returns ``True`` if a valid cookie file exists or was created, ``False`` otherwise.
    """
    import sys

    cookie_path = Path(path)
    if cookie_path.is_file():
        try:
            jar = parse_cookie_file(cookie_path)
            if "hatch_sess" in jar:
                return True
        except (OSError, CookieError):
            pass

    if is_interactive is None:
        is_interactive = sys.stdin.isatty() if hasattr(sys, "stdin") else False

    if not is_interactive:
        return False

    print("=" * 60)
    print("muse2api: cookies file not found")
    print(f"Target file: {cookie_path.resolve()}")
    print("=" * 60)
    print("To connect to muse.ai, cookies from a logged-in session are required:")
    print("  1. Log in to https://muse.ai in your browser.")
    print("  2. Copy the 'hatch_sess' cookie value from DevTools:")
    print("     F12 -> Application -> Cookies -> https://muse.ai -> copy 'hatch_sess'")
    print("     (Or export cookies.txt using a browser extension).")
    print()
    try:
        val = input("Paste your 'hatch_sess' cookie value or full Cookie header:\n> ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        return False

    if not val:
        print("No cookie provided.")
        return False

    # Format the cookie
    if "=" not in val and not val.startswith("{"):
        cookie_text = f"hatch_sess={val}\n"
    else:
        cookie_text = val if val.endswith("\n") else val + "\n"

    try:
        parse_cookies(cookie_text)
    except CookieError as exc:
        print(f"[!] Warning: cookie format warning: {exc}")

    try:
        cookie_path.parent.mkdir(parents=True, exist_ok=True)
        cookie_path.write_text(cookie_text, encoding="utf-8")
        print(f"[+] Saved cookies to {cookie_path.name}!")
        print("=" * 60)
        return True
    except OSError as exc:
        print(f"[!] Could not write {cookie_path}: {exc}")
        return False
