"""Fallback path, uploaded recordings: the upload page, the operator CLI path and the AUDIO_NORMALIZE job."""

import hashlib
import os
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from app.deps import clock, db_engine, keyring, object_storage
from app.domain.state_machine import Trigger, TriggerType
from app.intake import messages as msgs
from app.integrations.wecom.client import MediaTooLarge, WeComApiError
from app.main import app
from app.postprocess import jobs
from app.postprocess.jobs import run_jobs
from app.postprocess.media import Converted, MediaInfo, MediaRejected
from app.security.keyring import Keyring
from app.services import results, user_uploads
from app.services.task_inputs import InputKind, put_input
from app.services.task_transitions import run_trigger
from app.services.uploads import ObjectStat
from factories import NOW, make_user
from meeting_protocol import FailureCode, TaskStatus
from sqlalchemy import text
from test_postprocess import FakeLlm, deps

RING = Keyring({1: os.urandom(32)}, 1)
M4A = b"\x00\x00\x00\x20ftypM4A \x00\x00\x02\x00" + b"audio" * 100
PDF = b"%PDF-1.7\n" + b"x" * 100


class Bucket:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    async def upload_file(self, path, key, *, content_type):
        self.objects[key] = Path(path).read_bytes()

    async def download_file(self, key, path):
        Path(path).write_bytes(self.objects[key])

    async def delete(self, key):
        self.objects.pop(key, None)

    async def delete_prefix(self, prefix):
        for k in [k for k in self.objects if k.startswith(prefix)]:
            del self.objects[k]

    async def stat(self, key):
        data = self.objects.get(key)
        return None if data is None else ObjectStat(len(data), hashlib.sha256(data).hexdigest())

    async def presign_get(self, key, *, expires_at):
        return f"https://cos.example/{key}?get"

    async def presign_put(self, key, *, size, sha256, expires_at):
        raise AssertionError("not used")


class FakeMedia:
    """probe/convert without ffmpeg: the first bytes decide, durations come from the test."""

    def __init__(self, duration_ms=60_000, mean_volume=-20.0, has_audio=True, on_convert=None):
        self.duration_ms = duration_ms
        self.mean_volume = mean_volume
        self.has_audio = has_audio
        self.on_convert = on_convert

    async def probe(self, path):
        data = Path(path).read_bytes()
        if data[4:8] != b"ftyp":
            return None
        return MediaInfo("mov", self.duration_ms, self.has_audio)

    async def to_speech_audio(self, src, dst, *, demuxer):
        if self.on_convert:
            await self.on_convert()
        Path(dst).write_bytes(b"converted:" + Path(src).read_bytes()[:16])
        return Converted(self.duration_ms, self.mean_volume)


class Inbound:
    def __init__(self, data=M4A, error=None):
        self.data = data
        self.error = error
        self.calls = 0

    async def download_media(self, media_id, dest, *, max_bytes):
        self.calls += 1
        if self.error:
            raise self.error
        Path(dest).write_bytes(self.data)


def pipeline(bucket, media=None, inbound=None):
    base = deps(llm=FakeLlm())
    return jobs.PostProcessing(
        asr=base.asr,
        llm=base.llm,
        storage=bucket,
        merger=base.merger,
        asr_daily_audio_seconds=base.asr_daily_audio_seconds,
        media=media or FakeMedia(),
        inbound=inbound,
        keyring=RING,
    )


@pytest.fixture
async def web(engine, clean):
    bucket = Bucket()
    app.dependency_overrides.update(
        {db_engine: lambda: engine, clock: lambda: NOW, keyring: lambda: RING, object_storage: lambda: bucket}
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test") as client:
        yield client, bucket
    app.dependency_overrides.clear()


async def upload_link(engine):
    async with engine.begin() as conn:
        user_id = await make_user(conn)
        task_id = await user_uploads.create_upload_task(conn, user_id=user_id, key="kf:x:1", now=NOW)
        link = await results.mint_upload_link(
            conn, task_id=task_id, user_id=user_id, now=NOW, base_url="https://x"
        )
    return task_id, link.removeprefix("https://x")


async def q(engine, sql, **params):
    async with engine.connect() as conn:
        return (await conn.execute(text(sql), params)).all()


async def status(engine, task_id):
    return tuple(
        (await q(engine, "SELECT status, failure_code FROM meeting_tasks WHERE id = :t", t=task_id))[0]
    )


HEADERS = {"x-consent-version": msgs.UPLOAD_CONSENT_VERSION, "x-file-name": "%E4%BC%9A%E8%AE%AE.m4a"}


async def test_upload_page_to_transcription(engine, web):
    client, bucket = web
    task_id, path = await upload_link(engine)
    form = await client.get(path)
    assert form.status_code == 200 and "同意并上传" in form.text
    csp = form.headers["content-security-policy"]
    assert "script-src 'sha256-" in csp and "unsafe-inline'" not in csp.split("script-src")[1]

    r = await client.put(path, content=M4A, headers={**HEADERS, "origin": "https://test"})
    assert r.status_code == 200, r.text
    assert await status(engine, task_id) == (TaskStatus.UPLOADING, None)
    [staged] = [k for k in bucket.objects if k.startswith(f"tasks/{task_id}/audio/source-")]
    assert bucket.objects[staged] == M4A
    [consent] = await q(
        engine, "SELECT channel, consent_version FROM consent_records WHERE task_id = :t", t=task_id
    )
    assert tuple(consent) == ("web_upload", msgs.UPLOAD_CONSENT_VERSION)
    [inp] = await q(engine, "SELECT kind, file_name FROM task_inputs WHERE task_id = :t", t=task_id)
    assert tuple(inp) == ("STAGED_OBJECT", "会议.m4a")
    again = await client.put(path, content=M4A, headers=HEADERS)  # one upload per link
    assert again.status_code == 409 and "正在处理" in again.json()["error"]
    page = await client.get(path)
    assert page.status_code == 200 and "已经上传过" in page.text

    # the commit is stamped with the time after the transfer, a moment past NOW
    await run_jobs(engine, pipeline(bucket), now=NOW + timedelta(seconds=5))
    assert await status(engine, task_id) == (TaskStatus.TRANSCRIBING, None)
    [asset] = await q(
        engine,
        "SELECT source, segment_no, upload_status, duration_ms, object_key FROM audio_assets WHERE task_id = :t",
        t=task_id,
    )
    assert tuple(asset[:4]) == ("user_upload", 0, "READY", 60_000)
    assert bucket.objects[asset.object_key].startswith(b"converted:")
    assert staged not in bucket.objects  # the original is deleted once converted
    [inp] = await q(
        engine,
        "SELECT payload_ciphertext IS NULL, consumed_at FROM task_inputs WHERE task_id = :t",
        t=task_id,
    )
    assert inp[0] and inp[1] == NOW + timedelta(seconds=5)
    kinds = [
        r.kind
        for r in await q(
            engine, "SELECT kind FROM processing_jobs WHERE task_id = :t ORDER BY kind", t=task_id
        )
    ]
    assert kinds == ["ASR", "AUDIO_NORMALIZE"]


@pytest.mark.parametrize(
    ("body", "headers", "code"),
    [
        (M4A, {**HEADERS, "x-consent-version": "old"}, 422),
        (PDF, HEADERS, 422),
        (M4A, {**HEADERS, "origin": "https://evil.example"}, 403),
        (b"", HEADERS, 400),
    ],
)
async def test_upload_page_refusals(engine, web, body, headers, code):
    client, bucket = web
    task_id, path = await upload_link(engine)
    r = await client.put(path, content=body, headers=headers)
    assert r.status_code == code and r.json()["error"]
    assert await status(engine, task_id) == (TaskStatus.AWAITING_DETAILS, None)


async def test_upload_size_limit(engine, web, monkeypatch):
    from app.api import upload_page

    monkeypatch.setattr(upload_page, "MAX_SOURCE_BYTES", 100)
    client, _ = web
    _, path = await upload_link(engine)
    assert (await client.put(path, content=M4A, headers=HEADERS)).status_code == 413


async def test_expired_upload_link(engine, web):
    client, _ = web
    _, path = await upload_link(engine)
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE result_access_tokens SET expires_at = :n"), {"n": NOW})
    assert (await client.get(path)).status_code == 410
    assert (await client.put(path, content=M4A, headers=HEADERS)).status_code == 410


async def test_result_and_upload_tokens_do_not_mix(engine, web):
    client, _ = web
    _, path = await upload_link(engine)
    token = path.removeprefix("/u/")
    assert (await client.post(f"/r/{token}")).status_code == 410  # an upload link never opens minutes


async def wecom_task(engine, media_id="mid-1"):
    """A USER_UPLOAD task whose recording arrived as a WeChat file message, consent given."""
    async with engine.begin() as conn:
        user_id = await make_user(conn)
        task_id = await user_uploads.create_upload_task(conn, user_id=user_id, key="kf:x:2", now=NOW)
        await put_input(
            conn, RING, task_id=task_id, kind=InputKind.WECOM_MEDIA, payload=media_id, source_msg_type="file"
        )
    await run_trigger(engine, task_id, Trigger(TriggerType.DETAILS_COMPLETE), now=NOW, actor="test")
    await run_trigger(
        engine,
        task_id,
        Trigger(TriggerType.CONSENT_GRANTED, consent_version=msgs.UPLOAD_CONSENT_VERSION),
        now=NOW,
        actor="test",
    )
    return task_id


@pytest.mark.parametrize(
    ("media", "inbound", "expected"),
    [
        (FakeMedia(), Inbound(data=PDF), FailureCode.INVALID_UPLOAD),  # probe refuses it
        (FakeMedia(has_audio=False), Inbound(), FailureCode.INVALID_UPLOAD),  # video without sound
        (FakeMedia(duration_ms=6 * 3600 * 1000), Inbound(), FailureCode.INVALID_UPLOAD),  # over 5 hours
        (FakeMedia(mean_volume=-80.0), Inbound(), FailureCode.EMPTY_AUDIO),
        (
            FakeMedia(),
            Inbound(error=WeComApiError(40007, "invalid media_id", "media/get")),
            FailureCode.UPLOAD_FAILED,
        ),
        (FakeMedia(), Inbound(error=MediaTooLarge(10**10)), FailureCode.INVALID_UPLOAD),
    ],
)
async def test_unusable_uploads_fail_the_task(engine, clean, media, inbound, expected):
    task_id = await wecom_task(engine)
    await run_jobs(engine, pipeline(Bucket(), media=media, inbound=inbound), now=NOW)
    assert await status(engine, task_id) == (TaskStatus.FAILED, expected)
    notices = await q(
        engine, "SELECT count(*) FROM task_effects WHERE task_id = :t AND effect = 'NOTIFY_USER'", t=task_id
    )
    assert notices[0][0] == 1
    [inp] = await q(
        engine, "SELECT payload_ciphertext IS NULL FROM task_inputs WHERE task_id = :t", t=task_id
    )
    assert inp[0]  # the media_id is not kept after the decision


async def test_converter_rejection(engine, clean):
    class Broken(FakeMedia):
        async def to_speech_audio(self, src, dst, *, demuxer):
            raise MediaRejected("ffmpeg exited 1")

    task_id = await wecom_task(engine)
    await run_jobs(engine, pipeline(Bucket(), media=Broken(), inbound=Inbound()), now=NOW)
    assert await status(engine, task_id) == (TaskStatus.FAILED, FailureCode.INVALID_UPLOAD)


async def test_transient_download_errors_retry_then_fail(engine, clean):
    inbound = Inbound(error=WeComApiError(-1, "transport: ReadTimeout", "media/get"))
    task_id = await wecom_task(engine)
    at = NOW
    for _ in range(jobs.MAX_INGEST_ATTEMPTS):
        await run_jobs(engine, pipeline(Bucket(), inbound=inbound), now=at)
        at += timedelta(minutes=10)
    assert inbound.calls == jobs.MAX_INGEST_ATTEMPTS
    assert await status(engine, task_id) == (TaskStatus.FAILED, FailureCode.UPLOAD_FAILED)


async def test_cancel_during_conversion_stores_nothing(engine, clean):
    task_id = None

    async def cancel():
        await run_trigger(engine, task_id, Trigger(TriggerType.CANCEL_REQUESTED), now=NOW, actor="test")

    task_id = await wecom_task(engine)
    await run_jobs(engine, pipeline(Bucket(), media=FakeMedia(on_convert=cancel), inbound=Inbound()), now=NOW)
    assert (await status(engine, task_id))[0] == TaskStatus.CANCELED
    assert await q(engine, "SELECT 1 FROM audio_assets WHERE task_id = :t", t=task_id) == []
    [job] = await q(engine, "SELECT status FROM processing_jobs WHERE task_id = :t", t=task_id)
    assert job.status == "FAILED"


async def test_operator_cli_path(engine, clean, tmp_path):
    bucket = Bucket()
    source = tmp_path / "meeting.m4a"
    source.write_bytes(M4A)
    task_id = await user_uploads.stage_local_file(engine, bucket, RING, source, user_label="demo", now=NOW)
    assert await status(engine, task_id) == (TaskStatus.UPLOADING, None)
    [consent] = await q(engine, "SELECT channel FROM consent_records WHERE task_id = :t", t=task_id)
    assert consent.channel == "api"
    await run_jobs(engine, pipeline(bucket), now=NOW)
    assert (await status(engine, task_id))[0] == TaskStatus.TRANSCRIBING
    with pytest.raises(user_uploads.UploadError):
        bad = tmp_path / "x.pdf"
        bad.write_bytes(PDF)
        await user_uploads.stage_local_file(engine, bucket, RING, bad, user_label="demo", now=NOW)


async def test_upload_link_of_a_task_that_ended_is_refused_before_the_body(engine, web):
    client, bucket = web
    task_id, path = await upload_link(engine)
    await run_trigger(engine, task_id, Trigger(TriggerType.INTAKE_EXPIRED), now=NOW, actor="test")
    assert (await client.get(path)).status_code == 410
    assert (await client.put(path, content=M4A, headers=HEADERS)).status_code == 410
    assert bucket.objects == {}


async def test_second_link_of_the_same_task_cannot_replace_the_accepted_upload(engine, web):
    client, bucket = web
    task_id, first = await upload_link(engine)
    async with engine.begin() as conn:
        user_id = await conn.scalar(text("SELECT user_id FROM meeting_tasks WHERE id = :t"), {"t": task_id})
        second = await results.mint_upload_link(
            conn, task_id=task_id, user_id=user_id, now=NOW, base_url="https://x"
        )
    second = second.removeprefix("https://x")
    assert (await client.put(first, content=M4A, headers=HEADERS)).status_code == 200
    late = await client.put(second, content=M4A + b"other", headers=HEADERS)
    assert late.status_code == 409 and "正在处理" in late.json()["error"]  # the task already has its file
    [staged] = [k for k in bucket.objects if "/source-" in k]
    assert bucket.objects[staged] == M4A
    [revoked] = await q(engine, "SELECT count(*) FROM result_access_tokens WHERE revoked_at IS NOT NULL")
    assert revoked[0] == 1


async def test_an_upload_that_is_not_accepted_leaves_nothing_in_storage(engine, web, monkeypatch):
    client, bucket = web
    _, path = await upload_link(engine)

    async def lost_race(conn, slot, *, now):
        return False  # e.g. the task was cancelled while the file was being transferred

    monkeypatch.setattr(results, "use_upload_slot", lost_race)
    assert (await client.put(path, content=M4A, headers=HEADERS)).status_code == 422
    assert bucket.objects == {}


async def test_file_name_header_is_display_only(engine, web):
    client, _ = web
    task_id, path = await upload_link(engine)
    headers = {**HEADERS, "x-file-name": "%00..%2F..%2Fetc%2Fpasswd%E4%BC%9A"}
    assert (await client.put(path, content=M4A, headers=headers)).status_code == 200
    [inp] = await q(engine, "SELECT file_name FROM task_inputs WHERE task_id = :t", t=task_id)
    assert inp.file_name == "_.._.._etc_passwd会"


async def test_busy_server_and_low_disk(engine, web, monkeypatch):
    import asyncio
    import collections

    from app.api import upload_page

    client, _ = web
    _, path = await upload_link(engine)
    monkeypatch.setattr(upload_page, "_UPLOADS", asyncio.Semaphore(0))
    assert (await client.put(path, content=M4A, headers=HEADERS)).status_code == 503
    monkeypatch.setattr(upload_page, "_UPLOADS", asyncio.Semaphore(1))
    usage = collections.namedtuple("usage", "total used free")
    monkeypatch.setattr(upload_page.shutil, "disk_usage", lambda p: usage(1, 1, 10))
    assert (await client.put(path, content=M4A, headers=HEADERS)).status_code == 503


async def test_rejected_upload_is_discarded_from_storage(engine, clean):
    task_id = await wecom_task(engine)
    await run_jobs(engine, pipeline(Bucket(), media=FakeMedia(has_audio=False), inbound=Inbound()), now=NOW)
    discards = await q(
        engine, "SELECT count(*) FROM task_effects WHERE task_id = :t AND effect = 'DISCARD_AUDIO'", t=task_id
    )
    assert discards[0][0] == 2  # now, and again once every upload URL issued before has expired


async def test_duration_is_checked_after_conversion_too(engine, clean):
    class NoHeader(FakeMedia):
        async def probe(self, path):
            info = await super().probe(path)
            return info and MediaInfo(info.demuxer, 0, True)  # container without a duration

    task_id = await wecom_task(engine)
    media = NoHeader(duration_ms=5 * 3600 * 1000)
    await run_jobs(engine, pipeline(Bucket(), media=media, inbound=Inbound()), now=NOW)
    assert await status(engine, task_id) == (TaskStatus.FAILED, FailureCode.INVALID_UPLOAD)


async def test_used_link_of_an_ended_task_says_so(engine, web):
    client, _ = web
    task_id, path = await upload_link(engine)
    assert (await client.put(path, content=M4A, headers=HEADERS)).status_code == 200
    await run_trigger(engine, task_id, Trigger(TriggerType.CANCEL_REQUESTED), now=NOW, actor="test")
    page = await client.get(path)
    assert "已经用过" in page.text and "正在处理" not in page.text


async def test_stalled_upload_is_cut_off(engine, web, monkeypatch):
    import asyncio

    from app.api import upload_page

    monkeypatch.setattr(upload_page, "IDLE_TIMEOUT_S", 0.05)
    client, bucket = web
    task_id, path = await upload_link(engine)

    async def slow():
        yield M4A[:100]
        await asyncio.sleep(0.5)
        yield M4A[100:]

    headers = {**HEADERS, "content-length": str(len(M4A))}
    r = await client.put(path, content=slow(), headers=headers)
    assert r.status_code == 408
    assert bucket.objects == {} and upload_page._IN_FLIGHT == {}


async def test_ingest_in_progress_outlives_the_upload_deadline(engine, clean):
    from app.services.dispatch import scheduler_tick

    task_id = await wecom_task(engine)
    await scheduler_tick(engine, now=NOW + timedelta(hours=3))  # past upload_deadline_at, job still pending
    assert (await status(engine, task_id))[0] == TaskStatus.UPLOADING


async def test_waiting_for_ffmpeg_is_reported_and_bounded(engine, clean, caplog):
    import dataclasses

    task_id = await wecom_task(engine)
    no_ffmpeg = dataclasses.replace(pipeline(Bucket(), inbound=Inbound()), media=None)
    await run_jobs(engine, no_ffmpeg, now=NOW)
    assert (await status(engine, task_id))[0] == TaskStatus.UPLOADING
    assert "ffmpeg/ffprobe not available" in caplog.text
    at = NOW + jobs.BUDGET_RECHECK
    while at < NOW + jobs.UNCONFIGURED_MAX_WAIT:
        await run_jobs(engine, no_ffmpeg, now=at)
        assert (await status(engine, task_id))[0] == TaskStatus.UPLOADING
        at += jobs.BUDGET_RECHECK
    await run_jobs(engine, no_ffmpeg, now=at)
    assert await status(engine, task_id) == (TaskStatus.FAILED, FailureCode.UPLOAD_FAILED)
    [job] = await q(engine, "SELECT status, last_error FROM processing_jobs WHERE task_id = :t", t=task_id)
    assert tuple(job) == ("FAILED", "upload processing not configured")
