from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .chat import ChatService, session_key
from .config import Settings
from .pool import AccountPool
from .protocol import ProtocolConfig


async def _ping(settings: Settings) -> int:
    protocol = ProtocolConfig.load()
    pool = AccountPool(settings, protocol=protocol)
    try:
        account, session = await pool.bootstrap()
        print(f"session ok: account={account.name} vm={session.vm_id} "
              f"notary={'yes' if session.notary_token else 'no'}")
        _account, _session, conn = await pool.connect()
        try:
            response = await conn.request("POST", protocol.route("connection.ping").path, {})
            print(f"ping: HTTP {response.status} {response.text()[:200]}")
            response = await conn.request("GET", protocol.route("model.get").path)
            print(f"model: {response.text()[:300]}")
            response = await conn.request("GET", protocol.route("sessions.list").path)
            data = response.json() or {}
            sessions = data.get("sessions") if isinstance(data, dict) else None
            print(f"sessions: {len(sessions) if isinstance(sessions, list) else 'n/a'}")
        finally:
            await conn.close()
        return 0
    finally:
        await pool.close()


async def _chat(settings: Settings, prompt: str, session: str | None) -> int:
    protocol = ProtocolConfig.load()
    pool = AccountPool(settings, protocol=protocol)
    service = ChatService(settings, pool, protocol=protocol)
    try:
        def on_delta(chunk: str) -> None:
            sys.stdout.write(chunk)
            sys.stdout.flush()

        result = await service.run(
            prompt,
            on_delta=on_delta,
            session_key_value=session_key(session, None),
        )
        print()
        print(f"\n[message_id={result.message_id}]", file=sys.stderr)
        return 0
    finally:
        await pool.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="muse2api")
    parser.add_argument("--log-level", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("ping", help="bootstrap the gateway and check the RPC path")
    chat = sub.add_parser("chat", help="send one prompt and print the streamed reply")
    chat.add_argument("prompt")
    chat.add_argument("--session", default=None)
    serve = sub.add_parser("serve", help="run the OpenAI-compatible HTTP server")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    discover = sub.add_parser("discover", help="refresh protocol.json from the live web bundle")
    discover.add_argument("--offline", default=None,
                          help="directory of captured .js.map files instead of the network")
    discover.add_argument("--dry-run", action="store_true", help="print without writing")
    discover.add_argument("--force", action="store_true", help="ignore the cached protocol")
    discover.add_argument("--maps", action="store_true", help="also fetch source maps (slower)")
    discover.add_argument("--cache-hours", type=float, default=24.0)

    args = parser.parse_args(argv)
    settings = Settings.from_env()
    if args.log_level:
        settings.log_level = args.log_level
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.command == "ping":
        return asyncio.run(_ping(settings))
    if args.command == "chat":
        return asyncio.run(_chat(settings, args.prompt, args.session))
    if args.command == "discover":
        from .discover import discover as run_discover
        from .discover import summarize

        print(summarize(run_discover(
            offline=args.offline,
            write=not args.dry_run,
            force=args.force,
            cache_hours=args.cache_hours,
            include_maps=args.maps,
        )))
        return 0

    if args.command == "serve":
        import uvicorn

        from .server import create_app

        if args.host:
            settings.host = args.host
        if args.port:
            settings.port = args.port
        uvicorn.run(create_app(settings), host=settings.host, port=settings.port,
                    log_level=settings.log_level)
        return 0
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
