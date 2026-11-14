"""From a WeChat message to a scheduled task and back: inbox -> conversation -> effects -> outbox."""

import json
import os
from datetime import timedelta

import httpx
import pytest
from app.integrations.wecom.client import BASE_URL, WeComClient
from app.security.keyring import Keyring
from app.services.conversation import process_inbox
from app.services.dispatch import credential_aad
from app.services.effects import run_effects
from app.services.kf_sync import store_message
from app.services.outbox import send_outbox
from factories import NOW
from sqlalchemy import text

RING = Keyring({1: os.urandom(32)}, 1)
KF = "wkKF"
CORP = "wwcorp"
USER = "wmUSER"

INVITE = """张三 邀请您参加腾讯会议
会议主题：产品周会
会议时间：2026/10/11 15:00-16:00 (GMT+08:00) 中国标准时间 - 北京

#腾讯会议：936-109-253
会议密码：1234
"""


class FakeKf:
    def __init__(self, state=1):
        self.sent: list[dict] = []
        self.state = state
        self.transfers: list[int] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/cgi-bin/gettoken":
            return httpx.Response(200, json={"errcode": 0, "access_token": "a", "expires_in": 7200})
        body = json.loads(request.content)
        if request.url.path == "/cgi-bin/kf/service_state/get":
            return httpx.Response(200, json={"errcode": 0, "service_state": self.state})
        if request.url.path == "/cgi-bin/kf/service_state/trans":
            self.transfers.append(body["service_state"])
            self.state = body["service_state"]
            return httpx.Response(200, json={"errcode": 0})
        if request.url.path == "/cgi-bin/kf/send_msg":
            self.sent.append(body)
            return httpx.Response(200, json={"errcode": 0, "msgid": body["msgid"]})
        return httpx.Response(404)

    def client(self) -> WeComClient:
        return WeComClient(
            CORP, "s", http=httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(self.handler))
        )


class NullStorage:
    deleted: list[str] = []

    async def delete(self, key):
        self.deleted.append(key)

    async def delete_prefix(self, prefix):
        self.deleted.append(prefix)


_seq = iter(range(1, 10_000))


async def say(engine, content: str, *, at=None) -> str:
    msgid = f"m{next(_seq)}"
    msg = {
        "msgid": msgid,
        "open_kfid": KF,
        "external_userid": USER,
        "send_time": int((at or NOW).timestamp()),
        "origin": 3,
        "msgtype": "text",
        "text": {"content": content},
    }
    async with engine.begin() as conn:
        await store_message(conn, CORP, msg, now=at or NOW, keyring=RING)
    await process_inbox(engine, now=at or NOW, keyring=RING, corp_id=CORP)
    return msgid


async def outbox(engine) -> list[tuple[str, str]]:
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT purpose, content FROM outgoing_messages ORDER BY created_at, purpose")
            )
        ).all()
    return [tuple(r) for r in rows]


async def task_row(engine):
    async with engine.connect() as conn:
        return (await conn.execute(text("SELECT * FROM meeting_tasks"))).one()


@pytest.fixture
async def workers(engine, clean):
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO worker_nodes (name, token_hash, status) VALUES ('w1', 'h', 'ONLINE')")
        )


async def test_invite_to_scheduled(engine, workers):
    await say(engine, INVITE)
    task = await task_row(engine)
    assert (task.status, task.meeting_code) == ("AWAITING_CONSENT", "936109253")
    password = RING.decrypt_text(
        task.meeting_password_ciphertext, aad=credential_aad(task.id, "meeting_password_ciphertext")
    )
    assert password == "1234"
    replies = await outbox(engine)
    assert replies[0][0] == "CONFIRM" and "936-109-253" in replies[0][1] and "回复「同意」" in replies[0][1]

    await say(engine, "同意")
    task = await task_row(engine)
    assert (task.status, task.consent_version) == ("SCHEDULED", "consent-v1")
    async with engine.connect() as conn:
        consent = (await conn.execute(text("SELECT kind, channel, evidence_ref FROM consent_records"))).one()
    assert consent.kind == "REQUESTER_AUTHORITY" and consent.channel == "wecom_kf"

    await run_effects(engine, now=NOW, storage=NullStorage())
    assert (
        "SCHEDULED",
        "已安排：助手会在会议开始前 3 分钟尝试入会。回复「取消」可随时取消。",
    ) in await outbox(engine)

    fake = FakeKf(state=0)
    sent = await send_outbox(engine, fake.client(), now=NOW)
    assert sent == 2 and fake.transfers == [1]
    assert [m["msgid"] for m in fake.sent] == [m["msgid"] for m in fake.sent if len(m["msgid"]) == 32]


async def test_invite_without_time_asks_then_accepts_time(engine, workers):
    await say(engine, "#腾讯会议：936-109-253")
    assert (await task_row(engine)).status == "AWAITING_DETAILS"
    assert (await outbox(engine))[0][0] == "ASK_TIME"
    await say(engine, "2026/10/11 15:00-16:30")
    task = await task_row(engine)
    assert task.status == "AWAITING_CONSENT"
    assert task.planned_end_at - task.scheduled_at == timedelta(minutes=90)


async def test_password_reply_rules(engine, workers):
    await say(engine, "#腾讯会议：936-109-253\n会议时间：2026/10/11 15:00-16:00")
    await say(engine, "hello")  # an ordinary word is not a password
    task = await task_row(engine)
    assert task.meeting_password_ciphertext is None
    await say(engine, "密码 abc123")
    task = await task_row(engine)
    assert (
        RING.decrypt_text(
            task.meeting_password_ciphertext, aad=credential_aad(task.id, "meeting_password_ciphertext")
        )
        == "abc123"
    )


async def test_no_capacity(engine, workers):
    await say(engine, INVITE)
    await say(engine, "同意")
    other = INVITE.replace("936-109-253", "111-222-333")
    await say(engine, other)
    await say(engine, "同意")
    async with engine.connect() as conn:
        statuses = (
            await conn.execute(text("SELECT meeting_code, status FROM meeting_tasks ORDER BY created_at"))
        ).all()
    assert [tuple(s) for s in statuses] == [("936109253", "SCHEDULED"), ("111222333", "AWAITING_CONSENT")]
    assert (
        "NO_CAPACITY",
        "这个时间段助手已有其他会议，暂时无法安排。可以换个时间，或在会后上传录音。",
    ) in await outbox(engine)


async def test_cancel_command(engine, workers):
    await say(engine, INVITE)
    await say(engine, "同意")
    await say(engine, "取消")
    assert (await task_row(engine)).status == "CANCELED"
    await run_effects(engine, now=NOW, storage=NullStorage())
    assert any(p == "CANCELED" for p, _ in await outbox(engine))


@pytest.mark.parametrize(
    ("text_", "purpose"),
    [
        ("你好", "NOT_UNDERSTOOD"),
        ("特邀会议\n#腾讯会议：936-109-253", "INVITE_ONLY"),
        ("#腾讯会议：936-109-253\n#腾讯会议：111-222-333", "MULTIPLE_MEETINGS"),
        ("取消", "NOTHING_TO_CANCEL"),
    ],
)
async def test_replies_without_task(engine, workers, text_, purpose):
    await say(engine, text_)
    assert [p for p, _ in await outbox(engine)] == [purpose]
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM meeting_tasks")) == 0


async def test_reply_window_and_budget(engine, workers):
    await say(engine, INVITE, at=NOW - timedelta(hours=49))
    fake = FakeKf()
    assert await send_outbox(engine, fake.client(), now=NOW) == 0  # 49 h after the user's message
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT status FROM outgoing_messages")) == "DEFERRED"
    await say(engine, "你好")  # a new message reopens the window and releases the deferred notice
    assert await send_outbox(engine, fake.client(), now=NOW) == 2


async def test_human_session_defers(engine, workers):
    await say(engine, INVITE)
    fake = FakeKf(state=3)
    assert await send_outbox(engine, fake.client(), now=NOW) == 0
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT status FROM outgoing_messages")) == "DEFERRED"


async def test_send_failure_event_defers_message(engine, workers):
    await say(engine, INVITE)
    fake = FakeKf()
    await send_outbox(engine, fake.client(), now=NOW)
    msgid = fake.sent[0]["msgid"]
    event = {
        "msgid": "e1",
        "open_kfid": KF,
        "send_time": int(NOW.timestamp()),
        "origin": 4,
        "msgtype": "event",
        "event": {
            "event_type": "msg_send_fail",
            "external_userid": USER,
            "fail_msgid": msgid,
            "fail_type": 6,
        },
    }
    async with engine.begin() as conn:
        await store_message(conn, CORP, event, now=NOW, keyring=RING)
    await process_inbox(engine, now=NOW, keyring=RING, corp_id=CORP)
    async with engine.connect() as conn:
        assert (
            await conn.scalar(
                text("SELECT status FROM outgoing_messages WHERE wecom_msgid = :m"), {"m": msgid}
            )
            == "DEFERRED"
        )


async def test_inbox_redelivery_is_idempotent(engine, workers):
    msgid = await say(engine, INVITE)
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE kf_inbox SET status = 'PENDING', next_attempt_at = :n WHERE msg_id = :m"),
            {"n": NOW, "m": msgid},
        )
    await process_inbox(engine, now=NOW, keyring=RING, corp_id=CORP)
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM meeting_tasks")) == 1
        assert await conn.scalar(text("SELECT count(*) FROM outgoing_messages")) == 1


async def test_reversed_time_asks_instead_of_crashing(engine, workers):
    await say(engine, "#腾讯会议：936-109-253\n会议时间：2026/10/11 16:00-15:00")
    task = await task_row(engine)
    assert (task.status, task.scheduled_at) == ("AWAITING_DETAILS", None)
    assert [p for p, _ in await outbox(engine)] == ["ASK_TIME"]
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT status FROM kf_inbox")) == "DONE"


async def test_conflicting_codes_are_not_scheduled(engine, workers):
    await say(engine, "原会议 https://meeting.tencent.com/p/1111111111\n#腾讯会议：936-109-253")
    assert [p for p, _ in await outbox(engine)] == ["CODE_CONFLICT"]
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM meeting_tasks")) == 0


async def test_past_meeting_asks_for_time(engine, workers):
    await say(engine, "#腾讯会议：936-109-253\n会议时间：2026/10/01 15:00-16:00")
    assert (await task_row(engine)).scheduled_at is None
    assert [p for p, _ in await outbox(engine)] == ["ASK_TIME"]


async def store(engine, content: str, *, at=None) -> str:
    msgid = f"m{next(_seq)}"
    msg = {
        "msgid": msgid,
        "open_kfid": KF,
        "external_userid": USER,
        "send_time": int((at or NOW).timestamp()),
        "origin": 3,
        "msgtype": "text",
        "text": {"content": content},
    }
    async with engine.begin() as conn:
        await store_message(conn, CORP, msg, now=at or NOW, keyring=RING)
    return msgid


async def test_messages_pulled_together_apply_in_order(engine, workers):
    await store(engine, INVITE)
    await store(engine, "同意")
    await process_inbox(engine, now=NOW, keyring=RING, corp_id=CORP)
    assert (await task_row(engine)).status == "SCHEDULED"
    assert [p for p, _ in await outbox(engine)] == ["CONFIRM"]


async def test_a_failing_message_holds_back_the_users_later_ones(engine, workers, monkeypatch):
    from app.services import conversation

    original = conversation._Ctx.on_invite
    calls = []

    async def flaky(self, parsed):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("transient")
        return await original(self, parsed)

    monkeypatch.setattr(conversation._Ctx, "on_invite", flaky)
    await store(engine, INVITE)
    await store(engine, "同意")
    await process_inbox(engine, now=NOW, keyring=RING, corp_id=CORP)
    async with engine.connect() as conn:
        rows = (await conn.execute(text("SELECT status, attempts FROM kf_inbox ORDER BY ingest_seq"))).all()
    assert [tuple(r) for r in rows] == [("PENDING", 1), ("PENDING", 0)]
    assert await outbox(engine) == []  # "同意" was not answered with NOT_UNDERSTOOD

    later = NOW + timedelta(minutes=1)  # the invite's retry is not due yet: "同意" still waits
    await process_inbox(engine, now=later, keyring=RING, corp_id=CORP)
    assert await outbox(engine) == []

    await process_inbox(engine, now=NOW + timedelta(minutes=6), keyring=RING, corp_id=CORP)
    assert (await task_row(engine)).status == "SCHEDULED"


async def test_agree_after_the_meeting_ended(engine, workers):
    await say(engine, INVITE)  # 2026/10/11 15:00-16:00 +08 = 07:00-08:00Z
    after = NOW.replace(day=11, hour=9)
    await say(engine, "同意", at=after)
    task = await task_row(engine)
    assert task.status == "CANCELED"
    replies = [p for p, _ in await outbox(engine)]
    assert replies == ["CONFIRM", "MEETING_OVER"]
    await run_effects(engine, now=after, storage=NullStorage())
    assert [p for p, _ in await outbox(engine)] == ["CONFIRM", "MEETING_OVER"]  # expiry itself is silent


async def test_stale_requests_expire(engine, workers):
    from app.services.dispatch import scheduler_tick

    await say(engine, INVITE)
    await scheduler_tick(engine, now=NOW.replace(day=11, hour=7, minute=59))
    assert (await task_row(engine)).status == "AWAITING_CONSENT"
    counts = await scheduler_tick(engine, now=NOW.replace(day=11, hour=8))
    assert counts["INTAKE_EXPIRED"] == 1
    assert (await task_row(engine)).status == "CANCELED"


async def test_stop_before_recording_only_says_canceled(engine, workers):
    from app.services.dispatch import scheduler_tick

    await say(engine, INVITE)
    await say(engine, "同意")
    dispatch_at = NOW.replace(day=11, hour=6, minute=57)
    await scheduler_tick(engine, now=dispatch_at)
    assert (await task_row(engine)).status == "DISPATCHING"
    await say(engine, "停止", at=dispatch_at)
    assert (await task_row(engine)).status == "CANCELED"
    await run_effects(engine, now=dispatch_at, storage=NullStorage())
    purposes = [p for p, _ in await outbox(engine)]
    assert "STOPPED" not in purposes and purposes.count("CANCELED") == 1


async def test_view_command(engine, workers):
    await say(engine, "查看纪要")
    assert [p for p, _ in await outbox(engine)] == ["NO_RESULT"]
    await say(engine, INVITE)
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE meeting_tasks SET status = 'COMPLETED', consent_at = :n, consent_version = 'v1'"),
            {"n": NOW},
        )
    await say(engine, "查看纪要")
    purpose, content = (await outbox(engine))[-1]
    assert purpose == "RESULT_LINK" and "{result_link}" in content
