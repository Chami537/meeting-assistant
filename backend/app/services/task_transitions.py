"""Persist state-machine decisions.

Lock order, used by every writer that touches a task: the meeting_tasks row (SELECT ... FOR UPDATE) first,
then its task_attempts rows, then audio_assets / processing_jobs / transcripts. load_snapshot takes the task
row lock and reads all child-derived facts under it, so a concurrent upload, seal or lease cannot slip in
between the decision and the write. The compare-and-set on (status, lock_version) stays as a second guard.

Every effect is written in the same transaction as the status change: database effects directly, external
ones (DISCARD_AUDIO, NOTIFY_USER) as task_effects rows for the effect dispatcher.
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from uuid import UUID

from meeting_protocol import (
    AttemptStatus,
    AudioUploadStatus,
    ProcessingJobKind,
    ProcessingJobStatus,
    RecordingOutcome,
    ServerEventType,
    TaskEffectKind,
    TaskKind,
    TaskStatus,
)
from meeting_protocol.rules import TERMINAL_STATUSES
from sqlalchemy import delete, exists, func, insert, literal, or_, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ..domain.state_machine import Effect, TaskSnapshot, Transition, Trigger, TriggerType, decide
from ..models.tables import (
    audio_assets,
    meeting_minutes,
    meeting_tasks,
    processing_jobs,
    task_attempts,
    task_effects,
    task_events,
    transcripts,
)

ASR_PROVIDER = "tencent_asr"
UPLOAD_URL_TTL = timedelta(minutes=30)  # lifetime of a pre-signed segment upload URL
CONTENT_RETENTION = timedelta(days=180)  # transcript and minutes after the task ended (design doc §10)
SUMMARY_PROVIDER = "llm"
INGEST_PROVIDER = "upload"

_ACTIVE_ATTEMPT = (AttemptStatus.CLAIMED, AttemptStatus.RUNNING)
_LIVE_SEGMENT = (AudioUploadStatus.PENDING, AudioUploadStatus.UPLOADING, AudioUploadStatus.READY)
_RETRYABLE_SQLSTATES = {"40P01", "40001"}  # deadlock detected, serialization failure


class TaskNotFound(Exception):
    pass


class ConcurrentTransition(Exception):
    """The task changed under us or the transaction lost a deadlock: roll back and run it again."""


async def load_snapshot(conn: AsyncConnection, task_id: UUID, *, lock: bool = True) -> TaskSnapshot:
    t = meeting_tasks.c
    query = select(
        t.id,
        t.task_kind,
        t.status,
        t.lock_version,
        t.attempt_count,
        t.max_attempts,
        t.scheduled_at,
        t.planned_end_at,
        (t.meeting_code.is_not(None) | t.join_link_ciphertext.is_not(None)).label("has_join_info"),
        t.recording_outcome,
        t.dispatch_deadline_at,
        t.next_retry_at,
        t.upload_deadline_at,
        t.actual_joined_at,
        t.max_record_minutes,
    ).where(t.id == task_id)
    if lock:
        query = query.with_for_update()
    row = (await conn.execute(query)).one_or_none()
    if row is None:
        raise TaskNotFound(task_id)

    a, at = audio_assets.c, task_attempts.c
    registered = exists().where(a.task_id == task_id, a.upload_status.in_(_LIVE_SEGMENT))
    sealed = exists().where(
        at.task_id == task_id, at.last_sealed_segment_no.is_not(None), at.status != AttemptStatus.CANCELED
    )
    ready = exists().where(a.task_id == task_id, a.upload_status == AudioUploadStatus.READY)
    active = exists().where(at.task_id == task_id, at.status.in_(_ACTIVE_ATTEMPT))
    facts = (
        await conn.execute(
            select(
                or_(registered, sealed).label("has_audio"),
                ready.label("has_ready_audio"),
                active.label("has_active_attempt"),
            )
        )
    ).one()

    return TaskSnapshot(
        id=row.id,
        task_kind=TaskKind(row.task_kind),
        status=TaskStatus(row.status),
        lock_version=row.lock_version,
        attempt_count=row.attempt_count,
        max_attempts=row.max_attempts,
        scheduled_at=row.scheduled_at,
        planned_end_at=row.planned_end_at,
        has_join_info=row.has_join_info,
        recording_outcome=RecordingOutcome(row.recording_outcome) if row.recording_outcome else None,
        dispatch_deadline_at=row.dispatch_deadline_at,
        next_retry_at=row.next_retry_at,
        upload_deadline_at=row.upload_deadline_at,
        actual_joined_at=row.actual_joined_at,
        max_record_minutes=row.max_record_minutes,
        has_audio=facts.has_audio,
        has_ready_audio=facts.has_ready_audio,
        has_active_attempt=facts.has_active_attempt,
    )


async def apply_trigger(
    conn: AsyncConnection, task_id: UUID, trigger: Trigger, *, now: datetime, actor: str
) -> Transition:
    """Decide and persist inside the caller's transaction. LEASE_CLAIMED belongs to the lease service,
    which inserts the attempt in the same transaction and calls persist_transition itself."""
    if trigger.type == TriggerType.LEASE_CLAIMED:
        raise ValueError("LEASE_CLAIMED is persisted by the lease service together with the attempt")
    snapshot = await load_snapshot(conn, task_id)
    transition = decide(snapshot, trigger, now)
    await persist_transition(conn, snapshot, trigger, transition, now=now, actor=actor)
    return transition


async def persist_transition(
    conn: AsyncConnection,
    snapshot: TaskSnapshot,
    trigger: Trigger,
    transition: Transition,
    *,
    now: datetime,
    actor: str,
) -> None:
    """Write a decided transition; raises ConcurrentTransition if the task changed since `snapshot`."""
    task_id = snapshot.id
    t = meeting_tasks.c
    values = dict(transition.updates)
    if transition.to_status in TERMINAL_STATUSES:
        # Retention starts when the task ends; an earlier deletion request (a sooner date) is kept.
        values["retention_until"] = func.least(
            func.coalesce(t.retention_until, now + CONTENT_RETENTION), now + CONTENT_RETENTION
        )
    written = await conn.execute(
        update(meeting_tasks)
        .where(t.id == task_id, t.status == snapshot.status, t.lock_version == snapshot.lock_version)
        .values(status=transition.to_status, lock_version=snapshot.lock_version + 1, **values)
        .returning(t.id)
    )
    if written.first() is None:
        raise ConcurrentTransition(task_id)

    for effect in transition.effects:
        await _apply_effect(conn, task_id, effect, transition, trigger, now)

    await conn.execute(
        insert(task_events).values(
            task_id=task_id,
            event_type=ServerEventType.STATE_CHANGED,
            actor=actor,
            data={
                "from": transition.from_status,
                "to": transition.to_status,
                "trigger": trigger.type,
                "reason": transition.reason,
                "effects": list(transition.effects),
            },
        )
    )


_JOB_FOR_EFFECT = {
    Effect.ENQUEUE_ASR: (ProcessingJobKind.ASR, ASR_PROVIDER),
    Effect.ENQUEUE_SUMMARY: (ProcessingJobKind.SUMMARY, SUMMARY_PROVIDER),
    Effect.ENQUEUE_INGEST: (ProcessingJobKind.AUDIO_NORMALIZE, INGEST_PROVIDER),
}


async def _apply_effect(
    conn: AsyncConnection,
    task_id: UUID,
    effect: Effect,
    transition: Transition,
    trigger: Trigger,
    now: datetime,
) -> None:
    at, a, j = task_attempts.c, audio_assets.c, processing_jobs.c
    match effect:
        case Effect.END_ACTIVE_ATTEMPT | Effect.EXPIRE_ACTIVE_ATTEMPT:
            target = AttemptStatus.FINISHED if effect == Effect.END_ACTIVE_ATTEMPT else AttemptStatus.EXPIRED
            await conn.execute(
                update(task_attempts)
                .where(at.task_id == task_id, at.status.in_(_ACTIVE_ATTEMPT))
                .values(status=target, ended_at=now)
            )
        case Effect.CANCEL_ATTEMPTS:
            # Includes FINISHED/EXPIRED/FAILED attempts so their pending late uploads are refused too.
            await conn.execute(
                update(task_attempts)
                .where(at.task_id == task_id, at.status != AttemptStatus.CANCELED)
                .values(status=AttemptStatus.CANCELED, ended_at=func.coalesce(at.ended_at, now))
            )
        case Effect.FAIL_UNFINISHED_SEGMENTS:
            await conn.execute(
                update(audio_assets)
                .where(
                    a.task_id == task_id,
                    a.upload_status.in_((AudioUploadStatus.PENDING, AudioUploadStatus.UPLOADING)),
                )
                .values(upload_status=AudioUploadStatus.FAILED)
            )
        case Effect.CANCEL_PROCESSING_JOBS:
            await conn.execute(
                update(processing_jobs)
                .where(
                    j.task_id == task_id,
                    j.status.in_((ProcessingJobStatus.PENDING, ProcessingJobStatus.SUBMITTED)),
                )
                .values(status=ProcessingJobStatus.FAILED, last_error="canceled")
            )
        case Effect.DELETE_RESULTS:
            await conn.execute(delete(meeting_minutes).where(meeting_minutes.c.task_id == task_id))
            await conn.execute(delete(transcripts).where(transcripts.c.task_id == task_id))
        case Effect.ENQUEUE_ASR | Effect.ENQUEUE_SUMMARY | Effect.ENQUEUE_INGEST:
            kind, provider = _JOB_FOR_EFFECT[effect]
            next_version = (
                select(func.coalesce(func.max(j.input_version), 0) + 1)
                .where(j.task_id == task_id, j.kind == kind)
                .scalar_subquery()
            )
            await conn.execute(
                insert(processing_jobs).from_select(
                    ["task_id", "kind", "provider", "input_version", "next_poll_at", "created_at"],
                    select(
                        literal(task_id),
                        literal(kind.value),
                        literal(provider),
                        next_version,
                        literal(now),
                        literal(now),
                    ),
                )
            )
        case Effect.DISCARD_AUDIO | Effect.NOTIFY_USER:
            payload = {"to": transition.to_status, "reason": transition.reason, "trigger": trigger.type}
            # DISCARD_AUDIO runs again once every upload URL issued before now has expired, catching a
            # PUT that was still in flight when the first deletion ran.
            runs = (
                [now] if effect == Effect.NOTIFY_USER else [now, now + UPLOAD_URL_TTL + timedelta(minutes=5)]
            )
            for at in runs:
                await conn.execute(
                    insert(task_effects).values(
                        task_id=task_id,
                        effect=TaskEffectKind(effect.value),
                        payload=payload,
                        next_attempt_at=at,
                    )
                )
        case _:
            raise AssertionError(f"unhandled effect {effect}")


def is_retryable_db_error(exc: BaseException) -> bool:
    """Deadlocks and serialization failures abort the transaction; the whole unit can be retried."""
    if not isinstance(exc, DBAPIError):
        return False
    for candidate in (exc.orig, getattr(exc.orig, "__cause__", None)):
        if getattr(candidate, "sqlstate", None) in _RETRYABLE_SQLSTATES:
            return True
    return False


async def in_transaction_with_retry[T](
    engine: AsyncEngine, work: Callable[[AsyncConnection], Awaitable[T]], *, attempts: int = 3
) -> T:
    """Run `work` in its own transaction; retry on ConcurrentTransition, deadlock or serialization failure."""
    for attempt in range(1, attempts + 1):
        try:
            async with engine.begin() as conn:
                return await work(conn)
        except ConcurrentTransition:
            if attempt == attempts:
                raise
        except DBAPIError as exc:
            if not is_retryable_db_error(exc) or attempt == attempts:
                raise
        await asyncio.sleep(0.05 * attempt)
    raise AssertionError("unreachable")


async def run_trigger(
    engine: AsyncEngine, task_id: UUID, trigger: Trigger, *, now: datetime, actor: str
) -> Transition:
    return await in_transaction_with_retry(
        engine, lambda conn: apply_trigger(conn, task_id, trigger, now=now, actor=actor)
    )
