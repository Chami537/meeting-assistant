"""Task state machine (design doc §5.3).

Pure: no I/O, time is passed in. `decide` maps (task snapshot, trigger) to a Transition: the new status,
the columns to write in the same UPDATE, the effects to persist in the same transaction, and the reason to
log. A trigger that does not apply to the current state raises InvalidTransition.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID

from meeting_protocol import FailureCode, RecordingOutcome, TaskKind, TaskStatus, WorkerEventType
from meeting_protocol.rules import (
    DISCARD_AUDIO_FAILURE_CODES,
    IN_ATTEMPT_STATUSES,
    INTAKE_STATUSES,
    POST_PROCESSING_STATUSES,
    PRE_RECORDING_STATUSES,
    RETRYABLE_FAILURE_CODES,
    TERMINAL_STATUSES,
)

S = TaskStatus
F = FailureCode

DISPATCH_LEAD = timedelta(minutes=3)
DISPATCH_GRACE = timedelta(minutes=10)
RETRY_DELAY = timedelta(seconds=30)
UPLOAD_WINDOW = timedelta(hours=2)
RECORDING_OVERRUN = timedelta(minutes=30)  # recording stops at planned end + 30 min at the latest (§5.4)

# Failure codes a Worker (or a server-side timeout) may report, per in-meeting status (§5.3).
_ANY_ATTEMPT_CODES = RETRYABLE_FAILURE_CODES | {F.INTERNAL_ERROR, F.CONSENT_REVOKED, F.REMOVED_BY_HOST}
ATTEMPT_FAILURE_CODES_BY_STATUS: dict[TaskStatus, frozenset[FailureCode]] = {
    S.JOINING: _ANY_ATTEMPT_CODES
    | {F.HOST_REJECTED, F.MEETING_LOCKED, F.WRONG_PASSWORD, F.MEETING_NOT_STARTED_TIMEOUT},
    S.WAITING_ROOM: _ANY_ATTEMPT_CODES | {F.HOST_REJECTED, F.WAITING_ROOM_TIMEOUT},
    S.IN_MEETING: _ANY_ATTEMPT_CODES,
    S.AWAITING_RECORDING_APPROVAL: _ANY_ATTEMPT_CODES | {F.RECORDING_DENIED, F.RECORDING_APPROVAL_TIMEOUT},
    # RECORDING_DENIED while recording means the host withdrew permission: handled as CONSENT_REVOKED.
    S.RECORDING: _ANY_ATTEMPT_CODES | {F.RECORDING_DENIED},
}
ATTEMPT_FAILURE_CODES = frozenset().union(*ATTEMPT_FAILURE_CODES_BY_STATUS.values())

# Statuses from which "stop and summarize" keeps audio recorded by earlier attempts (§5.3 stop matrix).
_RECONNECTING_STATUSES = PRE_RECORDING_STATUSES | {S.DISPATCHING, S.RETRY_WAIT}


class TriggerType(StrEnum):
    DETAILS_COMPLETE = "DETAILS_COMPLETE"
    CONSENT_GRANTED = "CONSENT_GRANTED"
    DISPATCH_DUE = "DISPATCH_DUE"
    LEASE_CLAIMED = "LEASE_CLAIMED"  # only the lease service may persist this (it inserts the attempt)
    DISPATCH_DEADLINE_PASSED = "DISPATCH_DEADLINE_PASSED"
    DISPATCH_FAILED = (
        "DISPATCH_FAILED"  # the task cannot be handed to any Worker (e.g. unreadable credentials)
    )
    ENTERED_WAITING_ROOM = "ENTERED_WAITING_ROOM"
    JOINED = "JOINED"
    RECORDING_NOTICE_SENT = "RECORDING_NOTICE_SENT"
    RECORDING_NOTICE_FAILED = "RECORDING_NOTICE_FAILED"
    RECORDING_APPROVED = "RECORDING_APPROVED"
    MEETING_ENDED = "MEETING_ENDED"
    RECORDING_LIMIT_REACHED = "RECORDING_LIMIT_REACHED"
    ATTEMPT_FAILED = "ATTEMPT_FAILED"  # carries failure_code
    LEASE_EXPIRED = "LEASE_EXPIRED"
    RETRY_DUE = "RETRY_DUE"
    STOP_REQUESTED = "STOP_REQUESTED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    UPLOAD_COMPLETE = "UPLOAD_COMPLETE"
    UPLOAD_EMPTY = "UPLOAD_EMPTY"
    UPLOAD_DEADLINE_PASSED = "UPLOAD_DEADLINE_PASSED"
    UPLOAD_INVALID = "UPLOAD_INVALID"
    TRANSCRIPTION_DONE = "TRANSCRIPTION_DONE"
    SUMMARY_DONE = "SUMMARY_DONE"
    JOB_FAILED = "JOB_FAILED"  # carries failure_code ASR_FAILED or LLM_FAILED
    INTAKE_EXPIRED = "INTAKE_EXPIRED"  # an unconfirmed request whose meeting is over (or abandoned)


class Effect(StrEnum):
    """All effects are persisted in the transaction that changes the status (crash-safe)."""

    END_ACTIVE_ATTEMPT = "END_ACTIVE_ATTEMPT"  # CLAIMED/RUNNING -> FINISHED: sealed segments may still upload
    EXPIRE_ACTIVE_ATTEMPT = "EXPIRE_ACTIVE_ATTEMPT"  # CLAIMED/RUNNING -> EXPIRED: lease lost
    CANCEL_ATTEMPTS = "CANCEL_ATTEMPTS"  # every attempt not yet CANCELED -> CANCELED: no more uploads
    FAIL_UNFINISHED_SEGMENTS = "FAIL_UNFINISHED_SEGMENTS"
    CANCEL_PROCESSING_JOBS = "CANCEL_PROCESSING_JOBS"
    DELETE_RESULTS = "DELETE_RESULTS"  # transcripts and minutes
    ENQUEUE_ASR = "ENQUEUE_ASR"  # processing_jobs row
    ENQUEUE_SUMMARY = "ENQUEUE_SUMMARY"  # processing_jobs row
    ENQUEUE_INGEST = "ENQUEUE_INGEST"  # processing_jobs AUDIO_NORMALIZE row: fetch and normalize an upload
    DISCARD_AUDIO = "DISCARD_AUDIO"  # task_effects row: delete COS objects, then mark audio_assets DELETED
    NOTIFY_USER = "NOTIFY_USER"  # task_effects row: rendered into outgoing_messages by the dispatcher


@dataclass(frozen=True)
class TaskSnapshot:
    """Task row plus facts derived from child rows, all read under the task row lock."""

    id: UUID
    task_kind: TaskKind
    status: TaskStatus
    lock_version: int
    attempt_count: int
    max_attempts: int
    scheduled_at: datetime | None
    planned_end_at: datetime | None
    has_join_info: bool  # meeting_code or join link present
    recording_outcome: RecordingOutcome | None
    dispatch_deadline_at: datetime | None
    next_retry_at: datetime | None
    upload_deadline_at: datetime | None
    actual_joined_at: datetime | None
    max_record_minutes: int
    has_audio: bool  # sealed or registered segments that have not been discarded
    has_ready_audio: bool  # at least one READY segment
    has_active_attempt: bool  # an attempt in CLAIMED or RUNNING


@dataclass(frozen=True)
class Trigger:
    type: TriggerType
    failure_code: FailureCode | None = None
    consent_version: str | None = None


@dataclass(frozen=True)
class Transition:
    from_status: TaskStatus
    to_status: TaskStatus
    updates: dict[str, Any] = field(default_factory=dict)
    effects: tuple[Effect, ...] = ()
    reason: FailureCode | None = None  # why it failed or was interrupted; logged in task_events.data


class InvalidTransition(Exception):
    pass


def trigger_for_worker_event(
    event: WorkerEventType, failure_code: FailureCode | None = None
) -> Trigger | None:
    """Map a Worker event to a trigger; None for events that do not change the task status."""
    match event:
        case WorkerEventType.IN_WAITING_ROOM:
            return Trigger(TriggerType.ENTERED_WAITING_ROOM)
        case WorkerEventType.JOINED:
            return Trigger(TriggerType.JOINED)
        case WorkerEventType.RECORDING_NOTICE_SENT:
            return Trigger(TriggerType.RECORDING_NOTICE_SENT)
        case WorkerEventType.RECORDING_NOTICE_FAILED:
            return Trigger(TriggerType.RECORDING_NOTICE_FAILED)
        case WorkerEventType.RECORDING_APPROVED:
            return Trigger(TriggerType.RECORDING_APPROVED)
        case WorkerEventType.RECORDING_DENIED:
            return Trigger(TriggerType.ATTEMPT_FAILED, F.RECORDING_DENIED)
        case WorkerEventType.REMOVED_FROM_MEETING:
            return Trigger(TriggerType.ATTEMPT_FAILED, F.REMOVED_BY_HOST)
        case WorkerEventType.MEETING_ENDED:
            return Trigger(TriggerType.MEETING_ENDED)
        case WorkerEventType.FAILED:
            if failure_code == F.MEETING_ENDED:
                return Trigger(TriggerType.MEETING_ENDED)
            # WorkerEvent requires a code; INTERNAL_ERROR is the non-retryable fallback (§5.3).
            return Trigger(TriggerType.ATTEMPT_FAILED, failure_code or F.INTERNAL_ERROR)
        case _:
            # JOIN_REQUESTED, RECORDING_STARTED, SEGMENT_SEALED, RECORDING_STOPPED, LEFT: bookkeeping only
            return None


def recording_limit_at(task: TaskSnapshot) -> datetime:
    """When RECORDING must stop: the per-task limit or the planned end plus overrun, whichever is first."""
    started = task.actual_joined_at or task.scheduled_at
    return min(started + timedelta(minutes=task.max_record_minutes), task.planned_end_at + RECORDING_OVERRUN)


def decide(task: TaskSnapshot, trigger: Trigger, now: datetime) -> Transition:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return _HANDLERS[trigger.type](task, trigger, now)


# ---------------------------------------------------------------- helpers


def _require(task: TaskSnapshot, trigger: Trigger, allowed: set[TaskStatus] | frozenset[TaskStatus]) -> None:
    if task.status not in allowed:
        raise InvalidTransition(f"{trigger.type} not allowed in {task.status}")


def _to(
    task: TaskSnapshot,
    new_status: TaskStatus,
    now: datetime,
    *,
    updates: dict[str, Any] | None = None,
    effects: tuple[Effect, ...] = (),
    reason: FailureCode | None = None,
) -> Transition:
    upd = dict(updates or {})
    if task.status == S.RETRY_WAIT and new_status != S.RETRY_WAIT:
        upd.setdefault("next_retry_at", None)
        if new_status != S.FAILED:
            upd.setdefault("failure_code", None)
    if new_status == S.DISPATCHING:
        upd["dispatch_deadline_at"] = max(now, task.scheduled_at) + DISPATCH_GRACE
    if new_status == S.UPLOADING and task.status != S.UPLOADING:
        upd["upload_deadline_at"] = now + UPLOAD_WINDOW
    return Transition(task.status, new_status, upd, effects, reason)


def _outcome(task: TaskSnapshot, outcome: RecordingOutcome) -> RecordingOutcome:
    """INTERRUPTED is sticky so the minutes can say the recording has gaps."""
    if task.recording_outcome == RecordingOutcome.INTERRUPTED:
        return RecordingOutcome.INTERRUPTED
    return outcome


def _fail(
    task: TaskSnapshot, code: FailureCode, now: datetime, effects: tuple[Effect, ...] = ()
) -> Transition:
    return _to(
        task,
        S.FAILED,
        now,
        updates={"failure_code": code, "next_retry_at": None},
        effects=(*effects, Effect.NOTIFY_USER),
        reason=code,
    )


def _interrupted_upload(
    task: TaskSnapshot,
    now: datetime,
    reason: FailureCode,
    *,
    effects: tuple[Effect, ...] = (),
    updates: dict[str, Any] | None = None,
) -> Transition:
    upd = {"recording_outcome": RecordingOutcome.INTERRUPTED, **(updates or {})}
    return _to(task, S.UPLOADING, now, updates=upd, effects=effects, reason=reason)


def _discard(task: TaskSnapshot, code: FailureCode, now: datetime) -> Transition:
    return _fail(task, code, now, effects=(Effect.CANCEL_ATTEMPTS, Effect.DISCARD_AUDIO))


def _after_attempt_failure(
    task: TaskSnapshot, code: FailureCode, now: datetime, ending: Effect = Effect.END_ACTIVE_ATTEMPT
) -> Transition:
    if code in DISCARD_AUDIO_FAILURE_CODES:
        return _discard(task, code, now)
    can_retry = (
        code in RETRYABLE_FAILURE_CODES
        and task.attempt_count < task.max_attempts
        and task.planned_end_at is not None
        and now < task.planned_end_at
    )
    if can_retry:
        updates: dict[str, Any] = {
            "next_retry_at": now + RETRY_DELAY,
            "failure_code": code,
            "host_recording_approval_at": None,  # the next attempt must obtain its own approval
        }
        if task.has_audio:
            updates["recording_outcome"] = RecordingOutcome.INTERRUPTED
        return _to(task, S.RETRY_WAIT, now, updates=updates, effects=(ending,), reason=code)
    if task.has_audio:
        return _interrupted_upload(task, now, code, effects=(ending,))
    return _fail(task, code, now, effects=(ending,))


# ---------------------------------------------------------------- handlers


def _details_complete(task: TaskSnapshot, trigger: Trigger, now: datetime) -> Transition:
    _require(task, trigger, {S.AWAITING_DETAILS})
    return _to(task, S.AWAITING_CONSENT, now)


def _consent_granted(task: TaskSnapshot, trigger: Trigger, now: datetime) -> Transition:
    _require(task, trigger, {S.AWAITING_CONSENT})
    if not trigger.consent_version:
        raise InvalidTransition("CONSENT_GRANTED needs consent_version")
    updates = {"consent_at": now, "consent_version": trigger.consent_version}
    match task.task_kind:
        case TaskKind.LIVE_MEETING:
            if task.scheduled_at is None or task.planned_end_at is None or not task.has_join_info:
                raise InvalidTransition("live meeting needs scheduled_at, planned_end_at and a way to join")
            if task.planned_end_at <= now:
                raise InvalidTransition("the meeting is already over")
            return _to(task, S.SCHEDULED, now, updates=updates, effects=(Effect.NOTIFY_USER,))
        case TaskKind.USER_UPLOAD:
            return _to(task, S.UPLOADING, now, updates=updates, effects=(Effect.ENQUEUE_INGEST,))
        case TaskKind.FORWARDED_MINUTES:
            return _to(task, S.SUMMARIZING, now, updates=updates, effects=(Effect.ENQUEUE_SUMMARY,))


def _intake_expired(task: TaskSnapshot, trigger: Trigger, now: datetime) -> Transition:
    """Silent: nothing was scheduled, and the user is told when they reply to it (or never needs to be)."""
    _require(task, trigger, INTAKE_STATUSES)
    return _to(task, S.CANCELED, now, effects=_upload_discard(task))


def _upload_discard(task: TaskSnapshot) -> tuple[Effect, ...]:
    """A user-supplied recording that will not be processed is deleted now, not at the 180-day purge: the
    upload declaration promises 30 days at most. (Its delayed second run also catches an upload that was
    still being written when the task ended.)"""
    return (Effect.DISCARD_AUDIO,) if task.task_kind == TaskKind.USER_UPLOAD else ()


def _dispatch_due(task: TaskSnapshot, trigger: Trigger, now: datetime) -> Transition:
    _require(task, trigger, {S.SCHEDULED})
    if now < task.scheduled_at - DISPATCH_LEAD:
        raise InvalidTransition("dispatch is not due yet")
    return _to(task, S.DISPATCHING, now)


def _lease_claimed(task: TaskSnapshot, trigger: Trigger, now: datetime) -> Transition:
    _require(task, trigger, {S.DISPATCHING})
    if task.attempt_count >= task.max_attempts:
        raise InvalidTransition("attempts exhausted")
    return _to(
        task, S.JOINING, now, updates={"attempt_count": task.attempt_count + 1, "dispatch_deadline_at": None}
    )


def _dispatch_deadline_passed(task: TaskSnapshot, trigger: Trigger, now: datetime) -> Transition:
    _require(task, trigger, {S.DISPATCHING})
    if task.dispatch_deadline_at is not None and now < task.dispatch_deadline_at:
        raise InvalidTransition("dispatch deadline not reached")
    if task.has_audio:
        return _interrupted_upload(task, now, F.NO_WORKER)
    return _fail(task, F.NO_WORKER, now)  # V1 has one Worker: not retried


def _dispatch_failed(task: TaskSnapshot, trigger: Trigger, now: datetime) -> Transition:
    _require(task, trigger, {S.DISPATCHING})
    return _fail(task, F.INTERNAL_ERROR, now)


def _entered_waiting_room(task: TaskSnapshot, trigger: Trigger, now: datetime) -> Transition:
    _require(task, trigger, {S.JOINING})
    return _to(task, S.WAITING_ROOM, now)


def _joined(task: TaskSnapshot, trigger: Trigger, now: datetime) -> Transition:
    _require(task, trigger, {S.JOINING, S.WAITING_ROOM})
    updates = {"actual_joined_at": now} if task.actual_joined_at is None else {}
    return _to(task, S.IN_MEETING, now, updates=updates)


def _recording_notice(task: TaskSnapshot, trigger: Trigger, now: datetime) -> Transition:
    # After RECORDING_NOTICE_FAILED the Worker only accepts local-recording permission as approval.
    _require(task, trigger, {S.IN_MEETING})
    return _to(task, S.AWAITING_RECORDING_APPROVAL, now)


def _recording_approved(task: TaskSnapshot, trigger: Trigger, now: datetime) -> Transition:
    _require(task, trigger, {S.AWAITING_RECORDING_APPROVAL})
    return _to(task, S.RECORDING, now, updates={"host_recording_approval_at": now})


def _meeting_ended(task: TaskSnapshot, trigger: Trigger, now: datetime) -> Transition:
    _require(task, trigger, IN_ATTEMPT_STATUSES)
    if task.status == S.RECORDING:
        # The Worker saw the end itself; it sends LEFT once its sealed segments are queued for upload.
        return _to(
            task,
            S.UPLOADING,
            now,
            updates={"recording_outcome": _outcome(task, RecordingOutcome.FULL), "actual_ended_at": now},
        )
    if task.has_audio:  # earlier attempts recorded with approval
        return _interrupted_upload(task, now, F.MEETING_ENDED, effects=(Effect.END_ACTIVE_ATTEMPT,))
    return _fail(task, F.MEETING_ENDED, now, effects=(Effect.END_ACTIVE_ATTEMPT,))


def _recording_limit_reached(task: TaskSnapshot, trigger: Trigger, now: datetime) -> Transition:
    _require(task, trigger, {S.RECORDING})
    if now < recording_limit_at(task):
        raise InvalidTransition("recording limit not reached")
    # The Worker learns it must stop from the next heartbeat (task UPLOADING, attempt still RUNNING).
    return _to(
        task, S.UPLOADING, now, updates={"recording_outcome": _outcome(task, RecordingOutcome.LIMIT_REACHED)}
    )


_REVOCATION_CODES = DISCARD_AUDIO_FAILURE_CODES | {F.RECORDING_DENIED}


def _attempt_failed(task: TaskSnapshot, trigger: Trigger, now: datetime) -> Transition:
    if task.status == S.UPLOADING and task.has_active_attempt and trigger.failure_code in _REVOCATION_CODES:
        # Stop was requested but the Worker is still in the meeting: a revocation now still deletes the audio.
        code = F.CONSENT_REVOKED if trigger.failure_code == F.RECORDING_DENIED else trigger.failure_code
        return _discard(task, code, now)
    _require(task, trigger, IN_ATTEMPT_STATUSES)
    code = trigger.failure_code
    if code not in ATTEMPT_FAILURE_CODES_BY_STATUS[task.status]:
        raise InvalidTransition(f"{code} cannot happen in {task.status}")
    if task.status == S.RECORDING and code == F.RECORDING_DENIED:
        code = F.CONSENT_REVOKED
    return _after_attempt_failure(task, code, now)


def _lease_expired(task: TaskSnapshot, trigger: Trigger, now: datetime) -> Transition:
    _require(task, trigger, IN_ATTEMPT_STATUSES)
    return _after_attempt_failure(task, F.WORKER_LOST, now, ending=Effect.EXPIRE_ACTIVE_ATTEMPT)


def _retry_due(task: TaskSnapshot, trigger: Trigger, now: datetime) -> Transition:
    _require(task, trigger, {S.RETRY_WAIT})
    if task.next_retry_at is not None and now < task.next_retry_at:
        raise InvalidTransition("retry is not due yet")
    return _to(task, S.DISPATCHING, now)


def _stop_requested(task: TaskSnapshot, trigger: Trigger, now: datetime) -> Transition:
    if task.status == S.RECORDING:
        return _to(
            task,
            S.UPLOADING,
            now,
            updates={
                "stop_requested_at": now,
                "recording_outcome": _outcome(task, RecordingOutcome.STOPPED_BY_USER),
            },
        )
    if task.status in _RECONNECTING_STATUSES and task.has_audio:
        # Reconnecting after a drop: summarize what earlier attempts recorded instead of deleting it.
        effects = (Effect.END_ACTIVE_ATTEMPT,) if task.has_active_attempt else ()
        # reason stays None: the real interruption cause was logged when the task entered RETRY_WAIT.
        return _to(
            task,
            S.UPLOADING,
            now,
            updates={"recording_outcome": RecordingOutcome.INTERRUPTED, "stop_requested_at": now},
            effects=effects,
        )
    if task.status in POST_PROCESSING_STATUSES or task.status in TERMINAL_STATUSES:
        raise InvalidTransition(f"stop has no effect in {task.status}")
    return _cancel_requested(task, trigger, now)  # nothing recorded: stopping means cancelling


def _cancel_requested(task: TaskSnapshot, trigger: Trigger, now: datetime) -> Transition:
    if task.status in TERMINAL_STATUSES:
        raise InvalidTransition(f"cancel has no effect in {task.status}")
    updates = {"cancel_requested_at": now, "failure_code": None, "next_retry_at": None}
    effects: tuple[Effect, ...]
    if task.status in INTAKE_STATUSES or task.status == S.SCHEDULED:
        effects = _upload_discard(task)
    elif task.status in POST_PROCESSING_STATUSES:
        effects = (
            Effect.CANCEL_ATTEMPTS,
            Effect.CANCEL_PROCESSING_JOBS,
            Effect.DELETE_RESULTS,
            Effect.DISCARD_AUDIO,
        )
    elif task.has_audio or task.status == S.RECORDING:
        effects = (Effect.CANCEL_ATTEMPTS, Effect.DISCARD_AUDIO)
    else:  # DISPATCHING, RETRY_WAIT or in a meeting before recording, nothing captured
        effects = (Effect.CANCEL_ATTEMPTS,)
    return _to(task, S.CANCELED, now, updates=updates, effects=(*effects, Effect.NOTIFY_USER))


def _require_recording_stopped(task: TaskSnapshot, trigger: Trigger) -> None:
    if task.has_active_attempt:
        raise InvalidTransition(f"{trigger.type}: the Worker may still seal segments")


def _upload_complete(task: TaskSnapshot, trigger: Trigger, now: datetime) -> Transition:
    _require(task, trigger, {S.UPLOADING})
    _require_recording_stopped(task, trigger)
    if not task.has_ready_audio:
        raise InvalidTransition("no READY segment to transcribe")
    return _to(task, S.TRANSCRIBING, now, effects=(Effect.ENQUEUE_ASR,))


def _upload_empty(task: TaskSnapshot, trigger: Trigger, now: datetime) -> Transition:
    _require(task, trigger, {S.UPLOADING})
    _require_recording_stopped(task, trigger)
    return _fail(task, F.EMPTY_AUDIO, now, effects=_upload_discard(task))


def _upload_deadline_passed(task: TaskSnapshot, trigger: Trigger, now: datetime) -> Transition:
    _require(task, trigger, {S.UPLOADING})
    if task.upload_deadline_at is not None and now < task.upload_deadline_at:
        raise InvalidTransition("upload deadline not reached")
    ending = (Effect.END_ACTIVE_ATTEMPT,) if task.has_active_attempt else ()
    if task.has_ready_audio:
        return _to(
            task, S.TRANSCRIBING, now, effects=(*ending, Effect.FAIL_UNFINISHED_SEGMENTS, Effect.ENQUEUE_ASR)
        )
    return _fail(
        task, F.UPLOAD_FAILED, now, effects=(*ending, Effect.FAIL_UNFINISHED_SEGMENTS, *_upload_discard(task))
    )


_INVALID_INPUT_STATUSES = {
    TaskKind.USER_UPLOAD: {S.AWAITING_DETAILS, S.AWAITING_CONSENT, S.UPLOADING},
    TaskKind.FORWARDED_MINUTES: {S.AWAITING_DETAILS, S.AWAITING_CONSENT, S.SUMMARIZING},
}


_INVALID_INPUT_CODES = {F.INVALID_UPLOAD, F.UPLOAD_FAILED}  # unusable file / file that could not be fetched


def _upload_invalid(task: TaskSnapshot, trigger: Trigger, now: datetime) -> Transition:
    allowed = _INVALID_INPUT_STATUSES.get(task.task_kind)
    if allowed is None:
        raise InvalidTransition("only uploaded files or forwarded minutes can be invalid")
    _require(task, trigger, allowed)
    code = trigger.failure_code or F.INVALID_UPLOAD
    if code not in _INVALID_INPUT_CODES:
        raise InvalidTransition(f"{code} is not an input failure")
    effects = (Effect.CANCEL_PROCESSING_JOBS,) if task.status == S.SUMMARIZING else ()
    return _fail(task, code, now, effects=(*effects, *_upload_discard(task)))


def _transcription_done(task: TaskSnapshot, trigger: Trigger, now: datetime) -> Transition:
    _require(task, trigger, {S.TRANSCRIBING})
    return _to(task, S.SUMMARIZING, now, effects=(Effect.ENQUEUE_SUMMARY,))


def _summary_done(task: TaskSnapshot, trigger: Trigger, now: datetime) -> Transition:
    _require(task, trigger, {S.SUMMARIZING})
    return _to(task, S.COMPLETED, now, effects=(Effect.NOTIFY_USER,))


_JOB_FAILURE_CODES = {
    S.TRANSCRIBING: {F.ASR_FAILED, F.EMPTY_AUDIO},  # EMPTY_AUDIO: recognition found no speech at all
    S.SUMMARIZING: {F.LLM_FAILED},
}


def _job_failed(task: TaskSnapshot, trigger: Trigger, now: datetime) -> Transition:
    _require(task, trigger, set(_JOB_FAILURE_CODES))
    if trigger.failure_code not in _JOB_FAILURE_CODES[task.status]:
        raise InvalidTransition(f"{trigger.failure_code} does not match {task.status}")
    return _fail(task, trigger.failure_code, now)


_HANDLERS = {
    TriggerType.DETAILS_COMPLETE: _details_complete,
    TriggerType.CONSENT_GRANTED: _consent_granted,
    TriggerType.DISPATCH_DUE: _dispatch_due,
    TriggerType.LEASE_CLAIMED: _lease_claimed,
    TriggerType.DISPATCH_DEADLINE_PASSED: _dispatch_deadline_passed,
    TriggerType.DISPATCH_FAILED: _dispatch_failed,
    TriggerType.ENTERED_WAITING_ROOM: _entered_waiting_room,
    TriggerType.JOINED: _joined,
    TriggerType.RECORDING_NOTICE_SENT: _recording_notice,
    TriggerType.RECORDING_NOTICE_FAILED: _recording_notice,
    TriggerType.RECORDING_APPROVED: _recording_approved,
    TriggerType.MEETING_ENDED: _meeting_ended,
    TriggerType.RECORDING_LIMIT_REACHED: _recording_limit_reached,
    TriggerType.ATTEMPT_FAILED: _attempt_failed,
    TriggerType.LEASE_EXPIRED: _lease_expired,
    TriggerType.RETRY_DUE: _retry_due,
    TriggerType.STOP_REQUESTED: _stop_requested,
    TriggerType.CANCEL_REQUESTED: _cancel_requested,
    TriggerType.UPLOAD_COMPLETE: _upload_complete,
    TriggerType.UPLOAD_EMPTY: _upload_empty,
    TriggerType.UPLOAD_DEADLINE_PASSED: _upload_deadline_passed,
    TriggerType.UPLOAD_INVALID: _upload_invalid,
    TriggerType.TRANSCRIPTION_DONE: _transcription_done,
    TriggerType.SUMMARY_DONE: _summary_done,
    TriggerType.JOB_FAILED: _job_failed,
    TriggerType.INTAKE_EXPIRED: _intake_expired,
}

assert set(_HANDLERS) == set(TriggerType), "every trigger needs a handler"
