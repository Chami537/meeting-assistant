"""State machine rules from the design doc §5.3. Effects are compared exactly."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from app.domain.state_machine import (
    ATTEMPT_FAILURE_CODES,
    DISPATCH_GRACE,
    RETRY_DELAY,
    UPLOAD_WINDOW,
    Effect,
    InvalidTransition,
    TaskSnapshot,
    Trigger,
    TriggerType,
    decide,
    trigger_for_worker_event,
)
from meeting_protocol import FailureCode, RecordingOutcome, TaskKind, TaskStatus, WorkerEventType
from support.golden import LIVE_ALLOWED

S = TaskStatus
T = TriggerType
F = FailureCode
E = Effect
NOW = datetime(2026, 10, 10, 15, 0, tzinfo=UTC)


def snap(status: TaskStatus, **kw) -> TaskSnapshot:
    base = TaskSnapshot(
        id=uuid4(),
        task_kind=TaskKind.LIVE_MEETING,
        status=status,
        lock_version=0,
        attempt_count=1,
        max_attempts=3,
        scheduled_at=NOW - timedelta(minutes=5),
        planned_end_at=NOW + timedelta(hours=1),
        has_join_info=True,
        recording_outcome=None,
        dispatch_deadline_at=None,
        next_retry_at=None,
        upload_deadline_at=None,
        actual_joined_at=None,
        max_record_minutes=1,  # started 5 minutes ago: the limit has passed unless a test says otherwise
        has_audio=False,
        has_ready_audio=False,
        has_active_attempt=False,
    )
    return replace(base, **kw)


def go(status: TaskStatus, trigger: Trigger | TriggerType, **kw):
    if isinstance(trigger, TriggerType):
        trigger = Trigger(trigger)
    return decide(snap(status, **kw), trigger, NOW)


def failed(code: FailureCode) -> Trigger:
    return Trigger(T.ATTEMPT_FAILED, failure_code=code)


# ---------------------------------------------------------------- golden table


def _accepted(trigger: Trigger, **kw) -> set[TaskStatus]:
    out = set()
    for status in S:
        try:
            decide(snap(status, **kw), trigger, NOW)
        except InvalidTransition:
            continue
        out.add(status)
    return out


@pytest.mark.parametrize("trigger_type", list(T))
def test_accepted_statuses_match_golden_table(trigger_type):
    neutral = {"has_ready_audio": True}  # lets UPLOAD_COMPLETE through; other flags stay at their defaults
    if trigger_type == T.JOB_FAILED:
        accepted = _accepted(Trigger(T.JOB_FAILED, F.ASR_FAILED), **neutral) | _accepted(
            Trigger(T.JOB_FAILED, F.LLM_FAILED), **neutral
        )
    else:
        trigger = Trigger(trigger_type, failure_code=F.WORKER_LOST, consent_version="v1")
        accepted = _accepted(trigger, **neutral)
    assert accepted == LIVE_ALLOWED[trigger_type]


def test_naive_time_is_rejected():
    with pytest.raises(ValueError):
        decide(snap(S.AWAITING_DETAILS), Trigger(T.DETAILS_COMPLETE), datetime(2026, 10, 10, 15, 0))


# ---------------------------------------------------------------- intake and scheduling


def test_details_complete():
    t = go(S.AWAITING_DETAILS, T.DETAILS_COMPLETE)
    assert (t.to_status, t.updates, t.effects) == (S.AWAITING_CONSENT, {}, ())


def test_consent_schedules_live_meeting():
    t = go(S.AWAITING_CONSENT, Trigger(T.CONSENT_GRANTED, consent_version="v1"))
    assert t.to_status == S.SCHEDULED
    assert t.updates == {"consent_at": NOW, "consent_version": "v1"}
    assert t.effects == (E.NOTIFY_USER,)


def test_consent_needs_version():
    with pytest.raises(InvalidTransition):
        go(S.AWAITING_CONSENT, T.CONSENT_GRANTED)


@pytest.mark.parametrize(
    "missing", [{"scheduled_at": None}, {"planned_end_at": None}, {"has_join_info": False}]
)
def test_consent_needs_meeting_fields(missing):
    with pytest.raises(InvalidTransition):
        go(S.AWAITING_CONSENT, Trigger(T.CONSENT_GRANTED, consent_version="v1"), **missing)


def test_consent_for_a_meeting_that_is_over_is_refused():
    with pytest.raises(InvalidTransition):
        go(
            S.AWAITING_CONSENT,
            Trigger(T.CONSENT_GRANTED, consent_version="v1"),
            scheduled_at=NOW - timedelta(hours=2),
            planned_end_at=NOW,
        )


@pytest.mark.parametrize("status", [S.AWAITING_DETAILS, S.AWAITING_CONSENT])
def test_intake_expiry_cancels_silently(status):
    t = go(status, T.INTAKE_EXPIRED)
    assert (t.to_status, t.effects, t.updates) == (S.CANCELED, (), {})


@pytest.mark.parametrize(
    ("status", "trigger"),
    [
        (S.AWAITING_DETAILS, Trigger(T.INTAKE_EXPIRED)),
        (S.AWAITING_CONSENT, Trigger(T.CANCEL_REQUESTED)),
        (S.UPLOADING, Trigger(T.UPLOAD_EMPTY)),
        (S.UPLOADING, Trigger(T.UPLOAD_DEADLINE_PASSED)),
    ],
)
def test_user_upload_that_ends_unprocessed_is_discarded(status, trigger):
    t = go(status, trigger, task_kind=TaskKind.USER_UPLOAD, scheduled_at=None, planned_end_at=None)
    assert E.DISCARD_AUDIO in t.effects


def test_consent_user_upload_goes_to_uploading():
    t = go(
        S.AWAITING_CONSENT,
        Trigger(T.CONSENT_GRANTED, consent_version="u1"),
        task_kind=TaskKind.USER_UPLOAD,
        scheduled_at=None,
        planned_end_at=None,
        has_join_info=False,
    )
    assert t.to_status == S.UPLOADING
    assert t.updates == {
        "consent_at": NOW,
        "consent_version": "u1",
        "upload_deadline_at": NOW + UPLOAD_WINDOW,
    }
    assert t.effects == (E.ENQUEUE_INGEST,)


def test_consent_forwarded_minutes_goes_to_summarizing():
    t = go(
        S.AWAITING_CONSENT,
        Trigger(T.CONSENT_GRANTED, consent_version="u1"),
        task_kind=TaskKind.FORWARDED_MINUTES,
        has_join_info=False,
    )
    assert t.to_status == S.SUMMARIZING
    assert t.updates == {"consent_at": NOW, "consent_version": "u1"}
    assert t.effects == (E.ENQUEUE_SUMMARY,)


def test_dispatch_due_three_minutes_early():
    t = go(S.SCHEDULED, T.DISPATCH_DUE, scheduled_at=NOW + timedelta(minutes=3))
    assert t.to_status == S.DISPATCHING
    assert t.updates == {"dispatch_deadline_at": NOW + timedelta(minutes=3) + DISPATCH_GRACE}


def test_dispatch_not_due_yet():
    with pytest.raises(InvalidTransition):
        go(S.SCHEDULED, T.DISPATCH_DUE, scheduled_at=NOW + timedelta(minutes=4))


def test_dispatch_deadline_counts_from_now_when_late():
    t = go(S.SCHEDULED, T.DISPATCH_DUE, scheduled_at=NOW - timedelta(hours=1))
    assert t.updates["dispatch_deadline_at"] == NOW + DISPATCH_GRACE


def test_lease_claimed():
    t = go(S.DISPATCHING, T.LEASE_CLAIMED, attempt_count=1)
    assert (t.to_status, t.updates, t.effects) == (
        S.JOINING,
        {"attempt_count": 2, "dispatch_deadline_at": None},
        (),
    )


def test_lease_refused_when_attempts_exhausted():
    with pytest.raises(InvalidTransition):
        go(S.DISPATCHING, T.LEASE_CLAIMED, attempt_count=3, max_attempts=3)


def test_no_worker_fails_without_retry():
    t = go(S.DISPATCHING, T.DISPATCH_DEADLINE_PASSED, dispatch_deadline_at=NOW)
    assert t.to_status == S.FAILED
    assert t.updates == {"failure_code": F.NO_WORKER, "next_retry_at": None}
    assert (t.effects, t.reason) == ((E.NOTIFY_USER,), F.NO_WORKER)


def test_no_worker_keeps_earlier_audio():
    t = go(S.DISPATCHING, T.DISPATCH_DEADLINE_PASSED, dispatch_deadline_at=NOW, has_audio=True)
    assert t.to_status == S.UPLOADING
    assert t.updates == {
        "recording_outcome": RecordingOutcome.INTERRUPTED,
        "upload_deadline_at": NOW + UPLOAD_WINDOW,
    }
    assert (t.effects, t.reason) == ((), F.NO_WORKER)


def test_dispatch_deadline_not_reached():
    with pytest.raises(InvalidTransition):
        go(S.DISPATCHING, T.DISPATCH_DEADLINE_PASSED, dispatch_deadline_at=NOW + timedelta(seconds=1))


# ---------------------------------------------------------------- joining and approval


def test_waiting_room_then_joined():
    assert go(S.JOINING, T.ENTERED_WAITING_ROOM).to_status == S.WAITING_ROOM
    t = go(S.WAITING_ROOM, T.JOINED)
    assert (t.to_status, t.updates) == (S.IN_MEETING, {"actual_joined_at": NOW})


def test_rejoin_keeps_first_join_time():
    assert go(S.JOINING, T.JOINED, actual_joined_at=NOW - timedelta(minutes=30)).updates == {}


@pytest.mark.parametrize("trigger", [T.RECORDING_NOTICE_SENT, T.RECORDING_NOTICE_FAILED])
def test_notice_leads_to_awaiting_approval(trigger):
    t = go(S.IN_MEETING, trigger)
    assert (t.to_status, t.updates, t.effects) == (S.AWAITING_RECORDING_APPROVAL, {}, ())


def test_approval_starts_recording():
    t = go(S.AWAITING_RECORDING_APPROVAL, T.RECORDING_APPROVED)
    assert (t.to_status, t.updates, t.effects) == (S.RECORDING, {"host_recording_approval_at": NOW}, ())


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (S.JOINING, F.HOST_REJECTED),
        (S.JOINING, F.MEETING_LOCKED),
        (S.JOINING, F.WRONG_PASSWORD),
        (S.JOINING, F.MEETING_NOT_STARTED_TIMEOUT),
        (S.WAITING_ROOM, F.HOST_REJECTED),
        (S.WAITING_ROOM, F.WAITING_ROOM_TIMEOUT),
        (S.AWAITING_RECORDING_APPROVAL, F.RECORDING_DENIED),
        (S.AWAITING_RECORDING_APPROVAL, F.RECORDING_APPROVAL_TIMEOUT),
        (S.IN_MEETING, F.INTERNAL_ERROR),
    ],
)
def test_non_retryable_failures(status, code):
    t = decide(snap(status, has_active_attempt=True), failed(code), NOW)
    assert t.to_status == S.FAILED
    assert t.updates == {"failure_code": code, "next_retry_at": None}
    assert (t.effects, t.reason) == ((E.END_ACTIVE_ATTEMPT, E.NOTIFY_USER), code)


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (S.AWAITING_RECORDING_APPROVAL, F.RECORDING_DENIED),
        (S.AWAITING_RECORDING_APPROVAL, F.RECORDING_APPROVAL_TIMEOUT),
        (S.JOINING, F.WRONG_PASSWORD),
    ],
)
def test_non_retryable_failure_on_rejoin_keeps_earlier_audio(status, code):
    t = decide(snap(status, has_audio=True), failed(code), NOW)
    assert t.to_status == S.UPLOADING
    assert t.updates["recording_outcome"] == RecordingOutcome.INTERRUPTED
    assert (t.effects, t.reason) == ((E.END_ACTIVE_ATTEMPT,), code)


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (S.RECORDING, F.HOST_REJECTED),
        (S.RECORDING, F.WRONG_PASSWORD),
        (S.RECORDING, F.RECORDING_APPROVAL_TIMEOUT),
        (S.IN_MEETING, F.RECORDING_DENIED),
        (S.JOINING, F.WAITING_ROOM_TIMEOUT),
        (S.WAITING_ROOM, F.WRONG_PASSWORD),
    ],
)
def test_failure_codes_are_specific_to_their_stage(status, code):
    with pytest.raises(InvalidTransition):
        decide(snap(status), failed(code), NOW)


def test_attempt_failure_code_set():
    expected = {
        F.WORKER_LOST,
        F.NETWORK_ERROR,
        F.CLIENT_ERROR,
        F.INTERNAL_ERROR,
        F.CONSENT_REVOKED,
        F.REMOVED_BY_HOST,
        F.HOST_REJECTED,
        F.MEETING_LOCKED,
        F.WRONG_PASSWORD,
        F.MEETING_NOT_STARTED_TIMEOUT,
        F.WAITING_ROOM_TIMEOUT,
        F.RECORDING_DENIED,
        F.RECORDING_APPROVAL_TIMEOUT,
    }
    assert ATTEMPT_FAILURE_CODES == expected


@pytest.mark.parametrize("code", sorted(set(F) - set(ATTEMPT_FAILURE_CODES)) + [None])
def test_attempt_failure_rejects_foreign_codes(code):
    for status in (S.JOINING, S.RECORDING):
        with pytest.raises(InvalidTransition):
            decide(snap(status), Trigger(T.ATTEMPT_FAILED, failure_code=code), NOW)


@pytest.mark.parametrize("status", [S.JOINING, S.WAITING_ROOM, S.IN_MEETING, S.AWAITING_RECORDING_APPROVAL])
def test_meeting_ended_before_recording(status):
    t = go(status, T.MEETING_ENDED, has_active_attempt=True)
    assert t.to_status == S.FAILED
    assert (t.updates["failure_code"], t.effects) == (F.MEETING_ENDED, (E.END_ACTIVE_ATTEMPT, E.NOTIFY_USER))
    t = go(status, T.MEETING_ENDED, has_audio=True, has_active_attempt=True)
    assert (t.to_status, t.effects, t.reason) == (S.UPLOADING, (E.END_ACTIVE_ATTEMPT,), F.MEETING_ENDED)


@pytest.mark.parametrize("status", [S.WAITING_ROOM, S.IN_MEETING, S.AWAITING_RECORDING_APPROVAL, S.RECORDING])
@pytest.mark.parametrize("code", [F.REMOVED_BY_HOST, F.CONSENT_REVOKED])
def test_removal_and_revocation_discard_audio(status, code):
    t = decide(snap(status, has_audio=True), failed(code), NOW)
    assert t.to_status == S.FAILED
    assert t.updates == {"failure_code": code, "next_retry_at": None}
    assert t.effects == (E.CANCEL_ATTEMPTS, E.DISCARD_AUDIO, E.NOTIFY_USER)


def test_recording_denied_while_recording_is_a_revocation():
    t = decide(snap(S.RECORDING, has_audio=True), failed(F.RECORDING_DENIED), NOW)
    assert t.updates["failure_code"] == F.CONSENT_REVOKED
    assert t.effects == (E.CANCEL_ATTEMPTS, E.DISCARD_AUDIO, E.NOTIFY_USER)


# ---------------------------------------------------------------- recording outcomes


def test_meeting_ended_while_recording():
    t = go(S.RECORDING, T.MEETING_ENDED, has_active_attempt=True)
    assert t.to_status == S.UPLOADING
    assert t.updates == {
        "recording_outcome": RecordingOutcome.FULL,
        "actual_ended_at": NOW,
        "upload_deadline_at": NOW + UPLOAD_WINDOW,
    }
    assert t.effects == ()  # the Worker leaves on its own and sends LEFT


def test_limit_reached():
    t = go(S.RECORDING, T.RECORDING_LIMIT_REACHED)
    assert t.updates == {
        "recording_outcome": RecordingOutcome.LIMIT_REACHED,
        "upload_deadline_at": NOW + UPLOAD_WINDOW,
    }
    assert t.effects == ()


def test_stop_while_recording():
    t = go(S.RECORDING, T.STOP_REQUESTED)
    assert t.to_status == S.UPLOADING
    assert t.updates == {
        "stop_requested_at": NOW,
        "recording_outcome": RecordingOutcome.STOPPED_BY_USER,
        "upload_deadline_at": NOW + UPLOAD_WINDOW,
    }
    assert t.effects == ()


@pytest.mark.parametrize("trigger", [T.MEETING_ENDED, T.RECORDING_LIMIT_REACHED, T.STOP_REQUESTED])
def test_interrupted_is_sticky(trigger):
    t = go(S.RECORDING, trigger, recording_outcome=RecordingOutcome.INTERRUPTED)
    assert t.updates["recording_outcome"] == RecordingOutcome.INTERRUPTED


# ---------------------------------------------------------------- retry


@pytest.mark.parametrize("code", [F.WORKER_LOST, F.NETWORK_ERROR, F.CLIENT_ERROR])
def test_retryable_failure_waits_and_clears_approval(code):
    t = decide(snap(S.RECORDING, has_audio=True), failed(code), NOW)
    assert t.to_status == S.RETRY_WAIT
    assert t.updates == {
        "next_retry_at": NOW + RETRY_DELAY,
        "failure_code": code,
        "host_recording_approval_at": None,
        "recording_outcome": RecordingOutcome.INTERRUPTED,
    }
    assert (t.effects, t.reason) == ((E.END_ACTIVE_ATTEMPT,), code)


def test_lease_expired_expires_the_attempt():
    t = go(S.IN_MEETING, T.LEASE_EXPIRED)
    assert t.to_status == S.RETRY_WAIT
    assert t.updates == {
        "next_retry_at": NOW + RETRY_DELAY,
        "failure_code": F.WORKER_LOST,
        "host_recording_approval_at": None,
    }
    assert t.effects == (E.EXPIRE_ACTIVE_ATTEMPT,)


@pytest.mark.parametrize(
    "kw",
    [
        {"attempt_count": 3, "max_attempts": 3},
        {"planned_end_at": NOW},
        {"planned_end_at": NOW - timedelta(minutes=1)},
    ],
)
def test_no_retry_when_exhausted_or_meeting_over(kw):
    t = go(S.RECORDING, T.LEASE_EXPIRED, **kw)
    assert (t.to_status, t.effects) == (S.FAILED, (E.EXPIRE_ACTIVE_ATTEMPT, E.NOTIFY_USER))
    t = go(S.RECORDING, T.LEASE_EXPIRED, has_audio=True, **kw)
    assert t.to_status == S.UPLOADING
    assert t.updates["recording_outcome"] == RecordingOutcome.INTERRUPTED
    assert (t.effects, t.reason) == ((E.EXPIRE_ACTIVE_ATTEMPT,), F.WORKER_LOST)


def test_retry_due_redispatches_and_clears_error():
    t = go(S.RETRY_WAIT, T.RETRY_DUE, next_retry_at=NOW, scheduled_at=NOW - timedelta(minutes=50))
    assert t.to_status == S.DISPATCHING
    assert t.updates == {
        "failure_code": None,
        "next_retry_at": None,
        "dispatch_deadline_at": NOW + DISPATCH_GRACE,
    }


def test_retry_not_due_yet():
    with pytest.raises(InvalidTransition):
        go(S.RETRY_WAIT, T.RETRY_DUE, next_retry_at=NOW + timedelta(seconds=1))


# ---------------------------------------------------------------- stop / cancel matrix (§5.3)


@pytest.mark.parametrize(
    "status",
    [
        S.AWAITING_DETAILS,
        S.AWAITING_CONSENT,
        S.SCHEDULED,
        S.DISPATCHING,
        S.JOINING,
        S.AWAITING_RECORDING_APPROVAL,
        S.RETRY_WAIT,
    ],
)
def test_stop_without_audio_means_cancel(status):
    assert go(status, T.STOP_REQUESTED).to_status == S.CANCELED


@pytest.mark.parametrize(
    ("status", "active"),
    [
        (S.RETRY_WAIT, False),
        (S.DISPATCHING, False),
        (S.JOINING, True),
        (S.WAITING_ROOM, True),
        (S.IN_MEETING, True),
        (S.AWAITING_RECORDING_APPROVAL, True),
    ],
)
def test_stop_while_reconnecting_summarizes_earlier_audio(status, active):
    t = go(status, T.STOP_REQUESTED, has_audio=True, has_active_attempt=active)
    assert t.to_status == S.UPLOADING
    assert t.updates["recording_outcome"] == RecordingOutcome.INTERRUPTED
    assert t.updates["stop_requested_at"] == NOW
    assert t.effects == ((E.END_ACTIVE_ATTEMPT,) if active else ())
    if status == S.RETRY_WAIT:
        assert t.updates["failure_code"] is None


@pytest.mark.parametrize(
    "status", [S.UPLOADING, S.TRANSCRIBING, S.SUMMARIZING, S.COMPLETED, S.CANCELED, S.FAILED]
)
def test_stop_is_invalid_after_recording(status):
    with pytest.raises(InvalidTransition):
        go(status, T.STOP_REQUESTED)


@pytest.mark.parametrize(
    ("status", "has_audio", "expected"),
    [
        (S.AWAITING_DETAILS, False, (E.NOTIFY_USER,)),
        (S.SCHEDULED, False, (E.NOTIFY_USER,)),
        (S.DISPATCHING, False, (E.CANCEL_ATTEMPTS, E.NOTIFY_USER)),
        (S.IN_MEETING, False, (E.CANCEL_ATTEMPTS, E.NOTIFY_USER)),
        (S.IN_MEETING, True, (E.CANCEL_ATTEMPTS, E.DISCARD_AUDIO, E.NOTIFY_USER)),
        (S.RECORDING, False, (E.CANCEL_ATTEMPTS, E.DISCARD_AUDIO, E.NOTIFY_USER)),
        (S.RETRY_WAIT, False, (E.CANCEL_ATTEMPTS, E.NOTIFY_USER)),
        (S.RETRY_WAIT, True, (E.CANCEL_ATTEMPTS, E.DISCARD_AUDIO, E.NOTIFY_USER)),
        (
            S.TRANSCRIBING,
            True,
            (E.CANCEL_ATTEMPTS, E.CANCEL_PROCESSING_JOBS, E.DELETE_RESULTS, E.DISCARD_AUDIO, E.NOTIFY_USER),
        ),
    ],
)
def test_cancel_matrix(status, has_audio, expected):
    t = go(status, T.CANCEL_REQUESTED, has_audio=has_audio)
    assert t.to_status == S.CANCELED
    assert t.effects == expected
    assert t.updates == {"cancel_requested_at": NOW, "failure_code": None, "next_retry_at": None}


@pytest.mark.parametrize("status", [S.COMPLETED, S.CANCELED, S.FAILED])
def test_cancel_is_invalid_when_terminal(status):
    with pytest.raises(InvalidTransition):
        go(status, T.CANCEL_REQUESTED)


# ---------------------------------------------------------------- post-processing


def test_upload_complete_then_asr_then_summary():
    t = go(S.UPLOADING, T.UPLOAD_COMPLETE, has_ready_audio=True)
    assert (t.to_status, t.effects) == (S.TRANSCRIBING, (E.ENQUEUE_ASR,))
    t = go(S.TRANSCRIBING, T.TRANSCRIPTION_DONE)
    assert (t.to_status, t.effects) == (S.SUMMARIZING, (E.ENQUEUE_SUMMARY,))
    t = go(S.SUMMARIZING, T.SUMMARY_DONE)
    assert (t.to_status, t.effects) == (S.COMPLETED, (E.NOTIFY_USER,))


@pytest.mark.parametrize("trigger", [T.UPLOAD_COMPLETE, T.UPLOAD_EMPTY])
def test_upload_cannot_finish_while_worker_still_recording(trigger):
    with pytest.raises(InvalidTransition):
        go(S.UPLOADING, trigger, has_ready_audio=True, has_active_attempt=True)


def test_upload_complete_needs_ready_audio():
    with pytest.raises(InvalidTransition):
        go(S.UPLOADING, T.UPLOAD_COMPLETE)


def test_empty_audio():
    t = go(S.UPLOADING, T.UPLOAD_EMPTY)
    assert (t.updates["failure_code"], t.effects) == (F.EMPTY_AUDIO, (E.NOTIFY_USER,))


@pytest.mark.parametrize(
    ("ready", "active", "expected_status", "expected_effects"),
    [
        (True, False, S.TRANSCRIBING, (E.FAIL_UNFINISHED_SEGMENTS, E.ENQUEUE_ASR)),
        (True, True, S.TRANSCRIBING, (E.END_ACTIVE_ATTEMPT, E.FAIL_UNFINISHED_SEGMENTS, E.ENQUEUE_ASR)),
        (False, False, S.FAILED, (E.FAIL_UNFINISHED_SEGMENTS, E.NOTIFY_USER)),
        (False, True, S.FAILED, (E.END_ACTIVE_ATTEMPT, E.FAIL_UNFINISHED_SEGMENTS, E.NOTIFY_USER)),
    ],
)
def test_upload_deadline(ready, active, expected_status, expected_effects):
    t = go(
        S.UPLOADING,
        T.UPLOAD_DEADLINE_PASSED,
        upload_deadline_at=NOW,
        has_ready_audio=ready,
        has_active_attempt=active,
    )
    assert (t.to_status, t.effects) == (expected_status, expected_effects)


def test_upload_deadline_not_reached():
    with pytest.raises(InvalidTransition):
        go(S.UPLOADING, T.UPLOAD_DEADLINE_PASSED, upload_deadline_at=NOW + timedelta(seconds=1))


@pytest.mark.parametrize(
    ("kind", "status", "effects"),
    [
        (TaskKind.USER_UPLOAD, S.AWAITING_DETAILS, (E.DISCARD_AUDIO, E.NOTIFY_USER)),
        (TaskKind.USER_UPLOAD, S.UPLOADING, (E.DISCARD_AUDIO, E.NOTIFY_USER)),
        (TaskKind.FORWARDED_MINUTES, S.AWAITING_CONSENT, (E.NOTIFY_USER,)),
        (TaskKind.FORWARDED_MINUTES, S.SUMMARIZING, (E.CANCEL_PROCESSING_JOBS, E.NOTIFY_USER)),
    ],
)
def test_invalid_input(kind, status, effects):
    t = go(status, T.UPLOAD_INVALID, task_kind=kind)
    assert (t.updates["failure_code"], t.effects) == (F.INVALID_UPLOAD, effects)


def test_input_that_could_not_be_fetched():
    trigger = Trigger(T.UPLOAD_INVALID, failure_code=F.UPLOAD_FAILED)
    t = go(S.UPLOADING, trigger, task_kind=TaskKind.USER_UPLOAD)
    assert t.updates["failure_code"] == F.UPLOAD_FAILED
    with pytest.raises(InvalidTransition):
        go(S.UPLOADING, Trigger(T.UPLOAD_INVALID, failure_code=F.ASR_FAILED), task_kind=TaskKind.USER_UPLOAD)


@pytest.mark.parametrize(
    ("kind", "status"),
    [
        (TaskKind.LIVE_MEETING, S.UPLOADING),
        (TaskKind.USER_UPLOAD, S.SUMMARIZING),
        (TaskKind.FORWARDED_MINUTES, S.UPLOADING),
    ],
)
def test_invalid_input_rejected_elsewhere(kind, status):
    with pytest.raises(InvalidTransition):
        go(status, T.UPLOAD_INVALID, task_kind=kind)


@pytest.mark.parametrize(
    ("status", "code", "ok"),
    [
        (S.TRANSCRIBING, F.ASR_FAILED, True),
        (S.TRANSCRIBING, F.EMPTY_AUDIO, True),
        (S.SUMMARIZING, F.LLM_FAILED, True),
        (S.SUMMARIZING, F.EMPTY_AUDIO, False),
        (S.TRANSCRIBING, F.LLM_FAILED, False),
        (S.UPLOADING, F.ASR_FAILED, False),
    ],
)
def test_job_failed(status, code, ok):
    trigger = Trigger(T.JOB_FAILED, failure_code=code)
    if ok:
        t = go(status, trigger)
        assert (t.updates["failure_code"], t.effects) == (code, (E.NOTIFY_USER,))
    else:
        with pytest.raises(InvalidTransition):
            go(status, trigger)


# ---------------------------------------------------------------- global properties


def _all_triggers():
    return [Trigger(t, failure_code=c, consent_version="v") for t in T for c in [*F, None]]


def test_terminal_states_accept_nothing():
    for status in (S.COMPLETED, S.CANCELED, S.FAILED):
        for trigger in _all_triggers():
            with pytest.raises(InvalidTransition):
                decide(snap(status, has_ready_audio=True), trigger, NOW)


def test_failure_code_only_set_for_failed_or_retry_wait():
    """Mirrors the schema CHECK: every decided transition leaves failure_code consistent."""
    for status in S:
        for kind in TaskKind:
            for flags in ({}, {"has_audio": True}, {"has_ready_audio": True, "has_active_attempt": True}):
                task = snap(
                    status,
                    task_kind=kind,
                    dispatch_deadline_at=NOW,
                    next_retry_at=NOW,
                    upload_deadline_at=NOW,
                    **flags,
                )
                for trigger in _all_triggers():
                    try:
                        t = decide(task, trigger, NOW)
                    except InvalidTransition:
                        continue
                    code = t.updates.get("failure_code", "unchanged")
                    if t.to_status in (S.FAILED, S.RETRY_WAIT):
                        assert code not in (None, "unchanged"), (status, trigger, t)
                        assert t.reason == code
                    elif status in (S.FAILED, S.RETRY_WAIT):
                        assert code is None, (status, trigger, t)


# ---------------------------------------------------------------- worker event mapping

EXPECTED_MAPPING = {
    WorkerEventType.JOIN_REQUESTED: None,
    WorkerEventType.IN_WAITING_ROOM: Trigger(T.ENTERED_WAITING_ROOM),
    WorkerEventType.JOINED: Trigger(T.JOINED),
    WorkerEventType.RECORDING_NOTICE_SENT: Trigger(T.RECORDING_NOTICE_SENT),
    WorkerEventType.RECORDING_NOTICE_FAILED: Trigger(T.RECORDING_NOTICE_FAILED),
    WorkerEventType.RECORDING_APPROVED: Trigger(T.RECORDING_APPROVED),
    WorkerEventType.RECORDING_DENIED: Trigger(T.ATTEMPT_FAILED, F.RECORDING_DENIED),
    WorkerEventType.RECORDING_STARTED: None,
    WorkerEventType.SEGMENT_SEALED: None,
    WorkerEventType.RECORDING_STOPPED: None,
    WorkerEventType.REMOVED_FROM_MEETING: Trigger(T.ATTEMPT_FAILED, F.REMOVED_BY_HOST),
    WorkerEventType.MEETING_ENDED: Trigger(T.MEETING_ENDED),
    WorkerEventType.LEFT: None,
    WorkerEventType.FAILED: Trigger(T.ATTEMPT_FAILED, F.INTERNAL_ERROR),  # no code: non-retryable fallback
}


def test_mapping_covers_every_worker_event():
    assert set(EXPECTED_MAPPING) == set(WorkerEventType)


@pytest.mark.parametrize(("event", "expected"), list(EXPECTED_MAPPING.items()))
def test_worker_event_mapping(event, expected):
    assert trigger_for_worker_event(event) == expected


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (F.MEETING_ENDED, Trigger(T.MEETING_ENDED)),
        (F.WRONG_PASSWORD, Trigger(T.ATTEMPT_FAILED, F.WRONG_PASSWORD)),
        (F.NETWORK_ERROR, Trigger(T.ATTEMPT_FAILED, F.NETWORK_ERROR)),
    ],
)
def test_failed_event_codes(code, expected):
    assert trigger_for_worker_event(WorkerEventType.FAILED, code) == expected
