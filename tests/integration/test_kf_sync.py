"""WeChat KF intake: callback over HTTP, encrypted token, paged sync with redaction and dedup."""

import base64
import json
import os
import time

import httpx
import pytest
from app.config import Settings, get_settings
from app.deps import clock, db_engine, keyring, wecom_client, wecom_crypto
from app.integrations.wecom.client import BASE_URL, WeComClient
from app.integrations.wecom.crypto import CallbackCrypto, signature
from app.main import app
from app.security.keyring import Keyring
from app.services.kf_sync import open_content, sync_account
from factories import NOW
from sqlalchemy import text

RING = Keyring({1: os.urandom(32)}, 1)
CRYPTO = CallbackCrypto(
    token="tok", encoding_aes_key=base64.b64encode(os.urandom(32)).decode()[:43], receive_id="wwcorp"
)
KF = "wkKF123"


def customer_text(msgid: str, content: str, user: str = "wmUSER1", send_time: int | None = None) -> dict:
    return {
        "msgid": msgid,
        "open_kfid": KF,
        "external_userid": user,
        "send_time": send_time or int(NOW.timestamp()) - 5,
        "origin": 3,
        "msgtype": "text",
        "text": {"content": content},
    }


class FakeKf:
    def __init__(self, pages):
        self.pages = list(pages)
        self.sync_bodies: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/cgi-bin/gettoken":
            return httpx.Response(200, json={"errcode": 0, "access_token": "a", "expires_in": 7200})
        body = json.loads(request.content)
        self.sync_bodies.append(body)
        page = (
            self.pages.pop(0)
            if self.pages
            else {"next_cursor": body.get("cursor", ""), "has_more": 0, "msg_list": []}
        )
        # ASCII escapes, as on the wire: a lone surrogate travels as \ud83d and only json.loads makes it a str
        body_ = json.dumps({"errcode": 0, **page}, ensure_ascii=True)
        return httpx.Response(200, content=body_, headers={"content-type": "application/json"})

    def client(self) -> WeComClient:
        return WeComClient(
            "wwcorp",
            "secret",
            http=httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(self.handler)),
        )


def signed_post(inner_xml: str, *, ts: int | None = None) -> tuple[dict, bytes]:
    encrypted = CRYPTO.encrypt(inner_xml)
    ts = str(ts or int(NOW.timestamp()))
    params = {"msg_signature": signature(CRYPTO.token, ts, "n1", encrypted), "timestamp": ts, "nonce": "n1"}
    body = f"<xml><ToUserName><![CDATA[wwcorp]]></ToUserName><Encrypt><![CDATA[{encrypted}]]></Encrypt></xml>"
    return params, body.encode()


@pytest.fixture
async def http(engine, clean):
    fake = FakeKf([])
    app.dependency_overrides.update(
        {
            db_engine: lambda: engine,
            clock: lambda: NOW,
            keyring: lambda: RING,
            wecom_crypto: lambda: CRYPTO,
            wecom_client: lambda: fake.client(),
            get_settings: lambda: Settings(wecom_kf_open_kfid=KF),
        }
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client, fake
    app.dependency_overrides.clear()


async def test_url_verification(http):
    client, _ = http
    echostr = CRYPTO.encrypt("echo-123")
    ts = str(int(time.time()))
    params = {
        "msg_signature": signature("tok", ts, "n", echostr),
        "timestamp": ts,
        "nonce": "n",
        "echostr": echostr,
    }
    r = await client.get("/webhooks/wecom/kf", params=params)
    assert (r.status_code, r.text) == (200, "echo-123")
    params["msg_signature"] = "0" * 40
    assert (await client.get("/webhooks/wecom/kf", params=params)).status_code == 403


async def test_callback_stores_token_encrypted_and_pulls(http, engine):
    client, fake = http
    fake.pages = [
        {"next_cursor": "c1", "has_more": 0, "msg_list": [customer_text("m1", "#腾讯会议：936-109-253")]}
    ]
    inner = f"<xml><Event><![CDATA[kf_msg_or_event]]></Event><Token><![CDATA[cbtoken]]></Token><OpenKfId><![CDATA[{KF}]]></OpenKfId></xml>"
    params, body = signed_post(inner)
    r = await client.post("/webhooks/wecom/kf", params=params, content=body)
    assert (r.status_code, r.text) == (200, "")
    assert fake.sync_bodies[0]["token"] == "cbtoken"  # the background pull used the callback token
    async with engine.connect() as conn:
        state = (
            await conn.execute(text("SELECT next_cursor, pending_sync_token_ciphertext FROM kf_sync_state"))
        ).one()
        count = await conn.scalar(text("SELECT count(*) FROM kf_inbox"))
    assert state.next_cursor == "c1" and state.pending_sync_token_ciphertext is None  # cleared after the pull
    assert count == 1


async def test_callback_with_bad_signature_is_rejected(http, engine):
    client, _ = http
    params, body = signed_post("<xml><Event>kf_msg_or_event</Event></xml>")
    params["msg_signature"] = "f" * 40
    assert (await client.post("/webhooks/wecom/kf", params=params, content=body)).status_code == 403


async def test_oversized_callback_is_refused_before_parsing(http):
    client, _ = http
    params, body = signed_post("<xml><Event>kf_msg_or_event</Event></xml>")
    big = body + b" " * (64 * 1024)
    assert (await client.post("/webhooks/wecom/kf", params=params, content=big)).status_code == 413

    async def chunks():  # no Content-Length: counted while streaming
        for _ in range(100):
            yield b" " * 1024

    assert (await client.post("/webhooks/wecom/kf", params=params, content=chunks())).status_code == 413
    assert (await client.post("/webhooks/wecom/kf", params=params, content=body)).status_code == 200


async def test_replayed_old_callback_is_rejected(http):
    client, _ = http
    params, body = signed_post("<xml><Event>kf_msg_or_event</Event></xml>", ts=int(NOW.timestamp()) - 3600)
    assert (await client.post("/webhooks/wecom/kf", params=params, content=body)).status_code == 403


async def test_sync_pages_redacts_and_dedups(engine, clean):
    fake = FakeKf(
        [
            {
                "next_cursor": "c1",
                "has_more": 1,
                "msg_list": [customer_text("m1", "#腾讯会议：936-109-253\n会议密码：1234")],
            },
            {"next_cursor": "c2", "has_more": 1, "msg_list": []},  # empty page but has_more: keep pulling
            {
                "next_cursor": "c3",
                "has_more": 0,
                "msg_list": [
                    customer_text("m1", "duplicate"),
                    {
                        "msgid": "s1",
                        "open_kfid": KF,
                        "external_userid": "wmUSER1",
                        "send_time": 1,
                        "origin": 5,
                        "msgtype": "text",
                        "text": {"content": "servicer"},
                    },
                    {
                        "msgid": "e1",
                        "open_kfid": KF,
                        "send_time": 1,
                        "origin": 4,
                        "msgtype": "event",
                        "event": {
                            "event_type": "msg_send_fail",
                            "external_userid": "wmUSER1",
                            "fail_msgid": "x",
                            "fail_type": 6,
                        },
                    },
                ],
            },
        ]
    )
    new = await sync_account(engine, fake.client(), corp_id="wwcorp", open_kfid=KF, now=NOW, keyring=RING)
    assert new == 3
    assert [b.get("cursor") for b in fake.sync_bodies] == [None, "c1", "c2"]

    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT msg_id, message_type, status, payload_redacted, content_ciphertext, user_id FROM kf_inbox ORDER BY msg_id"
                )
            )
        ).all()
        user = (await conn.execute(text("SELECT external_userid, last_inbound_at FROM kf_users"))).one()
        cursor = await conn.scalar(text("SELECT next_cursor FROM kf_sync_state"))
    by_id = {r.msg_id: r for r in rows}
    assert {k: (v.message_type, v.status) for k, v in by_id.items()} == {
        "e1": ("event:msg_send_fail", "PENDING"),
        "m1": ("text", "PENDING"),
        "s1": ("text", "IGNORED"),
    }
    m1 = by_id["m1"]
    assert "text" not in m1.payload_redacted and "external_userid" not in m1.payload_redacted
    assert "936" not in json.dumps(m1.payload_redacted, ensure_ascii=False)
    assert open_content(m1.content_ciphertext, KF, "m1", RING)["text"]["content"].startswith("#腾讯会议")
    assert by_id["e1"].payload_redacted["event"] == {"event_type": "msg_send_fail", "fail_type": 6}
    assert m1.user_id is not None and by_id["e1"].user_id == m1.user_id
    assert user.external_userid == "wmUSER1" and user.last_inbound_at is not None
    assert cursor == "c3"

    # a later pull from the stored cursor sees no duplicates
    fake.pages = [{"next_cursor": "c3", "has_more": 0, "msg_list": [customer_text("m1", "again")]}]
    assert (
        await sync_account(engine, fake.client(), corp_id="wwcorp", open_kfid=KF, now=NOW, keyring=RING) == 0
    )


async def test_expired_callback_token_is_not_used(engine, clean):
    from datetime import timedelta

    from app.services.kf_sync import record_callback_token

    async with engine.begin() as conn:
        await record_callback_token(conn, KF, "old", now=NOW - timedelta(minutes=11), keyring=RING)
    fake = FakeKf([])
    await sync_account(engine, fake.client(), corp_id="wwcorp", open_kfid=KF, now=NOW, keyring=RING)
    assert "token" not in fake.sync_bodies[0]


async def test_rejected_callback_token_falls_back_and_is_forgotten(engine, clean):
    from app.services.kf_sync import record_callback_token

    class RejectingKf(FakeKf):
        def handler(self, request):
            if request.url.path == "/cgi-bin/kf/sync_msg" and "token" in json.loads(request.content):
                self.sync_bodies.append(json.loads(request.content))
                return httpx.Response(200, json={"errcode": 95007, "errmsg": "invalid msg token"})
            return super().handler(request)

    async with engine.begin() as conn:
        await record_callback_token(conn, KF, "stale", now=NOW, keyring=RING)
    fake = RejectingKf(
        [
            {"next_cursor": "c1", "has_more": 1, "msg_list": [customer_text("m1", "你好")]},
            {"next_cursor": "c2", "has_more": 0, "msg_list": []},
        ]
    )
    await sync_account(engine, fake.client(), corp_id="wwcorp", open_kfid=KF, now=NOW, keyring=RING)
    assert [("token" in b) for b in fake.sync_bodies] == [True, False, False]
    async with engine.connect() as conn:
        state = (
            await conn.execute(text("SELECT next_cursor, pending_sync_token_ciphertext FROM kf_sync_state"))
        ).one()
    assert state == ("c2", None)


async def test_messages_keep_their_pull_order(engine, clean):
    msgs_ = [customer_text(f"m{i}", f"第{i}条") for i in range(30)]
    fake = FakeKf([{"next_cursor": "c1", "has_more": 0, "msg_list": msgs_}])
    await sync_account(engine, fake.client(), corp_id="wwcorp", open_kfid=KF, now=NOW, keyring=RING)
    async with engine.connect() as conn:
        order = (await conn.execute(text("SELECT msg_id FROM kf_inbox ORDER BY ingest_seq"))).scalars().all()
    assert order == [f"m{i}" for i in range(30)]


async def test_malformed_items_are_quarantined_and_the_cursor_moves_on(engine, clean, caplog):
    """One item that cannot be stored must not roll back its page: that would fetch the same page forever
    and stop every user's messages."""
    no_msgid = {k: v for k, v in customer_text("x", "缺 msgid").items() if k != "msgid"}
    bad_userid = {**customer_text("m3", "userid 不是字符串"), "external_userid": ["wmU"]}
    bad_event = {"msgid": "e9", "open_kfid": KF, "origin": 4, "msgtype": "event", "event": "not an object"}
    page = {
        "next_cursor": "c1",
        "has_more": 0,
        "msg_list": [
            customer_text("m1", "第一条"),
            no_msgid,
            "not even an object",
            bad_userid,
            bad_event,
            {**customer_text("m2", "send_time 不是数字"), "send_time": "soon"},
        ],
    }
    fake = FakeKf([page, page])
    new = await sync_account(engine, fake.client(), corp_id="wwcorp", open_kfid=KF, now=NOW, keyring=RING)
    assert new == 2
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT msg_id, message_type, status, last_error, payload_redacted, user_id "
                    "FROM kf_inbox ORDER BY ingest_seq"
                )
            )
        ).all()
        cursor = await conn.scalar(text("SELECT next_cursor FROM kf_sync_state"))
        inbound = await conn.scalar(text("SELECT last_inbound_at FROM kf_users"))
    assert cursor == "c1"
    good = [(r.msg_id, r.status) for r in rows if r.message_type != "malformed"]
    assert good == [("m1", "PENDING"), ("m2", "PENDING")]
    assert inbound == NOW  # an unreadable send_time counts as the time of the pull
    quarantined = [r for r in rows if r.message_type == "malformed"]
    assert len(quarantined) == 4
    assert {r.msg_id for r in quarantined} >= {"m3", "e9"}
    assert all(r.status == "FAILED" for r in quarantined)
    # the sender of the item without a msgid is known (m1 came first): linked, and told
    linked = [r for r in quarantined if r.user_id is not None]
    assert len(linked) == 1 and linked[0].msg_id.startswith("malformed:")
    async with engine.connect() as conn:
        replies = (await conn.execute(text("SELECT purpose, dedup_key FROM outgoing_messages"))).all()
    assert replies == [("MESSAGE_FAILED", f"reply:{linked[0].msg_id}:MESSAGE_FAILED")]
    assert all(r.payload_redacted == {"error": r.last_error} for r in quarantined)
    assert "缺 msgid" not in caplog.text and "wmU" not in caplog.text

    # the same page again (a crash before the cursor was used): nothing new, nothing duplicated
    fake.pages = [page]
    await sync_account(engine, fake.client(), corp_id="wwcorp", open_kfid=KF, now=NOW, keyring=RING)
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM kf_inbox")) == len(rows)
    by_hash = next(r for r in quarantined if r.msg_id.startswith("malformed:"))
    anonymous = {
        k: v for k, v in no_msgid.items() if k != "external_userid"
    }  # erasure-safe: no identity kept
    async with engine.connect() as conn:
        sealed = await conn.scalar(
            text("SELECT content_ciphertext FROM kf_inbox WHERE msg_id = :m"), {"m": by_hash.msg_id}
        )
    assert open_content(sealed, KF, by_hash.msg_id, RING) in (anonymous, "not even an object")


async def test_a_page_without_msg_list_moves_the_cursor(engine, clean):
    fake = FakeKf([{"next_cursor": "c9", "has_more": 0, "msg_list": None}])
    assert (
        await sync_account(engine, fake.client(), corp_id="wwcorp", open_kfid=KF, now=NOW, keyring=RING) == 0
    )
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT next_cursor FROM kf_sync_state")) == "c9"


async def test_text_postgres_or_utf8_cannot_take_is_quarantined(engine, clean):
    """A lone surrogate (half an emoji) or a NUL cannot be encoded or stored: the item is kept in a form
    that can be, and the rest of the page still goes in."""
    page = {
        "next_cursor": "c1",
        "has_more": 0,
        "msg_list": [
            customer_text("m1", "第一条"),
            customer_text("m2", "半个表情\ud83d"),
            customer_text("m\x003", "msgid 里有 NUL"),
            customer_text("m4", "第四条"),
        ],
    }
    fake = FakeKf([page])
    assert (
        await sync_account(engine, fake.client(), corp_id="wwcorp", open_kfid=KF, now=NOW, keyring=RING) == 2
    )
    async with engine.connect() as conn:
        rows = (
            await conn.execute(text("SELECT msg_id, message_type FROM kf_inbox ORDER BY ingest_seq"))
        ).all()
        cursor = await conn.scalar(text("SELECT next_cursor FROM kf_sync_state"))
        sealed = await conn.scalar(text("SELECT content_ciphertext FROM kf_inbox WHERE msg_id = 'm2'"))
    assert cursor == "c1"
    assert [r.msg_id for r in rows if r.message_type == "text"] == ["m1", "m4"]
    assert [r.msg_id for r in rows if r.message_type == "malformed"][0] == "m2"
    assert sum(r.msg_id.startswith("malformed:") for r in rows) == 1  # the NUL msgid is replaced
    kept = open_content(sealed, KF, "m2", RING)
    assert "external_userid" not in kept and kept["text"]["content"].startswith("半个表情")


async def test_lost_connection_is_not_blamed_on_the_item(engine, clean, monkeypatch):
    from app.services import kf_sync
    from sqlalchemy.exc import OperationalError

    async def down(*args, **kwargs):
        raise OperationalError("upsert", {}, ConnectionResetError())

    monkeypatch.setattr(kf_sync, "_upsert_user", down)
    fake = FakeKf([{"next_cursor": "c1", "has_more": 0, "msg_list": [customer_text("m1", "你好")]}])
    with pytest.raises(OperationalError):
        await sync_account(engine, fake.client(), corp_id="wwcorp", open_kfid=KF, now=NOW, keyring=RING)
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM kf_inbox")) == 0
        assert await conn.scalar(text("SELECT next_cursor FROM kf_sync_state")) is None


async def test_database_trouble_that_is_not_the_items_is_not_blamed_on_it(engine, clean, monkeypatch):
    """A cancelled query (57014) is not a data problem: nothing is set aside, the page is pulled again."""
    from app.services import kf_sync
    from sqlalchemy.exc import DBAPIError

    class Canceled(Exception):
        sqlstate = "57014"

    async def canceled(*args, **kwargs):
        raise DBAPIError("upsert", {}, Canceled())

    monkeypatch.setattr(kf_sync, "_upsert_user", canceled)
    fake = FakeKf([{"next_cursor": "c1", "has_more": 0, "msg_list": [customer_text("m1", "你好")]}])
    with pytest.raises(DBAPIError):
        await sync_account(engine, fake.client(), corp_id="wwcorp", open_kfid=KF, now=NOW, keyring=RING)
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM kf_inbox")) == 0
        assert await conn.scalar(text("SELECT next_cursor FROM kf_sync_state")) is None


async def test_quarantine_of_a_known_customer_opens_the_window_and_keeps_no_identity(engine, clean):
    from datetime import timedelta

    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO kf_users (corp_id, external_userid, last_inbound_at) VALUES ('wwcorp', 'wmUSER1', :t)"
            ),
            {"t": NOW - timedelta(days=3)},
        )
    item = {
        "msgid": "e1",
        "open_kfid": KF,
        "origin": 3,
        "send_time": int(NOW.timestamp()) - 5,
        "msgtype": "event",
        "external_userid": ["not", "a", "string"],  # what makes store_message refuse it
        "event": {"event_type": "x", "external_userid": "wmUSER1"},
    }
    fake = FakeKf([{"next_cursor": "c1", "has_more": 0, "msg_list": [item]}])
    await sync_account(engine, fake.client(), corp_id="wwcorp", open_kfid=KF, now=NOW, keyring=RING)
    async with engine.connect() as conn:
        row = (await conn.execute(text("SELECT user_id, content_ciphertext FROM kf_inbox"))).one()
        user = (await conn.execute(text("SELECT id, last_inbound_at FROM kf_users"))).one()
        replies = (await conn.execute(text("SELECT purpose FROM outgoing_messages"))).scalars().all()
    assert row.user_id == user.id and replies == ["MESSAGE_FAILED"]
    assert user.last_inbound_at == NOW - timedelta(seconds=5)
    kept = json.dumps(open_content(row.content_ciphertext, KF, "e1", RING))
    assert "wmUSER1" not in kept and "external_userid" not in kept
