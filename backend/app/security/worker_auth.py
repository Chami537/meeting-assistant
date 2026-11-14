"""Per-Worker bearer tokens: the database keeps sha256(token) in worker_nodes.token_hash (design doc §5.2)."""

import hashlib
import secrets
from uuid import UUID

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from ..deps import db_engine
from ..models.tables import worker_nodes


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_worker_token() -> tuple[str, str]:
    """Returns (token for the Worker's config, hash for worker_nodes.token_hash)."""
    token = secrets.token_urlsafe(32)
    return token, hash_token(token)


async def current_worker(
    authorization: str = Header(default=""), engine: AsyncEngine = Depends(db_engine)
) -> UUID:
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="missing bearer token")
    w = worker_nodes.c
    async with engine.connect() as conn:
        row = (
            await conn.execute(select(w.id, w.status, w.revoked_at).where(w.token_hash == hash_token(token)))
        ).one_or_none()
    if row is None or row.revoked_at is not None or row.status == "DISABLED":
        raise HTTPException(status_code=401, detail="unknown, revoked or disabled worker")
    return row.id
