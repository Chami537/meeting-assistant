"""Transactional outbox for task side effects (DISCARD_AUDIO, NOTIFY_USER).

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-09
"""

from pathlib import Path

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

_SQL = Path(__file__).with_name("0002_task_effects.sql")


def upgrade() -> None:
    op.get_bind().connection.driver_connection.execute(_SQL.read_text(encoding="utf-8"))


def downgrade() -> None:
    op.execute("DROP TABLE task_effects")
