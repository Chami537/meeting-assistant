"""Minutes v2 sections: key points, key facts, outlook, name fixes.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-13
"""

from pathlib import Path

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

_SQL = Path(__file__).with_name("0005_minutes_sections.sql")


def upgrade() -> None:
    op.get_bind().connection.driver_connection.execute(_SQL.read_text(encoding="utf-8"))


def downgrade() -> None:
    op.execute(
        "ALTER TABLE meeting_minutes DROP COLUMN key_points, DROP COLUMN key_facts, DROP COLUMN outlook, "
        "DROP COLUMN name_fixes"
    )
