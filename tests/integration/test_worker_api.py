"""The Worker protocol over HTTP (design doc §5.2), with a fake object store and a controllable clock."""

import base64
import os
import uuid
from datetime import timedelta

import httpx
import pytest
from app.deps import clock, db_engine, keyring, object_storage
from app.domain.state_machine import Trigger, TriggerType
from app.main import app
from app.security.keyring import Keyring
from app.security.worker_auth import new_worker_token
from app.services.dispatch import credential_aad, reap_expired_leases, scheduler_tick
from app.services.task_transitions import run_trigger
from app.services.uploads import ObjectStat, UploadTarget
from factories import NOW, make_task
from meeting_protocol import TaskStatus
from sqlalchemy import text

RING = Keyring({1: base64.b64decode(base64.b64encode(os.urandom(32)))}, 1)
SHA = "a" * 64


class FakeStorage:
    def __init__(self):
        self.objects: dict[str, ObjectStat] = {}

    async def presign_put(self, object_key, *, size, sha256, expires_at):
        return UploadTarget(
            object_key=object_key,
            url=f"https://cos.example/{object_key}?sig=x",
            expires_at=expires_at,
            headers={"x-cos-meta-sha256": sha256},
        )

    async def presign_get(self, object_key, *, expires_at):
        return f"https://cos.example/{object_key}?get"

    async def stat(self, object_key):
        return self.objects.get(object_key)

    async def delete(self, object_key):
        self.objects.pop(object_key, None)

    async def delete_prefix(self, prefix):
        for key in [k for k in self.objects if k.startswith(prefix)]:
            del self.objects[key]

    def put(self, object_key, size, sha256=SHA):
        self.objects[object_key] = ObjectStat(bytes=size, sha256=sha256)


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


@pytest.fixture
async def env(engine, clean):
    storage, tick = FakeStorage(), Clock()
    app.dependency_overrides.update(
        {db_engine: lambda: engine, clock: tick, keyring: lambda: RING, object_storage: lambda: storage}
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client, storage, tick
    app.dependency_overrides.clear()


async def make_worker(engine, name=None) -> tuple[uuid.UUID, dict]:
    token, token_hash = new_worker_token()
    async with engine.begin() as conn:
        worker_id = await conn.scalar(
            text("INSERT INTO worker_nodes (name, token_hash) VALUES (:n, :h) RETURNING id"),
            {"n": name or uuid.uuid4().hex, "h": token_hash},
        )
    return worker_id, {"Authorization": f"Bearer {token}"}


async def dispatching_task(engine, *, password="1234") -> uuid.UUID:
    async with engine.begin() as conn:
        task_id = await make_task(conn, TaskStatus.DISPATCHING)
        sealed = RING.encrypt_text(password, aad=credential_aad(task_id, "meeting_password_ciphertext"))
        await conn.execute(
            text(
                "UPDATE meeting_tasks SET meeting_password_ciphertext = :c, secret_key_version = :v WHERE id = :t"
            ),
            {"c": sealed.ciphertext, "v": sealed.key_version, "t": task_id},
        )
    return task_id


async def user_trigger(engine, task_id, trigger_type, now):
    await run_trigger(engine, task_id, Trigger(trigger_type), now=now, actor="user")


async def task_status(engine, task_id) -> str:
    async with engine.connect() as conn:
        return await conn.scalar(text("SELECT status FROM meeting_tasks WHERE id = :t"), {"t": task_id})


class Worker:
    def __init__(self, client, headers, tick):
        self.client, self.headers, self.tick = client, headers, tick
        self.seq = 0
        self.attempt_id = None

    async def lease(self):
        r = await self.client.post("/v1/workers/lease", headers=self.headers)
        if r.status_code == 200:
            self.attempt_id = r.json()["attempt_id"]
            self.seq = 0
        return r

    async def event(self, type_, **kw):
        self.seq += 1
        body = {"seq": self.seq, "type": type_, "occurred_at": self.tick.now.isoformat(), **kw}
        return await self.client.post(
            f"/v1/workers/attempts/{self.attempt_id}/events", headers=self.headers, json=body
        )

    async def heartbeat(self):
        return await self.client.post(
            f"/v1/workers/attempts/{self.attempt_id}/heartbeat", headers=self.headers
        )

    async def upload(
        self, storage, segment_no, *, size=1000, sha=SHA, rms=-20.0, attempt_id=None, stored_sha=SHA
    ):
        attempt = attempt_id or self.attempt_id
        body = {
            "file_sha256": sha,
            "bytes": size,
            "format": "wav",
            "duration_ms": 60000,
            "recorded_started_at": (self.tick.now - timedelta(minutes=1)).isoformat(),
            "recorded_ended_at": self.tick.now.isoformat(),
        }
        r = await self.client.post(
            f"/v1/workers/attempts/{attempt}/segments/{segment_no}/upload-url",
            headers=self.headers,
            json=body,
        )
        if r.status_code != 200:
            return r
        assert r.json()["upload_headers"] == {"x-cos-meta-sha256": sha}
        storage.put(r.json()["object_key"], size, stored_sha)
        return await self.client.post(
            f"/v1/workers/attempts/{attempt}/segments/{segment_no}/complete",
            headers=self.headers,
            json={"file_sha256": sha, "bytes": size, "rms_dbfs": rms},
        )


async def _join_and_record(w: Worker):
    for type_ in [
        "JOIN_REQUESTED",
        "IN_WAITING_ROOM",
        "JOINED",
        "RECORDING_NOTICE_SENT",
        "RECORDING_APPROVED",
        "RECORDING_STARTED",
    ]:
        assert (await w.event(type_)).status_code == 200


# ---------------------------------------------------------------- full meeting


async def test_simulated_meeting_end_to_end(env, engine):
    client, storage, tick = env
    task_id = await dispatching_task(engine)
    _, headers = await make_worker(engine)
    w = Worker(client, headers, tick)

    r = await w.lease()
    assert r.status_code == 200
    body = r.json()
    assert body["task"]["meeting_password"] == "1234"  # decrypted only for the Worker
    assert body["task"]["meeting_code"] == "123456789"
    assert await task_status(engine, task_id) == "JOINING"

    await _join_and_record(w)
    assert await task_status(engine, task_id) == "RECORDING"
    for n in (0, 1):
        assert (await w.event("SEGMENT_SEALED", data={"segment_no": n})).status_code == 200
    assert (await w.upload(storage, 0)).status_code == 200
    assert (await w.event("MEETING_ENDED")).status_code == 200
    assert await task_status(engine, task_id) == "UPLOADING"
    assert (await w.event("LEFT")).status_code == 200
    assert await task_status(engine, task_id) == "UPLOADING"  # segment 1 still missing
    assert (await w.upload(storage, 1)).status_code == 200  # late upload by the FINISHED attempt
    assert await task_status(engine, task_id) == "TRANSCRIBING"

    async with engine.connect() as conn:
        job = (
            await conn.execute(
                text("SELECT kind, status FROM processing_jobs WHERE task_id = :t"), {"t": task_id}
            )
        ).one()
        outcome = await conn.scalar(
            text("SELECT recording_outcome FROM meeting_tasks WHERE id = :t"), {"t": task_id}
        )
    assert tuple(job) == ("ASR", "PENDING")
    assert outcome == "FULL"


async def test_silent_recording_fails_as_empty(env, engine):
    client, storage, tick = env
    task_id = await dispatching_task(engine)
    _, headers = await make_worker(engine)
    w = Worker(client, headers, tick)
    await w.lease()
    await _join_and_record(w)
    await w.event("SEGMENT_SEALED", data={"segment_no": 0})
    await w.upload(storage, 0, rms=-80.0)
    await w.event("MEETING_ENDED")
    await w.event("LEFT")
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT status, failure_code FROM meeting_tasks WHERE id = :t"), {"t": task_id}
            )
        ).one()
    assert tuple(row) == ("FAILED", "EMPTY_AUDIO")


# ---------------------------------------------------------------- lease


async def test_lease_is_idempotent_and_exclusive(env, engine):
    client, _, tick = env
    task_id = await dispatching_task(engine)
    _, h1 = await make_worker(engine)
    _, h2 = await make_worker(engine)
    first = await client.post("/v1/workers/lease", headers=h1)
    again = await client.post("/v1/workers/lease", headers=h1)  # response lost, Worker retries
    other = await client.post("/v1/workers/lease", headers=h2)
    assert first.json()["attempt_id"] == again.json()["attempt_id"]
    assert other.status_code == 204
    async with engine.connect() as conn:
        count = await conn.scalar(
            text("SELECT attempt_count FROM meeting_tasks WHERE id = :t"), {"t": task_id}
        )
    assert count == 1


@pytest.mark.parametrize("header", [{}, {"Authorization": "Bearer nope"}, {"Authorization": "Basic x"}])
async def test_unknown_worker_is_rejected(env, header):
    client, _, _ = env
    assert (await client.post("/v1/workers/lease", headers=header)).status_code == 401


async def test_revoked_worker_is_rejected(env, engine):
    client, _, _ = env
    worker_id, headers = await make_worker(engine)
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE worker_nodes SET revoked_at = now() WHERE id = :w"), {"w": worker_id})
    assert (await client.post("/v1/workers/lease", headers=headers)).status_code == 401


# ---------------------------------------------------------------- heartbeat and fencing


async def test_heartbeat_renews_and_relays_stop(env, engine):
    client, _, tick = env
    task_id = await dispatching_task(engine)
    _, headers = await make_worker(engine)
    w = Worker(client, headers, tick)
    await w.lease()
    await _join_and_record(w)
    tick.advance(seconds=10)
    r = await w.heartbeat()
    assert r.json()["command"] == "CONTINUE"
    await user_trigger(engine, task_id, TriggerType.STOP_REQUESTED, tick.now)
    r = await w.heartbeat()
    assert r.json()["command"] == "STOP_AND_UPLOAD"


async def test_other_worker_cannot_use_an_attempt(env, engine):
    client, _, tick = env
    await dispatching_task(engine)
    _, h1 = await make_worker(engine)
    _, h2 = await make_worker(engine)
    w = Worker(client, h1, tick)
    await w.lease()
    r = await client.post(f"/v1/workers/attempts/{w.attempt_id}/heartbeat", headers=h2)
    assert (r.status_code, r.json()["reason"]) == (409, "NOT_OWNER")


async def test_lost_lease_is_reaped_and_fenced(env, engine):
    client, storage, tick = env
    task_id = await dispatching_task(engine)
    _, headers = await make_worker(engine)
    w = Worker(client, headers, tick)
    await w.lease()
    await _join_and_record(w)
    await w.event("SEGMENT_SEALED", data={"segment_no": 0})

    tick.advance(minutes=2)  # no heartbeats: the lease ran out
    assert await reap_expired_leases(engine, now=tick.now) == 1
    assert await task_status(engine, task_id) == "RETRY_WAIT"

    r = await w.heartbeat()
    assert (r.status_code, r.json()["reason"]) == (409, "EXPIRED")
    r = await w.event("RECORDING_STOPPED")
    assert r.status_code == 409
    # an EXPIRED attempt may still upload what it sealed
    assert (await w.upload(storage, 0)).status_code == 200


async def test_cancel_stops_late_uploads(env, engine):
    client, storage, tick = env
    task_id = await dispatching_task(engine)
    _, headers = await make_worker(engine)
    w = Worker(client, headers, tick)
    await w.lease()
    await _join_and_record(w)
    await w.event("SEGMENT_SEALED", data={"segment_no": 0})
    tick.advance(minutes=2)
    await reap_expired_leases(engine, now=tick.now)
    await user_trigger(engine, task_id, TriggerType.CANCEL_REQUESTED, tick.now)
    r = await w.upload(storage, 0)
    assert (r.status_code, r.json()["reason"]) == (409, "CANCELED")


async def test_late_upload_window_closes(env, engine):
    client, storage, tick = env
    await dispatching_task(engine)
    _, headers = await make_worker(engine)
    w = Worker(client, headers, tick)
    await w.lease()
    await _join_and_record(w)
    await w.event("SEGMENT_SEALED", data={"segment_no": 0})
    tick.advance(minutes=2)
    await reap_expired_leases(engine, now=tick.now)
    tick.advance(hours=3)
    r = await w.upload(storage, 0)
    assert (r.status_code, r.json()["reason"]) == (409, "EXPIRED")


async def test_unsealed_segment_is_rejected(env, engine):
    client, storage, tick = env
    await dispatching_task(engine)
    _, headers = await make_worker(engine)
    w = Worker(client, headers, tick)
    await w.lease()
    await _join_and_record(w)
    assert (await w.upload(storage, 0)).status_code == 422


async def test_object_mismatch_is_rejected(env, engine):
    client, storage, tick = env
    await dispatching_task(engine)
    _, headers = await make_worker(engine)
    w = Worker(client, headers, tick)
    await w.lease()
    await _join_and_record(w)
    await w.event("SEGMENT_SEALED", data={"segment_no": 0})
    r = await client.post(
        f"/v1/workers/attempts/{w.attempt_id}/segments/0/upload-url",
        headers=headers,
        json={
            "file_sha256": SHA,
            "bytes": 1000,
            "format": "wav",
            "duration_ms": 60000,
            "recorded_started_at": (tick.now - timedelta(minutes=1)).isoformat(),
            "recorded_ended_at": tick.now.isoformat(),
        },
    )
    storage.put(r.json()["object_key"], 999)  # truncated upload
    r = await client.post(
        f"/v1/workers/attempts/{w.attempt_id}/segments/0/complete",
        headers=headers,
        json={"file_sha256": SHA, "bytes": 1000},
    )
    assert r.status_code == 422


# ---------------------------------------------------------------- events


async def test_event_sequence_rules(env, engine):
    client, _, tick = env
    await dispatching_task(engine)
    _, headers = await make_worker(engine)
    w = Worker(client, headers, tick)
    await w.lease()
    assert (await w.event("JOIN_REQUESTED")).json() == {"accepted": True}
    w.seq -= 1
    assert (await w.event("JOIN_REQUESTED")).json() == {"accepted": False}  # retransmission
    w.seq += 1
    r = await w.event("JOINED")  # seq 3 while 2 is missing
    assert (r.status_code, r.json()["reason"]) == (409, "OUT_OF_ORDER")


async def test_failed_event_needs_a_code(env, engine):
    client, _, tick = env
    await dispatching_task(engine)
    _, headers = await make_worker(engine)
    w = Worker(client, headers, tick)
    await w.lease()
    assert (await w.event("FAILED")).status_code == 422
    w.seq -= 1
    assert (await w.event("FAILED", failure_code="WRONG_PASSWORD")).status_code == 200


async def test_unexplained_leave_is_retried(env, engine):
    client, _, tick = env
    task_id = await dispatching_task(engine)
    _, headers = await make_worker(engine)
    w = Worker(client, headers, tick)
    await w.lease()
    await w.event("JOINED")
    await w.event("LEFT")
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT status, failure_code FROM meeting_tasks WHERE id = :t"), {"t": task_id}
            )
        ).one()
    assert tuple(row) == ("RETRY_WAIT", "CLIENT_ERROR")


# ---------------------------------------------------------------- scheduler


async def test_scheduler_tick_moves_due_tasks(env, engine):
    _, _, tick = env
    async with engine.begin() as conn:
        scheduled = await make_task(conn, TaskStatus.SCHEDULED)
        no_worker = await make_task(conn, TaskStatus.DISPATCHING)
        retry = await make_task(conn, TaskStatus.RETRY_WAIT)
    counts = await scheduler_tick(engine, now=tick.now)
    assert (
        counts["DISPATCH_DUE"] == 1 and counts["DISPATCH_DEADLINE_PASSED"] == 1 and counts["RETRY_DUE"] == 1
    )
    assert await task_status(engine, scheduled) == "DISPATCHING"
    assert await task_status(engine, no_worker) == "FAILED"
    assert await task_status(engine, retry) == "DISPATCHING"


async def test_reaper_after_stop_only_expires_the_attempt(env, engine):
    client, storage, tick = env
    task_id = await dispatching_task(engine)
    _, headers = await make_worker(engine)
    w = Worker(client, headers, tick)
    await w.lease()
    await _join_and_record(w)
    await w.event("SEGMENT_SEALED", data={"segment_no": 0})
    await user_trigger(engine, task_id, TriggerType.STOP_REQUESTED, tick.now)
    tick.advance(minutes=2)  # Worker vanished before LEFT
    await reap_expired_leases(engine, now=tick.now)
    assert await task_status(engine, task_id) == "UPLOADING"
    assert (await w.upload(storage, 0)).status_code == 200  # late upload completes the task
    assert await task_status(engine, task_id) == "TRANSCRIBING"


# ---------------------------------------------------------------- review round 2 regressions


async def test_failure_after_upload_settles_without_waiting_for_deadline(env, engine):
    client, storage, tick = env
    task_id = await dispatching_task(engine)
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE meeting_tasks SET max_attempts = 1 WHERE id = :t"), {"t": task_id})
    _, headers = await make_worker(engine)
    w = Worker(client, headers, tick)
    await w.lease()
    await _join_and_record(w)
    await w.event("SEGMENT_SEALED", data={"segment_no": 0})
    await w.upload(storage, 0)
    await w.event("FAILED", failure_code="NETWORK_ERROR")  # no retries left, audio is complete
    assert await task_status(engine, task_id) == "TRANSCRIBING"


async def test_recording_limit_is_enforced_by_the_scheduler(env, engine):
    client, _, tick = env
    task_id = await dispatching_task(engine)
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE meeting_tasks SET max_record_minutes = 1 WHERE id = :t"), {"t": task_id}
        )
    _, headers = await make_worker(engine)
    w = Worker(client, headers, tick)
    await w.lease()
    await _join_and_record(w)
    counts = await scheduler_tick(engine, now=tick.now)
    assert counts["RECORDING_LIMIT_REACHED"] == 0  # joined just now
    tick.advance(minutes=2)
    await w.heartbeat()
    counts = await scheduler_tick(engine, now=tick.now)
    assert counts["RECORDING_LIMIT_REACHED"] == 1
    assert (await w.heartbeat()).json()["command"] == "STOP_AND_UPLOAD"
    async with engine.connect() as conn:
        outcome = await conn.scalar(
            text("SELECT recording_outcome FROM meeting_tasks WHERE id = :t"), {"t": task_id}
        )
    assert outcome == "LIMIT_REACHED"


@pytest.mark.parametrize(
    "event",
    [
        {"type": "FAILED", "failure_code": "CONSENT_REVOKED"},
        {"type": "RECORDING_DENIED"},
        {"type": "REMOVED_FROM_MEETING"},
    ],
)
async def test_revocation_after_stop_still_deletes(env, engine, event):
    client, storage, tick = env
    task_id = await dispatching_task(engine)
    _, headers = await make_worker(engine)
    w = Worker(client, headers, tick)
    await w.lease()
    await _join_and_record(w)
    await w.event("SEGMENT_SEALED", data={"segment_no": 0})
    await w.upload(storage, 0)
    await user_trigger(engine, task_id, TriggerType.STOP_REQUESTED, tick.now)
    kind = event.pop("type")
    assert (await w.event(kind, **event)).status_code == 200
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT status, failure_code FROM meeting_tasks WHERE id = :t"), {"t": task_id}
            )
        ).one()
        effects = (
            (await conn.execute(text("SELECT effect FROM task_effects WHERE task_id = :t"), {"t": task_id}))
            .scalars()
            .all()
        )
    assert row.status == "FAILED" and row.failure_code in ("CONSENT_REVOKED", "REMOVED_BY_HOST")
    assert "DISCARD_AUDIO" in effects


async def test_segments_before_approval_are_refused(env, engine):
    client, _, tick = env
    await dispatching_task(engine)
    _, headers = await make_worker(engine)
    w = Worker(client, headers, tick)
    await w.lease()
    for type_ in ["JOINED", "RECORDING_NOTICE_SENT"]:
        await w.event(type_)
    r = await w.event("SEGMENT_SEALED", data={"segment_no": 0})
    assert r.status_code == 422
    w.seq -= 1  # the refused event did not consume its seq
    assert (await w.event("RECORDING_APPROVED")).status_code == 200


async def test_out_of_stage_failure_code_is_not_retried(env, engine):
    client, _, tick = env
    task_id = await dispatching_task(engine)
    _, headers = await make_worker(engine)
    w = Worker(client, headers, tick)
    await w.lease()
    await w.event("IN_WAITING_ROOM")
    await w.event("FAILED", failure_code="MEETING_LOCKED")  # only valid while JOINING
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT status, failure_code FROM meeting_tasks WHERE id = :t"), {"t": task_id}
            )
        ).one()
    assert tuple(row) == ("FAILED", "INTERNAL_ERROR")


async def test_out_of_order_carries_expected_seq(env, engine):
    client, _, tick = env
    await dispatching_task(engine)
    _, headers = await make_worker(engine)
    w = Worker(client, headers, tick)
    await w.lease()
    await w.event("JOIN_REQUESTED")
    w.seq += 2
    r = await w.event("JOINED")
    assert r.status_code == 409 and r.json()["reason"] == "OUT_OF_ORDER" and r.json()["expected_seq"] == 2
    w.seq = 0
    r = await w.event("JOINED")  # seq 1 again but a different event
    assert r.status_code == 409 and r.json()["expected_seq"] == 2


async def test_relet_lease_reports_event_position(env, engine):
    client, _, tick = env
    await dispatching_task(engine)
    _, headers = await make_worker(engine)
    w = Worker(client, headers, tick)
    await w.lease()
    await _join_and_record(w)
    await w.event("SEGMENT_SEALED", data={"segment_no": 0})
    again = await client.post("/v1/workers/lease", headers=headers)
    assert (again.json()["last_event_seq"], again.json()["last_sealed_segment_no"]) == (7, 0)


async def test_complete_is_idempotent_after_the_task_moved_on(env, engine):
    client, storage, tick = env
    task_id = await dispatching_task(engine)
    _, headers = await make_worker(engine)
    w = Worker(client, headers, tick)
    await w.lease()
    await _join_and_record(w)
    await w.event("SEGMENT_SEALED", data={"segment_no": 0})
    await w.event("MEETING_ENDED")
    await w.event("LEFT")
    assert (await w.upload(storage, 0)).status_code == 200
    assert await task_status(engine, task_id) == "TRANSCRIBING"
    r = await client.post(
        f"/v1/workers/attempts/{w.attempt_id}/segments/0/complete",
        headers=headers,
        json={"file_sha256": SHA, "bytes": 1000},
    )
    assert r.status_code == 200  # the lost response is retried safely


async def test_segment_numbers_must_be_consecutive(env, engine):
    client, _, tick = env
    await dispatching_task(engine)
    _, headers = await make_worker(engine)
    w = Worker(client, headers, tick)
    await w.lease()
    await _join_and_record(w)
    assert (await w.event("SEGMENT_SEALED", data={"segment_no": 3})).status_code == 422


@pytest.mark.parametrize(
    ("segment", "body_patch", "status"),
    [
        (-1, {}, 422),
        (2**31, {}, 422),
        (0, {"format": "../x"}, 422),
    ],
)
async def test_upload_input_bounds(env, engine, segment, body_patch, status):
    client, _, tick = env
    await dispatching_task(engine)
    _, headers = await make_worker(engine)
    w = Worker(client, headers, tick)
    await w.lease()
    body = {
        "file_sha256": SHA,
        "bytes": 1000,
        "format": "wav",
        "duration_ms": 1000,
        "recorded_started_at": tick.now.isoformat(),
        "recorded_ended_at": (tick.now + timedelta(seconds=1)).isoformat(),
        **body_patch,
    }
    r = await client.post(
        f"/v1/workers/attempts/{w.attempt_id}/segments/{segment}/upload-url", headers=headers, json=body
    )
    assert r.status_code == status


async def test_infinite_loudness_is_rejected(env, engine):
    client, _, tick = env
    await dispatching_task(engine)
    _, headers = await make_worker(engine)
    w = Worker(client, headers, tick)
    await w.lease()
    r = await client.post(
        f"/v1/workers/attempts/{w.attempt_id}/segments/0/complete",
        headers=headers,
        content='{"file_sha256": "' + SHA + '", "bytes": 1, "rms_dbfs": -Infinity}',
    )
    assert r.status_code == 422


async def test_object_without_hash_metadata_is_rejected(env, engine):
    client, storage, tick = env
    await dispatching_task(engine)
    _, headers = await make_worker(engine)
    w = Worker(client, headers, tick)
    await w.lease()
    await _join_and_record(w)
    await w.event("SEGMENT_SEALED", data={"segment_no": 0})
    assert (await w.upload(storage, 0, stored_sha=None)).status_code == 422


async def test_unreadable_credentials_fail_the_task_and_dispatch_continues(env, engine):
    client, _, tick = env
    broken = await dispatching_task(engine)
    other_ring = Keyring({1: os.urandom(32)}, 1)
    async with engine.begin() as conn:
        bad = other_ring.encrypt_text("x", aad=credential_aad(broken, "meeting_password_ciphertext"))
        await conn.execute(
            text(
                "UPDATE meeting_tasks SET meeting_password_ciphertext = :c, scheduled_at = scheduled_at - interval '1 hour' WHERE id = :t"
            ),
            {"c": bad.ciphertext, "t": broken},
        )
    good = await dispatching_task(engine)
    _, headers = await make_worker(engine)
    r = await client.post("/v1/workers/lease", headers=headers)
    assert r.status_code == 200 and r.json()["task"]["task_id"] == str(good)
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT status, failure_code FROM meeting_tasks WHERE id = :t"), {"t": broken}
            )
        ).one()
    assert tuple(row) == ("FAILED", "INTERNAL_ERROR")


# ---------------------------------------------------------------- review round 2: test gaps


async def test_retry_runs_a_second_attempt_and_keeps_both_recordings(env, engine):
    client, storage, tick = env
    task_id = await dispatching_task(engine)
    _, headers = await make_worker(engine)
    w = Worker(client, headers, tick)
    await w.lease()
    first = w.attempt_id
    await _join_and_record(w)
    for n in range(3):
        await w.event("SEGMENT_SEALED", data={"segment_no": n})
    tick.advance(minutes=2)
    await reap_expired_leases(engine, now=tick.now)  # attempt 1 lost: RETRY_WAIT
    tick.advance(seconds=30)
    await scheduler_tick(engine, now=tick.now)  # RETRY_DUE: DISPATCHING
    r = await w.lease()
    assert r.json()["attempt_no"] == 2 and w.attempt_id != first
    await _join_and_record(w)
    await w.event("SEGMENT_SEALED", data={"segment_no": 0})
    assert (await w.upload(storage, 0)).status_code == 200
    await w.event("MEETING_ENDED")
    await w.event("LEFT")
    for n in (0, 1):
        assert (await w.upload(storage, n, attempt_id=first)).status_code == 200
    assert await task_status(engine, task_id) == "UPLOADING"  # attempt 1 segment 2 still missing
    assert (await w.upload(storage, 2, attempt_id=first)).status_code == 200
    assert await task_status(engine, task_id) == "TRANSCRIBING"
    async with engine.connect() as conn:
        keys = (
            (
                await conn.execute(
                    text("SELECT object_key FROM audio_assets WHERE task_id = :t"), {"t": task_id}
                )
            )
            .scalars()
            .all()
        )
    assert len(keys) == len(set(keys)) == 4


@pytest.mark.parametrize("call", ["heartbeat", "event", "upload-url", "complete"])
async def test_no_endpoint_lets_another_worker_act(env, engine, call):
    client, storage, tick = env
    await dispatching_task(engine)
    _, h1 = await make_worker(engine)
    _, h2 = await make_worker(engine)
    w = Worker(client, h1, tick)
    await w.lease()
    await _join_and_record(w)
    await w.event("SEGMENT_SEALED", data={"segment_no": 0})
    base = f"/v1/workers/attempts/{w.attempt_id}"
    upload_body = {
        "file_sha256": SHA,
        "bytes": 1000,
        "format": "wav",
        "duration_ms": 1000,
        "recorded_started_at": tick.now.isoformat(),
        "recorded_ended_at": (tick.now + timedelta(seconds=1)).isoformat(),
    }
    if call == "complete":
        await client.post(f"{base}/segments/0/upload-url", headers=h1, json=upload_body)
    requests = {
        "heartbeat": (f"{base}/heartbeat", None),
        "event": (f"{base}/events", {"seq": 8, "type": "MEETING_ENDED", "occurred_at": tick.now.isoformat()}),
        "upload-url": (f"{base}/segments/0/upload-url", upload_body),
        "complete": (f"{base}/segments/0/complete", {"file_sha256": SHA, "bytes": 1000}),
    }
    url, body = requests[call]
    r = await client.post(url, headers=h2, json=body)
    assert (r.status_code, r.json()["reason"]) == (409, "NOT_OWNER")


async def test_one_quiet_segment_does_not_make_the_meeting_empty(env, engine):
    client, storage, tick = env
    task_id = await dispatching_task(engine)
    _, headers = await make_worker(engine)
    w = Worker(client, headers, tick)
    await w.lease()
    await _join_and_record(w)
    for n in range(3):
        await w.event("SEGMENT_SEALED", data={"segment_no": n})
    await w.upload(storage, 0, rms=-80.0)
    await w.upload(storage, 1, rms=-20.0)
    await w.upload(storage, 2, rms=None)  # loudness not reported: not counted as silence
    await w.event("MEETING_ENDED")
    await w.event("LEFT")
    assert await task_status(engine, task_id) == "TRANSCRIBING"


async def test_same_size_different_content_is_rejected(env, engine):
    client, storage, tick = env
    await dispatching_task(engine)
    _, headers = await make_worker(engine)
    w = Worker(client, headers, tick)
    await w.lease()
    await _join_and_record(w)
    await w.event("SEGMENT_SEALED", data={"segment_no": 0})
    assert (await w.upload(storage, 0, stored_sha="b" * 64)).status_code == 422
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT upload_status FROM audio_assets")) == "UPLOADING"


async def test_late_upload_refused_once_the_task_moved_on(env, engine):
    client, storage, tick = env
    task_id = await dispatching_task(engine)
    _, headers = await make_worker(engine)
    w = Worker(client, headers, tick)
    await w.lease()
    await _join_and_record(w)
    for n in (0, 1):
        await w.event("SEGMENT_SEALED", data={"segment_no": n})
    await w.upload(storage, 0)
    await w.event("MEETING_ENDED")
    await w.event("LEFT")
    tick.advance(minutes=119)
    await scheduler_tick(engine, now=tick.now + timedelta(minutes=2))  # past the upload deadline
    assert await task_status(engine, task_id) == "TRANSCRIBING"
    r = await w.upload(storage, 1)
    assert (r.status_code, r.json()["reason"]) == (409, "FINISHED")


async def test_reaper_settles_a_stopped_task_whose_segments_are_all_in(env, engine):
    client, storage, tick = env
    task_id = await dispatching_task(engine)
    _, headers = await make_worker(engine)
    w = Worker(client, headers, tick)
    await w.lease()
    await _join_and_record(w)
    await w.event("SEGMENT_SEALED", data={"segment_no": 0})
    await user_trigger(engine, task_id, TriggerType.STOP_REQUESTED, tick.now)
    await w.upload(storage, 0)
    assert await task_status(engine, task_id) == "UPLOADING"  # Worker still holds its attempt
    tick.advance(minutes=2)  # it crashes before LEFT
    await reap_expired_leases(engine, now=tick.now)
    assert await task_status(engine, task_id) == "TRANSCRIBING"


@pytest.mark.parametrize("how", ["cancel", "revoke"])
async def test_cancel_and_revocation_reach_the_worker_as_canceled(env, engine, how):
    client, _, tick = env
    task_id = await dispatching_task(engine)
    _, headers = await make_worker(engine)
    w = Worker(client, headers, tick)
    await w.lease()
    await _join_and_record(w)
    if how == "cancel":
        await user_trigger(engine, task_id, TriggerType.CANCEL_REQUESTED, tick.now)
    else:
        await w.event("FAILED", failure_code="CONSENT_REVOKED")
    r = await w.heartbeat()
    assert (r.status_code, r.json()["reason"]) == (409, "CANCELED")
    r = await w.event("LEFT")
    assert (r.status_code, r.json()["reason"]) == (409, "CANCELED")


async def test_lease_takes_the_earliest_meeting(env, engine):
    client, _, tick = env
    later = await dispatching_task(engine)
    earlier = await dispatching_task(engine)
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE meeting_tasks SET scheduled_at = scheduled_at + interval '5 minutes' WHERE id = :t"),
            {"t": later},
        )
    _, headers = await make_worker(engine)
    r = await client.post("/v1/workers/lease", headers=headers)
    assert r.json()["task"]["task_id"] == str(earlier)


async def test_dispatch_starts_three_minutes_early(env, engine):
    _, _, tick = env
    async with engine.begin() as conn:
        soon = await make_task(conn, TaskStatus.SCHEDULED)
        later = await make_task(conn, TaskStatus.SCHEDULED)
        await conn.execute(
            text("UPDATE meeting_tasks SET scheduled_at = :s WHERE id = :t"),
            {"s": tick.now + timedelta(minutes=2), "t": soon},
        )
        await conn.execute(
            text("UPDATE meeting_tasks SET scheduled_at = :s WHERE id = :t"),
            {"s": tick.now + timedelta(minutes=4), "t": later},
        )
    await scheduler_tick(engine, now=tick.now)
    assert (await task_status(engine, soon), await task_status(engine, later)) == ("DISPATCHING", "SCHEDULED")


@pytest.mark.parametrize(
    ("ready", "expected"), [(True, ("TRANSCRIBING", None)), (False, ("FAILED", "UPLOAD_FAILED"))]
)
async def test_scheduler_applies_the_upload_deadline(env, engine, ready, expected):
    _, _, tick = env
    async with engine.begin() as conn:
        task_id = await make_task(
            conn, TaskStatus.UPLOADING, has_audio=True, ready_segment=ready, active_attempt=False
        )
        await conn.execute(
            text(
                "UPDATE audio_assets SET upload_status = 'UPLOADING' WHERE upload_status = 'PENDING' AND task_id = :t"
            ),
            {"t": task_id},
        )
    counts = await scheduler_tick(engine, now=tick.now)
    assert counts["UPLOAD_DEADLINE_PASSED"] == 1
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT status, failure_code FROM meeting_tasks WHERE id = :t"), {"t": task_id}
            )
        ).one()
        jobs = await conn.scalar(
            text("SELECT count(*) FROM processing_jobs WHERE task_id = :t"), {"t": task_id}
        )
    assert tuple(row) == expected
    assert jobs == (1 if ready else 0)


async def test_upload_fencing_edges(env, engine):
    client, storage, tick = env
    await dispatching_task(engine)
    _, headers = await make_worker(engine)
    w = Worker(client, headers, tick)
    await w.lease()
    await _join_and_record(w)
    await w.event("SEGMENT_SEALED", data={"segment_no": 0})
    assert (await w.upload(storage, 1)).status_code == 422  # sealed is 0: segment 1 is still being written
    assert (await w.upload(storage, 0)).status_code == 200
    again = await client.post(
        f"/v1/workers/attempts/{w.attempt_id}/segments/0/upload-url",
        headers=headers,
        json={
            "file_sha256": SHA,
            "bytes": 1000,
            "format": "wav",
            "duration_ms": 1000,
            "recorded_started_at": tick.now.isoformat(),
            "recorded_ended_at": (tick.now + timedelta(seconds=1)).isoformat(),
        },
    )
    assert again.status_code == 422
    async with engine.connect() as conn:
        assert (
            await conn.scalar(text("SELECT upload_status FROM audio_assets WHERE segment_no = 0")) == "READY"
        )


async def test_resent_left_after_finishing_is_acknowledged(env, engine):
    client, _, tick = env
    await dispatching_task(engine)
    _, headers = await make_worker(engine)
    w = Worker(client, headers, tick)
    await w.lease()
    await _join_and_record(w)
    await w.event("MEETING_ENDED")
    await w.event("LEFT")
    w.seq -= 1
    r = await w.event("LEFT")
    assert (r.status_code, r.json()) == (200, {"accepted": False})
