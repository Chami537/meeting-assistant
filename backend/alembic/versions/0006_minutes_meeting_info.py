"""Minutes v3: meeting type and date.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-16
"""

from pathlib import Path

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

_SQL = Path(__file__).with_name("0006_minutes_meeting_info.sql")


def upgrade() -> None:
    op.get_bind().connection.driver_connection.execute(_SQL.read_text(encoding="utf-8"))


def downgrade() -> None:
    op.execute(
        "ALTER TABLE meeting_minutes DROP COLUMN meeting_type, DROP COLUMN meeting_date, "
        "DROP COLUMN meeting_date_source"
    )
