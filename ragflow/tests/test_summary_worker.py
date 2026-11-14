"""The summary worker's own logic with RAGFlow stubbed out: run with `python -m pytest ragflow/tests`."""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

CALLS = []


class ConnectionError_(Exception):
    pass


requests = types.ModuleType("requests")
requests.ConnectionError = ConnectionError_
requests.post = None
sys.modules["requests"] = requests


def submit_result(**kwargs):
    CALLS.append(("submit", kwargs))
    if kwargs["document_id"] in SUBMIT_FAILS:
        raise ValueError("Source document changed while uploading enrichment outputs")
    return {"status": "accepted"}


ingestion = types.ModuleType("api.db.services.document_enrichment_ingestion_service")
ingestion.submit_result = submit_result
sys.modules["api.db.services.document_enrichment_ingestion_service"] = ingestion
SUBMIT_FAILS = set()

spec = importlib.util.spec_from_file_location("summary_worker", Path(__file__).parents[1] / "rag/svr/summary_worker.py")
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


class Response:
    def __init__(self, status, body):
        self.status_code, self.body, self.text = status, body, str(body)

    def json(self):
        return self.body


DOCS = [{"id": f"d{i}", "kb_id": "kb", "tenant_id": "t", "name": f"报告{i}.pdf", "token_num": 1000} for i in range(4)]
REPLY = {"request_id": "r" * 32, "prompt_version": "summary-v1", "model": "claude-haiku-5-5", "summary": "摘要。",
         "mode": "whole", "groups": 1, "truncated": False, "input_chars": 900, "used_chars": 900}


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    # put back before every test: the audio tests stub the same module, which the worker imports as it runs
    monkeypatch.setitem(sys.modules, "api.db.services.document_enrichment_ingestion_service", ingestion)
    CALLS.clear()
    SUBMIT_FAILS.clear()
    monkeypatch.setattr(worker, "candidates", lambda limit: DOCS[:limit])
    monkeypatch.setattr(worker, "read_chunks", lambda doc: [{"text": f"{doc['name']}正文", "page": 1}])
    monkeypatch.setattr(worker, "BATCH", 10)

    def post(url, json, timeout):
        CALLS.append(("post", json["title"]))
        return REPLY_FOR.get(json["title"], Response(200, REPLY))

    monkeypatch.setattr(worker, "summarize", lambda doc, chunks: worker.__dict__["_summarize"](doc, chunks, post))


worker._summarize = worker.summarize
REPLY_FOR = {}


def test_pick_skips_recent_submissions_and_repeated_failures():
    rows = [{"id": i} for i in "abcde"]
    picked = worker.pick(rows, submitted={"a": 1000.0, "b": 0.0}, failures={"c": 3, "d": 2}, now=3700.0, batch=2)
    assert [r["id"] for r in picked] == ["b", "d"]  # a was submitted within the hour, c failed three times


def test_only_the_articles_own_chunks_in_page_order():
    rows = [
        {"content_with_weight": "正文一", "page_num_int": [1, 1]},
        {"content_with_weight": "旧摘要", "doc_type_kwd": "summary"},
        {"content_with_weight": "新版摘要块", "enrichment_slot_kwd": "slot"},
        {"content_with_weight": "正文二", "page_num_int": [2]},
    ]
    assert worker.article_chunks(rows) == [{"text": "正文一", "page": 1}, {"text": "正文二", "page": 2}]


def test_a_round_summarizes_and_submits_each_document():
    submitted, failures = {}, {}
    worker.run_round(submitted, failures)
    assert [c[0] for c in CALLS] == ["post", "submit"] * 4
    assert CALLS[1][1] == {
        "request_id": "r" * 32, "document_id": "d0", "artifact_type": "summary",
        "payload": {"summary": "摘要。", "prompt_version": "summary-v1", "mode": "whole", "groups": 1,
                    "truncated": False, "input_chars": 900, "used_chars": 900},  # the reply less its id and model
        "content_outputs": [{"field": "SELF", "store": True, "index": True, "index_fields": ["summary"]}],
        "language": "zh-cn", "service_name": "article-summary-service", "service_version": "1.0.0",
        "model_name": "claude-haiku-5-5", "schema_version": "1",
    }
    assert set(submitted) == {"d0", "d1", "d2", "d3"} and failures == {}
    CALLS.clear()
    worker.run_round(submitted, failures)  # within the hour: nothing again
    assert CALLS == []


def test_failures_are_counted_and_the_round_goes_on():
    REPLY_FOR["报告1.pdf"] = Response(502, "summary rejected: 2 figures not in the article")
    SUBMIT_FAILS.add("d2")
    submitted, failures = {}, {}
    try:
        worker.run_round(submitted, failures)
    finally:
        REPLY_FOR.clear()
    assert failures == {"d1": 1, "d2": 1} and set(submitted) == {"d0", "d3"}


def test_an_unavailable_api_ends_the_round_without_counting_failures():
    REPLY_FOR["报告1.pdf"] = Response(503, "minutes model not configured")
    submitted, failures = {}, {}
    try:
        worker.run_round(submitted, failures)
    finally:
        REPLY_FOR.clear()
    assert [c[0] for c in CALLS] == ["post", "submit", "post"] and failures == {} and set(submitted) == {"d0"}


def test_an_unreachable_api_is_unavailable():
    def refused(*a, **k):
        raise ConnectionError_("refused")

    with pytest.raises(worker.Unavailable):
        worker._summarize(DOCS[0], [], refused)


def test_dry_run_calls_nothing():
    worker.run_round({}, {}, dry_run=True)
    assert CALLS == []
