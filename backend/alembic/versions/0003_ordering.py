"""Per-user inbox order (kf_inbox.ingest_seq) and outbox msgid generations.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-09
"""

from pathlib import Path

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

_SQL = Path(__file__).with_name("0003_ordering.sql")


def upgrade() -> None:
    op.get_bind().connection.driver_connection.execute(_SQL.read_text(encoding="utf-8"))


def downgrade() -> None:
    op.execute("ALTER TABLE outgoing_messages DROP COLUMN send_generation")
    op.execute("DROP INDEX idx_kf_inbox_user_seq")
    op.execute("ALTER TABLE kf_inbox DROP COLUMN ingest_seq")
