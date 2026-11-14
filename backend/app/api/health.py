from fastapi import APIRouter, HTTPException
from sqlalchemy import text

from ..db import get_engine

router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict:
    """Liveness: the process is up. Does not touch dependencies."""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz() -> dict:
    """Readiness: the database answers."""
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(status_code=503, detail="database unavailable") from exc
    return {"status": "ready"}
