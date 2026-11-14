"""Segment uploads from the Worker (design doc §5.2): issue a scoped upload target, verify the object,
and move the task on once every sealed segment is in.

Fencing: an active attempt may upload segments it has sealed. A FINISHED or EXPIRED attempt may still
upload them ("late upload") while the task waits for audio, nobody asked to cancel, within LATE_UPLOAD_WINDOW
of the attempt ending. CANCELED attempts never upload.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from meeting_protocol import AttemptStatus, AudioUploadStatus, ConflictReason, TaskKind, TaskStatus
from meeting_protocol.rules import IN_ATTEMPT_STATUSES
from meeting_protocol.worker_api import SegmentCompleteRequest, SegmentUploadRequest
from sqlalchemy import and_, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from ..domain.state_machine import InvalidTransition, Trigger, TriggerType
from ..models.tables import audio_assets, meeting_tasks, task_attempts
from .task_transitions import UPLOAD_URL_TTL, apply_trigger
from .worker_errors import UploadRejected, WorkerConflict

LATE_UPLOAD_WINDOW = timedelta(hours=2)
AUDIO_RETENTION = timedelta(days=30)  # product default, design doc §10
SILENCE_DBFS = -60.0  # every READY segment quieter than this: the meeting audio is empty

_LATE_UPLOAD_TASK_STATUSES = IN_ATTEMPT_STATUSES | {
    TaskStatus.RETRY_WAIT,
    TaskStatus.DISPATCHING,
    TaskStatus.UPLOADING,
}


@dataclass(frozen=True)
class ObjectStat:
    bytes: int
    sha256: str | None  # from object metadata written by the uploader


@dataclass(frozen=True)
class UploadTarget:
    object_key: str
    url: str
    expires_at: datetime
    headers: dict[str, str] = field(default_factory=dict)  # the PUT must carry these (they are signed)


class ObjectStorage(Protocol):
    async def presign_put(
        self, object_key: str, *, size: int, sha256: str, expires_at: datetime
    ) -> UploadTarget: ...

    async def presign_get(self, object_key: str, *, expires_at: datetime) -> str: ...

    async def stat(self, object_key: str) -> ObjectStat | None: ...

    async def delete(self, object_key: str) -> None: ...

    async def delete_prefix(self, prefix: str) -> None:
        """Delete every object under prefix and abort unfinished multipart uploads there."""
        ...


@dataclass(frozen=True)
class _Attempt:
    task_id: UUID
    attempt_no: int
    worker_id: UUID
    status: AttemptStatus
    ended_at: datetime | None
    last_sealed_segment_no: int | None


async def _fence(
    conn: AsyncConnection, worker_id: UUID, attempt_id: UUID, segment_no: int, *, now: datetime
) -> _Attempt:
    """Lock task then attempt, and apply the upload rules. Raises WorkerConflict or UploadRejected."""
    at, t = task_attempts.c, meeting_tasks.c
    task_id = await conn.scalar(select(at.task_id).where(at.id == attempt_id))
    if task_id is None:
        raise WorkerConflict(ConflictReason.NOT_OWNER)
    task = (
        await conn.execute(select(t.status, t.cancel_requested_at).where(t.id == task_id).with_for_update())
    ).one()
    row = (
        await conn.execute(
            select(at.task_id, at.attempt_no, at.worker_id, at.status, at.ended_at, at.last_sealed_segment_no)
            .where(at.id == attempt_id)
            .with_for_update()
        )
    ).one()
    attempt = _Attempt(
        row.task_id,
        row.attempt_no,
        row.worker_id,
        AttemptStatus(row.status),
        row.ended_at,
        row.last_sealed_segment_no,
    )
    if attempt.worker_id != worker_id:
        raise WorkerConflict(ConflictReason.NOT_OWNER)

    match attempt.status:
        case AttemptStatus.CLAIMED | AttemptStatus.RUNNING:
            pass
        case AttemptStatus.FINISHED | AttemptStatus.EXPIRED:
            reason = (
                ConflictReason.FINISHED
                if attempt.status == AttemptStatus.FINISHED
                else ConflictReason.EXPIRED
            )
            late_ok = (
                TaskStatus(task.status) in _LATE_UPLOAD_TASK_STATUSES
                and task.cancel_requested_at is None
                and attempt.ended_at is not None
                and now - attempt.ended_at <= LATE_UPLOAD_WINDOW
            )
            if not late_ok:
                raise WorkerConflict(reason, "late upload no longer accepted")
        case _:
            raise WorkerConflict(ConflictReason.CANCELED)

    if attempt.last_sealed_segment_no is None or segment_no > attempt.last_sealed_segment_no:
        raise UploadRejected(f"segment {segment_no} was not sealed (send SEGMENT_SEALED first)")
    return attempt


async def request_upload(
    conn: AsyncConnection,
    storage: ObjectStorage,
    worker_id: UUID,
    attempt_id: UUID,
    segment_no: int,
    req: SegmentUploadRequest,
    *,
    now: datetime,
) -> UploadTarget:
    if req.recorded_ended_at <= req.recorded_started_at:
        raise UploadRejected("recorded_ended_at must be after recorded_started_at")
    attempt = await _fence(conn, worker_id, attempt_id, segment_no, now=now)
    a = audio_assets.c
    existing = (
        await conn.execute(
            select(a.object_key, a.file_sha256, a.bytes, a.upload_status).where(
                a.attempt_id == attempt_id, a.segment_no == segment_no
            )
        )
    ).one_or_none()
    if existing is not None:
        if existing.file_sha256 != req.file_sha256 or existing.bytes != req.bytes:
            raise UploadRejected("segment already registered with a different hash or size")
        if existing.upload_status == AudioUploadStatus.READY:
            raise UploadRejected("segment already uploaded")
        object_key = existing.object_key
        await conn.execute(
            update(audio_assets)
            .where(a.attempt_id == attempt_id, a.segment_no == segment_no)
            .values(upload_status=AudioUploadStatus.UPLOADING)
        )
    else:
        object_key = f"tasks/{attempt.task_id}/audio/{attempt.attempt_no:02d}-{segment_no:05d}.{req.format}"
        await conn.execute(
            insert(audio_assets).values(
                task_id=attempt.task_id,
                source="worker",
                attempt_id=attempt_id,
                segment_no=segment_no,
                object_key=object_key,
                file_sha256=req.file_sha256,
                format=req.format,
                duration_ms=req.duration_ms,
                bytes=req.bytes,
                recorded_started_at=req.recorded_started_at,
                recorded_ended_at=req.recorded_ended_at,
                upload_status=AudioUploadStatus.UPLOADING,
                retention_until=now + AUDIO_RETENTION,
            )
        )
    return await storage.presign_put(
        object_key, size=req.bytes, sha256=req.file_sha256, expires_at=now + UPLOAD_URL_TTL
    )


async def complete_upload(
    conn_factory,
    storage: ObjectStorage,
    worker_id: UUID,
    attempt_id: UUID,
    segment_no: int,
    req: SegmentCompleteRequest,
    *,
    now: datetime,
) -> None:
    """`conn_factory` is in_transaction_with_retry bound to an engine: the object store is asked outside any
    transaction, so a slow COS call never holds task or attempt locks."""
    a, at = audio_assets.c, task_attempts.c

    async def read(conn):
        return (
            await conn.execute(
                select(a.object_key, a.file_sha256, a.bytes, a.upload_status, at.worker_id, at.status)
                .join(task_attempts, at.id == a.attempt_id)
                .where(a.attempt_id == attempt_id, a.segment_no == segment_no)
            )
        ).one_or_none()

    row = await conn_factory(read)
    if row is None:
        raise UploadRejected("request an upload URL first")
    if row.worker_id != worker_id:
        raise WorkerConflict(ConflictReason.NOT_OWNER)
    if row.upload_status == AudioUploadStatus.READY and row.status != AttemptStatus.CANCELED:
        return  # idempotent: a retry after a lost response is not an error
    if (req.file_sha256, req.bytes) != (row.file_sha256, row.bytes):
        raise UploadRejected("completion does not match the registered segment")
    stat = await storage.stat(row.object_key)
    if stat is None or stat.bytes != row.bytes or stat.sha256 != row.file_sha256:
        raise UploadRejected("stored object does not match the declared size and hash")

    async def commit(conn):
        attempt = await _fence(conn, worker_id, attempt_id, segment_no, now=now)
        written = await conn.execute(
            update(audio_assets)
            .where(
                a.attempt_id == attempt_id,
                a.segment_no == segment_no,
                a.object_key == row.object_key,
                a.upload_status != AudioUploadStatus.DELETED,
            )
            .values(upload_status=AudioUploadStatus.READY, verified_at=now, rms_dbfs=req.rms_dbfs)
            .returning(a.id)
        )
        if written.first() is None:
            raise UploadRejected("segment changed while it was being verified")
        await maybe_finish_upload(conn, attempt.task_id, now=now)

    await conn_factory(commit)


async def maybe_finish_upload(conn: AsyncConnection, task_id: UUID, *, now: datetime) -> TaskStatus | None:
    """Once the Worker has left and every sealed segment is READY, hand the task to transcription (or fail
    it as empty). Called after each completed segment and when a Worker leaves. Caller holds the task lock.
    Uploaded recordings have no Worker and no sealing: their upload path signals completion itself, and the
    upload deadline is the backstop."""
    t, at, a = meeting_tasks.c, task_attempts.c, audio_assets.c
    task = (await conn.execute(select(t.status, t.task_kind).where(t.id == task_id).with_for_update())).one()
    if task.status != TaskStatus.UPLOADING or task.task_kind != TaskKind.LIVE_MEETING:
        return None
    active = await conn.scalar(
        select(func.count()).where(
            at.task_id == task_id, at.status.in_((AttemptStatus.CLAIMED, AttemptStatus.RUNNING))
        )
    )
    if active:
        return None
    sealed_total = await conn.scalar(
        select(func.coalesce(func.sum(at.last_sealed_segment_no + 1), 0)).where(
            at.task_id == task_id, at.status != AttemptStatus.CANCELED, at.last_sealed_segment_no.is_not(None)
        )
    )
    ready = (
        (
            await conn.execute(
                select(a.rms_dbfs)
                .join(task_attempts, and_(at.id == a.attempt_id, at.status != AttemptStatus.CANCELED))
                .where(a.task_id == task_id, a.upload_status == AudioUploadStatus.READY)
            )
        )
        .scalars()
        .all()
    )
    if len(ready) < sealed_total:
        return None  # still waiting for some sealed segment; the upload deadline is the backstop
    silent = bool(ready) and all(r is not None and r < SILENCE_DBFS for r in ready)
    trigger = TriggerType.UPLOAD_EMPTY if (silent or not ready) else TriggerType.UPLOAD_COMPLETE
    try:
        transition = await apply_trigger(conn, task_id, Trigger(trigger), now=now, actor="uploads")
    except InvalidTransition:
        return None
    return transition.to_status


async def apply_and_settle(
    conn: AsyncConnection, task_id: UUID, trigger: Trigger, *, now: datetime, actor: str
):
    """apply_trigger, then settle uploads: a task that lands in UPLOADING with no Worker left and every
    sealed segment READY moves on immediately instead of waiting for the upload deadline."""
    transition = await apply_trigger(conn, task_id, trigger, now=now, actor=actor)
    if transition.to_status == TaskStatus.UPLOADING:
        await maybe_finish_upload(conn, task_id, now=now)
    return transition
