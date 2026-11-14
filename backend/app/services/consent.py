"""consent_records (design doc §8.2): evidence of what the user agreed to, kept after the task is deleted."""

import hashlib
from datetime import datetime, timedelta
from uuid import UUID

from meeting_protocol import ConsentKind
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from ..models.tables import consent_records, kf_users

CONSENT_RETENTION = timedelta(days=3 * 365)  # placeholder until legal sets the period (design doc §10)


async def record_requester_consent(
    conn: AsyncConnection,
    *,
    task_id: UUID,
    user_id: UUID,
    channel: str,
    evidence_ref: str,
    version: str,
    text_sha256: str,
    now: datetime,
) -> None:
    user = (
        await conn.execute(
            select(kf_users.c.corp_id, kf_users.c.external_userid).where(kf_users.c.id == user_id)
        )
    ).one()
    await conn.execute(
        insert(consent_records).values(
            task_id=task_id,
            user_ref_hash=hashlib.sha256(f"{user.corp_id}:{user.external_userid}".encode()).hexdigest(),
            kind=ConsentKind.REQUESTER_AUTHORITY,
            subject="requester",
            channel=channel,
            evidence_ref=evidence_ref,
            consent_version=version,
            text_sha256=text_sha256,
            granted_at=now,
            retain_until=now + CONSENT_RETENTION,
        )
    )
