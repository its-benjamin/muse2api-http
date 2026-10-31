# muse2api-http — Hermes Agent provider plugin

OpenAI-compatible bridge to muse.ai through `muse2api-http`. Serve the API (default
`127.0.0.1:8130/v1`, or `MUSE2API_HOST`/`MUSE2API_PORT`), then plug it into Hermes.

## Install

```bash
mkdir -p $HERMES_HOME/plugins/model-providers/muse2api-http/
cp -r plugins/model-providers/muse2api-http/* $HERMES_HOME/plugins/model-providers/muse2api-http/
```

Then configure and pick the model:

```bash
hermes config set MUSE2API_KEY=<optional-key-used-by-muse2api-http>
hermes config set model.provider muse2api-http
hermes model          # muse-spark should appear
```

## API key

`MUSE2API_KEY` defaults to no authentication. To lock the server behind a key,
set it in `muse2api-http/.env` or the server's environment, then set the same
key in Hermes.

## Image generation

For `image_gen`:\\n\\n0 Hermione's OpenAI-compatible image-gen plugin calls\\n
`POST {base_url}/images/generations` — the same surface `muse2api-http` exposes.\\n
Install a compatible plugin (for example
`https://github.com/tailofcat/hermes_imgen`), point `OPENAI_COMPAT_IMAGE_BASE_URL`
at this server's `/v1`, and set `OPENAI_COMPAT_IMAGE_MODEL=muse-spark`.\\n

## Notes

- Responses/Codex-style `/v1/responses` is also available; if you prefer that mode,
  change `api_mode` in `__init__.py` to `\"codex_responses\"`.
- Streaming, tools (auto/required/forced), and image attachments are supported.

## Account risk & ban disclaimer

Automating unofficial muse.ai web sessions via reverse-engineered protocols carries risk of account flags, rate limits, or permanent account bans. Account safety and any risk of bans are solely your own responsibility. Use strictly for private personal agent setups.
