"""Operator commands. Run from backend/: `python -m app.cli worker add win-01`.

worker add NAME         register a Worker; prints its token once (only the hash is stored)
worker rotate NAME      issue a new token; the old one stops working at once
worker disable NAME     stop handing out leases (an attempt in progress loses its next heartbeat)
worker enable NAME
worker revoke NAME      permanent; the name stays taken
worker list
keys new                print a fresh DATA_ENCRYPTION_KEYS entry value
"""

import argparse
import asyncio
import sys
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from .models.tables import worker_nodes
from .security.keyring import new_key_b64
from .security.worker_auth import new_worker_token


class CliError(Exception):
    pass


async def add_worker(engine: AsyncEngine, name: str, platform: str) -> str:
    token, digest = new_worker_token()
    try:
        async with engine.begin() as conn:
            await conn.execute(insert(worker_nodes).values(name=name, platform=platform, token_hash=digest))
    except IntegrityError:
        raise CliError(f"a worker named {name!r} already exists") from None
    return token


async def rotate_worker(engine: AsyncEngine, name: str) -> str:
    token, digest = new_worker_token()
    w = worker_nodes.c
    await _update_one(engine, name, w.revoked_at.is_(None), token_hash=digest, token_rotated_at=_now())
    return token


async def set_status(engine: AsyncEngine, name: str, status: str) -> None:
    await _update_one(engine, name, worker_nodes.c.revoked_at.is_(None), status=status)


async def revoke_worker(engine: AsyncEngine, name: str) -> None:
    await _update_one(engine, name, worker_nodes.c.revoked_at.is_(None), revoked_at=_now(), status="DISABLED")


async def list_workers(engine: AsyncEngine) -> list[tuple]:
    w = worker_nodes.c
    async with engine.connect() as conn:
        rows = await conn.execute(
            select(w.name, w.platform, w.status, w.last_heartbeat_at, w.revoked_at).order_by(w.name)
        )
        return [tuple(r) for r in rows]


async def _update_one(engine: AsyncEngine, name: str, condition, **values) -> None:
    w = worker_nodes.c
    async with engine.begin() as conn:
        result = await conn.execute(update(worker_nodes).where(w.name == name, condition).values(**values))
    if result.rowcount != 1:
        raise CliError(f"no active worker named {name!r}")


def _now() -> datetime:
    return datetime.now(UTC)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    groups = parser.add_subparsers(dest="group", required=True)
    worker = groups.add_parser("worker").add_subparsers(dest="action", required=True)
    add = worker.add_parser("add")
    add.add_argument("name")
    add.add_argument("--platform", default="windows")
    for action in ("rotate", "disable", "enable", "revoke"):
        worker.add_parser(action).add_argument("name")
    worker.add_parser("list")
    groups.add_parser("keys").add_subparsers(dest="action", required=True).add_parser("new")
    return parser


async def run(argv: Sequence[str], engine: AsyncEngine | None = None, out=sys.stdout) -> int:
    args = _parser().parse_args(argv)
    if args.group == "keys":
        print(new_key_b64(), file=out)
        return 0
    if engine is None:
        from .db import get_engine

        engine = get_engine()
    try:
        match args.action:
            case "add":
                token = await add_worker(engine, args.name, args.platform)
                print(f"worker {args.name} added. Token (shown once, put it in the Worker config):", file=out)
                print(token, file=out)
            case "rotate":
                token = await rotate_worker(engine, args.name)
                print(f"new token for {args.name} (shown once):", file=out)
                print(token, file=out)
            case "disable" | "enable":
                await set_status(engine, args.name, "DISABLED" if args.action == "disable" else "OFFLINE")
                print(f"worker {args.name} {args.action}d", file=out)
            case "revoke":
                await revoke_worker(engine, args.name)
                print(f"worker {args.name} revoked", file=out)
            case "list":
                for name, platform, status, heartbeat, revoked in await list_workers(engine):
                    seen = heartbeat.isoformat(timespec="seconds") if heartbeat else "-"
                    print(f"{name}\t{platform}\t{'REVOKED' if revoked else status}\t{seen}", file=out)
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run(sys.argv[1:])))
