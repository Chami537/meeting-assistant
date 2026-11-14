"""The minutes API RAGFlow's audio parser calls: SYSTRAN segments in, the summary job's minutes out."""

import re
import subprocess
import sys
from datetime import date

import httpx
import pytest
from app.api import minutes_api
from app.main import app
from app.postprocess import minutes as mm
from app.postprocess import proofread
from app.postprocess.providers import LlmReply, ProviderError

SEGMENTS = [  # as SYSTRAN returns them: seconds, a speaker or none, out of order is allowed
    {"start": 65.5, "end": 70.0, "speaker": None, "text": "新产线预计6月底调试。"},
    {"start": 0.0, "end": 4.2, "speaker": "发言人1", "text": "欢迎参加晨川科技业绩说明会。"},
    {"start": 4.2, "end": 5.0, "speaker": "发言人2", "text": "   "},
]
REPLY = {
    "summary": "晨川科技业绩说明会。",
    "meeting_type": "公司交流",
    "meeting_date": None,
    "key_points": [{"text": "新产线进展", "details": ["6月底调试"], "evidence_segments": [1]}],
    "key_facts": [{"text": "公司召开业绩说明会", "evidence_segments": [0]}],
    "outlook": [{"text": "新产线调试", "kind": "计划", "timeframe": "6月底", "evidence_segments": [1]}],
    "decisions": [],
    "action_items": [],
    "risks": [],
    "name_fixes": [],
}


class FakeLlm:
    name, model, region = "fake", "fake-model", ""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.prompts = []
        self.proofread: list[str] = []
        self.fixes: list[dict] = []

    async def complete_json(self, *, system, user, schema):
        if schema is proofread.SCHEMA:  # the proofreading request after the minutes: nothing to fix
            self.proofread.append(user)
            return LlmReply({"fixes": self.fixes})
        self.prompts.append(user)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return LlmReply(reply)


@pytest.fixture
def llm():
    fake = FakeLlm(REPLY)
    app.dependency_overrides[minutes_api._llm] = lambda: fake
    yield fake
    app.dependency_overrides.clear()


async def post(body):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.post("/v1/minutes", json=body)


async def test_minutes_from_systran_segments(llm):
    r = await post({"title": "03-晨川科技20260318.mp3", "segments": SEGMENTS})
    assert r.status_code == 200
    body = r.json()
    assert body["prompt_version"] == mm.PROMPT_VERSION and body["model"] == "fake-model"
    assert len(body["request_id"]) == 32  # within submit_result's 64
    assert (body["meeting_date"], body["meeting_date_source"]) == ("2026-03-18", "文件名")
    assert body["minutes"]["key_points"][0]["text"] == "新产线进展"
    [prompt] = llm.prompts
    assert "<title>03-晨川科技20260318</title>" in prompt
    # in time order, numbered, milliseconds, the blank segment dropped, no speaker shown as 未知
    assert "[0] 00:00:00 发言人1: 欢迎参加" in prompt and "[1] 00:01:05 未知: 新产线" in prompt
    assert "[2]" not in prompt


async def test_markdown_for_the_knowledge_base(llm):
    md = (await post({"title": "03-晨川科技20260318.mp3", "segments": SEGMENTS})).json()["markdown"]
    assert md.startswith("# 会议纪要：03-晨川科技20260318\n\n会议时间：2026-03-18 · 会议类型：公司交流\n")
    assert "## 主要观点\n\n- 新产线进展\n  - 6月底调试\n" in md
    assert "- 【计划】新产线调试（6月底）\n" in md
    assert "## 重要数据与事实\n\n- 公司召开业绩说明会\n" in md
    assert not re.search(r"\[\d", md)  # no recording times or segment numbers: they only blur retrieval
    assert "## 决定" not in md and "## 待办" not in md  # empty sections are left out


async def test_each_request_has_its_own_id(llm, caplog):
    llm.replies.append(REPLY)
    caplog.set_level("INFO", logger=minutes_api.log.name)
    ids = [(await post({"segments": SEGMENTS})).json()["request_id"] for _ in range(2)]
    assert ids[0] != ids[1]
    assert all(any(i in r.getMessage() for r in caplog.records) for i in ids)  # to find it in the log


def test_markdown_leaves_out_name_fixes_and_shows_upload_date():
    columns = mm.as_columns(mm.Minutes(summary="摘要", name_fixes=[{"heard": "辰川", "written": "晨川"}]))
    columns["name_fixes"].append({"heard": "北晨一号", "written": "北辰一号", "checked": False})
    md = mm.to_markdown(columns, title=None, meeting_date=date(2026, 10, 16), date_source="上传日期")
    assert md == "# 会议纪要\n\n会议时间：2026-10-16（上传日期） · 会议类型：其他\n\n## 摘要\n\n摘要\n"
    assert len(columns["name_fixes"]) == 2  # still in the structured minutes


async def test_thin_minutes_are_asked_again(llm, monkeypatch):
    monkeypatch.setattr(mm, "THIN_TRANSCRIPT_CHARS", 1)
    thin = {**REPLY, "key_points": [], "key_facts": [], "outlook": []}
    llm.replies = [thin, REPLY]
    r = await post({"title": "会议.m4a", "segments": SEGMENTS})
    assert r.status_code == 200 and len(llm.prompts) == 2
    assert r.json()["minutes"]["key_points"]


@pytest.mark.parametrize(
    ("reply", "status"),
    [
        (ProviderError("HTTP 502"), 503),  # the model's service: try again later
        (ProviderError("HTTP 429 RateLimitError"), 503),
        (ProviderError("HTTP 400 BadRequestError", permanent=True), 502),  # this request: as good as failed
        ({"summary": ""}, 502),  # an unusable reply
    ],
)
async def test_model_failures_are_502_or_503_without_the_transcript(llm, reply, status):
    llm.replies = [reply]
    r = await post({"title": "会议.m4a", "segments": SEGMENTS})
    assert r.status_code == status
    assert "新产线" not in r.text


async def test_too_long_is_413(llm, monkeypatch):
    monkeypatch.setattr(mm, "MAX_TRANSCRIPT_CHARS", 10)
    assert (await post({"segments": SEGMENTS})).status_code == 413
    assert llm.prompts == []


async def test_no_text_is_422(llm):
    assert (await post({"segments": [SEGMENTS[2]]})).status_code == 422


async def test_no_model_configured_is_503(monkeypatch):
    def unconfigured():
        raise RuntimeError("LLM_MODEL / LLM_PROVIDER are not configured")

    monkeypatch.setattr(minutes_api, "get_llm", unconfigured)
    assert (await post({"segments": SEGMENTS})).status_code == 503


def test_info_lines_reach_the_server_log():
    """Under uvicorn nothing else gives the root logger a handler: the request id must not be dropped."""
    code = "import app.main, logging; logging.getLogger('app.api.minutes_api').info('minutes API abc')"
    run = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd="backend", check=True
    )
    assert "INFO app.api.minutes_api minutes API abc" in run.stderr
