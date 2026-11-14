"""Retention and erasure (design doc §10 数据最小化与保留期, 删除流程).

Each pass is idempotent and bounded; the runner calls `purge` every tick.

- meeting secrets (join link, password) are cleared once the meeting phase is over;
- processed inbound message text: 7 days, and at once when the user's meeting secrets are cleared;
- outbound message text: 30 days after sending (or giving up);
- audio: AUDIO_RETENTION per segment; the task's whole audio prefix goes, merged files included;
- transcript and minutes: CONTENT_RETENTION after the task ended, or at once after a delete from the result
  page; COS objects first, then the rows;
- a user's erasure request ("删除我的数据"): active tasks are canceled first, and once no DISCARD_AUDIO run is
  pending (a cancel schedules a second one after every issued upload URL expired), everything is hard-deleted
  in FK order: COS objects, audio_assets, meeting_tasks
  (cascading to the rest), kf_users (cascading to inbox and outbox). consent_records stay (task_id SET NULL).
"""

import logging
from datetime import datetime, timedelta
from uuid import UUID

from meeting_protocol import AudioUploadStatus, ServerEventType, TaskStatus
from meeting_protocol.rules import TERMINAL_STATUSES
from sqlalchemy import delete, exists, func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ..domain.state_machine import InvalidTransition, Trigger, TriggerType
from ..models.tables import (
    audio_assets,
    kf_inbox,
    kf_users,
    meeting_minutes,
    meeting_tasks,
    outgoing_messages,
    result_access_tokens,
    task_effects,
    task_events,
    task_inputs,
    transcripts,
)
from .task_transitions import in_transaction_with_retry, run_trigger
from .uploads import ObjectStorage

log = logging.getLogger(__name__)

INBOX_RETENTION = timedelta(days=7)
OUTBOX_RETENTION = timedelta(days=30)

_SECRETS_DONE = {
    TaskStatus.UPLOADING,
    TaskStatus.TRANSCRIBING,
    TaskStatus.SUMMARIZING,
    *TERMINAL_STATUSES,
}
_INBOX_DONE = ("DONE", "IGNORED", "FAILED")


async def purge(
    engine: AsyncEngine, storage: ObjectStorage | None, *, now: datetime, limit: int = 20
) -> dict[str, int]:
    report = {
        "secrets": await in_transaction_with_retry(engine, lambda c: _purge_secrets(c, now=now)),
        "inbox": await in_transaction_with_retry(engine, lambda c: _purge_inbox(c, now=now)),
        "outbox": await in_transaction_with_retry(engine, lambda c: _purge_outbox(c, now=now)),
        "inputs": await in_transaction_with_retry(engine, lambda c: _purge_inputs(c)),
    }
    if storage is not None:
        report["audio"] = await _purge_audio(engine, storage, now=now, limit=limit)
    # Without object storage only tasks that never stored audio are cleared: objects are deleted first,
    # and audio can only exist where storage was configured.
    report["content"] = await _purge_content(engine, storage, now=now, limit=limit)
    report["erased"] = await _erase_users(engine, storage, now=now, limit=limit)
    return report


def _no_audio(task_id_column):
    return ~select(audio_assets.c.id).where(audio_assets.c.task_id == task_id_column).exists()


async def _purge_secrets(conn: AsyncConnection, *, now: datetime) -> int:
    t, i = meeting_tasks.c, kf_inbox.c
    result = await conn.execute(
        update(meeting_tasks)
        .where(t.secrets_purged_at.is_(None), t.status.in_(_SECRETS_DONE))
        .values(
            join_link_ciphertext=None,
            meeting_password_ciphertext=None,
            secret_key_version=None,
            secrets_purged_at=now,
        )
        .returning(t.id, t.user_id)
    )
    purged = result.all()
    if purged:
        await conn.execute(
            insert(task_events),
            [
                {"task_id": r.id, "event_type": ServerEventType.SECRETS_PURGED, "actor": "purge"}
                for r in purged
            ],
        )
        # The invite and the follow-ups carrying the password are no longer needed either.
        await conn.execute(
            update(kf_inbox)
            .where(
                i.user_id.in_({r.user_id for r in purged}),
                i.status.in_(_INBOX_DONE),
                i.content_ciphertext.is_not(None),
            )
            .values(content_ciphertext=None, content_key_version=None, content_purged_at=now)
        )
    return len(purged)


async def _purge_inputs(conn: AsyncConnection) -> int:
    """Handed-in material (media_id, staged key, forwarded text) of a task that has ended is never needed."""
    t = meeting_tasks.c
    result = await conn.execute(
        delete(task_inputs).where(
            task_inputs.c.task_id.in_(select(t.id).where(t.status.in_(TERMINAL_STATUSES)))
        )
    )
    return result.rowcount


async def _purge_inbox(conn: AsyncConnection, *, now: datetime) -> int:
    i = kf_inbox.c
    result = await conn.execute(
        update(kf_inbox)
        .where(
            i.content_ciphertext.is_not(None),
            i.status.in_(_INBOX_DONE),
            i.received_at <= now - INBOX_RETENTION,
        )
        .values(content_ciphertext=None, content_key_version=None, content_purged_at=now)
    )
    return result.rowcount


async def _purge_outbox(conn: AsyncConnection, *, now: datetime) -> int:
    o = outgoing_messages.c
    cutoff = now - OUTBOX_RETENTION
    result = await conn.execute(
        update(outgoing_messages)
        .where(
            o.content_purged_at.is_(None),
            or_(
                (o.status == "SENT") & (o.sent_at <= cutoff),
                o.status.in_(("FAILED", "CANCELED")) & (o.updated_at <= cutoff),
            ),
        )
        .values(content="", content_purged_at=now)
    )
    return result.rowcount


async def _purge_audio(engine: AsyncEngine, storage: ObjectStorage, *, now: datetime, limit: int) -> int:
    """Per task: once its first segment expires the whole audio prefix goes (segments of one meeting expire
    within hours of each other, and the merged file has no row of its own)."""
    a = audio_assets.c
    async with engine.connect() as conn:
        tasks = (
            (
                await conn.execute(
                    select(a.task_id)
                    .where(a.deleted_at.is_(None), a.retention_until <= now)
                    .group_by(a.task_id)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
    for task_id in tasks:
        await _delete_audio(engine, storage, task_id, now=now)
    return len(tasks)


async def _delete_audio(engine: AsyncEngine, storage: ObjectStorage, task_id: UUID, *, now: datetime) -> None:
    await storage.delete_prefix(f"tasks/{task_id}/audio/")
    a = audio_assets.c

    async def mark(conn: AsyncConnection) -> None:
        await conn.execute(
            update(audio_assets)
            .where(a.task_id == task_id, a.upload_status != AudioUploadStatus.DELETED)
            .values(upload_status=AudioUploadStatus.DELETED, deleted_at=now)
        )

    await in_transaction_with_retry(engine, mark)


async def _purge_content(
    engine: AsyncEngine, storage: ObjectStorage | None, *, now: datetime, limit: int
) -> int:
    t = meeting_tasks.c
    query = select(t.id).where(
        t.content_purged_at.is_(None), t.retention_until <= now, t.status.in_(TERMINAL_STATUSES)
    )
    if storage is None:
        query = query.where(_no_audio(t.id))
    async with engine.connect() as conn:
        tasks = (await conn.execute(query.order_by(t.retention_until).limit(limit))).scalars().all()
    for task_id in tasks:
        if storage is not None:
            await storage.delete_prefix(f"tasks/{task_id}/")  # audio, merged audio, evidence screenshots
        await in_transaction_with_retry(engine, lambda c, tid=task_id: _clear_task(c, tid, now=now))
    return len(tasks)


async def _clear_task(conn: AsyncConnection, task_id: UUID, *, now: datetime) -> None:
    t, a, o, r = meeting_tasks.c, audio_assets.c, outgoing_messages.c, result_access_tokens.c
    await conn.execute(select(t.id).where(t.id == task_id).with_for_update())
    await conn.execute(delete(meeting_minutes).where(meeting_minutes.c.task_id == task_id))
    await conn.execute(delete(transcripts).where(transcripts.c.task_id == task_id))
    await conn.execute(
        update(audio_assets)
        .where(a.task_id == task_id, a.upload_status != AudioUploadStatus.DELETED)
        .values(upload_status=AudioUploadStatus.DELETED, deleted_at=now)
    )
    await conn.execute(
        update(outgoing_messages)
        .where(
            o.task_id == task_id,
            o.content_purged_at.is_(None),
            o.status.in_(("SENT", "FAILED", "CANCELED")),
        )
        .values(content="", content_purged_at=now)
    )
    await conn.execute(
        update(result_access_tokens)
        .where(r.task_id == task_id, r.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    await conn.execute(
        update(meeting_tasks)
        .where(t.id == task_id)
        .values(
            content_purged_at=now,
            join_link_ciphertext=None,
            meeting_password_ciphertext=None,
            secret_key_version=None,
            secrets_purged_at=func.coalesce(t.secrets_purged_at, now),
        )
    )
    await conn.execute(
        insert(task_events).values(task_id=task_id, event_type=ServerEventType.CONTENT_PURGED, actor="purge")
    )


async def _erase_users(
    engine: AsyncEngine, storage: ObjectStorage | None, *, now: datetime, limit: int
) -> int:
    u, t = kf_users.c, meeting_tasks.c
    query = select(u.id).where(u.deletion_requested_at <= now)
    if storage is None:
        query = query.where(~select(t.id).where(t.user_id == u.id, ~_no_audio(t.id)).exists())
    async with engine.connect() as conn:
        users = (await conn.execute(query.order_by(u.deletion_requested_at).limit(limit))).scalars().all()
    erased = 0
    for user_id in users:
        async with engine.connect() as conn:
            active = (
                (
                    await conn.execute(
                        select(t.id).where(t.user_id == user_id, t.status.not_in(TERMINAL_STATUSES))
                    )
                )
                .scalars()
                .all()
            )
            pending = await conn.scalar(
                select(
                    exists().where(
                        task_effects.c.task_id == t.id,
                        t.user_id == user_id,
                        task_effects.c.effect == "DISCARD_AUDIO",
                        task_effects.c.status.in_(("PENDING", "RUNNING")),
                    )
                )
            )
        if active:
            for task_id in active:
                try:
                    await run_trigger(
                        engine, task_id, Trigger(TriggerType.CANCEL_REQUESTED), now=now, actor="erasure"
                    )
                except InvalidTransition:
                    pass  # became terminal meanwhile
            continue  # hard deletion on a later pass, after the second DISCARD_AUDIO
        if pending:
            continue  # a cancel's second DISCARD_AUDIO (UPLOAD_URL_TTL later) catches in-flight uploads
        if await _hard_delete(engine, storage, user_id):
            erased += 1
    return erased


async def _hard_delete(engine: AsyncEngine, storage: ObjectStorage | None, user_id: UUID) -> bool:
    t = meeting_tasks.c
    async with engine.connect() as conn:
        tasks = set((await conn.execute(select(t.id).where(t.user_id == user_id))).scalars().all())
    for task_id in tasks:
        if storage is not None:
            await storage.delete_prefix(f"tasks/{task_id}/")

    async def rows(conn: AsyncConnection) -> bool:
        # Same lock order as the conversation (inbox rows, then the user row). The user row lock keeps it from
        # creating a task while the rows go.
        await conn.execute(select(kf_inbox.c.id).where(kf_inbox.c.user_id == user_id).with_for_update())
        await conn.execute(select(kf_users.c.id).where(kf_users.c.id == user_id).with_for_update())
        now_tasks = (await conn.execute(select(t.id, t.status).where(t.user_id == user_id))).all()
        if {r.id for r in now_tasks} != tasks or any(r.status not in TERMINAL_STATUSES for r in now_tasks):
            return False  # a new task arrived after the objects were deleted: next pass
        await conn.execute(delete(audio_assets).where(audio_assets.c.task_id.in_(tasks)))
        await conn.execute(delete(meeting_tasks).where(t.user_id == user_id))
        await conn.execute(delete(kf_users).where(kf_users.c.id == user_id))
        return True

    done = await in_transaction_with_retry(engine, rows)
    if done:
        log.info("erased user %s (%d tasks)", user_id, len(tasks))
    return done
