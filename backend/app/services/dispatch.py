"""Dispatching and the Worker side of the protocol (design doc §5.2): lease, heartbeat, events, lease reaping
and the scheduler tick.

Global lock order: worker_nodes row, then meeting_tasks row, then task_attempts rows (task_transitions
documents the task -> attempt part). Lease and heartbeat take the worker row first, so one Worker's calls
are serialized; events and the reaper never lock a worker row.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from meeting_protocol import (
    AttemptStatus,
    ConflictReason,
    FailureCode,
    HeartbeatCommand,
    ProcessingJobKind,
    ProcessingJobStatus,
    TaskStatus,
    WorkerEventType,
)
from meeting_protocol.rules import IN_ATTEMPT_STATUSES
from meeting_protocol.worker_api import TaskAssignment, WorkerEvent
from sqlalchemy import func, insert, literal_column, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ..domain.state_machine import (
    ATTEMPT_FAILURE_CODES_BY_STATUS,
    DISPATCH_LEAD,
    RECORDING_OVERRUN,
    InvalidTransition,
    Trigger,
    TriggerType,
    decide,
    trigger_for_worker_event,
)
from ..models.tables import meeting_tasks, processing_jobs, task_attempts, task_events, worker_nodes
from ..security.keyring import Keyring, KeyringError
from .task_transitions import in_transaction_with_retry, load_snapshot, persist_transition
from .uploads import apply_and_settle, maybe_finish_upload
from .worker_errors import UploadRejected, WorkerConflict

log = logging.getLogger(__name__)

INTAKE_TTL = timedelta(hours=48)  # matches the WeChat KF reply window
LEASE_DURATION = timedelta(seconds=60)
_ACTIVE = (AttemptStatus.CLAIMED, AttemptStatus.RUNNING)
_MAX_UNREADABLE_SKIPS = 5


@dataclass(frozen=True)
class Lease:
    attempt_id: UUID
    attempt_no: int
    lease_until: datetime
    last_event_seq: int
    last_sealed_segment_no: int | None
    task: TaskAssignment


def credential_aad(task_id: UUID, column: str) -> bytes:
    """Binds a ciphertext to its row and column (see Keyring.encrypt)."""
    return f"meeting_tasks.{column}:{task_id}".encode()


def _attempt_conflict(status: str) -> ConflictReason:
    return {
        AttemptStatus.EXPIRED: ConflictReason.EXPIRED,
        AttemptStatus.CANCELED: ConflictReason.CANCELED,
    }.get(AttemptStatus(status), ConflictReason.FINISHED)


# ---------------------------------------------------------------- lease


async def lease(conn: AsyncConnection, worker_id: UUID, *, now: datetime, keyring: Keyring) -> Lease | None:
    """Give the Worker its current attempt again (a lost response must not burn an attempt), or claim the
    oldest dispatchable task. Returns None when there is nothing to do."""
    w = worker_nodes.c
    worker = (
        await conn.execute(select(w.status, w.revoked_at).where(w.id == worker_id).with_for_update())
    ).one_or_none()
    if worker is None or worker.revoked_at is not None or worker.status == "DISABLED":
        raise WorkerConflict(ConflictReason.NOT_OWNER, "worker unknown, revoked or disabled")
    await conn.execute(
        update(worker_nodes).where(w.id == worker_id).values(last_heartbeat_at=now, status="ONLINE")
    )

    at, t = task_attempts.c, meeting_tasks.c
    lease_until = now + LEASE_DURATION
    # Conditional renewal: an attempt the reaper expired meanwhile is not handed out again.
    renewed = (
        await conn.execute(
            update(task_attempts)
            .where(at.worker_id == worker_id, at.status.in_(_ACTIVE))
            .values(lease_until=lease_until)
            .returning(at.id, at.attempt_no, at.task_id, at.last_event_seq, at.last_sealed_segment_no)
        )
    ).one_or_none()
    if renewed is not None:
        return Lease(
            renewed.id,
            renewed.attempt_no,
            lease_until,
            renewed.last_event_seq,
            renewed.last_sealed_segment_no,
            await _assignment(conn, renewed.task_id, keyring),
        )

    for _ in range(_MAX_UNREADABLE_SKIPS):
        task_id = await conn.scalar(
            select(t.id)
            .where(
                t.status == TaskStatus.DISPATCHING,
                t.attempt_count < t.max_attempts,
                t.cancel_requested_at.is_(None),
            )
            .order_by(t.scheduled_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if task_id is None:
            return None
        try:
            assignment = await _assignment(conn, task_id, keyring)  # before any attempt exists
        except KeyringError:
            log.error("task %s: meeting credentials cannot be decrypted; failing it", task_id)
            snapshot = await load_snapshot(conn, task_id)
            trigger = Trigger(TriggerType.DISPATCH_FAILED)
            await persist_transition(
                conn, snapshot, trigger, decide(snapshot, trigger, now), now=now, actor="lease"
            )
            continue

        snapshot = await load_snapshot(conn, task_id)
        trigger = Trigger(TriggerType.LEASE_CLAIMED)
        transition = decide(snapshot, trigger, now)
        attempt_no = (
            await conn.scalar(select(func.coalesce(func.max(at.attempt_no), 0)).where(at.task_id == task_id))
        ) + 1
        attempt_id = await conn.scalar(
            insert(task_attempts)
            .values(task_id=task_id, worker_id=worker_id, attempt_no=attempt_no, lease_until=lease_until)
            .returning(at.id)
        )
        await persist_transition(conn, snapshot, trigger, transition, now=now, actor=f"worker:{worker_id}")
        return Lease(attempt_id, attempt_no, lease_until, 0, None, assignment)
    return None


async def _assignment(conn: AsyncConnection, task_id: UUID, keyring: Keyring) -> TaskAssignment:
    t = meeting_tasks.c
    row = (
        await conn.execute(
            select(
                t.meeting_code,
                t.join_link_ciphertext,
                t.meeting_password_ciphertext,
                t.display_name,
                t.scheduled_at,
                t.planned_end_at,
                t.max_record_minutes,
            ).where(t.id == task_id)
        )
    ).one()

    def _open(ciphertext: bytes | None, column: str) -> str | None:
        return keyring.decrypt_text(ciphertext, aad=credential_aad(task_id, column)) if ciphertext else None

    return TaskAssignment(
        task_id=task_id,
        meeting_code=row.meeting_code,
        join_url=_open(row.join_link_ciphertext, "join_link_ciphertext"),
        meeting_password=_open(row.meeting_password_ciphertext, "meeting_password_ciphertext"),
        display_name=row.display_name,
        scheduled_at=row.scheduled_at,
        planned_end_at=row.planned_end_at,
        max_record_minutes=row.max_record_minutes,
    )


# ---------------------------------------------------------------- heartbeat


async def heartbeat(
    conn: AsyncConnection, worker_id: UUID, attempt_id: UUID, *, now: datetime
) -> tuple[datetime, HeartbeatCommand]:
    at, t = task_attempts.c, meeting_tasks.c
    await conn.execute(
        update(worker_nodes).where(worker_nodes.c.id == worker_id).values(last_heartbeat_at=now)
    )
    row = (
        await conn.execute(
            select(at.worker_id, at.status, t.status.label("task_status"))
            .join(meeting_tasks, t.id == at.task_id)
            .where(at.id == attempt_id)
            .with_for_update(of=task_attempts)
        )
    ).one_or_none()
    if row is None or row.worker_id != worker_id:
        raise WorkerConflict(ConflictReason.NOT_OWNER)
    if row.status not in _ACTIVE:
        raise WorkerConflict(_attempt_conflict(row.status))

    lease_until = now + LEASE_DURATION
    await conn.execute(
        update(task_attempts).where(at.id == attempt_id).values(lease_until=lease_until, heartbeat_at=now)
    )
    # Stop / limit / meeting end moved the task to UPLOADING while the Worker may still be recording.
    in_meeting = row.task_status in IN_ATTEMPT_STATUSES
    return lease_until, HeartbeatCommand.CONTINUE if in_meeting else HeartbeatCommand.STOP_AND_UPLOAD


# ---------------------------------------------------------------- events


def _normalized_trigger(event: WorkerEvent, task_status: TaskStatus) -> Trigger | None:
    """A failure code that cannot happen in the current stage becomes INTERNAL_ERROR (non-retryable), so a
    misreported host refusal is never retried. The original code stays in the stored event."""
    trigger = trigger_for_worker_event(event.type, event.failure_code)
    if (
        trigger is None
        or trigger.type != TriggerType.ATTEMPT_FAILED
        or task_status not in IN_ATTEMPT_STATUSES
    ):
        return trigger
    if trigger.failure_code not in ATTEMPT_FAILURE_CODES_BY_STATUS[task_status]:
        return Trigger(TriggerType.ATTEMPT_FAILED, FailureCode.INTERNAL_ERROR)
    return trigger


async def record_worker_event(
    conn: AsyncConnection, worker_id: UUID, attempt_id: UUID, event: WorkerEvent, *, now: datetime
) -> bool:
    """Store one Worker event and apply its trigger. Returns False for an already-acknowledged seq.

    Order: find the task, lock its row, then the attempt row, so this never deadlocks with transitions or
    the reaper. Everything is validated before the seq is consumed.
    """
    at, t, e = task_attempts.c, meeting_tasks.c, task_events.c
    task_id = await conn.scalar(select(at.task_id).where(at.id == attempt_id))
    if task_id is None:
        raise WorkerConflict(ConflictReason.NOT_OWNER)
    task = (
        await conn.execute(
            select(t.status, t.host_recording_approval_at).where(t.id == task_id).with_for_update()
        )
    ).one()
    attempt = (
        await conn.execute(
            select(at.worker_id, at.status, at.last_event_seq, at.last_sealed_segment_no)
            .where(at.id == attempt_id)
            .with_for_update()
        )
    ).one()
    if attempt.worker_id != worker_id:
        raise WorkerConflict(ConflictReason.NOT_OWNER)
    if event.seq <= attempt.last_event_seq:
        stored = await conn.scalar(
            select(e.event_type).where(e.attempt_id == attempt_id, e.event_seq == event.seq)
        )
        if stored == event.type:
            return False  # retransmission of an event we already have
        raise WorkerConflict(
            ConflictReason.OUT_OF_ORDER,
            "seq already used by another event",
            expected_seq=attempt.last_event_seq + 1,
        )
    if attempt.status not in _ACTIVE:
        raise WorkerConflict(_attempt_conflict(attempt.status))
    if event.seq != attempt.last_event_seq + 1:
        raise WorkerConflict(
            ConflictReason.OUT_OF_ORDER, "events missing", expected_seq=attempt.last_event_seq + 1
        )

    task_status = TaskStatus(task.status)
    attempt_updates: dict = {"last_event_seq": event.seq, "status": AttemptStatus.RUNNING}
    if event.type == WorkerEventType.SEGMENT_SEALED:
        recording = task_status == TaskStatus.RECORDING or (
            task_status == TaskStatus.UPLOADING and task.host_recording_approval_at is not None
        )
        if not recording:
            raise UploadRejected("segments can only be sealed while recording with approval")
        segment_no = int(event.data["segment_no"])
        expected = (attempt.last_sealed_segment_no if attempt.last_sealed_segment_no is not None else -1) + 1
        if segment_no != expected:
            raise UploadRejected(f"segments are numbered from 0 without gaps; expected {expected}")
        attempt_updates["last_sealed_segment_no"] = segment_no

    await conn.execute(
        insert(task_events).values(
            task_id=task_id,
            attempt_id=attempt_id,
            event_seq=event.seq,
            event_type=event.type,
            actor=f"worker:{worker_id}",
            data={
                **event.data,
                "failure_code": event.failure_code,
                "occurred_at": event.occurred_at.isoformat(),
            },
        )
    )
    await conn.execute(update(task_attempts).where(at.id == attempt_id).values(**attempt_updates))

    trigger = _normalized_trigger(event, task_status)
    if event.type == WorkerEventType.LEFT:
        if task_status in IN_ATTEMPT_STATUSES:
            # Left without saying why: treat as a client failure (retryable) rather than a silent end.
            trigger = Trigger(TriggerType.ATTEMPT_FAILED, FailureCode.CLIENT_ERROR)
        else:
            await conn.execute(
                update(task_attempts)
                .where(at.id == attempt_id, at.status.in_(_ACTIVE))
                .values(status=AttemptStatus.FINISHED, ended_at=now)
            )
            await maybe_finish_upload(conn, task_id, now=now)
    if trigger is not None:
        try:
            await apply_and_settle(conn, task_id, trigger, now=now, actor=f"worker:{worker_id}")
        except InvalidTransition as exc:
            # Late or redundant event (e.g. MEETING_ENDED after the user pressed stop): stored, not applied.
            log.info("worker event %s ignored for task %s: %s", event.type, task_id, exc)
    return True


# ---------------------------------------------------------------- reaper and scheduler


async def reap_expired_leases(engine: AsyncEngine, *, now: datetime) -> int:
    at = task_attempts.c
    async with engine.connect() as conn:
        candidates = (
            await conn.execute(select(at.id, at.task_id).where(at.status.in_(_ACTIVE), at.lease_until < now))
        ).all()
    reaped = 0
    for attempt_id, task_id in candidates:
        try:
            reaped += await in_transaction_with_retry(
                engine, lambda conn, a=attempt_id, tid=task_id: _reap_one(conn, a, tid, now)
            )
        except Exception as exc:
            _skip(task_id, "reap", exc)
    return reaped


def _skip(task_id: UUID, what: str, exc: Exception) -> None:
    """One task that cannot be moved (a bug, bad data) must not stop the pass for every other task: it is
    logged and looked at again next tick."""
    log.error("scheduler: %s of task %s failed: %s", what, task_id, type(exc).__name__)


async def _reap_one(conn: AsyncConnection, attempt_id: UUID, task_id: UUID, now: datetime) -> int:
    at, t = task_attempts.c, meeting_tasks.c
    task_status = TaskStatus(await conn.scalar(select(t.status).where(t.id == task_id).with_for_update()))
    still_expired = await conn.scalar(
        select(at.id)
        .where(at.id == attempt_id, at.status.in_(_ACTIVE), at.lease_until < now)
        .with_for_update()
    )
    if still_expired is None:
        return 0  # a heartbeat renewed it meanwhile
    if task_status in IN_ATTEMPT_STATUSES:
        await apply_and_settle(conn, task_id, Trigger(TriggerType.LEASE_EXPIRED), now=now, actor="reaper")
    else:
        # e.g. UPLOADING after stop: only the attempt ends; the task keeps waiting for late uploads.
        await conn.execute(
            update(task_attempts)
            .where(at.id == attempt_id)
            .values(status=AttemptStatus.EXPIRED, ended_at=now)
        )
        await maybe_finish_upload(conn, task_id, now=now)
    return 1


def _minutes(column):
    return column * literal_column("interval '1 minute'")


async def scheduler_tick(engine: AsyncEngine, *, now: datetime) -> dict[str, int]:
    """One pass of the scheduling loop (design doc §4). Safe to run concurrently: every change goes through
    the task row lock and the state machine; a task that moved meanwhile is skipped."""
    t, at, pj = meeting_tasks.c, task_attempts.c, processing_jobs.c
    recording_started = func.coalesce(t.actual_joined_at, t.scheduled_at)
    limit_at = func.least(
        recording_started + _minutes(t.max_record_minutes),
        t.planned_end_at + literal_column(f"interval '{int(RECORDING_OVERRUN.total_seconds())} seconds'"),
    )
    no_active_attempt = ~select(at.id).where(at.task_id == t.id, at.status.in_(_ACTIVE)).exists()
    due = {
        TriggerType.DISPATCH_DUE: (t.status == TaskStatus.SCHEDULED)
        & (t.scheduled_at <= now + DISPATCH_LEAD),
        TriggerType.DISPATCH_DEADLINE_PASSED: (t.status == TaskStatus.DISPATCHING)
        & (t.dispatch_deadline_at <= now),
        TriggerType.RETRY_DUE: (t.status == TaskStatus.RETRY_WAIT) & (t.next_retry_at <= now),
        TriggerType.RECORDING_LIMIT_REACHED: (t.status == TaskStatus.RECORDING) & (limit_at <= now),
        # An uploaded recording still being fetched and converted is not past its deadline: its job either
        # finishes or rejects it (and waits, rather than failing it, while ffmpeg is unavailable).
        TriggerType.UPLOAD_DEADLINE_PASSED: (t.status == TaskStatus.UPLOADING)
        & (t.upload_deadline_at <= now)
        & ~select(pj.id)
        .where(
            pj.task_id == t.id,
            pj.kind == ProcessingJobKind.AUDIO_NORMALIZE,
            pj.status.in_((ProcessingJobStatus.PENDING, ProcessingJobStatus.SUBMITTED)),
        )
        .exists(),
        # An unconfirmed request whose meeting is over can never be scheduled; one without a time is
        # dropped once the user has not answered for INTAKE_TTL.
        TriggerType.INTAKE_EXPIRED: t.status.in_((TaskStatus.AWAITING_DETAILS, TaskStatus.AWAITING_CONSENT))
        & ((t.planned_end_at <= now) | (t.planned_end_at.is_(None) & (t.created_at <= now - INTAKE_TTL))),
    }
    counts = {"reaped": await reap_expired_leases(engine, now=now)}
    for trigger_type, condition in due.items():
        async with engine.connect() as conn:
            ids = (await conn.execute(select(t.id).where(condition).limit(500))).scalars().all()
        applied = 0
        for task_id in ids:
            try:
                await in_transaction_with_retry(
                    engine,
                    lambda conn, tid=task_id, tt=trigger_type: apply_and_settle(
                        conn, tid, Trigger(tt), now=now, actor="scheduler"
                    ),
                )
                applied += 1
            except InvalidTransition:
                pass  # the task moved on between the scan and the lock
            except Exception as exc:
                _skip(task_id, trigger_type.value, exc)
        counts[trigger_type.value] = applied

    # Backstop: an UPLOADING task with no Worker left whose segments are all in (any path that missed it).
    async with engine.connect() as conn:
        waiting = (
            (
                await conn.execute(
                    select(t.id).where(t.status == TaskStatus.UPLOADING, no_active_attempt).limit(500)
                )
            )
            .scalars()
            .all()
        )
    settled = 0
    for task_id in waiting:
        try:
            result = await in_transaction_with_retry(
                engine, lambda conn, tid=task_id: maybe_finish_upload(conn, tid, now=now)
            )
        except Exception as exc:
            _skip(task_id, "upload settle", exc)
            continue
        settled += result is not None
    counts["settled"] = settled
    return counts
