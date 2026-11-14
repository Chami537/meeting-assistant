"""One runner pass ties the pieces together: reconcile pull -> inbox -> effects -> outbox."""

import json
import os

import httpx
from app import runner
from app.config import Settings
from app.integrations.wecom.client import BASE_URL, WeComClient
from app.security.keyring import Keyring
from factories import NOW
from sqlalchemy import text

RING = Keyring({1: os.urandom(32)}, 1)


class FakeKf:
    def __init__(self):
        self.sent = []
        self.pages = [
            {
                "next_cursor": "c1",
                "has_more": 0,
                "msg_list": [
                    {
                        "msgid": "m1",
                        "open_kfid": "wkKF",
                        "external_userid": "wmU",
                        "send_time": int(NOW.timestamp()),
                        "origin": 3,
                        "msgtype": "text",
                        "text": {"content": "#腾讯会议：936-109-253\n会议时间：2026/10/11 15:00-16:00"},
                    }
                ],
            }
        ]

    def handler(self, request):
        if request.url.path == "/cgi-bin/gettoken":
            return httpx.Response(200, json={"errcode": 0, "access_token": "a", "expires_in": 7200})
        body = json.loads(request.content)
        if request.url.path == "/cgi-bin/kf/sync_msg":
            page = self.pages.pop(0) if self.pages else {"next_cursor": "c1", "has_more": 0, "msg_list": []}
            return httpx.Response(200, json={"errcode": 0, **page})
        if request.url.path == "/cgi-bin/kf/service_state/get":
            return httpx.Response(200, json={"errcode": 0, "service_state": 1})
        if request.url.path == "/cgi-bin/kf/send_msg":
            self.sent.append(body)
            return httpx.Response(200, json={"errcode": 0, "msgid": body["msgid"]})
        return httpx.Response(200, json={"errcode": 0})


async def test_run_once(engine, clean, monkeypatch):
    fake = FakeKf()
    client = WeComClient(
        "wwcorp", "s", http=httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(fake.handler))
    )
    monkeypatch.setattr(
        runner, "get_settings", lambda: Settings(wecom_corp_id="wwcorp", wecom_kf_open_kfid="wkKF")
    )
    monkeypatch.setattr(runner, "get_engine", lambda: engine)
    monkeypatch.setattr(runner, "keyring", lambda: RING)
    monkeypatch.setattr(runner, "wecom_client", lambda: client)

    report = await runner.run_once(now=NOW, reconcile=True)

    assert (report["synced"], report["inbox"], report["sent"]) == (1, 1, 1)
    assert "936-109-253" in fake.sent[0]["text"]["content"]
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT status FROM meeting_tasks")) == "AWAITING_CONSENT"


async def test_steps_are_isolated_and_effects_run_without_wecom(engine, clean, monkeypatch):
    from app.domain.state_machine import Trigger, TriggerType
    from app.services.task_transitions import run_trigger
    from factories import make_task
    from meeting_protocol import TaskStatus

    async with engine.begin() as conn:
        task_id = await make_task(conn, TaskStatus.SCHEDULED)
    await run_trigger(engine, task_id, Trigger(TriggerType.CANCEL_REQUESTED), now=NOW, actor="test")
    monkeypatch.setattr(runner, "get_engine", lambda: engine)
    monkeypatch.setattr(runner, "keyring", lambda: RING)

    monkeypatch.setattr(runner, "get_settings", lambda: Settings())  # WeChat not configured at all
    report = await runner.run_once(now=NOW, reconcile=True)
    assert report["effects"] == 1 and "sent" not in report

    def broken_pull(request):
        if request.url.path == "/cgi-bin/gettoken":
            return httpx.Response(200, json={"errcode": 0, "access_token": "a", "expires_in": 7200})
        return httpx.Response(200, json={"errcode": 95007, "errmsg": "x"})

    client = WeComClient(
        "wwcorp", "s", http=httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(broken_pull))
    )
    monkeypatch.setattr(runner, "wecom_client", lambda: client)
    monkeypatch.setattr(
        runner, "get_settings", lambda: Settings(wecom_corp_id="wwcorp", wecom_kf_open_kfid="wkKF")
    )
    report = await runner.run_once(now=NOW, reconcile=True)
    assert report["synced"] == "error"
    assert {"scheduler", "inbox", "effects", "sent", "purge"} <= set(report)


async def test_wecom_secret_missing_skips_only_the_wechat_steps(engine, clean, monkeypatch, caplog):
    from app.domain.state_machine import Trigger, TriggerType
    from app.services.task_transitions import run_trigger
    from factories import make_task
    from meeting_protocol import TaskStatus

    async with engine.begin() as conn:
        task_id = await make_task(conn, TaskStatus.SCHEDULED)
    await run_trigger(engine, task_id, Trigger(TriggerType.CANCEL_REQUESTED), now=NOW, actor="test")

    def unconfigured():
        raise RuntimeError("WECOM_CORP_ID / WECOM_APP_SECRET are not configured")

    monkeypatch.setattr(runner, "get_engine", lambda: engine)
    monkeypatch.setattr(runner, "keyring", lambda: RING)
    monkeypatch.setattr(runner, "wecom_client", unconfigured)
    monkeypatch.setattr(runner, "_last_warned", {})
    monkeypatch.setattr(
        runner, "get_settings", lambda: Settings(wecom_corp_id="wwcorp", wecom_kf_open_kfid="wkKF")
    )
    first = await runner.run_once(now=NOW, reconcile=True)
    second = await runner.run_once(now=NOW, reconcile=True)
    assert first["effects"] == 1 and {"scheduler", "purge"} <= set(first)
    assert not {"synced", "inbox", "sent"} & (set(first) | set(second))
    assert caplog.text.count("WECOM_APP_SECRET are not configured") == 1  # reported, not every tick


async def test_one_broken_task_does_not_stop_the_scheduler(engine, clean, monkeypatch, caplog):
    from app.services import dispatch
    from factories import make_task
    from meeting_protocol import TaskStatus

    async with engine.begin() as conn:
        broken = await make_task(conn, TaskStatus.SCHEDULED)
        fine = await make_task(conn, TaskStatus.SCHEDULED)
    real = dispatch.apply_and_settle

    async def apply(conn, task_id, trigger, **kw):
        if task_id == broken:
            raise KeyError("bad row")
        return await real(conn, task_id, trigger, **kw)

    monkeypatch.setattr(dispatch, "apply_and_settle", apply)
    counts = await dispatch.scheduler_tick(engine, now=NOW)
    assert counts["DISPATCH_DUE"] == 1
    async with engine.connect() as conn:
        statuses = dict((await conn.execute(text("SELECT id, status FROM meeting_tasks"))).all())
    assert statuses == {broken: "SCHEDULED", fine: "DISPATCHING"}
    assert f"DISPATCH_DUE of task {broken} failed: KeyError" in caplog.text


async def test_runner_refuses_to_start_without_a_usable_keyring(monkeypatch, caplog):
    import base64

    import pytest
    from app.security.keyring import KeyringError

    key = base64.b64encode(os.urandom(32)).decode()

    def bad_keys():
        raise KeyringError(f"pretend this quotes {key}")

    def no_database():
        raise AssertionError("must not get as far as the lock")

    monkeypatch.setattr(runner, "keyring", bad_keys)
    monkeypatch.setattr(runner, "get_engine", no_database)
    with pytest.raises(SystemExit) as exc:
        await runner.main()
    assert exc.value.code == 1
    assert "DATA_ENCRYPTION_KEYS is invalid (KeyringError)" in caplog.text and key not in caplog.text


def test_missing_ffmpeg_is_reported_once_per_interval(monkeypatch, caplog):
    import dataclasses

    from app.postprocess.jobs import PostProcessing

    base = PostProcessing(asr=None, llm=None, storage=None, merger=None, asr_daily_audio_seconds=1)
    monkeypatch.setattr(runner, "postprocessing", lambda: dataclasses.replace(base, media=None))
    monkeypatch.setattr(runner, "_last_warned", {})
    assert runner._postprocessing_or_none() is not None
    assert runner._postprocessing_or_none() is not None  # the jobs that need no ffmpeg still run
    assert caplog.text.count("ffmpeg/ffprobe not installed") == 1
