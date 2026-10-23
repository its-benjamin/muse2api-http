# muse2api (HTTP, no browser)

A standalone, OpenAI-compatible API for [muse.ai](https://muse.ai) that talks to
the site's own backend directly: HTTPS for bootstrap, a Noise XX WebSocket for
the Hatch gateway, and protobuf-framed RPC for chat. No Playwright, no CDP, no
headless browser at runtime.

Reverse engineering was done with [REA](https://github.com/morluto/rea) against
a captured HAR and the public Next.js bundle. `rea/` is a local working copy
(git-ignored); the extracted web sources live in `capture/` (also ignored).

## What works

| Surface | Behaviour |
|---|---|
| `POST /v1/chat/completions` | Streaming and non-streaming text chat, multi-turn per API session, tools/vision message parts normalized |
| `POST /v1/responses` | Codex-style Responses API adapter over the same chat backend |
| `POST /v1/images/generations` | Text-to-image; returns a hosted media URL (and `b64_json` when requested) |
| `POST /v1/images/edits` | Image edit / image-to-image with reference images |
| `POST /v1/videos` | Text-to-video and first-frame image-to-video, async task id |
| `GET /v1/videos/{id}` | Poll video status; completed tasks return the media URL |
| `GET /v1/media/{name}`, `/media/{name}` | Local cache for generated media |
| `GET /healthz` | Per-account health, VM id, uptime |
| `POST /v1/session/refresh` / `reset` | Refresh the upstream session; forget local side-chat state |

## How it works

1. **Bootstrap** (`http.py`): cookies → authenticated `muse.ai` requests. The
   VM identity and admission/notary tokens come from the same HTTP endpoints the
   web app calls (`/api/session`, `/api/hatch/vm/wake`, `/api/auth/check`, ...).
2. **Noise XX** (`noise.py`): `Noise_XX_25519_AESGCM_SHA256` initiator over
   `wss://<lb>/v1/noise?vm_id=...&auth_token=...&app_id=hatch-web`.
3. **Wire codec** (`wire.py`): protobuf shapes for `NoiseTransportFrame`,
   `ServiceRequest/Response`, `ServiceFrame` and application requests, including
   chunked request/response bodies.
4. **Chat** (`chat.py`): POST `/chat/stream`, then read the reply from
   `/chat/history`. Thread mode keeps one server-side muse side-chat per API
   session. `main` mode subscribes to `/chat/subscribe` deltas instead.

## Quick start

```bash
python -m pip install -r requirements.txt
export MUSE_COOKIE_FILE=cookies_muse.txt     # Netscape cookie export
python -m muse2api ping
python -m muse2api chat "hello"
python -m muse2api serve                     # http://127.0.0.1:18610
```

With the OpenAI SDK:

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:18610/v1", api_key="unused")
print(client.chat.completions.create(
    model="muse-spark",
    messages=[{"role": "user", "content": "hello"}],
).choices[0].message.content)
```

## Accounts

`MUSE_COOKIE_FILES=a.txt,b.txt` (or repeated `MUSE_COOKIE_FILE`) enables the
account pool. A failing login is put on a cooldown and the next healthy account
is used; `/healthz` shows each account. A single file is the normal setup.

## Protocol discovery

The web bundle is the source of truth. `muse2api/protocol.json` (git-ignored)
overrides the built-in defaults and is loaded at startup:

```bash
python -m muse2api discover              # fetch bundle + source maps from muse.ai
python -m muse2api discover --dry-run    # print without writing
python -m muse2api discover --offline capture/maps
```

It extracts `HATCH_SHARED_LB_HOST`, `NOISE_WS_PATH`, `HATCH_APP_ID` and the
daemon HTTP route table (82 routes in the current bundle). Environment overrides
always win: `MUSE_LB_HOST`, `MUSE_WS_PATH`, `MUSE_APP_ID`.

## Confidential VMs

Standard VMs send an attestation blob in Noise message 2; the client replies
with an empty message 3. Confidential VMs send an RV challenge instead and
require a notarised message 3 (fresh RV key or an HMAC proof derived from the
recovery key).

This implementation follows the reference policy: **fail closed**. A CVM
challenge without credentials is an error, never a silent downgrade. To use a
CVM account, set:

- `MUSE_CVM_RV_KEY`: 32-byte recovery key, hex or base64.
- `MUSE_ATTESTATION_CMD`: an external verifier. It receives one JSON object on
  stdin (`attestation`, `server_static_key`, `handshake_hash`, `client_nonce`,
  `chip_binding`, all base64) and must print
  `{"verdict": "accepted" | "accepted_chip_pinned" | "rejected", ...}`.

This is the same EJS-style seam yt-dlp uses: the site's own JS (or a native
verifier) does the cryptography, and the Python client only consumes its verdict.
The RV proof/HMAC path is implemented and unit-tested; attestation acceptance is
intentionally delegated because it needs AMD certificate chains and the site's
verifier trust anchors. Standard-VM accounts never enter this path.

## Request fingerprint

Stealth here means looking like the browser that owns the cookies, not adding
random headers:

- `curl_cffi` with `impersonate="chrome"` — the docs recommend the latest
  Chrome/Safari alias — reproduces Chrome's TLS, HTTP/2 and HTTP/3
  fingerprints. In curl_cffi 0.16.3 the alias resolves to Chrome 150, which
  includes an HTTP/3 fingerprint. `MUSE_HTTP_VERSION=auto` lets the profile
  negotiate h3; measured: HTTP/3 against muse.ai.
- The UA comes from the impersonation profile for HTTP and the WebSocket
  handshake, so both transports stay consistent. `MUSE_USER_AGENT` overrides it
  if an account is bound to a specific browser.
- Requests carry the captured Firefox XHR headers (`Accept: */*`,
  `Accept-Language`, `Origin`, `Referer`, `Sec-Fetch-*`, `Priority`,
  `Pragma`/`Cache-Control`, `TE: trailers`). Host, `Connection` and
  `Accept-Encoding` are left to the fingerprint profile.
- Cookies live in the curl session jar, not in a hand-built `Cookie` header, so
  rotated `Set-Cookie` values are kept automatically.
- The WebSocket upgrade goes through the same `curl_cffi` session, so the
  handshake carries the Firefox TLS/HTTP fingerprint and the exact captured
  header set (`Sec-Fetch-Mode: websocket`, `Sec-Fetch-Site: cross-site`,
  `Pragma`/`Cache-Control`). If that path ever fails, it falls back to the
  stdlib `websockets` client instead of dropping the request.
- The UA is whatever the impersonation profile sets, on both HTTP and the
  WebSocket upgrade; `MUSE_USER_AGENT` overrides it when an account is
  UA-bound. The `chrome` alias tracks curl_cffi's latest bundled Chrome, which
  also ships the HTTP/3 fingerprint.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `MUSE_COOKIE_FILE` | `cookies_muse.txt` | Netscape cookie jar |
| `MUSE_COOKIE_FILES` | — | Comma-separated jars for the pool |
| `MUSE2API_HOST` / `MUSE2API_PORT` | `127.0.0.1` / `18610` | Listen address |
| `MUSE2API_KEY` | unset | Optional bearer key required by the server |
| `MUSE_MODEL` | `muse-spark` | Advertised model id |
| `MUSE_SESSION_MODE` | `thread` | `thread` or `main` |
| `MUSE_CHAT_TIMEOUT` | `300` | Reply deadline, seconds |
| `MUSE_SESSION_CACHE` | `45` | Bootstrap cache, seconds |
| `MUSE_STATE_DIR` | `.muse-state` | Local session/media state |
| `MUSE_LB_HOST` / `MUSE_WS_PATH` / `MUSE_APP_ID` | discovered | Gateway overrides |
| `MUSE_CVM_RV_KEY` | unset | CVM recovery key |
| `MUSE_ATTESTATION_CMD` | unset | External attestation verifier |
| `MUSE_IMPERSONATE` | `chrome` | curl_cffi profile alias; pin a version if needed |
| `MUSE_HTTP_VERSION` | `auto` | `auto`, `2`, `3`, `1.1` |
| `MUSE_USER_AGENT` | profile | Optional UA override |

## Tests

```bash
python -m pytest tests -q
```

Codec, Noise, CVM proof construction and protocol-discovery parsing are covered
by unit tests. The live smoke used during development:

```bash
python -m muse2api ping
curl -s localhost:18610/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"muse-spark","messages":[{"role":"user","content":"hello"}]}'
```

## Security notes

Cookies are bearer credentials. `cookies*.txt`, `.muse-state/` and captured HAR
or web-source artifacts are git-ignored. Generated media is cached under
`.muse-state/media` and served only from that directory.
