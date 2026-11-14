"""The AI meeting page's jobs (services/meeting_parse.py) and their HTTP API (api/meeting_parse_api.py), with
fakes for our transcription service and the minutes model."""

import asyncio
import sqlite3

import httpx
import pytest
from app.api import meeting_parse_api
from app.config import Settings
from app.main import app
from app.postprocess import proofread
from app.postprocess.providers import (
    AsrResult,
    AsrSegment,
    AsrSentence,
    AsrState,
    LlmReply,
    ProviderBusy,
    ProviderError,
)
from app.services.meeting_parse import JobStore, MeetingParser
from fastapi.testclient import TestClient

URL = "http://files.example.com:9000/meetings/7/31/source.m4a?X-Amz-Signature=abc"
SEGMENTS = [
    AsrSegment(
        0,
        8000,
        "欢迎参加晨川科技业绩说明会。今年收入3529万元。",
        "发言人1",
        sentences=(
            AsrSentence(0, 3000, "欢迎参加晨川科技业绩说明会。"),
            AsrSentence(3200, 7900, "今年收入3529万元。"),
        ),
    ),
    AsrSegment(9000, 20000, "新产线预计6月底调试。", "发言人2"),
]
REPLY = {
    "summary": "晨川科技业绩说明会。",
    "meeting_type": "公司交流",
    "meeting_date": None,
    "key_points": [{"text": "收入增长", "details": ["今年收入3529万元"], "evidence_segments": [0]}],
    "key_facts": [{"text": "收入3529万元", "evidence_segments": [0]}],
    "outlook": [{"text": "新产线调试", "kind": "计划", "timeframe": "6月底", "evidence_segments": [1]}],
    "decisions": [],
    "action_items": [],
    "risks": [],
    "name_fixes": [],
}


class FakeAsr:
    """Our transcription service: `polls` are the answers to the polls, in order (the last one repeats)."""

    def __init__(self, *polls, busy=0, errors=0):
        self.polls = list(polls) or [AsrResult(AsrState.DONE, segments=SEGMENTS)]
        self.busy, self.errors = busy, errors
        self.submitted: list[tuple[str, str | None]] = []
        self.discarded: list[str] = []
        self.seen_phases: list[str | None] = []
        self.parser: MeetingParser | None = None

    async def submit(self, audio_url, title=None):
        if self.busy:
            self.busy -= 1
            raise ProviderBusy("queue full")
        if self.errors:
            self.errors -= 1
            raise ProviderError("audio download HTTP 403")
        self.submitted.append((audio_url, title))
        return f"task{len(self.submitted)}"

    async def poll(self, task_id):
        if self.parser is not None and (job := self.parser.status("job1")):
            self.seen_phases.append(job["phase"])
        return self.polls.pop(0) if len(self.polls) > 1 else self.polls[0]

    async def discard(self, task_id):
        self.discarded.append(task_id)

    async def aclose(self):
        pass


class FakeLlm:
    name, model, region = "fake", "fake-model", ""

    def __init__(self, *replies, parser=None):
        self.replies = list(replies) or [REPLY]
        self.prompts: list[str] = []
        self.parser = parser
        self.seen_phases: list[str | None] = []

    async def complete_json(self, *, system, user, schema):
        if schema is proofread.SCHEMA:
            return LlmReply({"fixes": []})
        self.prompts.append(user)
        if self.parser is not None and (job := self.parser.status("job1")):
            self.seen_phases.append(job["phase"])
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return LlmReply(reply)


async def no_sleep(seconds):
    await asyncio.sleep(0)


def parser_for(path, asr=None, llm=None, **kw) -> MeetingParser:
    asr = asr or FakeAsr()
    llm = llm or FakeLlm()
    kw = {"sleep": no_sleep, "poll_seconds": 0, "busy_seconds": 0, "minutes_wait_seconds": 0, **kw}
    get_llm = llm if callable(llm) and not isinstance(llm, FakeLlm) else (lambda: llm)
    parser = MeetingParser(
        JobStore(str(path / "jobs.sqlite3")), asr=asr, llm=get_llm, minutes_slots=asyncio.Semaphore(2), **kw
    )
    asr.parser = parser
    if isinstance(llm, FakeLlm):
        llm.parser = parser
    return parser


async def finished(parser: MeetingParser) -> None:
    while parser._tasks:
        await asyncio.gather(*list(parser._tasks), return_exceptions=True)


async def test_a_job_ends_with_the_pages_result_and_keeps_no_audio_link(tmp_path):
    asr, llm = FakeAsr(AsrResult(AsrState.RUNNING), AsrResult(AsrState.DONE, segments=SEGMENTS)), FakeLlm()
    parser = parser_for(tmp_path, asr, llm)
    parser.submit("job1", 31, URL, "03-晨川科技20260318")
    assert parser.status("job1") == {"status": "processing", "phase": "queued", "result": None, "error": None}
    await finished(parser)
    job = parser.status("job1")
    assert (job["status"], job["phase"], job["error"]) == ("done", None, None)
    paras = job["result"]["transcription"]["paragraphs"]
    assert [(p["id"], p["start_time_ms"], p["text"]) for p in paras] == [
        (1, 0, "欢迎参加晨川科技业绩说明会。"),
        (2, 3200, "今年收入3529万元。"),
        (3, 9000, "新产线预计6月底调试。"),
    ]
    points = job["result"]["summary"]["key_points"]
    assert [(p["title"], p["paragraph_ids"]) for p in points] == [
        ("收入增长", [1, 2]),
        ("重要数据与事实", [1, 2]),
        ("计划与预期", [3]),
    ]
    assert asr.submitted == [(URL, "03-晨川科技20260318")]  # the title goes as recognition context
    assert asr.discarded == ["task1"]  # the transcript does not wait 24 h on the service
    assert asr.seen_phases == ["transcribing", "transcribing"] and llm.seen_phases == ["summarizing"]
    assert "<title>03-晨川科技20260318</title>" in llm.prompts[0]
    stored = (
        sqlite3.connect(tmp_path / "jobs.sqlite3").execute("SELECT audio_url FROM meeting_jobs").fetchone()
    )
    assert stored == (None,)  # the signed link is not kept past the job
    await parser.close()


async def test_a_busy_service_is_waited_for_and_a_lost_job_is_submitted_again(tmp_path):
    asr = FakeAsr(AsrResult(AsrState.LOST), AsrResult(AsrState.DONE, segments=SEGMENTS), busy=3)
    parser = parser_for(tmp_path, asr)
    parser.submit("job1", 31, URL, None)
    await finished(parser)
    assert parser.status("job1")["status"] == "done"
    assert [task for task, _ in asr.submitted] == [URL, URL]
    await parser.close()


@pytest.mark.parametrize(
    ("asr", "error"),
    [
        (
            FakeAsr(AsrResult(AsrState.FAILED, error="qwen: audio could not be decoded")),
            "语音识别失败（qwen: audio could not be decoded）",
        ),
        (FakeAsr(AsrResult(AsrState.DONE, segments=[AsrSegment(0, 1000, "  ")])), "录音里没有识别出说话内容"),
        (FakeAsr(errors=3), "提交语音识别失败（audio download HTTP 403）"),  # its link expired, say
    ],
)
async def test_without_a_transcript_the_job_fails_with_a_reason_the_page_shows(tmp_path, asr, error):
    parser = parser_for(tmp_path, asr)
    parser.submit("job1", 31, URL, None)
    await finished(parser)
    assert parser.status("job1") == {"status": "failed", "phase": None, "result": None, "error": error}
    await parser.close()


async def test_two_failed_submits_are_tried_again(tmp_path):
    asr = FakeAsr(errors=2)
    parser = parser_for(tmp_path, asr)
    parser.submit("job1", 31, URL, None)
    await finished(parser)
    assert parser.status("job1")["status"] == "done" and len(asr.submitted) == 1
    await parser.close()


def not_configured():
    raise RuntimeError("LLM_MODEL is not configured")


@pytest.mark.parametrize(
    ("llm", "why"),
    [
        (FakeLlm(ProviderError("HTTP 400", permanent=True)), "写纪要失败（HTTP 400）"),
        (not_configured, "纪要模型没有配置"),
    ],
)
async def test_without_minutes_the_job_still_ends_with_its_transcript(tmp_path, llm, why):
    parser = parser_for(tmp_path, llm=llm)
    parser.submit("job1", 31, URL, None)
    await finished(parser)
    job = parser.status("job1")
    assert job["status"] == "done" and len(job["result"]["transcription"]["paragraphs"]) == 3
    assert job["result"]["summary"]["overview"] == f"纪要没有生成：{why}"
    assert job["result"]["summary"]["key_points"] == []
    await parser.close()


async def test_a_minutes_model_briefly_unavailable_is_asked_again(tmp_path):
    llm = FakeLlm(ProviderError("HTTP 503"), REPLY)
    parser = parser_for(tmp_path, llm=llm)
    parser.submit("job1", 31, URL, None)
    await finished(parser)
    assert len(llm.prompts) == 2 and parser.status("job1")["result"]["summary"]["key_points"]
    await parser.close()


async def test_the_job_fails_with_a_reason_before_the_go_backend_gives_up(tmp_path):
    now = [1000.0]

    class Stuck(FakeAsr):
        async def poll(self, task_id):
            now[0] += 60  # a minute a poll: still running after 33
            return AsrResult(AsrState.RUNNING)

    asr = Stuck()
    parser = parser_for(tmp_path, asr, clock=lambda: now[0])
    parser.submit("job1", 31, URL, None)
    await finished(parser)
    assert parser.status("job1")["error"] == "语音识别超时"
    assert asr.discarded == ["task1"]
    await parser.close()


async def test_a_job_posted_twice_runs_once(tmp_path):
    asr = FakeAsr()
    parser = parser_for(tmp_path, asr)
    parser.submit("job1", 31, URL, None)
    parser.submit("job1", 31, URL, None)
    await finished(parser)
    assert len(asr.submitted) == 1
    await parser.close()


async def test_a_restart_takes_up_cut_off_jobs_and_ends_stale_and_old_ones(tmp_path):
    now = 100_000.0
    store = JobStore(str(tmp_path / "jobs.sqlite3"))
    store.add("job1", 31, "03-晨川科技20260318", URL, now - 600)  # cut off ten minutes in
    store.add("stale", 32, None, URL, now - 7 * 3600)  # its signed link has expired
    store.add("old", 33, None, URL, now - 9 * 86400)
    store.finish("old", now - 8 * 86400, result={"x": 1})
    store.close()
    parser = parser_for(tmp_path, clock=lambda: now)
    parser.resume()
    await finished(parser)
    assert parser.status("job1")["status"] == "done"
    assert parser.status("stale") == {
        "status": "failed",
        "phase": None,
        "result": None,
        "error": "解析服务重启，任务中断，请重新导入",
    }
    assert parser.status("old") is None  # finished more than 7 days ago
    await parser.close()


async def test_closing_leaves_a_running_job_to_the_next_start(tmp_path):
    gate = asyncio.Event()

    class Slow(FakeAsr):
        async def poll(self, task_id):
            await gate.wait()
            return AsrResult(AsrState.DONE, segments=SEGMENTS)

    parser = parser_for(tmp_path, Slow())
    parser.submit("job1", 31, URL, None)
    await asyncio.sleep(0.01)
    await parser.close()
    row = (
        sqlite3.connect(tmp_path / "jobs.sqlite3")
        .execute("SELECT status, audio_url FROM meeting_jobs")
        .fetchone()
    )
    assert row == ("processing", URL)


async def test_a_restart_during_transcription_polls_that_job_instead_of_submitting_again(tmp_path):
    store = JobStore(str(tmp_path / "jobs.sqlite3"))
    store.add("job1", 31, None, URL, 1000.0)
    store.set_task("job1", "task-before-restart")  # the transcription service kept running it
    store.close()
    asr = FakeAsr()
    parser = parser_for(tmp_path, asr, clock=lambda: 1300.0)
    parser.resume()
    await finished(parser)
    assert parser.status("job1")["status"] == "done"
    assert asr.submitted == [] and asr.discarded == ["task-before-restart"]
    await parser.close()


async def test_a_job_too_old_to_finish_is_not_started_again(tmp_path):
    store = JobStore(str(tmp_path / "jobs.sqlite3"))
    store.add("job1", 31, None, URL, 1000.0)
    store.close()
    asr = FakeAsr()
    parser = parser_for(tmp_path, asr, clock=lambda: 1000.0 + 34 * 60)  # the page's backend gave up
    parser.resume()
    await finished(parser)
    assert parser.status("job1")["error"] == "解析服务重启，任务中断，请重新导入"
    assert asr.submitted == []  # no download, no upload jumping RAGFlow's queue
    await parser.close()


async def test_a_result_the_client_cannot_read_fails_the_job_at_once(tmp_path):
    class Garbled(FakeAsr):
        async def poll(self, task_id):
            raise ProviderError("unexpected result shape", permanent=True)

    parser = parser_for(tmp_path, Garbled())
    parser.submit("job1", 31, URL, None)
    await finished(parser)
    assert parser.status("job1")["error"] == "读取语音识别结果失败（unexpected result shape）"
    await parser.close()


async def test_an_unexpected_error_ends_the_job_without_logging_its_message(tmp_path, caplog):
    said = "我们决定下周三上线"  # what an error message could quote: the log shows code lines, not values

    class Broken(FakeAsr):
        async def poll(self, task_id):
            raise RuntimeError(said)

    parser = parser_for(tmp_path, Broken())
    parser.submit("job1", 31, URL, None)
    await finished(parser)
    assert parser.status("job1")["error"] == "解析失败（RuntimeError）"
    assert "下周三" not in caplog.text and "RuntimeError" in caplog.text
    assert "X-Amz-Signature" not in caplog.text  # nor the signed link
    await parser.close()


async def test_waiting_for_a_minutes_slot_ends_with_the_job_and_keeps_the_transcript(tmp_path):
    parser = parser_for(tmp_path, budget_seconds=0.2)
    parser._minutes_slots = asyncio.Semaphore(0)  # RAGFlow's requests hold both
    parser.submit("job1", 31, URL, None)
    await finished(parser)
    job = parser.status("job1")
    assert job["status"] == "done" and job["result"]["summary"]["overview"] == "纪要没有生成：等待写纪要超时"


async def test_one_minutes_request_takes_no_longer_than_the_minutes_apis(tmp_path):
    class Hung(FakeLlm):
        async def complete_json(self, *, system, user, schema):
            await asyncio.sleep(3600)

    parser = parser_for(tmp_path, llm=Hung(), minutes_timeout=0.05)
    parser.submit("job1", 31, URL, None)
    await finished(parser)
    assert parser.status("job1")["result"]["summary"]["overview"] == "纪要没有生成：写纪要超时"
    assert parser._minutes_slots._value == 2  # the slot is given back
    await parser.close()


async def test_old_jobs_are_deleted_while_the_service_runs(tmp_path):
    now = [100_000.0]
    parser = parser_for(tmp_path, clock=lambda: now[0])
    parser.submit("job1", 31, URL, None)
    await finished(parser)
    now[0] += 8 * 86400
    parser.submit("job2", 32, URL, None)  # no restart in between
    await finished(parser)
    assert parser.status("job1") is None and parser.status("job2")["status"] == "done"
    await parser.close()


# The HTTP API


@pytest.fixture
def served(tmp_path):
    parser = parser_for(tmp_path)
    app.state.meeting_parser = parser
    yield parser
    app.state.meeting_parser = None


async def call(method, path, **kw):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.request(method, path, **kw)


async def test_the_api_the_go_backend_calls(served):
    body = {"job_id": "job1", "meeting_id": 31, "audio_url": URL, "title": " 03-晨川科技\n20260318 "}
    reply = await call("POST", "/meeting/parse", json=body)
    assert (reply.status_code, reply.json()) == (
        200,
        {"ok": True},
    )  # exactly 200: the Go side counts no other
    await finished(served)
    job = (await call("GET", "/meeting/status/job1")).json()
    assert job["status"] == "done" and job["result"]["summary"]["title"] == "03-晨川科技 20260318"
    unknown = await call("GET", "/meeting/status/nope")
    assert (unknown.status_code, unknown.json()) == (404, {"detail": "未找到该任务"})
    for bad in ({**body, "audio_url": "file:///etc/passwd"}, {**body, "job_id": ""}, {"job_id": "x"}):
        assert (await call("POST", "/meeting/parse", json=bad)).status_code == 422
    data_board = {k: body[k] for k in ("job_id", "meeting_id", "audio_url")} | {"job_id": "job2"}
    assert (
        await call("POST", "/meeting/parse", json=data_board)
    ).status_code == 200  # no title: data-board's
    await finished(served)


async def test_not_configured_both_endpoints_answer_503():
    app.state.meeting_parser = None
    assert (await call("GET", "/meeting/status/job1")).status_code == 503
    body = {"job_id": "job1", "meeting_id": 31, "audio_url": URL}
    assert (await call("POST", "/meeting/parse", json=body)).status_code == 503


def test_the_app_starts_the_parser_only_when_configured(tmp_path, monkeypatch, caplog):
    assert meeting_parse_api.start(Settings(_env_file=None)) is None
    assert meeting_parse_api.start(Settings(_env_file=None, meeting_parse_db=str(tmp_path / "j.db"))) is None
    assert "ASR_QWEN_URL is not" in caplog.text
    settings = Settings(
        _env_file=None, meeting_parse_db=str(tmp_path / "j.db"), asr_qwen_url="http://asr:8020"
    )
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    with TestClient(app) as client:
        assert isinstance(app.state.meeting_parser, MeetingParser)
        assert client.get("/meeting/status/job1").status_code == 404
        assert app.state.meeting_parser.asr._flags == {"sentences": "1", "priority": "1"}
    app.state.meeting_parser = None
