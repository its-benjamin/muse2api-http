# muse2api-http: OpenAI-Compatible API Bridge for muse.ai

[![GitHub Release](https://img.shields.io/github/v/release/its-benjamin/muse2api-http?style=flat-square)](https://github.com/its-benjamin/muse2api-http/releases)
[![Tests](https://img.shields.io/github/actions/workflow/status/its-benjamin/muse2api-http/tests.yml?branch=master&label=tests&style=flat-square)](https://github.com/its-benjamin/muse2api-http/actions/workflows/tests.yml)
[![Docker Image](https://img.shields.io/badge/docker-GHCR-blue?style=flat-square&logo=docker)](https://github.com/its-benjamin/muse2api-http/pkgs/container/muse2api-http)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue?style=flat-square&logo=python)](https://www.python.org/)

A fast, standalone, **OpenAI-compatible HTTP API bridge for [muse.ai](https://muse.ai)**. Connect your muse.ai account directly to **OpenWebUI**, **LibreChat**, **Cursor**, **Hermes Agent**, or any OpenAI-compatible client for streaming chat completions, tool/function calling, text-to-image, and text-to-video generation.

**No headless browser required.** Unlike Playwright, Puppeteer, or Chromium wrappers, `muse2api-http` communicates directly with muse.ai's backend using HTTPS for bootstrap, a **Noise XX WebSocket** for the Hatch gateway, and protobuf-framed RPC for chat. By reproducing the browser's HTTP/2 protocol and TLS fingerprint using `curl_cffi`, the entire bridge runs as an ultra-lightweight, efficient Python/Docker service. Cookies are the only requirement.

The protocol layer is derived from the public Next.js bundle. `muse2api discover` refreshes the route table when muse.ai ships a new build; the bundled `muse2api/protocol.json` keeps the last known-good values so startup does not depend on the network.

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

## Deployment & Running

> **Note on personal use:** This bridge is built for **private, personal self-hosting**
> (e.g. your private Hermes agent, OpenWebUI, LibreChat, Cursor, or local scripts).
> All turns run under your single underlying muse.ai account (sharing its workspace
> and connector credentials). Do not host this as an open public API for untrusted callers.

### How to get your cookies

The gateway authenticates using cookies from your active [muse.ai](https://muse.ai) session:

1. Log in to [muse.ai](https://muse.ai) in your browser.
2. Get your session cookie:
   - **Interactive prompt (Easiest):** If you launch the binary or CLI in a terminal without `cookies_muse.txt`, it will automatically prompt you to paste your cookie and save it for you.
   - **Manual file:** Open DevTools (**F12** $
ightarrow$ **Application** or **Storage** $
ightarrow$ **Cookies** $
ightarrow$ `https://muse.ai`), copy the value of `hatch_sess`, and save it to `cookies_muse.txt` as `hatch_sess=<value>`.
   - **Full export:** Use a browser extension (such as *Get cookies.txt locally*) while on `muse.ai` and save the exported file as `cookies_muse.txt`.

The server automatically rotates and persists refreshed session tokens in `.muse-state/`.

Choose the running method that fits your setup:

### Option 1: Standalone binary (No Docker or Python needed)

Prebuilt binaries are published with each release:

1. Download the latest binary for your OS from [GitHub Releases](https://github.com/its-benjamin/muse2api-http/releases):
   - **Windows (x64)**: `muse2api-windows-amd64.exe`
   - **Linux (x64)**: `muse2api-linux-amd64`
   - **macOS (Apple Silicon M-series)**: `muse2api-darwin-arm64`
   - Verify integrity with `SHA256SUMS`.
2. Place your `cookies_muse.txt` in the same directory.
3. Run the server:
   ```bash
   # Windows
   muse2api-windows-amd64.exe serve

   # Linux / macOS
   chmod +x muse2api-*
   ./muse2api-linux-amd64 serve   # or ./muse2api-darwin-arm64 serve
   ```

### Option 2: Run with Python (`uvx`, `pipx`, or `pip`)

Run directly with `uvx` (no repository clone needed):

```bash
uvx --from git+https://github.com/its-benjamin/muse2api-http.git muse2api serve
```

Or clone the repository and run:

```bash
git clone https://github.com/its-benjamin/muse2api-http.git
cd muse2api-http
python -m pip install -r requirements.txt
python -m muse2api serve                     # http://127.0.0.1:18610
```

### Option 3: Docker (Prebuilt GHCR image)

Prebuilt multi-arch images (`linux/amd64`, `linux/arm64`) are published to GHCR. Run the latest release:

```bash
docker run -d --name muse2api-http \
  -p 8130:8130 \
  -v "$PWD/cookies_muse.txt:/app/cookies_muse.txt:ro" \
  -v muse2api-state:/app/.muse-state \
  ghcr.io/its-benjamin/muse2api-http:latest
```

Or with Docker Compose:

```yaml
services:
  muse2api-http:
    image: ghcr.io/its-benjamin/muse2api-http:latest
    container_name: muse2api-http
    restart: unless-stopped
    ports:
      - "8130:8130"
    volumes:
      - ./cookies_muse.txt:/app/cookies_muse.txt:ro
      - muse2api-state:/app/.muse-state
    # Optional overrides:
    # environment:
    #   - MUSE2API_KEY=your-secret-api-key
    #   - MUSE2API_APPROVAL_POLICY=deny  # or "settings" for personal agent use

volumes:
  muse2api-state:
```

The container runs unprivileged as `muse` (uid 10001) and listens on port `8130`
by default (map `-p 8130:8130` or `-p 18610:8130` to match the local port).
When idle, the gateway releases upstream connections while keeping the HTTP port
open and periodically renewing cookies in the background.

### Client Integrations (OpenWebUI, LibreChat, Cursor)

`muse2api-http` can be used as a drop-in custom OpenAI provider in standard frontends:

#### OpenWebUI
1. In OpenWebUI, navigate to **Settings** $\rightarrow$ **Admin Settings** $\rightarrow$ **Connections** $\rightarrow$ **OpenAI API**.
2. Add a new connection:
   - **API Base URL**: `http://127.0.0.1:18610/v1` (or `http://host.docker.internal:8130/v1` if OpenWebUI is in Docker)
   - **API Key**: any non-empty string (or your `MUSE2API_KEY` if configured)
3. Save and select the model `muse-spark`.

#### LibreChat
Add a custom endpoint to your `librechat.yaml`:

```yaml
endpoints:
  custom:
    - name: "muse.ai"
      apiKey: "dummy" # or MUSE2API_KEY if configured
      baseURL: "http://127.0.0.1:18610/v1"
      models:
        default: ["muse-spark"]
      titleModel: "muse-spark"
      modelDisplayLabel: "Muse AI"
```

#### Cursor & AI IDEs
Configure **Cursor Settings** $\rightarrow$ **Models** $\rightarrow$ **OpenAI API Key**:
- **Override OpenAI Base URL**: `http://127.0.0.1:18610/v1`
- **Model Name**: `muse-spark`

## Sessions and permissions

Each API conversation maps to one server-side muse side-chat. Send
`x-muse-session: <conversation-id>` to give every caller its own thread; without
it, clients share the `default` thread (and, when `MUSE2API_KEY` is set, the
bearer token cannot distinguish them).

Muse pauses agent work on permission prompts (network egress, connectors,
browser actions, direct network protocols). Because headless API clients have no
UI to show these prompts, an unhandled prompt will block until `MUSE_CHAT_TIMEOUT`.

### Hosting profiles

Choose an approval policy based on how you deploy the API:

1. **Public / Shared Host (Restricted, OpenAI-like)**:
   ```bash
   MUSE2API_APPROVAL_POLICY=deny
   MUSE2API_KEY=your-secret-key
   ```
   **Recommended if untrusted callers can access the endpoint.** Whenever the
   agent asks to access an external website, upload files, or trigger connector
   actions, the bridge immediately denies the request. The model fails fast with
   a polite refusal ("I cannot access that resource") rather than hanging for 5
   minutes, acting strictly like a standard sandboxed LLM without giving callers
   arbitrary network or tool capabilities on your account.

2. **Private / Self-Hosted (Full Agent Capabilities)**:
   ```bash
   MUSE2API_APPROVAL_POLICY=settings   # follows your muse.ai web UI toggles
   # or
   MUSE2API_APPROVAL_POLICY=allow_always  # creates durable domain grants
   ```
   **Recommended for personal use.** Permitted actions are approved
   automatically so complex multi-step tasks (web searches, downloads,
   connectors) finish seamlessly without manual clicks.

### Policy reference

- `off` (default): never answer prompts; the upstream turn waits for a human
  in the web UI or times out.
- `deny`: immediately refuse prompts. Best for public endpoints to prevent
  unauthorized egress while avoiding turn timeouts.
- `settings`: read the account's own permission settings (`/permissions/settings`
  and `/permissions/active`) and follow them. `allow` (including `auto_allow`
  defaults and connector/action overrides) becomes a session grant; `deny`/`off`
  becomes a refusal; a resolved `ask` falls back to `MUSE2API_APPROVAL_ASK`
  (`allow_once` by default). Settings are cached for 60 s per connection.
- `allow_once`: approve the weakest one-shot option offered (`allow_once`,
  `allow_today`, `allow_for_this_task`, ...).
- `allow_session`: grant session access for the waiting thread.
- `allow_always`: accept the daemon's durable grant with its scope
  (`destination_domain`, `entity`, `connector_all_actions`), preventing future
  prompts for that destination.

*Note on resetting approvals:* In the muse.ai web UI, clicking **Reset approvals to defaults**
removes all website and connector overrides. Future actions will strictly follow your
configured defaults (e.g. Always ask vs Ask for some actions).

Only approvals whose `scope.thread_id` (or `scope.session_scope_id`) matches the
current chat are touched. Account-wide HITL settings, connector OAuth consent,
and the `/api/consent/status` web flow are read by `settings` but never
modified.

## Media URLs

Generated URLs are relative by default. Set `MUSE2API_PUBLIC_BASE` (for example
`https://api.example.com/v1/media`) when clients cannot resolve `/v1/media/...`
against the API host. The `/v1/media/{name}` route itself stays the same.

## Accounts
`MUSE_COOKIE_FILES=a.txt,b.txt` (or repeated `MUSE_COOKIE_FILE`) enables the
account pool. A failing login is put on a cooldown and the next healthy account
is used; `/healthz` shows each account. A single file is the normal setup.

### Smart concurrency scaling

By default, concurrency scales dynamically with pool size: `concurrency = accounts * MUSE2API_CONCURRENCY_PER_ACCOUNT` (default: 2 per account).

- 1 account: 2 concurrent execution slots.
- 3 accounts: 6 concurrent execution slots.
- In-flight requests are automatically routed to the least-loaded available account.
- Set `MUSE2API_CONCURRENCY=<number>` to lock an absolute cap, or `auto` (default) to scale dynamically.

### Proxy pool & per-account egress

Configure multiple proxies to distribute traffic and isolate IP addresses across accounts:

```bash
MUSE_PROXIES="http://proxy1:8080,socks5://proxy2:1080,http://proxy3:8080"
# or load from file:
MUSE_PROXY_FILE="proxies.txt"
MUSE_PROXY_STRATEGY=smart  # smart (fitness-aware), round-robin, random, fill-first
```

- **Smart fitness tracking:** Failing, timed-out, or rate-limited proxies are placed on a cooldown (`MUSE_PROXY_COOLDOWN`, default 300 s) before retrying.
- **Per-account binding:** Each account is pinned to a dedicated proxy in the pool to prevent IP correlation, with automatic failover to the next healthy proxy if its current proxy fails.
- `/healthz` reports real-time proxy pool fitness and usage.
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
- **Idle sleep & cookie preservation:** When inactive for `MUSE_IDLE_SLEEP_SECONDS`
  (default 1800 s / 30 min, or set lower e.g. 300 s / 5 min), the gateway enters
  idle sleep: it closes open sessions to muse.ai and stops ongoing WebSocket keepalives.
  The HTTP server stays active to receive requests, and the background loop still
  renews rolling cookies via `/api/session` every 6 hours so your login never expires.
  Any new API request wakes the gateway immediately.

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
| `MUSE2API_CONCURRENCY` | `auto` | Concurrent upstream generations (`auto` scales with accounts, or fixed integer) |
| `MUSE2API_CONCURRENCY_PER_ACCOUNT` | `2` | Slots per account when `MUSE2API_CONCURRENCY=auto` |
| `MUSE_PROXIES` | unset | Comma-separated proxy list for the egress proxy pool |
| `MUSE_PROXY_FILE` | unset | File containing one proxy URL per line |
| `MUSE_PROXY_STRATEGY` | `smart` | Proxy rotation strategy: `smart`, `round-robin`, `random`, or `fill-first` |
| `MUSE_PROXY_COOLDOWN` | `300` | Cooldown seconds for failed/rate-limited proxies |
| `MUSE_IMAGE_TIMEOUT` | `240` | Image generation deadline, seconds |
| `MUSE_VIDEO_TIMEOUT` | `600` | Video generation deadline, seconds |
| `MUSE_REQUEST_TIMEOUT` | `60` | Per-request upstream HTTP timeout |
| `MUSE_LISTEN_MS` | `90000` | Gateway listen window, milliseconds |
| `MUSE_IDLE_SLEEP_SECONDS` | `1800` | Pause keepalive and release sessions after this much inactivity (`0` disables) |
| `MUSE2API_APPROVAL_POLICY` | `off` | `off`, `allow_once`, `allow_session`, `allow_always`, `settings`, or `deny` agent permission prompts |
| `MUSE2API_APPROVAL_ASK` | `allow_once` | Answer used by `settings` when the account resolves a prompt to `ask` |
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
- **Video is account- and region-gated upstream by muse.ai.** Video generation
  is restricted by muse.ai to specific accounts and eligible regions. If your account
  does not have video access, muse returns a regional refusal. To avoid repeated
  timeouts on doomed tasks, the bridge caches this refusal and fails fast with
  HTTP 503 (`video_unavailable_region`). If your account has video access, it
  generates normally. To clear the cached refusal or retest immediately, delete
  `.muse-state/video_block.json` or set `MUSE_VIDEO_BLOCK_SECONDS=0`.
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
`.github/workflows/docker.yml` builds and publishes the GHCR image on pushes to `master` and on `v*` tags. Unit tests cover the wire codec, Noise, CVM
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
