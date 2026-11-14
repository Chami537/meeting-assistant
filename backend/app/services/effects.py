"""Runs task_effects rows (the outbox of state transitions). Handlers are idempotent: a row may run again
after a crash between the external call and marking it DONE."""

import logging
from datetime import datetime, timedelta

from meeting_protocol import AudioUploadStatus, FailureCode, TaskEffectKind, TaskStatus
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ..intake import messages as msgs
from ..models.tables import audio_assets, kf_users, meeting_tasks, outgoing_messages, task_effects
from .task_transitions import in_transaction_with_retry
from .uploads import ObjectStorage

log = logging.getLogger(__name__)

EFFECT_LEASE = timedelta(minutes=5)
MAX_EFFECT_ATTEMPTS = 8
MAX_BACKOFF = timedelta(minutes=30)

_NOTICE = {
    TaskStatus.SCHEDULED: ("SCHEDULED", lambda reason: msgs.SCHEDULED),
    TaskStatus.CANCELED: ("CANCELED", lambda reason: msgs.CANCELED),
    TaskStatus.FAILED: ("FAILED", lambda reason: msgs.failure(FailureCode(reason) if reason else None)),
    TaskStatus.COMPLETED: ("RESULT_LINK", lambda reason: msgs.COMPLETED),
}


async def run_effects(
    engine: AsyncEngine, *, now: datetime, storage: ObjectStorage | None, limit: int = 50
) -> int:
    """Without object storage configured, DISCARD_AUDIO rows simply wait."""
    e = task_effects.c
    kinds = [TaskEffectKind.NOTIFY_USER] + ([TaskEffectKind.DISCARD_AUDIO] if storage is not None else [])
    async with engine.begin() as conn:
        claimable = (
            select(e.id)
            .where(e.status.in_(("PENDING", "RUNNING")), e.next_attempt_at <= now, e.effect.in_(kinds))
            .order_by(e.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        rows = (
            await conn.execute(
                update(task_effects)
                .where(e.id.in_(claimable.scalar_subquery()))
                .values(status="RUNNING", attempts=e.attempts + 1, next_attempt_at=now + EFFECT_LEASE)
                .returning(e.id, e.task_id, e.effect, e.payload, e.attempts)
            )
        ).all()
    done = 0
    for row in rows:
        mine = (e.id == row.id) & (e.status == "RUNNING") & (e.attempts == row.attempts)  # still our claim
        try:
            if row.effect == TaskEffectKind.DISCARD_AUDIO:
                await _discard_audio(engine, storage, row.task_id, now=now)
            async with engine.begin() as conn:
                if row.effect == TaskEffectKind.NOTIFY_USER:
                    await _notify(conn, row.id, row.task_id, row.payload, now=now)
                await conn.execute(update(task_effects).where(mine).values(status="DONE", done_at=now))
            done += 1
        except Exception as exc:
            # A deletion the user was promised never gives up: it keeps retrying (at most every
            # MAX_BACKOFF) and is reported loudly; a notice gives up after MAX_EFFECT_ATTEMPTS.
            exhausted = row.attempts >= MAX_EFFECT_ATTEMPTS
            give_up = exhausted and row.effect != TaskEffectKind.DISCARD_AUDIO
            log.log(
                logging.ERROR if exhausted else logging.WARNING,
                "effect %s (%s, task %s) failed %d times: %s",
                row.id,
                row.effect,
                row.task_id,
                row.attempts,
                type(exc).__name__,
            )
            backoff = min(timedelta(seconds=30 * 2 ** min(row.attempts, 6)), MAX_BACKOFF)
            async with engine.begin() as conn:
                await conn.execute(
                    update(task_effects)
                    .where(mine)
                    .values(
                        status="FAILED" if give_up else "PENDING",
                        next_attempt_at=now + backoff,
                        last_error=type(exc).__name__,
                    )
                )
    return done


async def _notify(conn: AsyncConnection, effect_id: int, task_id, payload: dict, *, now: datetime) -> None:
    notice = _NOTICE.get(TaskStatus(payload.get("to")))
    if notice is None:
        return
    t, u = meeting_tasks.c, kf_users.c
    task = (
        await conn.execute(
            select(t.user_id, t.source_open_kfid, u.deletion_requested_at)
            .join(kf_users, u.id == t.user_id)
            .where(t.id == task_id)
        )
    ).one()
    if task.source_open_kfid is None:
        return  # created through the service API: nobody to message in KF
    if task.deletion_requested_at is not None:
        return  # the user asked for erasure and was told so; per-task notices would only use up replies
    purpose, render = notice
    await conn.execute(
        pg_insert(outgoing_messages)
        .values(
            task_id=task_id,
            user_id=task.user_id,
            open_kfid=task.source_open_kfid,
            purpose=purpose,
            dedup_key=f"effect:{effect_id}",
            content=render(payload.get("reason")),
            next_attempt_at=now,
        )
        .on_conflict_do_nothing(index_elements=["dedup_key"])
    )


async def _discard_audio(engine: AsyncEngine, storage: ObjectStorage, task_id, *, now: datetime) -> None:
    """Delete stored objects first, then mark the rows; a crash in between only repeats the deletion."""
    await storage.delete_prefix(f"tasks/{task_id}/audio/")
    a = audio_assets.c

    async def mark(conn):
        await conn.execute(
            update(audio_assets)
            .where(a.task_id == task_id, a.upload_status != AudioUploadStatus.DELETED)
            .values(upload_status=AudioUploadStatus.DELETED, deleted_at=now)
        )

    await in_transaction_with_retry(engine, mark)
