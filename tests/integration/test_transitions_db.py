"""The state machine and the schema must agree, and the persistence layer must apply effects precisely,
take locks in the documented order and detect concurrent writers."""

import asyncio
from datetime import timedelta

import pytest
from app.domain.state_machine import Effect, InvalidTransition, Trigger, TriggerType, decide
from app.services.task_transitions import (
    ConcurrentTransition,
    apply_trigger,
    in_transaction_with_retry,
    is_retryable_db_error,
    load_snapshot,
    persist_transition,
)
from factories import NOW, fingerprint, make_attempt, make_bystander, make_segment, make_task
from meeting_protocol import AttemptStatus, FailureCode, RecordingOutcome, TaskKind, TaskStatus
from meeting_protocol.rules import LIVE_ONLY_STATUSES
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from support.golden import LIVE_ALLOWED

S = TaskStatus
T = TriggerType
F = FailureCode


def _all_triggers() -> list[Trigger]:
    out = []
    for t in TriggerType:
        if t == T.LEASE_CLAIMED:
            continue  # persisted by the lease service only; covered by test_lease_claimed_*
        if t in (T.ATTEMPT_FAILED, T.JOB_FAILED):
            out += [Trigger(t, failure_code=c) for c in FailureCode]
        elif t == T.CONSENT_GRANTED:
            out.append(Trigger(t, consent_version="v2"))
        else:
            out.append(Trigger(t))
    return out


VARIANTS = [
    {},
    {"has_audio": True},
    {"has_audio": True, "audio": "sealed"},
    {"ready_segment": True, "active_attempt": False},
]


async def _row(conn, sql: str, task_id):
    return (await conn.execute(text(sql), {"id": task_id})).one()


async def test_every_allowed_transition_satisfies_the_schema(engine, clean):
    live_accepted: set[tuple[S, T]] = set()
    async with engine.connect() as conn:
        for kind in TaskKind:
            for status in TaskStatus:
                if kind != TaskKind.LIVE_MEETING and status in LIVE_ONLY_STATUSES:
                    continue
                for variant in VARIANTS:
                    outer = await conn.begin()
                    task_id = await make_task(conn, status, kind=kind, **variant)
                    snapshot = await load_snapshot(conn, task_id)
                    for trigger in _all_triggers():
                        try:
                            expected = decide(snapshot, trigger, NOW)
                        except InvalidTransition:
                            continue
                        savepoint = await conn.begin_nested()
                        try:
                            transition = await apply_trigger(conn, task_id, trigger, now=NOW, actor="test")
                        except Exception as exc:  # a CHECK violation lands here
                            raise AssertionError(f"{kind} {status} {variant} {trigger}: {exc}") from exc
                        assert transition == expected
                        assert (await _row(conn, "SELECT status FROM meeting_tasks WHERE id = :id", task_id))[
                            0
                        ] == expected.to_status
                        if kind == TaskKind.LIVE_MEETING:
                            live_accepted.add((status, trigger.type))
                        await savepoint.rollback()
                    await outer.rollback()

    golden = {(s, t) for t, statuses in LIVE_ALLOWED.items() if t != T.LEASE_CLAIMED for s in statuses}
    assert live_accepted == golden


# ---------------------------------------------------------------- snapshot


async def test_snapshot_reads_every_field(engine, clean):
    async with engine.begin() as conn:
        task_id = await make_task(conn, S.JOINING)
        await conn.execute(
            text(
                "UPDATE meeting_tasks SET recording_outcome = 'INTERRUPTED', actual_joined_at = :j,"
                " meeting_code = NULL, join_link_ciphertext = '\\x01', secret_key_version = 1 WHERE id = :id"
            ),
            {"id": task_id, "j": NOW - timedelta(minutes=30)},
        )
        snap = await load_snapshot(conn, task_id)
    assert snap.recording_outcome == RecordingOutcome.INTERRUPTED
    assert snap.actual_joined_at == NOW - timedelta(minutes=30)
    assert snap.has_join_info  # a join link alone is enough
    assert (snap.has_audio, snap.has_ready_audio, snap.has_active_attempt) == (False, False, True)


@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        ("sealed_only", True),
        ("registered_only", True),
        ("canceled_attempt_sealed", False),
        ("failed_segment", False),
        ("deleted_segment", False),
        ("nothing", False),
    ],
)
async def test_has_audio_definition(engine, clean, setup, expected):
    async with engine.begin() as conn:
        task_id = await make_task(conn, S.RETRY_WAIT)
        match setup:
            case "sealed_only":
                await make_attempt(conn, task_id, 1, AttemptStatus.EXPIRED, NOW, sealed=0)
            case "registered_only":
                a = await make_attempt(conn, task_id, 1, AttemptStatus.EXPIRED, NOW)
                await make_segment(conn, task_id, a, 0, "UPLOADING", NOW)
            case "canceled_attempt_sealed":
                await make_attempt(conn, task_id, 1, AttemptStatus.CANCELED, NOW, sealed=3)
            case "failed_segment" | "deleted_segment":
                a = await make_attempt(conn, task_id, 1, AttemptStatus.EXPIRED, NOW)
                await make_segment(conn, task_id, a, 0, "FAILED", NOW)
                if setup == "deleted_segment":
                    await conn.execute(
                        text(
                            "UPDATE audio_assets SET upload_status = 'DELETED', deleted_at = :n WHERE task_id = :t"
                        ),
                        {"n": NOW, "t": task_id},
                    )
        assert (await load_snapshot(conn, task_id)).has_audio is expected


# ---------------------------------------------------------------- flows and effects


async def test_live_meeting_happy_path(engine, clean):
    async with engine.begin() as conn:
        task_id = await make_task(conn, S.AWAITING_DETAILS)
        steps = [
            Trigger(T.DETAILS_COMPLETE),
            Trigger(T.CONSENT_GRANTED, consent_version="v1"),
            Trigger(T.DISPATCH_DUE),
        ]
        for step in steps:
            await apply_trigger(conn, task_id, step, now=NOW, actor="test")
        # the lease service: insert the attempt and persist LEASE_CLAIMED in one transaction
        await make_attempt(conn, task_id, 1, AttemptStatus.CLAIMED, NOW)
        snap = await load_snapshot(conn, task_id)
        lease = Trigger(T.LEASE_CLAIMED)
        await persist_transition(conn, snap, lease, decide(snap, lease, NOW), now=NOW, actor="lease")
        for step in [
            Trigger(T.ENTERED_WAITING_ROOM),
            Trigger(T.JOINED),
            Trigger(T.RECORDING_NOTICE_SENT),
            Trigger(T.RECORDING_APPROVED),
            Trigger(T.MEETING_ENDED),
        ]:
            await apply_trigger(conn, task_id, step, now=NOW, actor="worker")
        # the Worker sends LEFT, the gateway finishes the attempt and registers the uploaded segment
        await conn.execute(
            text("UPDATE task_attempts SET status = 'FINISHED', ended_at = :n WHERE task_id = :t"),
            {"n": NOW, "t": task_id},
        )
        attempt_id = await conn.scalar(
            text("SELECT id FROM task_attempts WHERE task_id = :t"), {"t": task_id}
        )
        await make_segment(conn, task_id, attempt_id, 0, "READY", NOW)
        for step in [Trigger(T.UPLOAD_COMPLETE), Trigger(T.TRANSCRIPTION_DONE), Trigger(T.SUMMARY_DONE)]:
            await apply_trigger(conn, task_id, step, now=NOW, actor="jobs")

        row = await _row(
            conn,
            "SELECT status, recording_outcome, attempt_count, lock_version FROM meeting_tasks WHERE id = :id",
            task_id,
        )
        assert tuple(row) == ("COMPLETED", "FULL", 1, 12)
        jobs = (
            await conn.execute(
                text(
                    "SELECT kind, input_version, status FROM processing_jobs WHERE task_id = :t ORDER BY kind"
                ),
                {"t": task_id},
            )
        ).all()
        assert [tuple(j) for j in jobs] == [("ASR", 1, "PENDING"), ("SUMMARY", 1, "PENDING")]
        effects = (
            await conn.execute(
                text("SELECT effect, payload->>'to' FROM task_effects WHERE task_id = :t ORDER BY id"),
                {"t": task_id},
            )
        ).all()
        assert [tuple(e) for e in effects] == [("NOTIFY_USER", "SCHEDULED"), ("NOTIFY_USER", "COMPLETED")]
        logged = await conn.scalar(
            text("SELECT count(*) FROM task_events WHERE task_id = :t AND event_type = 'STATE_CHANGED'"),
            {"t": task_id},
        )
        assert logged == 12


async def test_lease_expiry_marks_attempt_expired_and_redispatches(engine, clean):
    async with engine.begin() as conn:
        task_id = await make_task(conn, S.RECORDING, has_audio=True)
        t = await apply_trigger(conn, task_id, Trigger(T.LEASE_EXPIRED), now=NOW, actor="reaper")
        assert t.to_status == S.RETRY_WAIT
        attempts = (
            await conn.execute(
                text("SELECT attempt_no, status FROM task_attempts WHERE task_id = :t ORDER BY attempt_no"),
                {"t": task_id},
            )
        ).all()
        assert [tuple(a) for a in attempts] == [(1, "EXPIRED"), (2, "EXPIRED")]
        approval = await conn.scalar(
            text("SELECT host_recording_approval_at FROM meeting_tasks WHERE id = :t"), {"t": task_id}
        )
        assert approval is None
        t = await apply_trigger(
            conn, task_id, Trigger(T.RETRY_DUE), now=NOW + timedelta(seconds=30), actor="scheduler"
        )
        assert t.to_status == S.DISPATCHING
        row = await _row(
            conn, "SELECT failure_code, recording_outcome FROM meeting_tasks WHERE id = :id", task_id
        )
        assert tuple(row) == (None, "INTERRUPTED")


async def test_exhausted_retries_keep_earlier_audio_and_log_reason(engine, clean):
    async with engine.begin() as conn:
        task_id = await make_task(conn, S.RECORDING, has_audio=True)
        await conn.execute(
            text("UPDATE meeting_tasks SET max_attempts = attempt_count WHERE id = :t"), {"t": task_id}
        )
        failed = Trigger(T.ATTEMPT_FAILED, failure_code=F.NETWORK_ERROR)
        t = await apply_trigger(conn, task_id, failed, now=NOW, actor="worker")
        assert (t.to_status, t.reason) == (S.UPLOADING, F.NETWORK_ERROR)
        attempts = (
            await conn.execute(
                text("SELECT attempt_no, status FROM task_attempts WHERE task_id = :t ORDER BY attempt_no"),
                {"t": task_id},
            )
        ).all()
        assert [tuple(a) for a in attempts] == [(1, "EXPIRED"), (2, "FINISHED")]  # both may still upload
        data = await conn.scalar(
            text("SELECT data FROM task_events WHERE task_id = :t ORDER BY id DESC LIMIT 1"), {"t": task_id}
        )
        assert data == {
            "from": "RECORDING",
            "to": "UPLOADING",
            "trigger": "ATTEMPT_FAILED",
            "reason": "NETWORK_ERROR",
            "effects": ["END_ACTIVE_ATTEMPT"],
        }


@pytest.mark.parametrize(
    "trigger", [Trigger(T.ATTEMPT_FAILED, F.WRONG_PASSWORD), Trigger(T.CANCEL_REQUESTED)]
)
async def test_claimed_attempt_is_ended(engine, clean, trigger):
    async with engine.begin() as conn:
        task_id = await make_task(conn, S.JOINING)  # CLAIMED attempt
        await apply_trigger(conn, task_id, trigger, now=NOW, actor="test")
        status = await conn.scalar(
            text("SELECT status FROM task_attempts WHERE task_id = :t"), {"t": task_id}
        )
        assert status == ("FINISHED" if trigger.type == T.ATTEMPT_FAILED else "CANCELED")


async def test_cancel_reaches_finished_attempts_still_uploading(engine, clean):
    async with engine.begin() as conn:
        task_id = await make_task(conn, S.UPLOADING, has_audio=True, active_attempt=False)
        await make_attempt(conn, task_id, 2, AttemptStatus.FINISHED, NOW, sealed=4)
        t = await apply_trigger(conn, task_id, Trigger(T.CANCEL_REQUESTED), now=NOW, actor="user")
        assert Effect.DISCARD_AUDIO in t.effects
        statuses = (
            await conn.execute(text("SELECT status FROM task_attempts WHERE task_id = :t"), {"t": task_id})
        ).scalars()
        assert set(statuses) == {"CANCELED"}
        effects = (
            await conn.execute(
                text("SELECT effect, next_attempt_at FROM task_effects WHERE task_id = :t ORDER BY id"),
                {"t": task_id},
            )
        ).all()
        # deletion runs now and again after every upload URL issued before now has expired
        assert [e.effect for e in effects] == ["DISCARD_AUDIO", "DISCARD_AUDIO", "NOTIFY_USER"]
        assert effects[0].next_attempt_at == NOW and effects[1].next_attempt_at == NOW + timedelta(minutes=35)


async def test_cancel_during_post_processing(engine, clean):
    async with engine.begin() as conn:
        bystander = await make_bystander(conn)
        before = await fingerprint(conn, bystander)
        task_id = await make_task(conn, S.TRANSCRIBING, has_audio=True)
        await conn.execute(
            text(
                """
                INSERT INTO processing_jobs (task_id, kind, provider, status, external_job_id, submitted_at, input_version)
                VALUES (:t, 'ASR', 'tencent_asr', 'PENDING', NULL, NULL, 1),
                       (:t, 'ASR', 'tencent_asr', 'SUBMITTED', 'x', :n, 2),
                       (:t, 'SUMMARY', 'llm', 'SUCCEEDED', NULL, NULL, 1)
                """
            ),
            {"t": task_id, "n": NOW},
        )
        transcript_id = await conn.scalar(
            text(
                "INSERT INTO transcripts (task_id, source, body) VALUES (:t, 'worker_asr', '[]') RETURNING id"
            ),
            {"t": task_id},
        )
        await conn.execute(
            text("INSERT INTO meeting_minutes (task_id, transcript_id, summary) VALUES (:t, :tr, 's')"),
            {"t": task_id, "tr": transcript_id},
        )

        await apply_trigger(conn, task_id, Trigger(T.CANCEL_REQUESTED), now=NOW, actor="user")

        jobs = (
            await conn.execute(
                text(
                    "SELECT input_version, kind, status FROM processing_jobs WHERE task_id = :t ORDER BY kind, input_version"
                ),
                {"t": task_id},
            )
        ).all()
        assert [tuple(j) for j in jobs] == [
            (1, "ASR", "FAILED"),
            (2, "ASR", "FAILED"),
            (1, "SUMMARY", "SUCCEEDED"),
        ]
        left = await conn.scalar(
            text(
                "SELECT (SELECT count(*) FROM transcripts WHERE task_id = :t) + (SELECT count(*) FROM meeting_minutes WHERE task_id = :t)"
            ),
            {"t": task_id},
        )
        assert left == 0
        assert await fingerprint(conn, bystander) == before


async def test_upload_deadline_fails_unfinished_segments_only(engine, clean):
    async with engine.begin() as conn:
        bystander = await make_bystander(conn)
        before = await fingerprint(conn, bystander)
        task_id = await make_task(conn, S.UPLOADING, has_audio=True, ready_segment=True, active_attempt=True)
        attempt_id = await conn.scalar(
            text("SELECT id FROM task_attempts WHERE task_id = :t AND attempt_no = 1"), {"t": task_id}
        )
        await make_segment(conn, task_id, attempt_id, 2, "UPLOADING", NOW)

        t = await apply_trigger(conn, task_id, Trigger(T.UPLOAD_DEADLINE_PASSED), now=NOW, actor="scheduler")

        assert t.to_status == S.TRANSCRIBING
        segments = (
            await conn.execute(
                text(
                    "SELECT segment_no, upload_status FROM audio_assets WHERE task_id = :t ORDER BY segment_no"
                ),
                {"t": task_id},
            )
        ).all()
        assert [tuple(s) for s in segments] == [(0, "FAILED"), (1, "READY"), (2, "FAILED")]
        attempts = (
            await conn.execute(
                text("SELECT attempt_no, status FROM task_attempts WHERE task_id = :t ORDER BY attempt_no"),
                {"t": task_id},
            )
        ).all()
        assert [tuple(a) for a in attempts] == [(1, "EXPIRED"), (2, "FINISHED")]
        job = (
            await conn.execute(
                text("SELECT kind, input_version, status FROM processing_jobs WHERE task_id = :t"),
                {"t": task_id},
            )
        ).one()
        assert tuple(job) == ("ASR", 1, "PENDING")
        assert await fingerprint(conn, bystander) == before


async def test_effects_never_touch_other_tasks(engine, clean):
    triggers = [
        (S.RECORDING, Trigger(T.ATTEMPT_FAILED, F.CONSENT_REVOKED)),
        (S.RECORDING, Trigger(T.LEASE_EXPIRED)),
        (S.JOINING, Trigger(T.ATTEMPT_FAILED, F.WRONG_PASSWORD)),
        (S.IN_MEETING, Trigger(T.STOP_REQUESTED)),
    ]
    async with engine.begin() as conn:
        bystander = await make_bystander(conn)
        before = await fingerprint(conn, bystander)
        for status, trigger in triggers:
            task_id = await make_task(conn, status, has_audio=True)
            await apply_trigger(conn, task_id, trigger, now=NOW, actor="test")
        assert await fingerprint(conn, bystander) == before


async def test_user_upload_consent_is_recorded(engine, clean):
    async with engine.begin() as conn:
        task_id = await make_task(conn, S.AWAITING_CONSENT, kind=TaskKind.USER_UPLOAD)
        await apply_trigger(
            conn, task_id, Trigger(T.CONSENT_GRANTED, consent_version="u1"), now=NOW, actor="user"
        )
        row = await _row(
            conn, "SELECT status, consent_at, consent_version FROM meeting_tasks WHERE id = :id", task_id
        )
        assert tuple(row) == ("UPLOADING", NOW, "u1")


async def test_lease_claimed_must_go_through_the_lease_service(engine, clean):
    async with engine.begin() as conn:
        task_id = await make_task(conn, S.DISPATCHING)
        with pytest.raises(ValueError):
            await apply_trigger(conn, task_id, Trigger(T.LEASE_CLAIMED), now=NOW, actor="test")


# ---------------------------------------------------------------- concurrency


async def test_stale_snapshot_cannot_overwrite(engine, clean):
    async with engine.begin() as conn:
        task_id = await make_task(conn, S.RECORDING)

    async with engine.connect() as first, engine.connect() as second:
        await second.begin()
        stale = await load_snapshot(second, task_id, lock=False)
        stale_transition = decide(stale, Trigger(T.MEETING_ENDED), NOW)
        await second.commit()

        await first.begin()
        await apply_trigger(first, task_id, Trigger(T.STOP_REQUESTED), now=NOW, actor="user")
        await first.commit()

        await second.begin()
        with pytest.raises(ConcurrentTransition):
            await persist_transition(
                second, stale, Trigger(T.MEETING_ENDED), stale_transition, now=NOW, actor="worker"
            )
        await second.rollback()

    async with engine.connect() as conn:
        row = await _row(
            conn, "SELECT status, recording_outcome, lock_version FROM meeting_tasks WHERE id = :id", task_id
        )
    assert tuple(row) == ("UPLOADING", "STOPPED_BY_USER", 1)


async def test_lock_version_alone_detects_a_concurrent_edit(engine, clean):
    """Same status, different version (e.g. the user changed the meeting time): the old decision must lose."""
    async with engine.begin() as conn:
        task_id = await make_task(conn, S.AWAITING_CONSENT)
        stale = await load_snapshot(conn, task_id)
        await conn.execute(
            text("UPDATE meeting_tasks SET lock_version = lock_version + 1 WHERE id = :t"), {"t": task_id}
        )
        cancel = Trigger(T.CANCEL_REQUESTED)
        with pytest.raises(ConcurrentTransition):
            await persist_transition(conn, stale, cancel, decide(stale, cancel, NOW), now=NOW, actor="user")
        logged = await conn.scalar(
            text("SELECT count(*) FROM task_events WHERE task_id = :t"), {"t": task_id}
        )
        assert logged == 0


async def test_snapshot_lock_serializes_writers(engine, clean):
    """load_snapshot holds the task row: a second writer waits, then decides on the committed state."""
    async with engine.begin() as conn:
        task_id = await make_task(conn, S.RECORDING, has_audio=True)

    async with engine.connect() as first, engine.connect() as second:
        await first.begin()
        await apply_trigger(first, task_id, Trigger(T.STOP_REQUESTED), now=NOW, actor="user")

        await second.begin()
        waiting = asyncio.create_task(
            apply_trigger(second, task_id, Trigger(T.LEASE_EXPIRED), now=NOW, actor="reaper")
        )
        await asyncio.sleep(0.3)
        assert not waiting.done()  # blocked on the row lock
        await first.commit()
        with pytest.raises(InvalidTransition):  # decided on UPLOADING, where LEASE_EXPIRED does not apply
            await waiting
        await second.rollback()


async def test_deadlock_is_retryable(engine, clean):
    async with engine.begin() as conn:
        a = await make_task(conn, S.SCHEDULED)
        b = await make_task(conn, S.SCHEDULED)

    async def lock_both(first_id, second_id, conn):
        await conn.begin()
        await conn.execute(text("SELECT 1 FROM meeting_tasks WHERE id = :t FOR UPDATE"), {"t": first_id})
        await asyncio.sleep(0.2)
        await conn.execute(text("SELECT 1 FROM meeting_tasks WHERE id = :t FOR UPDATE"), {"t": second_id})

    async with engine.connect() as c1, engine.connect() as c2:
        results = await asyncio.gather(lock_both(a, b, c1), lock_both(b, a, c2), return_exceptions=True)
        errors = [r for r in results if isinstance(r, BaseException)]
        assert len(errors) == 1 and isinstance(errors[0], DBAPIError)
        assert is_retryable_db_error(errors[0])
        for c in (c1, c2):
            await c.rollback()


async def test_retry_helper_reruns_after_a_conflict(engine, clean):
    calls = []

    async def work(conn):
        calls.append(1)
        if len(calls) == 1:
            raise ConcurrentTransition("first try loses")
        return await conn.scalar(text("SELECT 42"))

    assert await in_transaction_with_retry(engine, work) == 42
    assert len(calls) == 2


@pytest.mark.parametrize(
    ("statuses", "expected"), [(["PENDING", "UPLOADING", "FAILED"], False), (["PENDING", "READY"], True)]
)
async def test_has_ready_audio_definition(engine, clean, statuses, expected):
    async with engine.begin() as conn:
        task_id = await make_task(conn, S.UPLOADING)
        attempt = await make_attempt(conn, task_id, 1, AttemptStatus.EXPIRED, NOW)
        for n, status in enumerate(statuses):
            await make_segment(conn, task_id, attempt, n, status, NOW)
        assert (await load_snapshot(conn, task_id)).has_ready_audio is expected


async def test_retry_helper_retries_a_deadlock(engine, clean):
    class Orig(Exception):
        sqlstate = "40P01"

    calls = []

    async def work(conn):
        calls.append(1)
        if len(calls) == 1:
            raise DBAPIError("SELECT 1", {}, Orig())
        return 42

    assert await in_transaction_with_retry(engine, work) == 42
    assert len(calls) == 2
