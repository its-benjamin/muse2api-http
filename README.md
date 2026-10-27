# muse2api (HTTP, no browser)

A standalone, OpenAI-compatible API for [muse.ai](https://muse.ai) that talks to
the site's own backend directly: HTTPS for bootstrap, a Noise XX WebSocket for
the Hatch gateway, and protobuf-framed RPC for chat.

**No headless browser is needed.** There is no Playwright, no CDP, no Chromium
download, and no browser process at runtime: the client reproduces the web
client's HTTP/WebSocket protocol and TLS fingerprint with `curl_cffi`, so the
whole service runs in a small Python container. Cookies are the only thing you
supply.

The protocol layer is derived from the public Next.js bundle. `muse2api
discover` refreshes the route table when muse.ai ships a new build; the bundled
`muse2api/protocol.json` keeps the last known-good values so startup does not
depend on the network.

## Credits

This project builds on the protocol work in
[MuseAI-API](https://github.com/minhquan130599/MuseAI-API) by
[minhquan130599](https://github.com/minhquan130599): the Noise XX handshake,
the protobuf wire codec, and the Hatch bootstrap follow that implementation.
muse2api adds the HTTP server, the OpenAI-compatible surfaces, the cookie pool,
the media pipeline, and the test suite. If you want the original CLI / video
client instead of an API server, use MuseAI-API directly.

## What works

| Surface | Behaviour |
|---|---|
| `POST /v1/chat/completions` | Streaming/non-streaming chat with native image attachments, OpenAI `tools` bridging (auto / required / forced), and multi-turn isolation via `x-muse-session` |
| `POST /v1/responses` | Codex-style Responses API adapter; `function_call` output items and `function_call_arguments` streaming when tools are used |
| `POST /v1/images/generations` | Text-to-image; returns a hosted media URL and `b64_json` (reference image accepted as URL, data URI or base64) |
| `POST /v1/images/edits` | Image edit / image-to-image with reference images |
| `POST /v1/images/tasks` | Async image task; `Idempotency-Key` (or `idempotency_key`) returns the existing task instead of generating twice |
| `GET /v1/images/tasks/{id}` | Poll an async image task |
| `POST /v1/videos` | Text-to-video and first-frame image-to-video (`reference_image`, `image_url` or `image` as URL/data-URI/base64), async task id |
| `GET /v1/videos/{id}` | Poll video status; completed tasks return the media URL |
| `GET /v1/media/{name}`, `/media/{name}` | Local cache for generated media |
| `GET /v1/sessions` / `GET /v1/sessions/{id}/history` | Raw upstream session and history reads |
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

`/v1/models` lists `muse-spark` and the configured aliases, so any
OpenAI-compatible client works without extra adapters.

## Docker

Prebuilt images are published by `.github/workflows/docker.yml`:

- `ghcr.io/its-benjamin/muse2api-http:latest` — newest build of the default branch
- `ghcr.io/its-benjamin/muse2api-http:<branch>` — `main` / `master`
- `ghcr.io/its-benjamin/muse2api-http:sha-<commit>` and `v*` tags for releases

```bash
docker run -d --name muse2api-http -p 18610:18610 \
  -e MUSE_COOKIE_FILE=/cookies/cookies_muse.txt \
  -e MUSE_STATE_DIR=/state \
  -v "$PWD/cookies_muse.txt:/cookies/cookies_muse.txt:ro" \
  -v muse-state:/state \
  -v muse-media:/data/media \
  ghcr.io/its-benjamin/muse2api-http:latest
```

Or with Compose:

```yaml
services:
  muse2api-http:
    image: ghcr.io/its-benjamin/muse2api-http:latest
    container_name: muse2api-http
    restart: unless-stopped
    ports:
      - "18610:18610"
    environment:
      MUSE_COOKIE_FILE: /cookies/cookies_muse.txt
      MUSE_STATE_DIR: /state
      MUSE2API_KEY: change-me
    volumes:
      - ./cookies_muse.txt:/cookies/cookies_muse.txt:ro
      - muse-state:/state
      - muse-media:/data/media
volumes:
  muse-state:
  muse-media:
```

The container runs as uid 10001 and only `/state` and `/data/media` are
writable; mount those if you want tasks, rotated cookies and generated media to
survive a restart. The healthcheck polls `/healthz` every 30 s.

## Sessions and permissions

Each API conversation maps to one server-side muse side-chat. Send
`x-muse-session: <conversation-id>` to give every caller its own thread; without
it, clients share the `default` thread (and, when `MUSE2API_KEY` is set, the
bearer token cannot distinguish them).

Muse pauses agent work on permission prompts (network egress, connectors,
browser actions, purchases). The HTTP API has no UI to answer them, so a turn
that triggers one would wait until `MUSE_CHAT_TIMEOUT`. Set an explicit policy
to answer prompts that are scoped to the waiting thread:

```bash
MUSE2API_APPROVAL_POLICY=allow_once   # off | allow_once | allow_session | deny
```

- `off` (default): never answer; the upstream turn may time out.
- `allow_once`: approve the weakest one-shot option the server offers.
- `allow_session`: prefer a session grant, falling back to one-shot.
- `deny`: refuse, letting the assistant report the refusal.

Only approvals whose `scope.thread_id` (or `scope.session_scope_id`) matches the
current chat are touched (thread mode, the default). Account-wide HITL
settings, connector OAuth consent, and the `/api/consent/status` web flow are
**not** modified; connector setup still requires the browser. Auto-approval is
security-sensitive: enable it only for an account whose data and network access
you are willing to expose to the model.

## Media URLs

Generated URLs are relative by default. Set `MUSE2API_PUBLIC_BASE` (for example
`https://api.example.com/v1/media`) when clients cannot resolve `/v1/media/...`
against the API host. The `/v1/media/{name}` route itself stays the same.

## Accounts

`MUSE_COOKIE_FILES=a.txt,b.txt` (or repeated `MUSE_COOKIE_FILE`) enables the
account pool. A failing login is put on a cooldown and the next healthy account
is used; `/healthz` shows each account. A single file is the normal setup.

### Cookies stay alive on their own

muse.ai rotates its session cookies on use, so the export you start with is not
the cookie set the server ends up with. The bridge keeps the live jar instead of
re-reading the file on every restart:

- Rotated `Set-Cookie` values are merged into the jar after every request and
  written to `.muse-state/cookies-<hash>.json` (atomic replace, at most every
  30 s and immediately after `/api/session`).
- On startup the cached jar is used when it is newer than the exported file; a
  fresh export you drop in later always wins.
- If authentication fails (401/403), the account reloads the configured cookie
  file and retries once before it is cooled down — so replacing
  `cookies_muse.txt` is picked up without restarting the server.
- The keepalive loop calls `/api/session` every 6 hours (and the pool refreshes
  every 30 minutes while the server is active), refreshing the rolling cookies
  even when no client requests are coming in.

You only need a new export if the account was logged out or the VM was unused
for longer than the cookie lifetime (48 hours for the short-lived cookie) — the
cache keeps the last live values, but it cannot resurrect an expired login.

## Protocol discovery

The web bundle is the source of truth. `muse2api/protocol.json` ships the last
discovered route table and is loaded at startup; run discovery after a muse.ai
update to refresh it:

```bash
python -m muse2api discover              # fetch bundle + source maps from muse.ai
python -m muse2api discover --dry-run    # print without writing
python -m muse2api discover --offline <dir>
```

It extracts `HATCH_SHARED_LB_HOST`, `NOISE_WS_PATH`, `HATCH_APP_ID` and the
daemon HTTP route table. Environment overrides always win: `MUSE_LB_HOST`,
`MUSE_WS_PATH`, `MUSE_APP_ID`.

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

- `curl_cffi` with `impersonate="chrome"` reproduces the TLS (JA3/JA4) and
  HTTP/2 fingerprint of the Chrome build bundled with the installed
  `curl_cffi` (currently Chrome 150, so the UA and `sec-ch-ua` match it).
  Measured against a fingerprint echo: JA4 `t13d1516h2_8daaf6152771_806a8c22fdea`,
  the canonical Chrome value.
- HTTP/2 is pinned (`MUSE_HTTP_VERSION=2`). The profile's h2 settings,
  WINDOW_UPDATE and priority frames are exact; libcurl's HTTP/3 stack has
  different QUIC transport parameters from Chromium's, so h3 is opt-in only.
- The UA comes from the impersonation profile for HTTP and the WebSocket
  handshake, so both transports stay consistent. `MUSE_USER_AGENT` overrides it
  if an account is bound to a specific browser.
- Requests carry the browser XHR headers (`Accept: */*`, `Accept-Language`,
  `Origin`, `Referer`, `Sec-Fetch-*`, `Priority`, `Pragma`/`Cache-Control`,
  `TE: trailers`). Host, `Connection` and `Accept-Encoding` are left to the
  fingerprint profile.
- Cookies live in the curl session jar, not in a hand-built `Cookie` header, so
  rotated `Set-Cookie` values are kept automatically.
- The WebSocket upgrade goes through the same `curl_cffi` session, so the
  handshake carries the same TLS/HTTP fingerprint and header set
  (`Sec-Fetch-Mode: websocket`, `Sec-Fetch-Site: cross-site`,
  `Pragma`/`Cache-Control`). The stdlib `websockets` fallback is **opt-in**
  (`MUSE_WS_FALLBACK=1`): it runs on Python/OpenSSL, so it would present a
  different TLS fingerprint from the HTTP side and is refused by default.
- `MUSE_PROXY` routes the muse.ai HTTP session, the WebSocket upgrade and
  signed-media downloads through one egress proxy. User-supplied reference
  URLs are deliberately not proxied: the SSRF guard resolves them locally, and
  a proxy would move that resolution beyond the guard.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `MUSE_COOKIE_FILE` | `cookies_muse.txt` | Netscape cookie jar |
| `MUSE_COOKIE_FILES` | — | Comma-separated jars for the pool |
| `MUSE2API_HOST` / `MUSE2API_PORT` | `127.0.0.1` / `18610` | Listen address |
| `MUSE2API_KEY` | unset | Optional bearer key required by the server |
| `MUSE_MODEL` | `muse-spark` | Advertised model id |
| `MUSE_TIMEZONE` | `UTC` | Timezone sent to the upstream agent |
| `MUSE_SESSION_MODE` | `thread` | `thread` or `main` |
| `MUSE_CHAT_TIMEOUT` | `300` | Reply deadline, seconds |
| `MUSE2API_CONCURRENCY` | `2` | Concurrent upstream generations |
| `MUSE_IMAGE_TIMEOUT` | `240` | Image generation deadline, seconds |
| `MUSE_VIDEO_TIMEOUT` | `600` | Video generation deadline, seconds |
| `MUSE_REQUEST_TIMEOUT` | `60` | Per-request upstream HTTP timeout |
| `MUSE_LISTEN_MS` | `90000` | Gateway listen window, milliseconds |
| `MUSE_IDLE_SLEEP_SECONDS` | `1800` | Pause keepalive and release sessions after this much inactivity (`0` disables) |
| `MUSE2API_APPROVAL_POLICY` | `off` | `off`, `allow_once`, `allow_session`, or `deny` agent permission prompts |
| `MUSE2API_PUBLIC_BASE` | unset | Absolute prefix for generated media URLs |
| `MUSE2API_ALLOW_PRIVATE_URLS` | unset | Allow reference-image URLs that resolve to private/loopback hosts (SSRF guard opt-out) |
| `MUSE_VIDEO_BLOCK_SECONDS` | `21600` | Suppress new video tasks after a region refusal |
| `MUSE_SESSION_CACHE` | `45` | Bootstrap cache, seconds |
| `MUSE_STATE_DIR` | `.muse-state` | Local session/media state |
| `MUSE_LOG_LEVEL` | `info` | `debug`, `info`, `warning`, ... |
| `MUSE_LB_HOST` / `MUSE_WS_PATH` / `MUSE_APP_ID` | discovered | Gateway overrides |
| `MUSE_CVM_RV_KEY` | unset | CVM recovery key |
| `MUSE_ATTESTATION_CMD` | unset | External attestation verifier |
| `MUSE_IMPERSONATE` | `chrome` | curl_cffi profile alias; pin a version if needed |
| `MUSE_HTTP_VERSION` | `2` | `2` (default), `auto`, `3`, `1.1` |
| `MUSE_USER_AGENT` | profile | Optional UA override |
| `MUSE_PROXY` | unset | Egress proxy for muse.ai HTTP, WebSocket and media (`http://`, `socks5://`, `socks5h://`, with optional `user:pass`) |
| `MUSE_WS_FALLBACK` | `0` | Allow the stdlib `websockets` fallback (different TLS fingerprint; off by default) |

## Known limits

- **Function calling is bridged.** Muse exposes few client tools on the wire,
  so `muse2api/tool_bridge.py` renders the client catalog into the prompt,
  treats the model's own `<<TOOL_CALL>> ... <<TOOL_CALL>>` or fenced JSON
  `{"name": ...}` blocks as tool requests, and filters invented arguments to the
  declared schema. Auto / required / forced `tool_choice` are honoured for both
  `/v1/chat/completions` and `/v1/responses`, including `function_call` output
  items and `function_call_arguments` streaming.
- **Video is account/region gated.** When muse refuses ("not available for your
  account in your country or region"), the task fails with
  `code: "video_unavailable_region"`, a stable `error`, and the model's prose
  under `upstream_message`. The server then remembers the refusal for six hours
  and rejects new `/v1/videos` calls immediately with HTTP 503 instead of
  launching another doomed generation. The block is stored in
  `.muse-state/video_block.json` and applies to the whole pool; delete that file
  if you add an account from a supported region.
- **Video/image tasks are persisted, not resumable.** Task ids and results live
  in `.muse-state/tasks.json`, so polling survives a restart; a task that was
  queued or running when the process stopped is marked `failed` on startup
  instead of hanging forever. Generated files stay under `data/media`
  (`<MUSE_STATE_DIR>/../data/media`).
- **Connector OAuth consent is not automated.** Only egress/network/connector
  *action* approvals scoped to the running thread can be auto-answered.
- **Chat context lives upstream.** Deleting `.muse-state/chat_sessions.json`
  while the server is stopped forgets the mapping; the muse side-chat remains.

## Tests

```bash
python -m pip install -r requirements-dev.txt
python -m ruff check muse2api tests
python -m pytest tests -q
```

`.github/workflows/tests.yml` runs lint and the suite on Python 3.11–3.13;
`.github/workflows/docker.yml` builds and publishes the GHCR image on pushes to
`main`/`master` and on `v*` tags. Unit tests cover the wire codec, Noise, CVM
proof construction, protocol discovery, request validation, reference-image
SSRF guards, media size limits, cookie persistence/reload, task recovery and the
main-mode subscription loop.

Smoke test against a running server:

```bash
python -m muse2api ping
curl -s localhost:18610/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"muse-spark","messages":[{"role":"user","content":"hello"}]}'
```

## Repository hygiene

Only source, tests, workflows and the bundled protocol table are tracked.
Everything that contains credentials or runtime state is ignored:

| Path | Why it is ignored |
|---|---|
| `cookies*.txt` | Netscape exports are bearer credentials for the muse.ai account |
| `.env` | local secrets and overrides |
| `.muse-state/` | rotated cookies, session mapping, task and video-block state |
| `data/` | generated images and videos |
| `capture/`, `rea/`, `reference/`, `references/`, `curl_cffi_docs*` | local reverse-engineering artifacts |
| `*.har`, `*.log` | captures and logs can contain cookies or prompts |
| `.tmp/`, `.venv/`, `__pycache__/`, `*.py[cod]`, `.pytest_cache/`, `.ruff_cache/`, `build/`, `dist/`, `*.egg-info/` | build and tool caches |

`.dockerignore` keeps the same paths out of the build context, so a local
`docker build` never uploads cookies or state to the daemon.

## Security notes

Cookies are bearer credentials: anyone with `cookies_muse.txt` or
`.muse-state/cookies-*.json` can act as the muse.ai account until it expires.
Mount them read-only where possible, keep the state directory private, and never
commit either.

Client-supplied reference-image URLs are fetched by the server, so the fetch is
guarded: only public HTTP(S) hosts are allowed, every redirect hop is re-checked,
and downloads (reference images and generated media) are capped at 150 MiB.
Set `MUSE2API_ALLOW_PRIVATE_URLS=1` only if you intentionally pull references
from a LAN host. `MUSE2API_KEY` is compared in constant time; media URLs contain
unguessable UUIDs and are served without auth, so treat `MUSE2API_PUBLIC_BASE`
as public.
