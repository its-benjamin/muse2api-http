# muse2api (HTTP, no browser)

A standalone, OpenAI-compatible API for [muse.ai](https://muse.ai) that talks to
the site's own backend directly: HTTPS for bootstrap, a Noise XX WebSocket for
the Hatch gateway, and protobuf-framed RPC for chat. No Playwright, no CDP, no
headless browser at runtime.

Reverse engineering was done with [REA](https://github.com/morluto/rea) against
a captured HAR and the public Next.js bundle. `rea/` is a local working copy
(git-ignored); the extracted web sources live in `capture/` (also ignored).

## Hermes Agent integration

`plugins/model-providers/muse2api-http/` is a drop-in
[Model Provider Plugin](https://hermes-agent.nousresearch.com/docs/developer-guide/model-provider-plugin):
copy it to `$HERMES_HOME/plugins/model-providers/muse2api-http/`, point it at
`http://127.0.0.1:8130/v1`, and Hermes lists `muse-spark` (plus the alias models)
right from `/v1/models`; image generation rides the OpenAI-compatible
`/v1/images/generations` surface.

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
**not** modified; connector setup still requires the browser. Auto-approval is security-sensitive: enable it only
for an account whose data and network access you are willing to expose to the
model.

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
| `MUSE_HTTP_VERSION` | `auto` | `auto`, `2`, `3`, `1.1` |
| `MUSE_USER_AGENT` | profile | Optional UA override |

## Known limits

- **Function calling is bridged.** Muse exposes few client tools on the wire,
  so `muse2api/tool_bridge.py` renders the client catalog into the prompt,
  treats the model's own `<<TOOL_CALL>> ... <<TOOL_CALL>>` or `\u0060\u0060\u0060json
  {"name": ...} \u0060\u0060\u0060` blocks as tool requests, and filters invented
  arguments to the declared schema. Auto / required / forced tool_choice are
  honoured for both `/v1/chat/completions` and `/v1/responses`, including
  `function_call` output items and function_call_arguments streaming.
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

`.github/workflows/tests.yml` runs lint and the suite on Python 3.11–3.13.
Codec, Noise, CVM proof construction, protocol discovery, request validation,
reference-image SSRF guards and task recovery are covered by unit tests. The
live smoke used during development:

```bash
python -m muse2api ping
curl -s localhost:18610/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"muse-spark","messages":[{"role":"user","content":"hello"}]}'
```

## Security notes

Cookies are bearer credentials. `cookies*.txt`, `.muse-state/` and captured HAR
or web-source artifacts are git-ignored. Generated media is cached under
`data/media` and served only from that directory.

Client-supplied reference-image URLs are fetched by the server, so the fetch is
guarded: only public HTTP(S) hosts are allowed, every redirect hop is re-checked,
and downloads (reference images and generated media) are capped at 150 MiB.
Set `MUSE2API_ALLOW_PRIVATE_URLS=1` only if you intentionally pull references
from a LAN host. `MUSE2API_KEY` is compared in constant time; media URLs contain
unguessable UUIDs and are served without auth, so treat `MUSE2API_PUBLIC_BASE`
as public.
