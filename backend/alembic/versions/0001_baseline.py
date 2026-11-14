"""Baseline: schema V1.1.

0001_baseline.sql is a frozen copy of schema.sql at V1.1; later changes go into new revisions.
Databases already created from schema.sql are marked with `alembic stamp 0001`.

Revision ID: 0001
Revises:
Create Date: 2026-10-09
"""

from pathlib import Path

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

_SQL = Path(__file__).with_name("0001_baseline.sql")


def upgrade() -> None:
    # psycopg runs a parameterless multi-statement script in one call (tested on PG16)
    op.get_bind().connection.driver_connection.execute(_SQL.read_text(encoding="utf-8"))


def downgrade() -> None:
    raise NotImplementedError("the baseline cannot be downgraded; drop the database instead")
