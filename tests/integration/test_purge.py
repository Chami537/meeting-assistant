"""Retention and erasure (design doc §10)."""

from datetime import timedelta

import pytest
from app.services import purge as p
from factories import NOW, make_segment, make_task
from meeting_protocol import TaskStatus
from sqlalchemy import text


class Storage:
    def __init__(self):
        self.deleted_prefixes: list[str] = []

    async def delete_prefix(self, prefix):
        self.deleted_prefixes.append(prefix)


async def run(engine, storage=None, now=NOW):
    return await p.purge(engine, storage, now=now)


async def q(engine, sql, **params):
    async with engine.connect() as conn:
        return (await conn.execute(text(sql), params)).all()


async def exec_(engine, sql, **params):
    async with engine.begin() as conn:
        return await conn.execute(text(sql), params)


async def with_secrets(engine, status):
    async with engine.begin() as conn:
        task_id = await make_task(conn, status)
        await conn.execute(
            text(
                "UPDATE meeting_tasks SET join_link_ciphertext = 'x', meeting_password_ciphertext = 'y', "
                "secret_key_version = 1 WHERE id = :t"
            ),
            {"t": task_id},
        )
        user_id = await conn.scalar(text("SELECT user_id FROM meeting_tasks WHERE id = :t"), {"t": task_id})
    return task_id, user_id


async def inbox(engine, user_id, msg_id, status="DONE", received_at=NOW):
    await exec_(
        engine,
        "INSERT INTO kf_inbox (open_kfid, msg_id, user_id, message_type, payload_redacted, content_ciphertext, "
        "content_key_version, status, received_at) VALUES ('wk', :m, :u, 'text', '{}', 'c', 1, :s, :r)",
        m=msg_id,
        u=user_id,
        s=status,
        r=received_at,
    )


async def test_meeting_secrets_go_once_the_meeting_is_over(engine, clean):
    done, user = await with_secrets(engine, TaskStatus.TRANSCRIBING)
    waiting, _ = await with_secrets(engine, TaskStatus.RETRY_WAIT)  # will rejoin: still needs them
    await inbox(engine, user, "m1")
    await inbox(engine, user, "m2", status="PENDING")
    report = await run(engine)
    assert report["secrets"] == 1
    rows = {
        r.id: r
        for r in await q(
            engine,
            "SELECT id, join_link_ciphertext, meeting_password_ciphertext, secrets_purged_at FROM meeting_tasks",
        )
    }
    assert (rows[done].join_link_ciphertext, rows[done].meeting_password_ciphertext) == (None, None)
    assert rows[done].secrets_purged_at == NOW
    assert rows[waiting].join_link_ciphertext is not None and rows[waiting].secrets_purged_at is None
    inbox_rows = dict(await q(engine, "SELECT msg_id, content_ciphertext IS NULL FROM kf_inbox"))
    assert inbox_rows == {"m1": True, "m2": False}  # unprocessed text stays until it is handled
    assert (await run(engine))["secrets"] == 0
    events = await q(engine, "SELECT task_id, actor FROM task_events WHERE event_type = 'SECRETS_PURGED'")
    assert [tuple(e) for e in events] == [(done, "purge")]


async def test_old_messages_lose_their_text(engine, clean):
    async with engine.begin() as conn:
        task_id = await make_task(conn, TaskStatus.SCHEDULED)
        user = await conn.scalar(text("SELECT user_id FROM meeting_tasks WHERE id = :t"), {"t": task_id})
    await inbox(engine, user, "old", received_at=NOW - p.INBOX_RETENTION)
    await inbox(engine, user, "new", received_at=NOW - p.INBOX_RETENTION + timedelta(minutes=1))
    await exec_(
        engine,
        "INSERT INTO outgoing_messages (user_id, open_kfid, purpose, dedup_key, content, status, sent_at) VALUES "
        "(:u, 'wk', 'X', 'a', 'old', 'SENT', :old), (:u, 'wk', 'X', 'b', 'recent', 'SENT', :recent), "
        "(:u, 'wk', 'X', 'c', 'waiting', 'PENDING', NULL)",
        u=user,
        old=NOW - p.OUTBOX_RETENTION,
        recent=NOW - p.OUTBOX_RETENTION + timedelta(minutes=1),
    )
    report = await run(engine)
    assert (report["inbox"], report["outbox"]) == (1, 1)
    assert dict(await q(engine, "SELECT msg_id, content_ciphertext IS NULL FROM kf_inbox")) == {
        "old": True,
        "new": False,
    }
    assert sorted(r.content for r in await q(engine, "SELECT content FROM outgoing_messages")) == [
        "",
        "recent",
        "waiting",
    ]


async def test_expired_audio_goes_with_its_prefix(engine, clean):
    storage = Storage()
    async with engine.begin() as conn:
        task_id = await make_task(conn, TaskStatus.COMPLETED, ready_segment=True)
        fresh = await make_task(conn, TaskStatus.COMPLETED, ready_segment=True)
    await exec_(engine, "UPDATE audio_assets SET retention_until = :n WHERE task_id = :t", n=NOW, t=task_id)
    report = await run(engine, storage)
    assert report["audio"] == 1
    assert storage.deleted_prefixes == [f"tasks/{task_id}/audio/"]
    status = dict(await q(engine, "SELECT task_id, upload_status FROM audio_assets"))
    assert status == {task_id: "DELETED", fresh: "READY"}


async def completed_with_content(engine, retention):
    async with engine.begin() as conn:
        task_id = await make_task(conn, TaskStatus.COMPLETED)
        user = await conn.scalar(text("SELECT user_id FROM meeting_tasks WHERE id = :t"), {"t": task_id})
        tr = await conn.scalar(
            text(
                "INSERT INTO transcripts (task_id, source, body) VALUES (:t, 'worker_asr', '{}') RETURNING id"
            ),
            {"t": task_id},
        )
        await conn.execute(
            text("INSERT INTO meeting_minutes (task_id, transcript_id, summary) VALUES (:t, :tr, 's')"),
            {"t": task_id, "tr": tr},
        )
        await conn.execute(
            text(
                "INSERT INTO result_access_tokens (task_id, user_id, token_hash, expires_at) "
                "VALUES (:t, :u, :h, :e)"
            ),
            {"t": task_id, "u": user, "h": str(task_id), "e": NOW + timedelta(days=1)},
        )
        await conn.execute(
            text("UPDATE meeting_tasks SET retention_until = :r WHERE id = :t"),
            {"r": retention, "t": task_id},
        )
    return task_id


async def test_content_purge_after_retention(engine, clean):
    storage = Storage()
    due = await completed_with_content(engine, NOW)
    later = await completed_with_content(engine, NOW + timedelta(seconds=1))
    async with engine.begin() as conn:  # past retention but still running: left alone
        running = await make_task(conn, TaskStatus.SUMMARIZING)
        await conn.execute(
            text("UPDATE meeting_tasks SET retention_until = :n WHERE id = :t"), {"n": NOW, "t": running}
        )
    report = await run(engine, storage)
    assert report["content"] == 1
    assert storage.deleted_prefixes == [f"tasks/{due}/"]
    counts = dict(await q(engine, "SELECT task_id, count(*) FROM transcripts GROUP BY task_id"))
    assert counts == {later: 1}
    assert dict(await q(engine, "SELECT task_id, count(*) FROM meeting_minutes GROUP BY task_id")) == {
        later: 1
    }
    revoked = dict(await q(engine, "SELECT task_id, revoked_at FROM result_access_tokens"))
    assert revoked == {due: NOW, later: None}
    purged = dict(await q(engine, "SELECT id, content_purged_at FROM meeting_tasks"))
    assert purged == {due: NOW, later: None, running: None}
    assert (await run(engine, storage))["content"] == 0
    events = await q(engine, "SELECT task_id FROM task_events WHERE event_type = 'CONTENT_PURGED'")
    assert [e.task_id for e in events] == [due]


async def test_without_storage_only_tasks_without_audio_are_cleared(engine, clean):
    plain = await completed_with_content(engine, NOW)
    with_audio = await completed_with_content(engine, NOW)
    async with engine.begin() as conn:
        await make_segment(conn, with_audio, None, 0, "READY", NOW)
    report = await run(engine)
    assert "audio" not in report and report["content"] == 1
    purged = dict(await q(engine, "SELECT id, content_purged_at FROM meeting_tasks"))
    assert purged == {plain: NOW, with_audio: None}


@pytest.fixture
async def erasing_user(engine, clean):
    async with engine.begin() as conn:
        active = await make_task(conn, TaskStatus.RECORDING, has_audio=True, ready_segment=True)
        user = await conn.scalar(text("SELECT user_id FROM meeting_tasks WHERE id = :t"), {"t": active})
        finished = await make_task(conn, TaskStatus.COMPLETED, ready_segment=True)
        await conn.execute(
            text("UPDATE meeting_tasks SET user_id = :u WHERE id = :t"), {"u": user, "t": finished}
        )
        other = await make_task(conn, TaskStatus.COMPLETED, ready_segment=True)
        await conn.execute(
            text(
                "INSERT INTO consent_records (task_id, user_ref_hash, kind, subject, channel, consent_version, "
                "text_sha256, retain_until) VALUES (:t, 'h', 'REQUESTER_AUTHORITY', 'requester', 'wecom_kf', 'v1', "
                "'s', :r)"
            ),
            {"t": finished, "r": NOW + timedelta(days=365)},
        )
        await conn.execute(
            text("UPDATE kf_users SET deletion_requested_at = :n WHERE id = :u"), {"n": NOW, "u": user}
        )
    await inbox(engine, user, "m1")
    return {"user": user, "active": active, "finished": finished, "other": other}


async def test_erasure_cancels_first_then_waits_for_the_late_discard(engine, erasing_user):
    ids, storage = erasing_user, Storage()
    report = await run(engine, storage)
    assert report["erased"] == 0
    status = dict(await q(engine, "SELECT id, status FROM meeting_tasks"))
    assert status[ids["active"]] == TaskStatus.CANCELED
    discards = await q(engine, "SELECT next_attempt_at FROM task_effects WHERE effect = 'DISCARD_AUDIO'")
    assert len(discards) == 2

    # Nothing happens while a DISCARD_AUDIO run is still pending, however late.
    later = NOW + timedelta(days=1)
    assert (await run(engine, storage, now=later))["erased"] == 0
    await exec_(
        engine, "UPDATE task_effects SET status = 'DONE', done_at = :n WHERE next_attempt_at = :n", n=NOW
    )
    assert (await run(engine, storage, now=later))["erased"] == 0  # the late run is still to come
    await exec_(engine, "UPDATE task_effects SET status = 'DONE', done_at = :n", n=NOW)

    report = await run(engine, storage, now=later)
    assert report["erased"] == 1
    assert {f"tasks/{ids['active']}/", f"tasks/{ids['finished']}/"} <= set(storage.deleted_prefixes)
    remaining = {r.id for r in await q(engine, "SELECT id FROM meeting_tasks")}
    assert remaining == {ids["other"]}
    assert await q(engine, "SELECT 1 FROM kf_users WHERE id = :u", u=ids["user"]) == []
    assert await q(engine, "SELECT 1 FROM kf_inbox") == []
    assert [tuple(r) for r in await q(engine, "SELECT task_id, kind FROM consent_records")] == [
        (None, "REQUESTER_AUTHORITY")
    ]
