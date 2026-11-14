"""Fallback-path inputs: uploaded recordings and forwarded minutes (task_inputs), upload links, web consent.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-09
"""

from pathlib import Path

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

_SQL = Path(__file__).with_name("0004_task_inputs.sql")


def upgrade() -> None:
    op.get_bind().connection.driver_connection.execute(_SQL.read_text(encoding="utf-8"))


def downgrade() -> None:
    op.execute(
        "ALTER TABLE consent_records DROP CONSTRAINT consent_records_channel_check, "
        "ADD CONSTRAINT consent_records_channel_check "
        "CHECK (channel IN ('wecom_kf','meeting_chat','meeting_ui','api'))"
    )
    op.execute(
        "ALTER TABLE result_access_tokens DROP CONSTRAINT result_access_tokens_upload_no_session, "
        "DROP COLUMN purpose"
    )
    op.execute("DROP TABLE task_inputs")
