"""The summary API RAGFlow's summary worker calls: an article's chunks in, a short summary out."""

import asyncio

import httpx
import pytest
from app.api import minutes_api, summary_api
from app.main import app
from app.postprocess import article_summary as asum
from app.postprocess.providers import REPLY_TRUNCATED, LlmReply, ProviderError

CHUNKS = [
    {"text": "新乳业(002946)2025年收入110.6亿元，同比增长3.2%。维持买入评级。", "page": 1},
    {"text": "   ", "page": 2},
]
SUMMARY = "新乳业2025年收入110.6亿元，同比增长3.2%，维持买入评级。"


class FakeLlm:
    name, model, region = "fake", "fake-model", ""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.prompts = []

    async def complete_json(self, *, system, user, schema):
        self.prompts.append(user)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return LlmReply(reply)


@pytest.fixture
def llm():
    fake = FakeLlm({"sentences": [{"text": SUMMARY, "evidence_chunks": [0]}]})
    app.dependency_overrides[minutes_api._llm] = lambda: fake
    yield fake
    app.dependency_overrides.clear()


async def post(body):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.post("/v1/summary", json=body)


async def test_summary_of_the_chunks(llm, caplog):
    caplog.set_level("INFO", logger=summary_api.log.name)
    r = await post({"title": "示例证券_2026-10-13_示例乳业.pdf", "chunks": CHUNKS})
    assert r.status_code == 200
    body = r.json()
    assert body["summary"] == SUMMARY and body["prompt_version"] == asum.PROMPT_VERSION
    assert (body["model"], body["mode"], body["groups"], body["truncated"]) == (
        "fake-model",
        "whole",
        1,
        False,
    )
    assert len(body["request_id"]) == 32
    [prompt] = llm.prompts
    assert prompt.startswith("<title>示例证券_2026-10-13_示例乳业</title>")  # the extension is dropped
    [line] = [r.getMessage() for r in caplog.records if body["request_id"] in r.getMessage()]
    assert "110.6" not in line  # counts only, nothing of the article


async def test_no_text_is_422(llm):
    assert (await post({"chunks": [{"text": " "}]})).status_code == 422


async def test_made_up_figures_are_502(llm):
    llm.replies = [{"sentences": [{"text": "预计2027年收入200亿元。", "evidence_chunks": [0]}]}] * 2
    r = await post({"chunks": CHUNKS})
    assert r.status_code == 502 and r.json()["detail"] == "summary rejected: no sentence that holds"


async def test_model_failures_are_502_without_the_article(llm):
    llm.replies = [RuntimeError("新乳业 110.6 亿元")]
    r = await post({"chunks": CHUNKS})
    assert r.status_code == 502 and r.json()["detail"] == "summary failed: RuntimeError"
    llm.replies = [ProviderError("HTTP 400 BadRequestError", permanent=True)]
    r = await post({"chunks": CHUNKS})
    assert r.status_code == 502 and r.json()["detail"] == "summary failed: HTTP 400 BadRequestError"
    llm.replies = [ProviderError(REPLY_TRUNCATED)]
    assert (await post({"chunks": CHUNKS})).status_code == 502  # it would be cut again


@pytest.mark.parametrize(
    "error", ["HTTP 429 RateLimitError", "HTTP 529", "HTTP 502", "transport: ConnectError"]
)
async def test_a_failing_model_service_is_503_so_the_worker_tries_later(llm, error):
    llm.replies = [ProviderError(error)]
    r = await post({"chunks": CHUNKS})
    assert r.status_code == 503 and r.json()["detail"] == f"summary model unavailable: {error}"


async def test_no_model_configured_is_503(monkeypatch):
    def unconfigured():
        raise RuntimeError("LLM_MODEL is not set")

    monkeypatch.setattr(minutes_api, "get_llm", unconfigured)
    assert (await post({"chunks": CHUNKS})).status_code == 503


async def test_each_request_waits_for_one_of_the_places_the_minutes_share(llm, monkeypatch):
    gate = asyncio.Semaphore(1)
    monkeypatch.setattr(summary_api, "_RUNNING", gate)
    seen = []
    ask = llm.complete_json

    async def watched(**kw):
        seen.append(gate.locked())
        return await ask(**kw)

    llm.complete_json = watched
    assert (await post({"chunks": CHUNKS})).status_code == 200
    assert seen == [True]
