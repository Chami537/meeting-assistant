"""Processing jobs (design doc §7.5): speech recognition for TRANSCRIBING tasks, minutes for SUMMARIZING ones.

The transition that enters a stage writes the job row (ENQUEUE_ASR / ENQUEUE_SUMMARY); the runner claims due
rows here. Calls to the providers happen outside any transaction. Their results are committed under the task
lock (task row, then job row, the global lock order) and only while the job is still active: a cancel in the
meantime has marked it FAILED and the result is dropped.

Recognition has no idempotency key and is billed per recognized second, so the attempt counter is committed
before the submit and the TaskId right after it. A restart polls SUBMITTED jobs and never resubmits them;
a PENDING job with attempts > 0 and no recorded error means a run died somewhere around the submit, which is
logged before the (possibly duplicate, at most MAX_ASR_ATTEMPTS) resubmission.
"""

import asyncio
import hashlib
import logging
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from meeting_protocol import (
    AudioUploadStatus,
    FailureCode,
    ProcessingJobKind,
    ProcessingJobStatus,
    TaskKind,
    TaskStatus,
)
from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ..domain.state_machine import InvalidTransition, Trigger, TriggerType
from ..integrations.wecom.client import MEDIA_EXPIRED, MediaTooLarge, WeComApiError
from ..models.tables import (
    audio_assets,
    meeting_minutes,
    meeting_tasks,
    processing_jobs,
    task_inputs,
    transcripts,
)
from ..security.keyring import Keyring
from ..services.task_inputs import InputKind, TaskInput, read_input
from ..services.task_inputs import consume as consume_input
from ..services.task_transitions import in_transaction_with_retry
from ..services.uploads import AUDIO_RETENTION, SILENCE_DBFS, ObjectStorage, apply_and_settle
from ..services.user_uploads import MAX_SOURCE_BYTES
from . import minutes as mm
from .audio import AudioMerger
from .media import MediaRejected, MediaTool
from .providers import (
    REPLY_TRUNCATED,
    AsrProvider,
    AsrResult,
    AsrState,
    LlmProvider,
    ProviderBusy,
    ProviderError,
)
from .summary import describe as _describe
from .summary import write_minutes

log = logging.getLogger(__name__)

CLAIM_LEASE = timedelta(minutes=20)  # covers a merge of a long meeting plus the submit
ASR_POLL_INTERVAL = timedelta(seconds=30)
ASR_POLL_DEADLINE = timedelta(hours=23)  # the provider keeps results for 24h after submission
AUDIO_URL_TTL = timedelta(hours=12)  # recognition may sit in the provider's queue for hours
BUDGET_RECHECK = timedelta(minutes=30)
BUDGET_MAX_WAIT = timedelta(hours=24)  # a job held back by the daily budget longer than this fails
# A self-hosted provider that is down, restarting or full: the job waits without using an attempt, and the
# operator sees a warning at every recheck.
BUSY_RECHECK = timedelta(minutes=2)
BUSY_MAX_WAIT = timedelta(hours=6)
DISCARDS_PER_PASS = 10
# An uploaded recording waiting for ffmpeg (or the keyring) to be set up: the operator is alerted at every
# recheck; after this long the user is told it failed rather than left waiting without an answer.
UNCONFIGURED_MAX_WAIT = timedelta(hours=6)
# Merge + submit, or the minutes calls (a thin reply is asked once more), end inside the lease.
STEP_TIMEOUT = CLAIM_LEASE - timedelta(minutes=2)
MAX_ASR_ATTEMPTS = 3
MAX_SUMMARY_ATTEMPTS = 3
MAX_INGEST_ATTEMPTS = 3
# Recognition takes up to 5 hours per file, but the minutes request fits about 4 hours of speech.
MAX_UPLOAD_AUDIO_MS = 4 * 3600 * 1000

_ACTIVE = (ProcessingJobStatus.PENDING, ProcessingJobStatus.SUBMITTED)
_TRANSCRIPT_SOURCE = {TaskKind.LIVE_MEETING: "worker_asr", TaskKind.USER_UPLOAD: "upload_asr"}


class InboundMedia(Protocol):
    async def download_media(self, media_id: str, dest: Path, *, max_bytes: int) -> object: ...


@dataclass(frozen=True)
class PostProcessing:
    asr: AsrProvider
    llm: LlmProvider
    storage: ObjectStorage
    merger: AudioMerger
    asr_daily_audio_seconds: int  # rolling 24h cap on submitted audio, design doc §7.5 [C2]
    # Fallback path (uploaded recordings): probing/conversion, WeChat media download, input decryption.
    media: MediaTool | None = None
    inbound: InboundMedia | None = None
    keyring: Keyring | None = None


@dataclass(frozen=True)
class _Job:
    id: UUID
    task_id: UUID
    kind: ProcessingJobKind
    provider: str
    status: ProcessingJobStatus
    attempts: int
    input_version: int
    external_job_id: str | None
    submitted_at: datetime | None
    last_error: str | None
    created_at: datetime


def _hopeless(exc: Exception, job: _Job) -> bool:
    """Retrying cannot help (and every try is billed): the provider refused the request, or the reply hit the
    output cap on two tries in a row (job.last_error is still the previous try's)."""
    if not isinstance(exc, ProviderError):
        return False
    if str(exc) == REPLY_TRUNCATED:
        return job.last_error == REPLY_TRUNCATED
    return exc.permanent


def _backoff(attempts: int) -> timedelta:
    return timedelta(minutes=2 ** max(attempts - 1, 0))


async def run_jobs(engine: AsyncEngine, deps: PostProcessing, *, now: datetime, limit: int = 10) -> int:
    """Claim due jobs and advance each one step. Returns the number of jobs looked at."""
    j = processing_jobs.c
    async with engine.begin() as conn:
        claimable = (
            select(j.id)
            .where(j.status.in_(_ACTIVE), j.next_poll_at <= now)
            .order_by(j.next_poll_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        rows = (
            await conn.execute(
                update(processing_jobs)
                .where(j.id.in_(claimable.scalar_subquery()))
                .values(next_poll_at=now + CLAIM_LEASE)
                .returning(
                    j.id,
                    j.task_id,
                    j.kind,
                    j.provider,
                    j.status,
                    j.attempts,
                    j.input_version,
                    j.external_job_id,
                    j.submitted_at,
                    j.last_error,
                    j.created_at,
                )
            )
        ).all()
    for row in rows:
        job = _Job(**row._mapping)
        try:
            if job.kind == ProcessingJobKind.ASR and job.status == ProcessingJobStatus.PENDING:
                await _submit_asr(engine, deps, job, now=now)
            elif job.kind == ProcessingJobKind.ASR:
                await _poll_asr(engine, deps, job, now=now)
            elif job.kind == ProcessingJobKind.SUMMARY:
                await _summarize(engine, deps, job, now=now)
            elif job.kind == ProcessingJobKind.AUDIO_NORMALIZE:
                await _ingest(engine, deps, job, now=now)
            else:
                await _close(engine, job, error=f"no handler for {job.kind}", now=now)
        except Exception:
            # Unexpected (database, bug): the claim lease brings the job back later.
            log.exception("processing job %s failed", job.id)
    return len(rows)


# ---------------------------------------------------------------- recognition


async def _submit_asr(engine: AsyncEngine, deps: PostProcessing, job: _Job, *, now: datetime) -> None:
    a, j, t = audio_assets.c, processing_jobs.c, meeting_tasks.c
    async with engine.connect() as conn:
        task = (await conn.execute(select(t.status, t.task_kind).where(t.id == job.task_id))).one()
        segments = (
            await conn.execute(
                select(a.object_key, a.duration_ms)
                .where(a.task_id == job.task_id, a.upload_status == AudioUploadStatus.READY)
                .order_by(a.recorded_started_at.asc().nulls_last(), a.segment_no)
            )
        ).all()
        used = await conn.scalar(  # the cap is per provider: what one bills says nothing about another
            select(func.coalesce(func.sum(j.billed_audio_seconds), 0)).where(
                j.kind == ProcessingJobKind.ASR,
                j.provider == deps.asr.name,
                j.submitted_at > now - timedelta(hours=24),
            )
        )
        file_name = await conn.scalar(
            select(task_inputs.c.file_name).where(task_inputs.c.task_id == job.task_id)
        )
    if task.status != TaskStatus.TRANSCRIBING:
        await _close(engine, job, error="task left TRANSCRIBING", now=now)
        return
    if not segments:
        await _fail(engine, job, FailureCode.ASR_FAILED, error="no READY audio", now=now)
        return
    if job.attempts >= MAX_ASR_ATTEMPTS:
        await _fail(engine, job, FailureCode.ASR_FAILED, error=job.last_error or "interrupted", now=now)
        return
    estimate = sum(s.duration_ms or 0 for s in segments) // 1000
    if estimate > deps.asr_daily_audio_seconds:
        await _fail(engine, job, FailureCode.ASR_FAILED, error="audio exceeds the daily ASR budget", now=now)
        return
    if used + estimate > deps.asr_daily_audio_seconds:
        if now - job.created_at >= BUDGET_MAX_WAIT:
            await _fail(engine, job, FailureCode.ASR_FAILED, error="daily budget wait exceeded", now=now)
            return
        log.warning("ASR daily budget reached (%ss used); job %s deferred", used, job.id)
        await _retry_later(engine, job, error="daily budget", at=now + BUDGET_RECHECK)
        return
    check_capacity = getattr(deps.asr, "check_capacity", None)
    if check_capacity is not None:  # before the merge and the upload, which a full queue would waste
        try:
            await check_capacity()
        except ProviderBusy as exc:
            await _wait_busy(engine, job, exc, now=now)
            return
        except ProviderError:
            pass  # the submit meets it again and reports it as a failed try
    if job.attempts > 0 and job.last_error is None:
        log.warning("ASR job %s: a previous run stopped mid-submit; it may be billed twice", job.id)
    attempts = await _start_attempt(engine, job, now=now)
    if attempts is None:
        return

    try:
        # Bounded so the submit cannot outlive the lease and be repeated by another runner.
        async with asyncio.timeout(STEP_TIMEOUT.total_seconds()):
            if len(segments) == 1:
                key = segments[0].object_key
            else:
                key = f"tasks/{job.task_id}/audio/merged-v{job.input_version}.m4a"
                await deps.merger.concat([s.object_key for s in segments], key)
            url = await deps.storage.presign_get(key, expires_at=now + AUDIO_URL_TTL)
            external_id = await deps.asr.submit(url, title=mm.meeting_title(file_name))
    except ProviderBusy as exc:  # nothing was submitted: the try is given back
        await _wait_busy(engine, job, exc, now=now, attempts=attempts - 1)
        return
    except Exception as exc:
        error = _describe(exc)
        log.warning("ASR submit for job %s failed: %s", job.id, error)
        if attempts >= MAX_ASR_ATTEMPTS:
            await _fail(engine, job, FailureCode.ASR_FAILED, error=error, now=now)
        else:
            await _retry_later(engine, job, error=error, at=now + _backoff(attempts))
        return

    async def record(conn: AsyncConnection) -> bool:
        result = await conn.execute(
            update(processing_jobs)
            .where(j.id == job.id, j.status == ProcessingJobStatus.PENDING)
            .values(
                status=ProcessingJobStatus.SUBMITTED,
                provider=deps.asr.name,  # the provider that must be polled for this task id
                external_job_id=external_id,
                submitted_at=now,
                next_poll_at=now + ASR_POLL_INTERVAL,
                billed_audio_seconds=estimate,  # provisional, replaced by the billed figure when done
                last_error=None,
            )
        )
        return result.rowcount == 1

    if not await in_transaction_with_retry(engine, record):
        log.info("ASR job %s was canceled during submit; provider task %s is abandoned", job.id, external_id)

        async def keep_id(
            conn: AsyncConnection,
        ) -> None:  # so that discard_abandoned retries a failed discard
            await conn.execute(
                update(processing_jobs)
                .where(
                    j.id == job.id,
                    j.status != ProcessingJobStatus.SUBMITTED,
                    j.external_job_id.is_(None),
                )
                .values(external_job_id=external_id, provider=deps.asr.name)
            )

        await in_transaction_with_retry(engine, keep_id)
        await _discard(engine, deps, external_id, job_id=job.id)


async def _wait_busy(
    engine: AsyncEngine, job: _Job, exc: ProviderBusy, *, now: datetime, attempts: int | None = None
) -> None:
    # Only a job already waiting on a busy provider gives up: an old job (it waited on the budget, or was lost
    # and resubmitted) still gets the full wait the first time.
    if job.last_error == "provider busy" and now - job.created_at >= BUSY_MAX_WAIT:
        log.error("ASR job %s gave up: provider still busy (%s)", job.id, exc)
        await _fail(engine, job, FailureCode.ASR_FAILED, error="provider busy too long", now=now)
        return
    log.warning("ASR provider busy (%s); job %s waits", exc, job.id)
    await _retry_later(engine, job, error="provider busy", at=now + BUSY_RECHECK, attempts=attempts)


async def _poll_asr(engine: AsyncEngine, deps: PostProcessing, job: _Job, *, now: datetime) -> None:
    assert job.external_job_id is not None and job.submitted_at is not None
    if job.provider != deps.asr.name:  # ASR_PROVIDER was switched while this one was in flight
        log.warning(  # the old provider's copy (task id below) is left to its operator
            "ASR job %s was submitted to %s (task %s); submitting it to %s",
            job.id,
            job.provider,
            job.external_job_id,
            deps.asr.name,
        )
        # The new provider gets at least one try, whatever the old one used up.
        attempts = min(job.attempts, MAX_ASR_ATTEMPTS - 1)
        await _resubmit(engine, job, error=f"submitted to {job.provider[:40]}", now=now, attempts=attempts)
        return
    # Polled first: a result that finished while the runner was down for hours is still taken. The deadline
    # only ends a task that is still not done (a stuck worker) or no longer answers.
    past_deadline = now >= job.submitted_at + getattr(deps.asr, "poll_deadline", ASR_POLL_DEADLINE)
    try:
        result = await deps.asr.poll(job.external_job_id)
    except Exception as exc:
        error = _describe(exc)
        log.warning("ASR poll for job %s failed: %s", job.id, error)
        if past_deadline:
            await _give_up_polling(engine, deps, job, now=now)
        else:
            await _retry_later(engine, job, error=error, at=now + ASR_POLL_INTERVAL)
        return
    if result.state == AsrState.RUNNING:
        if past_deadline:
            await _give_up_polling(engine, deps, job, now=now)
        else:
            await _retry_later(engine, job, error=None, at=now + ASR_POLL_INTERVAL)
    elif result.state == AsrState.LOST:
        log.warning("ASR provider lost the task of job %s; submitting again", job.id)
        await _resubmit(engine, job, error="provider lost the task", now=now)
    elif result.state == AsrState.FAILED:
        await _fail(engine, job, FailureCode.ASR_FAILED, error=result.error or "provider failed", now=now)
        await _discard(engine, deps, job.external_job_id, job_id=job.id)
    else:
        await _store_transcript(engine, job, result, now=now)
        await _discard(engine, deps, job.external_job_id, job_id=job.id)


async def _give_up_polling(engine: AsyncEngine, deps: PostProcessing, job: _Job, *, now: datetime) -> None:
    assert job.external_job_id is not None
    await _fail(engine, job, FailureCode.ASR_FAILED, error="result window passed", now=now)
    await _discard(engine, deps, job.external_job_id, job_id=job.id)


async def _resubmit(
    engine: AsyncEngine, job: _Job, *, error: str, now: datetime, attempts: int | None = None
) -> None:
    """Back to PENDING without a task id: the next run submits again, within MAX_ASR_ATTEMPTS."""
    j = processing_jobs.c
    values = {"attempts": attempts} if attempts is not None else {}

    async def reset(conn: AsyncConnection) -> None:
        await conn.execute(
            update(processing_jobs)
            .where(j.id == job.id, j.status == ProcessingJobStatus.SUBMITTED)
            .values(
                status=ProcessingJobStatus.PENDING,
                external_job_id=None,
                submitted_at=None,
                billed_audio_seconds=None,
                last_error=error,
                next_poll_at=now,
                **values,
            )
        )

    await in_transaction_with_retry(engine, reset)


async def _discard(
    engine: AsyncEngine, deps: PostProcessing, external_id: str, *, job_id: UUID | None = None
) -> bool:
    """Best effort: a provider that keeps results (a self-hosted service) drops its copy of the transcript.
    Done, the job's external_job_id is cleared, which tells discard_abandoned there is nothing left."""
    discard = getattr(deps.asr, "discard", None)
    if discard is None:
        return False
    try:
        await discard(external_id)
    except Exception as exc:
        log.warning("could not discard ASR task %s: %s", external_id, _describe(exc))
        return False
    if job_id is not None:
        j = processing_jobs.c

        async def forget(conn: AsyncConnection) -> None:
            await conn.execute(
                update(processing_jobs)
                .where(
                    j.id == job_id,
                    j.external_job_id == external_id,
                    j.status != ProcessingJobStatus.SUBMITTED,  # a submitted job needs its id to be polled
                )
                .values(external_job_id=None)
            )

        await in_transaction_with_retry(engine, forget)
    return True


async def discard_abandoned(
    engine: AsyncEngine, deps: PostProcessing, *, limit: int = DISCARDS_PER_PASS
) -> int:
    """Provider copies nothing else removes: jobs canceled while recognition ran (the cancel closes the job
    without polling it), and discards that failed. Only this provider's jobs, and only if it can discard.
    Oldest-touched first; the first failure ends the sweep (the provider is likely down) and moves that row
    to the back, so one copy that cannot be deleted never starves the others."""
    if getattr(deps.asr, "discard", None) is None:
        return 0
    j = processing_jobs.c
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                select(j.id, j.external_job_id)
                .where(
                    j.kind == ProcessingJobKind.ASR,
                    j.provider == deps.asr.name,
                    j.status.in_((ProcessingJobStatus.FAILED, ProcessingJobStatus.SUCCEEDED)),
                    j.external_job_id.is_not(None),
                )
                .order_by(j.updated_at)
                .limit(limit)
            )
        ).all()
    done = 0
    for row in rows:
        if not await _discard(engine, deps, row.external_job_id, job_id=row.id):

            async def touch(conn: AsyncConnection, job_id=row.id) -> None:
                await conn.execute(
                    update(processing_jobs).where(j.id == job_id).values(updated_at=func.now())
                )

            await in_transaction_with_retry(engine, touch)
            break
        done += 1
    return done


def _normalized(result: AsrResult) -> list[dict[str, Any]]:
    """The transcript body: numbered segments, the unit that minutes cite as evidence."""
    out = []
    for seg in sorted(result.segments, key=lambda s: (s.start_ms, s.end_ms)):
        text = mm.clean_text(seg.text).strip()
        if text:
            out.append(
                {
                    "i": len(out),
                    "start_ms": seg.start_ms,
                    "end_ms": seg.end_ms,
                    "speaker": seg.speaker,
                    "text": text,
                }
            )
    return out


async def _store_transcript(engine: AsyncEngine, job: _Job, result: AsrResult, *, now: datetime) -> None:
    segments = _normalized(result)
    if not segments:
        await _fail(engine, job, FailureCode.EMPTY_AUDIO, error="no speech recognized", now=now)
        return
    tr, t = transcripts.c, meeting_tasks.c

    async def write(conn: AsyncConnection) -> dict[str, Any]:
        kind = await conn.scalar(select(t.task_kind).where(t.id == job.task_id))
        version = await conn.scalar(
            select(func.coalesce(func.max(tr.version), 0) + 1).where(tr.task_id == job.task_id)
        )
        await conn.execute(
            insert(transcripts).values(
                task_id=job.task_id,
                source=_TRANSCRIPT_SOURCE[TaskKind(kind)],
                processing_job_id=job.id,
                body={"segments": segments},
                language="zh",
                version=version,
            )
        )
        billed = {"billed_audio_seconds": result.billed_seconds} if result.billed_seconds is not None else {}
        return {"status": ProcessingJobStatus.SUCCEEDED, "last_error": None, **billed}

    await _settle(engine, job, Trigger(TriggerType.TRANSCRIPTION_DONE), write, now=now)


# ---------------------------------------------------------------- minutes


async def _summarize(engine: AsyncEngine, deps: PostProcessing, job: _Job, *, now: datetime) -> None:
    t, tr = meeting_tasks.c, transcripts.c
    async with engine.connect() as conn:
        task = (
            await conn.execute(
                select(t.status, t.scheduled_at, t.timezone, t.created_at).where(t.id == job.task_id)
            )
        ).one()
        status = task.status
        transcript = (
            await conn.execute(
                select(tr.id, tr.body).where(tr.task_id == job.task_id).order_by(tr.version.desc()).limit(1)
            )
        ).one_or_none()
        # An upload's file name often carries the company or project name the recognizer misspells, and
        # often the meeting's date.
        upload = (
            await conn.execute(
                select(task_inputs.c.file_name, task_inputs.c.created_at).where(
                    task_inputs.c.task_id == job.task_id
                )
            )
        ).one_or_none()
        title = upload.file_name if upload else None
    if status != TaskStatus.SUMMARIZING:
        await _close(engine, job, error="task left SUMMARIZING", now=now)
        return
    if transcript is None:
        await _fail(engine, job, FailureCode.LLM_FAILED, error="no transcript", now=now)
        return
    if job.attempts >= MAX_SUMMARY_ATTEMPTS:
        await _fail(engine, job, FailureCode.LLM_FAILED, error=job.last_error or "interrupted", now=now)
        return
    segments = transcript.body["segments"]
    try:
        mm.build_user_prompt(segments, title=title)  # retrying cannot fit it: failed before any attempt
    except mm.TranscriptTooLong as exc:
        await _fail(engine, job, FailureCode.LLM_FAILED, error=f"transcript too long: {exc}", now=now)
        return
    attempts = await _start_attempt(engine, job, now=now)
    if attempts is None:
        return

    deadline = asyncio.get_running_loop().time() + STEP_TIMEOUT.total_seconds()
    try:
        written = await write_minutes(
            deps.llm, segments, title=title, deadline=deadline, label=f"summary job {job.id}"
        )
    except Exception as exc:
        error = _describe(exc)
        log.warning("summary job %s failed: %s", job.id, error)
        if attempts >= MAX_SUMMARY_ATTEMPTS or _hopeless(exc, job):
            await _fail(engine, job, FailureCode.LLM_FAILED, error=error, now=now)
        else:
            await _retry_later(engine, job, error=error, at=now + _backoff(attempts))
        return
    minutes = written.minutes

    meeting_date, date_source = mm.resolve_meeting_date(
        scheduled_at=task.scheduled_at,
        title=title,
        said=minutes.meeting_date,
        uploaded_at=upload.created_at if upload else task.created_at,
        timezone=task.timezone,
    )
    m = meeting_minutes.c

    async def write(conn: AsyncConnection) -> dict[str, Any]:
        version = await conn.scalar(
            select(func.coalesce(func.max(m.version), 0) + 1).where(m.task_id == job.task_id)
        )
        await conn.execute(
            update(meeting_minutes).where(m.task_id == job.task_id, m.is_current).values(is_current=False)
        )
        await conn.execute(
            insert(meeting_minutes).values(
                task_id=job.task_id,
                transcript_id=transcript.id,
                version=version,
                is_current=True,
                model_provider=deps.llm.name,
                model_name=deps.llm.model,
                model_region=deps.llm.region,
                prompt_version=mm.PROMPT_VERSION,
                meeting_date=meeting_date,
                meeting_date_source=date_source,
                **mm.as_columns(minutes),
            )
        )
        return {
            "status": ProcessingJobStatus.SUCCEEDED,
            "last_error": None,
            "input_tokens": written.input_tokens,
            "output_tokens": written.output_tokens,
        }

    await _settle(engine, job, Trigger(TriggerType.SUMMARY_DONE), write, now=now)


# ---------------------------------------------------------------- uploaded recordings


class _Unusable(Exception):
    """The upload is not a usable recording; the task fails with INVALID_UPLOAD (or UPLOAD_FAILED)."""

    def __init__(self, reason: str, code: FailureCode = FailureCode.INVALID_UPLOAD):
        super().__init__(reason)
        self.code = code


async def _ingest(engine: AsyncEngine, deps: PostProcessing, job: _Job, *, now: datetime) -> None:
    """AUDIO_NORMALIZE: fetch the user's recording (WeChat media or a staged COS object), check it, convert
    it to 16 kHz mono AAC, store it as the task's READY segment and hand the task to transcription."""
    t = meeting_tasks.c
    if deps.keyring is None or deps.media is None:
        missing = "ffmpeg/ffprobe" if deps.media is None else "the keyring"
        if now - job.created_at >= UNCONFIGURED_MAX_WAIT:
            log.error("ingest job %s gave up: %s still not available", job.id, missing)
            unusable = _Unusable("upload processing not configured", FailureCode.UPLOAD_FAILED)
            await _reject(engine, job, unusable, now=now)
            return
        log.error("ingest job %s waits: %s not available", job.id, missing)
        await _retry_later(engine, job, error="upload processing not configured", at=now + BUDGET_RECHECK)
        return
    async with engine.connect() as conn:
        task = (await conn.execute(select(t.status, t.task_kind).where(t.id == job.task_id))).one()
        source = await read_input(conn, deps.keyring, job.task_id)
    if task.status != TaskStatus.UPLOADING or task.task_kind != TaskKind.USER_UPLOAD:
        await _close(engine, job, error="task left UPLOADING", now=now)
        return
    if source is None or source.payload is None:
        await _reject(engine, job, _Unusable("no input", FailureCode.UPLOAD_FAILED), now=now)
        return
    if job.attempts >= MAX_INGEST_ATTEMPTS:
        error = job.last_error or "interrupted"
        await _reject(engine, job, _Unusable(error, FailureCode.UPLOAD_FAILED), now=now)
        return
    attempts = await _start_attempt(engine, job, now=now)
    if attempts is None:
        return

    key = f"tasks/{job.task_id}/audio/upload-v{job.input_version}.m4a"
    try:
        async with asyncio.timeout(STEP_TIMEOUT.total_seconds()):
            stored = await _fetch_and_convert(deps, source, key)
    except _Unusable as exc:
        log.info("upload of task %s rejected: %s", job.task_id, exc)
        await _reject(engine, job, exc, now=now)
        return
    except Exception as exc:
        error = _describe(exc)
        log.warning("ingest job %s failed: %s", job.id, error)
        if attempts >= MAX_INGEST_ATTEMPTS:
            await _reject(engine, job, _Unusable(error, FailureCode.UPLOAD_FAILED), now=now)
        else:
            await _retry_later(engine, job, error=error, at=now + _backoff(attempts))
        return

    silent = stored.mean_volume_dbfs is not None and stored.mean_volume_dbfs < SILENCE_DBFS

    async def write(conn: AsyncConnection) -> dict[str, Any]:
        await conn.execute(
            insert(audio_assets).values(
                task_id=job.task_id,
                source="user_upload",
                segment_no=job.input_version - 1,
                object_key=key,
                file_sha256=stored.sha256,
                format="m4a",
                duration_ms=stored.duration_ms,
                bytes=stored.bytes,
                rms_dbfs=stored.mean_volume_dbfs,
                upload_status=AudioUploadStatus.READY,
                verified_at=now,
                retention_until=now + AUDIO_RETENTION,
            )
        )
        await consume_input(conn, job.task_id, now=now)
        return {"status": ProcessingJobStatus.SUCCEEDED, "last_error": None}

    trigger = Trigger(TriggerType.UPLOAD_EMPTY if silent else TriggerType.UPLOAD_COMPLETE)
    await _settle(engine, job, trigger, write, now=now, write_first=True)
    if source.kind == InputKind.STAGED_OBJECT:
        try:
            await deps.storage.delete(source.payload)  # the original; DISCARD / purge cover it otherwise
        except Exception as exc:
            log.warning("could not delete staged upload of task %s: %s", job.task_id, type(exc).__name__)


@dataclass(frozen=True)
class _Stored:
    sha256: str
    bytes: int
    duration_ms: int
    mean_volume_dbfs: float | None


async def _fetch_and_convert(deps: PostProcessing, source: TaskInput, key: str) -> _Stored:
    assert deps.media is not None and source.payload is not None
    with tempfile.TemporaryDirectory(prefix="ingest-") as tmp:
        original, audio = Path(tmp) / "source", Path(tmp) / "audio.m4a"
        if source.kind == InputKind.WECOM_MEDIA:
            if deps.inbound is None:
                raise ProviderError("WeChat media download not configured")
            try:
                await deps.inbound.download_media(source.payload, original, max_bytes=MAX_SOURCE_BYTES)
            except MediaTooLarge:
                raise _Unusable("larger than the source limit") from None
            except WeComApiError as exc:
                if exc.errcode in MEDIA_EXPIRED:
                    raise _Unusable(f"media expired ({exc.errcode})", FailureCode.UPLOAD_FAILED) from None
                raise ProviderError(f"media/get {exc.errcode}") from None
        elif source.kind == InputKind.STAGED_OBJECT:
            await deps.storage.download_file(source.payload, original)
            if original.stat().st_size > MAX_SOURCE_BYTES:
                raise _Unusable("larger than the source limit")
        else:
            raise _Unusable(f"{source.kind} is not a recording")
        info = await deps.media.probe(original)
        if info is None or not info.has_audio:
            raise _Unusable("not an audio or video file with sound")
        if info.duration_ms > MAX_UPLOAD_AUDIO_MS:
            raise _Unusable("longer than the upload limit")
        try:
            converted = await deps.media.to_speech_audio(original, audio, demuxer=info.demuxer)
        except MediaRejected as exc:
            raise _Unusable(str(exc)) from None
        if converted.duration_ms > MAX_UPLOAD_AUDIO_MS:  # containers without a duration header
            raise _Unusable("longer than the upload limit")
        digest = hashlib.sha256()
        with audio.open("rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                digest.update(block)
        size = audio.stat().st_size
        await deps.storage.upload_file(audio, key, content_type="audio/mp4")
        return _Stored(digest.hexdigest(), size, converted.duration_ms, converted.mean_volume_dbfs)


async def _reject(engine: AsyncEngine, job: _Job, exc: _Unusable, *, now: datetime) -> None:
    async def close(conn: AsyncConnection) -> dict[str, Any]:
        await consume_input(conn, job.task_id, now=now)
        return {"status": ProcessingJobStatus.FAILED, "last_error": str(exc)[:200]}

    await _settle(engine, job, Trigger(TriggerType.UPLOAD_INVALID, failure_code=exc.code), close, now=now)


# ---------------------------------------------------------------- job bookkeeping


async def _start_attempt(engine: AsyncEngine, job: _Job, *, now: datetime) -> int | None:
    """Commit attempts + 1 before calling out, renewing the lease from now. None: the job changed under us
    (canceled)."""
    j = processing_jobs.c

    async def bump(conn: AsyncConnection) -> int | None:
        return await conn.scalar(
            update(processing_jobs)
            .where(j.id == job.id, j.status == job.status, j.attempts == job.attempts)
            .values(attempts=j.attempts + 1, last_error=None, next_poll_at=now + CLAIM_LEASE)
            .returning(j.attempts)
        )

    return await in_transaction_with_retry(engine, bump)


async def _retry_later(
    engine: AsyncEngine, job: _Job, *, error: str | None, at: datetime, attempts: int | None = None
) -> None:
    """`attempts`: set the counter back (a try that never reached the provider)."""
    j = processing_jobs.c
    values = {"next_poll_at": at, "last_error": error}
    if attempts is not None:
        values["attempts"] = attempts

    async def park(conn: AsyncConnection) -> None:
        await conn.execute(
            update(processing_jobs).where(j.id == job.id, j.status == job.status).values(**values)
        )

    await in_transaction_with_retry(engine, park)


async def _settle(
    engine: AsyncEngine,
    job: _Job,
    trigger: Trigger,
    write: Callable[[AsyncConnection], Awaitable[dict[str, Any]]],
    *,
    now: datetime,
    write_first: bool = False,
) -> None:
    """Under the task lock: if the job is still active and the task accepts `trigger`, run `write` (which
    returns the job's final columns) and move the task on. Otherwise only close the job. `write_first`:
    the trigger depends on what `write` stores (an uploaded segment); both run in a savepoint that is
    rolled back if the task no longer accepts the trigger."""
    t, j = meeting_tasks.c, processing_jobs.c
    actor = f"job:{job.kind.lower()}"

    async def work(conn: AsyncConnection) -> None:
        await conn.execute(select(t.id).where(t.id == job.task_id).with_for_update())
        current = await conn.scalar(select(j.status).where(j.id == job.id).with_for_update())
        if current not in _ACTIVE:
            return
        values: dict[str, Any]
        try:
            if write_first:
                async with conn.begin_nested():
                    values = await write(conn)
                    await apply_and_settle(conn, job.task_id, trigger, now=now, actor=actor)
            else:
                await apply_and_settle(conn, job.task_id, trigger, now=now, actor=actor)
                values = await write(conn)
        except InvalidTransition:
            values = {"status": ProcessingJobStatus.FAILED, "last_error": "task moved on"}
        await conn.execute(update(processing_jobs).where(j.id == job.id).values(next_poll_at=None, **values))

    await in_transaction_with_retry(engine, work)


async def _fail(engine: AsyncEngine, job: _Job, code: FailureCode, *, error: str, now: datetime) -> None:
    """The job gives up and takes the task with it (JOB_FAILED notifies the user)."""

    async def close(conn: AsyncConnection) -> dict[str, Any]:
        return {"status": ProcessingJobStatus.FAILED, "last_error": error}

    await _settle(engine, job, Trigger(TriggerType.JOB_FAILED, failure_code=code), close, now=now)


async def _close(engine: AsyncEngine, job: _Job, *, error: str, now: datetime) -> None:
    """The task no longer waits for this job: close it without touching the task."""
    j = processing_jobs.c

    async def work(conn: AsyncConnection) -> None:
        await conn.execute(
            update(processing_jobs)
            .where(j.id == job.id, j.status.in_(_ACTIVE))
            .values(status=ProcessingJobStatus.FAILED, last_error=error, next_poll_at=None)
        )

    await in_transaction_with_retry(engine, work)
