"""Migrations, schema.sql and the SQLAlchemy Core tables must all describe the same database."""

import uuid
from pathlib import Path

import psycopg
import pytest
from app.models.tables import metadata
from sqlalchemy import text

ROOT = Path(__file__).parents[2]

_CATALOG = {
    "columns": """
        SELECT table_name, column_name, data_type, is_nullable, column_default
        FROM information_schema.columns WHERE table_schema = 'public' AND table_name <> 'alembic_version'
    """,
    "constraints": """
        SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid)
        FROM pg_constraint WHERE connamespace = 'public'::regnamespace AND conrelid::regclass::text <> 'alembic_version'
    """,
    "indexes": "SELECT tablename, indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' AND tablename <> 'alembic_version'",
    "triggers": "SELECT event_object_table, trigger_name, action_statement FROM information_schema.triggers",
}


def _catalog(url: str) -> dict[str, set]:
    with psycopg.connect(url) as conn:
        return {name: {tuple(r) for r in conn.execute(q).fetchall()} for name, q in _CATALOG.items()}


@pytest.fixture(scope="module")
def schema_sql_database(database_url):
    admin = database_url.rsplit("/", 1)[0].replace("postgresql+asyncpg://", "postgresql://", 1) + "/postgres"
    name = f"ma_schema_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    url = admin.rsplit("/", 1)[0] + f"/{name}"
    with psycopg.connect(url) as conn:
        conn.execute((ROOT / "schema.sql").read_text(encoding="utf-8"))
    yield url
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


def test_migrations_build_exactly_schema_sql(database_url, schema_sql_database):
    migrated = _catalog(database_url.replace("postgresql+asyncpg://", "postgresql://", 1))
    reference = _catalog(schema_sql_database)
    for part in _CATALOG:
        assert migrated[part] == reference[part], part


async def test_migrations_reached_head(engine):
    async with engine.connect() as conn:
        version = await conn.scalar(text("SELECT version_num FROM alembic_version"))
    assert version == "0006"


@pytest.mark.parametrize("table", sorted(metadata.tables))
async def test_columns_match(engine, table):
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT column_name, is_nullable FROM information_schema.columns"
                " WHERE table_schema = 'public' AND table_name = :t"
            ),
            {"t": table},
        )
        db = {name: nullable == "YES" for name, nullable in rows}
    model = {c.name: c.nullable for c in metadata.tables[table].columns}
    assert model == db
