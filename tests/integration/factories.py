"""Builders for rows that satisfy every schema CHECK, used by the integration tests."""

import uuid
from datetime import UTC, datetime, timedelta

from meeting_protocol import AttemptStatus, TaskKind, TaskStatus
from meeting_protocol.rules import IN_ATTEMPT_STATUSES, INTAKE_STATUSES
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

NOW = datetime(2026, 10, 10, 15, 0, tzinfo=UTC)


async def make_user(conn: AsyncConnection) -> uuid.UUID:
    return await conn.scalar(
        text("INSERT INTO kf_users (corp_id, external_userid) VALUES ('c', :u) RETURNING id"),
        {"u": uuid.uuid4().hex},
    )


async def make_task(
    conn: AsyncConnection,
    status: TaskStatus,
    *,
    kind: TaskKind = TaskKind.LIVE_MEETING,
    has_audio: bool = False,
    audio: str = "both",  # with has_audio: "sealed" (attempt only), "registered" (segment only) or "both"
    ready_segment: bool = False,
    attempt_status: AttemptStatus
    | None = None,  # active attempt; default CLAIMED in JOINING, RUNNING otherwise
    active_attempt: bool | None = None,  # default: whenever the status implies a Worker in the meeting
    now: datetime = NOW,
) -> uuid.UUID:
    live = kind == TaskKind.LIVE_MEETING
    intake = status in INTAKE_STATUSES
    if active_attempt is None:
        active_attempt = status in IN_ATTEMPT_STATUSES
    row = {
        "user_id": await make_user(conn),
        "kind": kind.value,
        "key": f"kf:test:{uuid.uuid4().hex}",
        "code": "123456789" if live else None,
        "consent_at": None if intake else now - timedelta(days=1),
        "consent_version": None if intake else "v1",
        "scheduled_at": now - timedelta(minutes=5) if live else None,
        "planned_end_at": now + timedelta(hours=1) if live else None,
        "status": status.value,
        "failure_code": {TaskStatus.FAILED: "INTERNAL_ERROR", TaskStatus.RETRY_WAIT: "WORKER_LOST"}.get(
            status
        ),
        "next_retry_at": now if status == TaskStatus.RETRY_WAIT else None,
        "dispatch_deadline_at": now if status == TaskStatus.DISPATCHING else None,
        "upload_deadline_at": now if status == TaskStatus.UPLOADING else None,
        "approval": now - timedelta(minutes=1) if status == TaskStatus.RECORDING else None,
        "attempt_count": (1 if (has_audio or ready_segment) and live else 0) + (1 if active_attempt else 0),
    }
    task_id = await conn.scalar(
        text(
            """
            INSERT INTO meeting_tasks (user_id, task_kind, idempotency_key, meeting_code, consent_at, consent_version,
              scheduled_at, planned_end_at, status, failure_code, next_retry_at, dispatch_deadline_at,
              upload_deadline_at, host_recording_approval_at, attempt_count, max_record_minutes)
            VALUES (:user_id, :kind, :key, :code, :consent_at, :consent_version, :scheduled_at, :planned_end_at, :status,
              :failure_code, :next_retry_at, :dispatch_deadline_at, :upload_deadline_at, :approval, :attempt_count, 1)
            RETURNING id
            """
        ),
        row,
    )
    attempt_no = 0
    if has_audio or ready_segment:
        old_attempt = None
        if live:
            attempt_no += 1
            sealed = 1 if has_audio and audio in ("sealed", "both") else None
            old_attempt = await make_attempt(
                conn, task_id, attempt_no, AttemptStatus.EXPIRED, now, sealed=sealed
            )
        if has_audio and audio in ("registered", "both"):
            await make_segment(conn, task_id, old_attempt, 0, "PENDING", now)
        if ready_segment:
            await make_segment(conn, task_id, old_attempt, 1, "READY", now)
    if active_attempt:
        default = AttemptStatus.CLAIMED if status == TaskStatus.JOINING else AttemptStatus.RUNNING
        await make_attempt(conn, task_id, attempt_no + 1, attempt_status or default, now)
    return task_id


async def make_attempt(conn, task_id, attempt_no, status, now, *, sealed=None) -> uuid.UUID:
    worker_id = await conn.scalar(
        text("INSERT INTO worker_nodes (name, token_hash) VALUES (:n, 'h') RETURNING id"),
        {"n": uuid.uuid4().hex},
    )
    active = status in (AttemptStatus.CLAIMED, AttemptStatus.RUNNING)
    return await conn.scalar(
        text(
            """
            INSERT INTO task_attempts (task_id, worker_id, attempt_no, lease_until, status, ended_at,
              last_sealed_segment_no)
            VALUES (:t, :w, :n, :lease, :s, :ended, :sealed) RETURNING id
            """
        ),
        {
            "t": task_id,
            "w": worker_id,
            "n": attempt_no,
            "lease": now + timedelta(minutes=1),
            "s": status.value,
            "ended": None if active else now - timedelta(minutes=10),
            "sealed": sealed,
        },
    )


async def make_segment(conn, task_id, attempt_id, segment_no, upload_status, now) -> None:
    await conn.execute(
        text(
            """
            INSERT INTO audio_assets (task_id, source, attempt_id, segment_no, object_key, file_sha256, format,
              bytes, recorded_started_at, recorded_ended_at, retention_until, upload_status, verified_at)
            VALUES (:t, :src, :a, :n, :k, :h, 'wav', 1000, :rs, :re, :ret, :st, :ver)
            """
        ),
        {
            "t": task_id,
            "src": "worker" if attempt_id else "user_upload",
            "a": attempt_id,
            "n": segment_no,
            "k": f"tasks/{task_id}/audio/{uuid.uuid4().hex}",
            "h": "0" * 64,
            "rs": now - timedelta(minutes=20) if attempt_id else None,
            "re": now - timedelta(minutes=19) if attempt_id else None,
            "ret": now + timedelta(days=30),
            "st": upload_status,
            "ver": now if upload_status == "READY" else None,
        },
    )


async def make_bystander(conn: AsyncConnection) -> uuid.UUID:
    """Another user's task with every kind of child row; effects on a different task must leave it alone."""
    task_id = await make_task(conn, TaskStatus.RECORDING, has_audio=True, ready_segment=True)
    await conn.execute(
        text(
            """
            INSERT INTO processing_jobs (task_id, kind, provider, status, external_job_id, submitted_at)
            VALUES (:t, 'ASR', 'tencent_asr', 'PENDING', NULL, NULL),
                   (:t, 'SUMMARY', 'llm', 'SUBMITTED', 'job-1', :now)
            """
        ),
        {"t": task_id, "now": NOW},
    )
    transcript_id = await conn.scalar(
        text("INSERT INTO transcripts (task_id, source, body) VALUES (:t, 'worker_asr', '[]') RETURNING id"),
        {"t": task_id},
    )
    await conn.execute(
        text("INSERT INTO meeting_minutes (task_id, transcript_id, summary) VALUES (:t, :tr, 's')"),
        {"t": task_id, "tr": transcript_id},
    )
    return task_id


async def fingerprint(conn: AsyncConnection, task_id: uuid.UUID) -> tuple:
    """Everything an effect could touch for one task."""
    queries = [
        "SELECT status, lock_version FROM meeting_tasks WHERE id = :t",
        "SELECT attempt_no, status FROM task_attempts WHERE task_id = :t ORDER BY attempt_no",
        "SELECT segment_no, upload_status FROM audio_assets WHERE task_id = :t ORDER BY segment_no",
        "SELECT kind, status FROM processing_jobs WHERE task_id = :t ORDER BY kind",
        "SELECT count(*) FROM transcripts WHERE task_id = :t",
        "SELECT count(*) FROM meeting_minutes WHERE task_id = :t",
        "SELECT count(*) FROM task_effects WHERE task_id = :t",
        "SELECT count(*) FROM task_events WHERE task_id = :t",
    ]
    out = []
    for q in queries:
        out.append(tuple(tuple(r) for r in (await conn.execute(text(q), {"t": task_id})).all()))
    return tuple(out)
