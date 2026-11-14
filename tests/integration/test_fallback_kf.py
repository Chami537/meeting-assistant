"""Fallback path through WeChat KF: files, pasted minutes, upload links (design doc §4 路径 D)."""

import io
import zipfile
from pathlib import Path

import pytest
from app.intake import messages as msgs
from app.integrations.wecom.client import MediaFile, WeComApiError
from app.postprocess.jobs import run_jobs
from app.services.conversation import process_inbox
from app.services.effects import run_effects
from app.services.kf_sync import store_message
from app.services.task_inputs import read_input
from factories import NOW
from sqlalchemy import text
from test_conversation import CORP, KF, RING, USER, NullStorage, outbox, store, workers  # noqa: F401
from test_postprocess import FakeLlm, deps

M4A = b"\x00\x00\x00\x20ftypM4A " + b"a" * 5000
TRANSCRIPT = "说话人01 00:00:05\n我们决定下周三上线。\n说话人02 00:00:20\n张三负责写测试，周五前完成。"


def docx(text_: str) -> bytes:
    ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    body = "".join(f"<w:p><w:r><w:t>{line}</w:t></w:r></w:p>" for line in text_.split("\n"))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", f"<w:document {ns}><w:body>{body}</w:body></w:document>")
    return buf.getvalue()


class Peek:
    def __init__(self, files: dict[str, bytes], expired=()):
        self.files = files
        self.expired = set(expired)
        self.heads: list[str] = []

    async def media_head(self, media_id, size):
        if media_id in self.expired:
            raise WeComApiError(40007, "invalid media_id", "media/get")
        self.heads.append(media_id)
        data = self.files[media_id]
        return data[:size], MediaFile("MEDIA.bin", None, len(data))

    async def download_media(self, media_id, dest, *, max_bytes):
        Path(dest).write_bytes(self.files[media_id])
        return MediaFile(None, None, len(self.files[media_id]))


_n = iter(range(1, 10_000))


async def send(engine, msgtype: str, payload: dict, peek=None) -> str:
    msgid = f"f{next(_n)}"
    msg = {
        "msgid": msgid,
        "open_kfid": KF,
        "external_userid": USER,
        "send_time": int(NOW.timestamp()),
        "origin": 3,
        "msgtype": msgtype,
        msgtype: payload,
    }
    async with engine.begin() as conn:
        await store_message(conn, CORP, msg, now=NOW, keyring=RING)
    await process_inbox(engine, now=NOW, keyring=RING, corp_id=CORP, media=peek)
    return msgid


async def say(engine, content: str, peek=None):
    await store(engine, content)
    await process_inbox(engine, now=NOW, keyring=RING, corp_id=CORP, media=peek)


async def tasks(engine):
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text("SELECT id, task_kind, status, failure_code FROM meeting_tasks ORDER BY created_at")
            )
        ).all()


async def purposes(engine):
    return [p for p, _ in await outbox(engine)]


async def test_recording_file_then_agree(engine, workers):  # noqa: F811
    peek = Peek({"mid-audio": M4A})
    await send(engine, "file", {"media_id": "mid-audio"}, peek)
    [task] = await tasks(engine)
    assert (task.task_kind, task.status) == ("USER_UPLOAD", "AWAITING_CONSENT")
    assert peek.heads == ["mid-audio"]  # only the head was fetched before consent
    async with engine.connect() as conn:
        source = await read_input(conn, RING, task.id)
    assert (source.kind, source.payload, source.source_msg_type) == ("WECOM_MEDIA", "mid-audio", "file")
    assert source.expires_at is not None
    assert (await outbox(engine))[-1] == ("UPLOAD_CONFIRM", msgs.UPLOAD_CONFIRM)

    await say(engine, "同意", peek)
    [task] = await tasks(engine)
    assert task.status == "UPLOADING"
    async with engine.connect() as conn:
        consent = (await conn.execute(text("SELECT channel, consent_version FROM consent_records"))).one()
        job = (await conn.execute(text("SELECT kind FROM processing_jobs"))).scalar_one()
    assert tuple(consent) == ("wecom_kf", msgs.UPLOAD_CONSENT_VERSION)
    assert job == "AUDIO_NORMALIZE"
    assert (await purposes(engine))[-1] == "PROCESSING"


async def test_video_message_is_a_recording_too(engine, workers):  # noqa: F811
    await send(engine, "video", {"media_id": "v1"}, Peek({"v1": M4A}))
    assert [t.task_kind for t in await tasks(engine)] == ["USER_UPLOAD"]


@pytest.mark.parametrize(
    ("data", "purpose"),
    [
        (b"%PDF-1.7\n" + b"x" * 100, "PDF_NOT_SUPPORTED"),
        (b"\x89PNG\r\n\x1a\n" + bytes(100), "UNSUPPORTED_FILE"),
        (b"PK\x03\x04" + b"\x00" * 50, "UNSUPPORTED_FILE"),  # a zip that is no Word document
    ],
)
async def test_unusable_files(engine, workers, data, purpose):  # noqa: F811
    await send(engine, "file", {"media_id": "m"}, Peek({"m": data}))
    assert await tasks(engine) == []
    assert await purposes(engine) == [purpose]


async def test_expired_file_and_voice(engine, workers):  # noqa: F811
    await send(engine, "file", {"media_id": "old"}, Peek({}, expired={"old"}))
    await send(engine, "voice", {"media_id": "v"}, Peek({}))
    assert await purposes(engine) == ["FILE_EXPIRED", "VOICE_NOT_SUPPORTED"]
    assert await tasks(engine) == []


async def test_word_transcript_to_minutes(engine, workers):  # noqa: F811
    peek = Peek({"doc": docx(TRANSCRIPT)})
    await send(engine, "file", {"media_id": "doc"}, peek)
    [task] = await tasks(engine)
    assert (task.task_kind, task.status) == ("FORWARDED_MINUTES", "AWAITING_CONSENT")
    assert (await purposes(engine)) == ["FORWARDED_CONFIRM"]

    await say(engine, "同意", peek)
    async with engine.connect() as conn:
        body = (await conn.execute(text("SELECT source, body FROM transcripts"))).one()
    assert body.source == "forwarded_text"
    assert [(s["speaker"], s["start_ms"]) for s in body.body["segments"]] == [
        ("说话人01", 5000),
        ("说话人02", 20_000),
    ]
    [task] = await tasks(engine)
    assert task.status == "SUMMARIZING"

    minutes = {"summary": "确定上线", "decisions": [{"text": "下周三上线", "evidence_segments": [0]}]}
    await run_jobs(engine, deps(llm=FakeLlm(minutes)), now=NOW)
    [task] = await tasks(engine)
    assert task.status == "COMPLETED"
    await run_effects(engine, now=NOW, storage=NullStorage())
    purpose, content = (await outbox(engine))[-1]
    assert purpose == "RESULT_LINK" and "{result_link}" in content


async def test_pasted_minutes_in_two_parts(engine, workers):  # noqa: F811
    part1 = "## 会议纪要\n" + "第一部分讨论了上线计划。" * 20
    part2 = "第二部分：张三负责测试。" * 20
    await say(engine, part1)
    await say(engine, part2)
    [task] = await tasks(engine)
    assert task.task_kind == "FORWARDED_MINUTES"
    assert await purposes(engine) == ["FORWARDED_CONFIRM", "FORWARDED_APPENDED"]
    async with engine.connect() as conn:
        source = await read_input(conn, RING, task.id)
    assert source.payload == f"{part1}\n{part2}"
    await say(engine, "同意")
    async with engine.connect() as conn:
        segments = (await conn.execute(text("SELECT body FROM transcripts"))).scalar_one()["segments"]
    assert segments[0]["text"] == "## 会议纪要" and segments[-1]["text"] == part2
    assert all(s["start_ms"] is None for s in segments)


async def test_short_unknown_text_is_not_minutes(engine, workers):  # noqa: F811
    await say(engine, "你好，请问怎么用？")
    assert await tasks(engine) == [] and await purposes(engine) == ["NOT_UNDERSTOOD"]


async def test_upload_request_and_size_notice(engine, workers):  # noqa: F811
    await say(engine, "上传录音")
    await say(engine, "文件过大，无法发送")  # WeChat's notice for a file over the KF limit (wording guessed)
    [task] = await tasks(engine)  # the second request reuses the waiting upload task
    assert (task.task_kind, task.status) == ("USER_UPLOAD", "AWAITING_DETAILS")
    replies = await outbox(engine)
    assert [p for p, _ in replies] == ["UPLOAD_LINK", "UPLOAD_LINK"]
    assert "{upload_link}" in replies[0][1]


@pytest.mark.parametrize(
    "content",
    [
        "https://meeting.tencent.com/crm/AbCdEf1234",
        "看录制 https://meeting.tencent.com/v2/cloud-record/share?id=1",
    ],
)
async def test_share_links_get_export_advice(engine, workers, content):  # noqa: F811
    await say(engine, content)
    assert await purposes(engine) == ["SHARE_LINK_HINT"]
    await send(engine, "link", {"title": "云录制", "url": "https://meeting.tencent.com/crm/AbC", "desc": ""})
    assert await purposes(engine) == ["SHARE_LINK_HINT", "SHARE_LINK_HINT"]


async def test_without_media_access_a_file_is_taken_for_a_recording(engine, workers):  # noqa: F811
    await send(engine, "file", {"media_id": "m"}, peek=None)
    assert [t.task_kind for t in await tasks(engine)] == ["USER_UPLOAD"]


async def test_redelivered_file_creates_one_task(engine, workers):  # noqa: F811
    peek = Peek({"m": M4A})
    msgid = await send(engine, "file", {"media_id": "m"}, peek)
    async with (
        engine.begin() as conn
    ):  # processed again (e.g. a crash after commit is impossible, but be safe)
        await conn.execute(text("UPDATE kf_inbox SET status = 'PENDING' WHERE msg_id = :m"), {"m": msgid})
    await process_inbox(engine, now=NOW, keyring=RING, corp_id=CORP, media=peek)
    assert len(await tasks(engine)) == 1


INVITE_NO_TIME_NO_PASSWORD = "#腾讯会议：936-109-253"
INVITE = "#腾讯会议：936-109-253\n会议时间：2026/10/11 15:00-16:00 (GMT+08:00)\n会议密码：1234"


async def inbox_states(engine):
    async with engine.connect() as conn:
        return [
            tuple(r)
            for r in (
                await conn.execute(text("SELECT status, attempts FROM kf_inbox ORDER BY ingest_seq"))
            ).all()
        ]


async def test_time_reply_never_lands_on_an_upload_task(engine, workers):  # noqa: F811
    await say(engine, "上传录音")
    await say(engine, "2026/10/11 15:00-16:00")
    assert all(state == "DONE" for state, _ in await inbox_states(engine))  # no crash, nothing held back
    [task] = await tasks(engine)
    assert (task.task_kind, task.status) == ("USER_UPLOAD", "AWAITING_DETAILS")


async def test_password_reply_goes_to_the_meeting_not_the_recording(engine, workers):  # noqa: F811
    await say(engine, INVITE_NO_TIME_NO_PASSWORD + "\n会议时间：2026/10/11 15:00-16:00")
    await send(engine, "file", {"media_id": "a"}, Peek({"a": M4A}))
    await say(engine, "密码 5678")
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT task_kind, meeting_password_ciphertext IS NOT NULL FROM meeting_tasks")
            )
        ).all()
    assert dict(rows) == {"LIVE_MEETING": True, "USER_UPLOAD": False}


async def test_agree_after_the_meeting_ended_does_not_authorise_an_older_upload(engine, workers):  # noqa: F811
    await send(engine, "file", {"media_id": "a"}, Peek({"a": M4A}))
    await say(engine, INVITE)  # 2026/10/11 07:00-08:00Z, newer than the recording
    after = NOW.replace(day=11, hour=9)
    await store(engine, "同意", at=after)
    await process_inbox(engine, now=after, keyring=RING, corp_id=CORP)
    kinds = {t.task_kind: t.status for t in await tasks(engine)}
    assert kinds == {"USER_UPLOAD": "AWAITING_CONSENT", "LIVE_MEETING": "CANCELED"}
    assert (await purposes(engine))[-1] == "MEETING_OVER"


async def test_one_agree_confirms_every_pending_recording(engine, workers):  # noqa: F811
    peek = Peek({"a": M4A, "b": M4A})
    await send(engine, "file", {"media_id": "a"}, peek)
    await send(engine, "file", {"media_id": "b"}, peek)
    await say(engine, "同意", peek)
    assert [t.status for t in await tasks(engine)] == ["UPLOADING", "UPLOADING"]
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM consent_records")) == 2
    purpose, content = (await outbox(engine))[-1]
    assert purpose == "PROCESSING" and "2 份" in content


async def test_continuation_and_done_words(engine, workers):  # noqa: F811
    await say(engine, "会议纪要：" + "讨论了上线安排。" * 30)
    await say(engine, "补充：张三负责测试。")  # short, but the minutes are still arriving
    await say(engine, "发完了")
    assert await purposes(engine) == ["FORWARDED_CONFIRM", "FORWARDED_APPENDED", "FORWARDED_DONE"]
    [task] = await tasks(engine)
    async with engine.connect() as conn:
        source = await read_input(conn, RING, task.id)
    assert source.payload.endswith("补充：张三负责测试。")
    await say(engine, "同意")
    async with engine.connect() as conn:
        consent = (
            await conn.execute(text("SELECT channel, consent_version, text_sha256 FROM consent_records"))
        ).one()
    assert tuple(consent) == ("wecom_kf", msgs.FORWARDED_CONSENT_VERSION, msgs.FORWARDED_CONSENT_SHA256)


async def test_append_window_closes(engine, workers):  # noqa: F811
    from datetime import timedelta

    part = "会议纪要：" + "讨论了上线安排。" * 30
    await say(engine, part)
    later = NOW + timedelta(minutes=31)
    await store(engine, part, at=later)
    await process_inbox(engine, now=later, keyring=RING, corp_id=CORP)
    assert [t.task_kind for t in await tasks(engine)] == ["FORWARDED_MINUTES", "FORWARDED_MINUTES"]


@pytest.mark.parametrize(
    "body",
    [
        "2026/10/09 10:00 会议开始\n" + "说话人01：讨论上线。\n" * 40,  # starts with a date and time
        "参考 https://meeting.tencent.com/crm/AbC 的录制。\n"
        + "讨论了上线安排。" * 40,  # mentions a share link
        "会议号 936-109-253 的纪要\n"
        + "\n".join(f"第{i}项：讨论内容。" for i in range(60)),  # long, has a code
    ],
)
async def test_long_minutes_are_not_mistaken(engine, workers, body):  # noqa: F811
    await say(engine, body)
    assert await purposes(engine) == ["FORWARDED_CONFIRM"]
    assert [t.task_kind for t in await tasks(engine)] == ["FORWARDED_MINUTES"]


@pytest.mark.parametrize("msgtype", ["image", "merged_msg", "location", "business_card"])
async def test_unsupported_message_types_get_an_answer(engine, workers, msgtype):  # noqa: F811
    await send(engine, msgtype, {"media_id": "x", "title": "聊天记录", "item": []})
    assert await purposes(engine) == ["UNSUPPORTED_MESSAGE"]


async def test_word_97_and_utf16_text(engine, workers):  # noqa: F811
    peek = Peek({"doc": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + bytes(100), "txt": TRANSCRIPT.encode("utf-16")})
    await send(engine, "file", {"media_id": "doc"}, peek)
    await send(engine, "file", {"media_id": "txt"}, peek)
    assert await purposes(engine) == ["LEGACY_DOC", "FORWARDED_CONFIRM"]


async def test_corrupt_word_file_is_answered_at_once(engine, workers):  # noqa: F811
    data = bytearray(docx(TRANSCRIPT * 20))
    start = data.index(b"word/document.xml") + 17
    for i in range(start + 10, start + 60):
        data[i] ^= 0x5A
    await send(engine, "file", {"media_id": "bad"}, Peek({"bad": bytes(data)}))
    assert await purposes(engine) == ["UNSUPPORTED_FILE"]
    assert await inbox_states(engine) == [("DONE", 1)]


async def test_text_too_long_is_refused_before_consent(engine, workers, monkeypatch):  # noqa: F811
    from app.intake import material

    monkeypatch.setattr(material, "MAX_TEXT_CHARS", 300)
    await say(engine, "纪要" * 200)
    assert await purposes(engine) == ["TEXT_TOO_LONG"] and await tasks(engine) == []


async def test_file_that_cannot_be_read_is_answered_after_the_last_attempt(engine, workers):  # noqa: F811
    from datetime import timedelta

    from app.services import conversation

    class Down(Peek):
        async def media_head(self, media_id, size):
            raise WeComApiError(-1, "transport: ConnectError", "media/get")

    await send(engine, "file", {"media_id": "a"}, Down({}))
    at = NOW
    for _ in range(conversation.MAX_INBOX_ATTEMPTS):
        at += timedelta(minutes=6)
        await process_inbox(engine, now=at, keyring=RING, corp_id=CORP, media=Down({}))
    assert await inbox_states(engine) == [("FAILED", conversation.MAX_INBOX_ATTEMPTS)]
    assert await purposes(engine) == ["FILE_UNREADABLE"]


async def test_text_that_keeps_failing_is_answered_after_the_last_attempt(engine, workers, monkeypatch):  # noqa: F811
    from datetime import timedelta

    from app.services import conversation

    async def broken(self, body):
        raise RuntimeError("bug")

    monkeypatch.setattr(conversation._Ctx, "on_text", broken)
    await say(engine, "同意")
    at = NOW
    for _ in range(conversation.MAX_INBOX_ATTEMPTS - 1):
        assert await purposes(engine) == []  # not before the last attempt
        at += timedelta(minutes=6)
        await process_inbox(engine, now=at, keyring=RING, corp_id=CORP)
    assert await inbox_states(engine) == [("FAILED", conversation.MAX_INBOX_ATTEMPTS)]
    assert await purposes(engine) == ["MESSAGE_FAILED"]


async def test_downloads_have_a_time_budget_per_pass(engine, workers, monkeypatch):  # noqa: F811
    from app.services import conversation

    monkeypatch.setattr(conversation, "PREFETCH_BUDGET_S", -1)
    msgid = f"f{next(_n)}"
    async with engine.begin() as conn:
        await store_message(
            conn,
            CORP,
            {
                "msgid": msgid,
                "open_kfid": KF,
                "external_userid": USER,
                "send_time": int(NOW.timestamp()),
                "origin": 3,
                "msgtype": "file",
                "file": {"media_id": "a"},
            },
            now=NOW,
            keyring=RING,
        )
    await store(engine, "同意")
    await process_inbox(engine, now=NOW, keyring=RING, corp_id=CORP, media=Peek({"a": M4A}))
    assert await inbox_states(engine) == [("PENDING", 0), ("PENDING", 0)]  # both wait, in order
    assert await purposes(engine) == []
    async with engine.connect() as conn:
        due = (await conn.execute(text("SELECT next_attempt_at FROM kf_inbox"))).scalars().all()
    assert due == [NOW, NOW]  # postponed by the budget, not by a failure: due again next pass


async def test_erasure_cancels_without_extra_notices(engine, workers):  # noqa: F811
    from app.services.purge import purge

    await send(engine, "file", {"media_id": "a"}, Peek({"a": M4A}))
    await say(engine, "删除")
    await purge(engine, NullStorage(), now=NOW)
    await run_effects(engine, now=NOW, storage=NullStorage())
    assert await purposes(engine) == ["UPLOAD_CONFIRM", "DELETE_REQUESTED"]


async def test_upload_link_end_to_end(engine, workers, monkeypatch):  # noqa: F811
    """上传录音 -> link minted when sent -> upload page -> ingest -> recognition -> minutes -> result link."""
    import re as re_
    from datetime import timedelta

    import httpx
    from app.deps import clock, db_engine, keyring, object_storage
    from app.main import app
    from app.postprocess.providers import AsrResult, AsrSegment, AsrState
    from app.services.outbox import send_outbox
    from test_conversation import FakeKf
    from test_fallback_uploads import Bucket, pipeline
    from test_postprocess import FakeAsr

    await say(engine, "上传录音")
    kf = FakeKf()
    await send_outbox(engine, kf.client(), now=NOW, base_url="https://test")
    [sent] = kf.sent
    path = re_.search(r"https://test(/u/[A-Za-z0-9_-]{43})", sent["text"]["content"]).group(1)
    assert "{upload_link}" not in sent["text"]["content"]

    bucket = Bucket()
    app.dependency_overrides.update(
        {db_engine: lambda: engine, clock: lambda: NOW, keyring: lambda: RING, object_storage: lambda: bucket}
    )
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test") as web:
            assert (await web.get(path)).status_code == 200
            put = await web.put(path, content=M4A, headers={"x-consent-version": msgs.UPLOAD_CONSENT_VERSION})
            assert put.status_code == 200, put.text
    finally:
        app.dependency_overrides.clear()

    class Instant(FakeAsr):
        async def poll(self, task_id):
            return AsrResult(AsrState.DONE, [AsrSegment(0, 2000, "我们决定下周三上线。", "0")])

    base = pipeline(bucket)
    import dataclasses

    full = dataclasses.replace(
        base,
        asr=Instant(),
        llm=FakeLlm({"summary": "确定上线", "decisions": [], "action_items": [], "risks": []}),
        keyring=RING,
    )
    from app.postprocess import jobs

    monkeypatch.setattr(jobs, "ASR_POLL_INTERVAL", timedelta(0))
    at = NOW + timedelta(seconds=5)
    for _ in range(4):
        await run_jobs(engine, full, now=at)
        at += timedelta(seconds=1)
    [task] = await tasks(engine)
    assert task.status == "COMPLETED"
    await run_effects(engine, now=at, storage=NullStorage())
    await send_outbox(engine, kf.client(), now=at, base_url="https://test")
    assert "https://test/r/" in kf.sent[-1]["text"]["content"]
    assert kf.sent[-1]["touser"] == sent["touser"]


async def test_late_upload_link_for_a_dropped_task_is_not_sent(engine, workers):  # noqa: F811
    from app.domain.state_machine import Trigger, TriggerType
    from app.services.outbox import send_outbox
    from app.services.task_transitions import run_trigger
    from test_conversation import FakeKf

    await say(engine, "上传录音")
    [task] = await tasks(engine)
    await run_trigger(engine, task.id, Trigger(TriggerType.INTAKE_EXPIRED), now=NOW, actor="test")
    kf = FakeKf()
    await send_outbox(engine, kf.client(), now=NOW, base_url="https://test")
    assert kf.sent == []
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT status FROM outgoing_messages")) == "CANCELED"


async def test_word_file_confirmation_does_not_invite_more_text(engine, workers):  # noqa: F811
    await send(engine, "file", {"media_id": "doc"}, Peek({"doc": docx(TRANSCRIPT)}))
    [(purpose, content)] = await outbox(engine)
    assert purpose == "FORWARDED_CONFIRM" and "继续发送" not in content


async def test_medium_length_minutes_with_a_meeting_number(engine, workers):  # noqa: F811
    body = "会议号：936-109-253 的会议纪要\n" + "\n".join(f"第{i}项：讨论了上线安排。" for i in range(20))
    await say(engine, body)
    assert await purposes(engine) == ["FORWARDED_CONFIRM"]


async def test_time_reply_for_a_meeting_while_minutes_are_open(engine, workers):  # noqa: F811
    await say(engine, "会议纪要：" + "讨论了上线安排。" * 30)
    await say(engine, INVITE_NO_TIME_NO_PASSWORD)
    await say(engine, "2026/10/11 15:00-16:00")
    assert await purposes(engine) == ["FORWARDED_CONFIRM", "ASK_TIME", "CONFIRM"]
    kinds = {t.task_kind: t.status for t in await tasks(engine)}
    assert kinds == {"FORWARDED_MINUTES": "AWAITING_CONSENT", "LIVE_MEETING": "AWAITING_CONSENT"}


async def test_agree_covers_a_recording_and_a_newer_meeting(engine, workers):  # noqa: F811
    await send(engine, "file", {"media_id": "a"}, Peek({"a": M4A}))
    await say(engine, INVITE)
    await say(engine, "同意")
    kinds = {t.task_kind: t.status for t in await tasks(engine)}
    assert kinds == {"USER_UPLOAD": "UPLOADING", "LIVE_MEETING": "SCHEDULED"}


async def test_agree_on_a_newer_recording_points_at_the_waiting_meeting(engine, workers):  # noqa: F811
    await say(engine, INVITE)
    await send(engine, "file", {"media_id": "a"}, Peek({"a": M4A}))
    await say(engine, "同意")
    kinds = {t.task_kind: t.status for t in await tasks(engine)}
    assert kinds == {"USER_UPLOAD": "UPLOADING", "LIVE_MEETING": "AWAITING_CONSENT"}
    purpose, content = (await outbox(engine))[-1]
    assert purpose == "PROCESSING" and "另有一场会议仍待确认" in content
    await say(engine, "同意")
    kinds = {t.task_kind: t.status for t in await tasks(engine)}
    assert kinds["LIVE_MEETING"] == "SCHEDULED"


async def test_meeting_over_is_said_once(engine, workers):  # noqa: F811
    await send(engine, "file", {"media_id": "a"}, Peek({"a": M4A}))
    await say(engine, INVITE)
    after = NOW.replace(day=11, hour=9)
    for _ in range(2):
        await store(engine, "同意", at=after)
        await process_inbox(engine, now=after, keyring=RING, corp_id=CORP)
    kinds = {t.task_kind: t.status for t in await tasks(engine)}
    assert kinds == {"USER_UPLOAD": "UPLOADING", "LIVE_MEETING": "CANCELED"}
    assert (await purposes(engine))[-2:] == ["MEETING_OVER", "PROCESSING"]
