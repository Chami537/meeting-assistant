"""Integration fixtures: a fresh database per test session, built by the Alembic baseline.

Needs TEST_DATABASE_URL (see scripts/test_db.sh); every test here is skipped without it.
"""

import os
import uuid
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

ROOT = Path(__file__).parents[2]
BASE_URL = os.environ.get("TEST_DATABASE_URL")


def pytest_collection_modifyitems(config, items):
    for item in items:
        if "tests/integration" in str(item.fspath):
            item.add_marker(pytest.mark.db)
            if not BASE_URL:
                item.add_marker(pytest.mark.skip(reason="TEST_DATABASE_URL not set"))


def _sync_url(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


@pytest.fixture(scope="session")
def database_url():
    name = f"ma_test_{uuid.uuid4().hex[:8]}"
    admin = _sync_url(BASE_URL)
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    url = BASE_URL.rsplit("/", 1)[0] + f"/{name}"

    cfg = Config(str(ROOT / "backend/alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "backend/alembic"))
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    try:
        command.upgrade(cfg, "head")
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous

    yield url
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


@pytest.fixture(scope="session")
async def engine(database_url):
    eng = create_async_engine(database_url, hide_parameters=True)  # as in production (app.db)
    yield eng
    await eng.dispose()


@pytest.fixture
async def clean(engine):
    yield
    async with engine.begin() as conn:
        tables = (
            await conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))
        ).scalars()
        names = ", ".join(f'"{t}"' for t in tables if t != "alembic_version")
        await conn.execute(text(f"TRUNCATE {names} CASCADE"))
