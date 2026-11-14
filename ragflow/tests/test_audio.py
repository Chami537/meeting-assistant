"""The patched rag/app/audio.py with RAGFlow stubbed out: run with `python -m pytest ragflow/tests`."""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

CALLS = []
DOCS = {"kb1": ["03-晨川科技20260318.mp3"]}
MINUTES = {
    "request_id": "a" * 32, "prompt_version": "minutes-v3", "model": "claude-haiku-5-5",
    "meeting_date": "2026-03-18", "meeting_date_source": "文件名",
    "minutes": {"summary": "摘要", "meeting_type": "公司交流"}, "markdown": "# 会议纪要：03-晨川科技20260318\n",
}
ASR = {  # as the recognizer returns it: out of order, a blank segment, extra fields, no language
    "text": "你好 再见", "language": None, "duration": 9.0, "segments": [
        {"start": 5.0, "end": 9.0, "text": "再见", "speaker": None, "compression_ratio": 1.1},
        {"start": 2.0, "end": 5.0, "text": "  ", "speaker": "发言人2"},
        {"start": 0.0, "end": 2.0, "text": "你好", "speaker": "发言人1"},
    ],
}


STUBS = {}  # put back before every test: the worker's tests stub the same modules, and audio.py imports some
# of them only when it runs


def stub(name, **attrs):
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules[name] = STUBS[name] = module


class Bundle:
    def __init__(self, *a, **k):
        pass

    def transcription_with_segments(self, path, progress_callback=None, prompt=None):
        CALLS.append(("asr", prompt))
        return ASR


class DocumentService:
    @staticmethod
    def query(kb_id, name):
        return [types.SimpleNamespace(id=f"doc-{i}") for i, n in enumerate(DOCS.get(kb_id, [])) if n == name]


def submit_result(**kwargs):
    CALLS.append(("submit", kwargs))
    if SUBMIT_FAILS == kwargs["artifact_type"]:
        raise ValueError("Document does not exist")
    return {"status": "accepted"}


SUBMIT_FAILS = False
stub("requests", post=None)
stub("common.constants", LLMType=types.SimpleNamespace(SPEECH2TEXT="speech2text"))
stub("api.db.services.llm_service", LLMBundle=Bundle)
stub("api.db.joint_services.tenant_model_service",
     get_tenant_default_model_by_type=lambda t, k: {"llm_name": "qwen3-asr-0.6b"})
stub("api.db.services.document_service", DocumentService=DocumentService)
stub("api.db.services.document_enrichment_ingestion_service", submit_result=submit_result)
spec = importlib.util.spec_from_file_location("audio", Path(__file__).parents[1] / "rag/app/audio.py")
audio = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audio)


class Response:
    def __init__(self, status, body):
        self.status_code, self.body, self.text = status, body, str(body)

    def json(self):
        return self.body


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    global SUBMIT_FAILS
    for name, module in STUBS.items():
        monkeypatch.setitem(sys.modules, name, module)
    CALLS.clear()
    SUBMIT_FAILS = None
    monkeypatch.setattr(audio.requests, "post", lambda url, json, timeout: (CALLS.append(("minutes", url)), Response(200, MINUTES))[1])


def run(name="03-晨川科技20260318.mp3"):
    messages = []
    docs = audio.chunk(name, b"RIFF", "t1", "Chinese", callback=lambda *a, **k: messages.append((a, k)),
                       kb_id="kb1", parser_config={})
    return docs, messages


def test_minutes_are_filed_with_the_recording_and_chunked_there():
    docs, messages = run()
    assert [c[0] for c in CALLS] == ["asr", "submit", "minutes", "submit"]
    assert docs == []  # the ingestion service indexes the minutes' Markdown as this document's chunks
    asr = CALLS[1][1]
    assert asr.pop("request_id").endswith(":asr") and asr == {
        "document_id": "doc-0", "artifact_type": "asr_transcript", "language": "",
        "payload": {"text": "你好 再见", "language": None, "duration": 9.0, "segments": [
            {"start": 0.0, "end": 2.0, "text": "你好", "speaker": "发言人1"},
            {"start": 5.0, "end": 9.0, "text": "再见", "speaker": None, "compression_ratio": 1.1},
        ]},
        "service_name": "asr-service", "service_version": "1.0.0", "model_name": "qwen3-asr-0.6b",
        "schema_version": "1", "content_outputs": [{"field": "SELF", "store": True, "index": False}],
    }
    assert CALLS[3][1] == {
        "request_id": "a" * 32, "document_id": "doc-0", "artifact_type": "meeting_minutes", "payload": MINUTES,
        "language": "zh-cn", "service_name": "meeting-minutes-service", "service_version": "minutes-v3",
        "model_name": "claude-haiku-5-5", "schema_version": "1",
        "content_outputs": [{"field": "SELF", "store": True, "index": True, "index_fields": ["markdown"]}],
    }
    assert any("已提交入库" in str(m) for m in messages)


def test_minutes_that_fail_fail_the_parse_and_are_not_filed(monkeypatch):
    monkeypatch.setattr(audio.requests, "post", lambda *a, **k: Response(502, "minutes failed"))
    with pytest.raises(RuntimeError, match="会议纪要生成失败"):
        run()
    assert [c[0] for c in CALLS] == ["asr", "submit"]


def test_filing_that_fails_fails_the_parse():
    global SUBMIT_FAILS
    SUBMIT_FAILS = "meeting_minutes"
    messages = []
    with pytest.raises(RuntimeError, match="会议纪要入库失败：Document does not exist"):
        audio.chunk("03-晨川科技20260318.mp3", b"RIFF", "t1", "Chinese",
                    callback=lambda *a, **k: messages.append((a, k)), kb_id="kb1", parser_config={})
    assert any(k.get("prog") == -1 for _, k in messages)


@pytest.mark.parametrize("names", [[], ["x.mp3", "x.mp3"]])
def test_no_single_document_of_that_name(names):
    DOCS["kb2"] = names
    with pytest.raises(RuntimeError, match="documents named x.mp3"):
        audio.chunk("x.mp3", b"RIFF", "t1", "Chinese", kb_id="kb2", parser_config={})
    assert [c[0] for c in CALLS] == ["asr"]


def test_a_transcript_that_cannot_be_filed_fails_the_parse_before_the_minutes():
    global SUBMIT_FAILS
    SUBMIT_FAILS = "asr_transcript"
    with pytest.raises(RuntimeError, match="识别原文入库失败"):
        run()
    assert [c[0] for c in CALLS] == ["asr", "submit"]


def test_text_only_result_is_filed_as_one_segment(monkeypatch):
    monkeypatch.setitem(ASR, "segments", [])
    run()
    assert CALLS[1][1]["payload"]["segments"] == [{"start": 0.0, "end": 9.0, "text": "你好 再见"}]
