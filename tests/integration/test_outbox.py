"""Outbox delivery against a strict fake of kf/send_msg: msgid uniqueness, deferral codes, transport errors,
per-user order and one-time result links."""

import hashlib
import json
import re
from datetime import timedelta

import httpx
import pytest
from app.integrations.wecom.client import BASE_URL, WeComClient
from app.services import outbox
from app.services.outbox import send_outbox
from factories import NOW, make_task
from meeting_protocol import TaskStatus
from sqlalchemy import text


class StrictKf:
    """Rejects a msgid it already accepted (95033), can be told to fail sends, or to accept and time out."""

    def __init__(self):
        self.accepted: dict[str, str] = {}  # msgid -> content
        self.errors: list[int] = []
        self.timeout_after_accept = 0
        self.raise_before = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/cgi-bin/gettoken":
            return httpx.Response(200, json={"errcode": 0, "access_token": "a", "expires_in": 7200})
        body = json.loads(request.content)
        if request.url.path == "/cgi-bin/kf/service_state/get":
            return httpx.Response(200, json={"errcode": 0, "service_state": 1})
        assert request.url.path == "/cgi-bin/kf/send_msg"
        if self.raise_before:
            self.raise_before -= 1
            raise httpx.ConnectError("down")
        if self.errors:
            return httpx.Response(200, json={"errcode": self.errors.pop(0), "errmsg": "x"})
        if body["msgid"] in self.accepted:
            return httpx.Response(200, json={"errcode": 95033, "errmsg": "msgid duplicated"})
        self.accepted[body["msgid"]] = body["text"]["content"]
        if self.timeout_after_accept:
            self.timeout_after_accept -= 1
            raise httpx.ReadTimeout("slow")
        return httpx.Response(200, json={"errcode": 0, "msgid": body["msgid"]})

    def client(self) -> WeComClient:
        return WeComClient(
            "c", "s", http=httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(self.handler))
        )


async def user(engine, name="wmA"):
    async with engine.begin() as conn:
        return await conn.scalar(
            text(
                "INSERT INTO kf_users (corp_id, external_userid, last_inbound_at) VALUES ('c', :u, :at) RETURNING id"
            ),
            {"u": name, "at": NOW - timedelta(minutes=1)},
        )


async def message(engine, user_id, content="hi", *, at=NOW, task_id=None, purpose="X"):
    async with engine.begin() as conn:
        return await conn.scalar(
            text(
                "INSERT INTO outgoing_messages (task_id, user_id, open_kfid, purpose, dedup_key, content, "
                "next_attempt_at, created_at) VALUES (:t, :u, 'wk', :p, :d, :c, :at, :at) RETURNING id"
            ),
            {"t": task_id, "u": user_id, "p": purpose, "d": content + str(at), "c": content, "at": at},
        )


async def rows(engine):
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT content, status, attempts, wecom_msgid, send_generation, last_error "
                    "FROM outgoing_messages ORDER BY created_at"
                )
            )
        ).all()


async def test_msgid_shape(engine, clean):
    fake = StrictKf()
    msg_id = await message(engine, await user(engine))
    await send_outbox(engine, fake.client(), now=NOW)
    [row] = await rows(engine)
    assert row.wecom_msgid == msg_id.hex[:27] + "g0000"
    assert re.fullmatch(r"[0-9a-f]{27}g\d{4}", row.wecom_msgid) and len(row.wecom_msgid) == 32


async def test_accepted_then_timed_out_is_not_sent_twice(engine, clean):
    fake = StrictKf()
    fake.timeout_after_accept = 1
    await message(engine, await user(engine))
    assert await send_outbox(engine, fake.client(), now=NOW) == 0
    [row] = await rows(engine)
    assert (row.status, row.last_error) == ("PENDING", "transport: ReadTimeout")
    assert await send_outbox(engine, fake.client(), now=NOW + timedelta(minutes=1)) == 1
    [row] = await rows(engine)
    assert row.status == "SENT"
    assert len(fake.accepted) == 1  # delivered once; the retry's 95033 confirmed it


async def test_deferred_message_is_resent_with_a_new_msgid(engine, clean):
    from app.models.tables import outgoing_messages
    from app.services.outbox import release_deferred_values
    from sqlalchemy import update

    fake = StrictKf()
    await message(engine, await user(engine))
    await send_outbox(engine, fake.client(), now=NOW)
    first = (await rows(engine))[0].wecom_msgid
    async with engine.begin() as conn:  # msg_send_fail (fail_type 6), then the user writes again
        await conn.execute(text("UPDATE outgoing_messages SET status = 'DEFERRED'"))
        await conn.execute(update(outgoing_messages).values(next_attempt_at=NOW, **release_deferred_values()))
    assert await send_outbox(engine, fake.client(), now=NOW + timedelta(seconds=1)) == 1
    [row] = await rows(engine)
    assert (row.status, row.attempts, row.send_generation) == ("SENT", 1, 1)
    assert row.wecom_msgid != first and row.wecom_msgid.endswith("g0001")
    assert len(fake.accepted) == 2


@pytest.mark.parametrize("code", [95001, 95002, 95018])
async def test_window_and_session_errors_defer(engine, clean, code):
    fake = StrictKf()
    fake.errors = [code]
    await message(engine, await user(engine))
    await send_outbox(engine, fake.client(), now=NOW)
    [row] = await rows(engine)
    assert (row.status, row.last_error) == ("DEFERRED", str(code))


async def test_transport_errors_end_in_failed_and_do_not_stop_the_batch(engine, clean, caplog):
    fake = StrictKf()
    fake.raise_before = 100
    a, b = await user(engine, "wmA"), await user(engine, "wmB")
    await message(engine, a, "to a")
    await message(engine, b, "to b", at=NOW + timedelta(seconds=1))
    at = NOW + timedelta(seconds=1)
    for _ in range(outbox.MAX_SEND_ATTEMPTS):
        await send_outbox(engine, fake.client(), now=at)
        at += timedelta(minutes=5)
    assert [(r.status, r.attempts, r.last_error) for r in await rows(engine)] == [
        ("FAILED", 5, "transport: ConnectError"),
        ("FAILED", 5, "transport: ConnectError"),
    ]
    gave_up = [r for r in caplog.records if r.levelname == "ERROR" and "gave up" in r.getMessage()]
    assert len(gave_up) == 2 and "to a" not in caplog.text  # alerted, without the content


async def test_a_users_messages_keep_their_order(engine, clean):
    fake = StrictKf()
    fake.errors = [-1]  # the first send fails once
    a, b = await user(engine, "wmA"), await user(engine, "wmB")
    await message(engine, a, "first")
    await message(engine, a, "second", at=NOW + timedelta(seconds=1))
    await message(engine, b, "other user", at=NOW + timedelta(seconds=2))
    await send_outbox(engine, fake.client(), now=NOW + timedelta(seconds=2))
    assert list(fake.accepted.values()) == ["other user"]
    statuses = [(r.content, r.status, r.attempts) for r in await rows(engine)]
    assert statuses == [("first", "PENDING", 1), ("second", "PENDING", 0), ("other user", "SENT", 1)]
    await send_outbox(engine, fake.client(), now=NOW + timedelta(seconds=10))  # first not due yet
    assert list(fake.accepted.values()) == ["other user"]
    await send_outbox(engine, fake.client(), now=NOW + timedelta(minutes=1))
    assert list(fake.accepted.values()) == ["other user", "first", "second"]


async def test_stuck_sending_after_the_last_attempt_fails(engine, clean, caplog):
    await message(engine, await user(engine))
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE outgoing_messages SET status = 'SENDING', attempts = :n, next_attempt_at = :at"),
            {"n": outbox.MAX_SEND_ATTEMPTS, "at": NOW},
        )
    await send_outbox(engine, StrictKf().client(), now=NOW)
    [row] = await rows(engine)
    assert (row.status, row.last_error) == ("FAILED", "no outcome after the last attempt")
    assert "gave up: no outcome after the last attempt" in caplog.text


async def test_result_link_is_minted_at_send_time(engine, clean):
    from app.intake import messages as msgs

    fake = StrictKf()
    async with engine.begin() as conn:
        task_id = await make_task(conn, TaskStatus.COMPLETED)
    u = await user(engine)
    await message(engine, u, msgs.COMPLETED, task_id=task_id, purpose="RESULT_LINK")
    await send_outbox(engine, fake.client(), now=NOW, base_url="https://x.example")
    [sent] = fake.accepted.values()
    token = re.search(r"https://x\.example/r/([A-Za-z0-9_-]{43})", sent).group(1)
    async with engine.connect() as conn:
        stored = (
            await conn.execute(text("SELECT token_hash, task_id, user_id FROM result_access_tokens"))
        ).one()
    assert stored == (hashlib.sha256(token.encode()).hexdigest(), task_id, u)
    [row] = await rows(engine)
    assert row.content == msgs.COMPLETED and token not in row.content  # the link itself is never stored


async def test_result_link_without_base_url_fails(engine, clean, caplog):
    from app.intake import messages as msgs

    async with engine.begin() as conn:
        task_id = await make_task(conn, TaskStatus.COMPLETED)
    await message(engine, await user(engine), msgs.RESULT_LINK, task_id=task_id, purpose="RESULT_LINK")
    await send_outbox(engine, StrictKf().client(), now=NOW)
    [row] = await rows(engine)
    assert (row.status, row.last_error) == ("FAILED", "PUBLIC_BASE_URL not configured")
    assert "(RESULT_LINK) failed: PUBLIC_BASE_URL not configured" in caplog.text
