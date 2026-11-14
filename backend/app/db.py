from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .config import get_settings


@lru_cache
def get_engine() -> AsyncEngine:
    url = get_settings().database_url
    if not url:
        raise RuntimeError("DATABASE_URL is not configured")
    # hide_parameters: database errors must not copy bound values (minutes, meeting codes) into logs.
    return create_async_engine(url, pool_pre_ping=True, hide_parameters=True)
