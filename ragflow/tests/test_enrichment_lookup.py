"""The enrichment lookup's own logic with RAGFlow's models and storage stubbed out: run with
`python -m pytest ragflow/tests`. Its two queries were tried on the test RAGFlow's database."""

import importlib.util
import json
import sys
import types
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest


class _DB:
    @staticmethod
    def connection_context():
        return lambda func: func


def _module(name, **attributes):
    module = types.ModuleType(name)
    module.__dict__.update(attributes)
    return module


STUBS = {
    "api.db.db_models": _module("api.db.db_models", DB=_DB, Document=None, DocumentEnrichment=None,
                                DocumentEnrichmentResult=None),
    "api.db.services.document_enrichment_storage": _module(
        "api.db.services.document_enrichment_storage", load_manifest=None, read_object=None),
    "api.db.services.knowledgebase_service": _module("api.db.services.knowledgebase_service", KnowledgebaseService=None),
    "common.constants": _module("common.constants",
                                DocumentEnrichmentStatus=SimpleNamespace(SUCCEEDED=SimpleNamespace(value="succeeded"))),
}
_saved = {name: sys.modules.get(name) for name in STUBS}
sys.modules.update(STUBS)
spec = importlib.util.spec_from_file_location("enrichment_lookup_service",
                                              Path(__file__).parents[1] / "api/apps/services/enrichment_lookup_service.py")
lookup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lookup)
for _name, _module_ in _saved.items():  # other test files stub some of these differently
    if _module_ is None:
        sys.modules.pop(_name)
    else:
        sys.modules[_name] = _module_

GENERATED = datetime(2026, 10, 21, 15, 30)
MANIFESTS, BLOBS = {}, {}


def stored(doc, kind, *outputs):
    """A result kept in object storage: outputs are (role, mime_type, bytes, file_name)."""
    key = f"{doc}/{kind}/manifest.json"
    MANIFESTS[key] = {"outputs": []}
    for role, mime, binary, file_name in outputs:
        output_key = f"{doc}/{kind}/{role}"
        BLOBS[output_key] = binary
        MANIFESTS[key]["outputs"].append({"role": role, "mime_type": mime, "file_name": file_name, "store": True,
                                          "storage_bucket": "b", "storage_key": output_key})
    return SimpleNamespace(id=f"r-{doc}-{kind}", document_id=doc, storage_bucket="b", storage_key=key, file_size=1,
                           checksum="c", content_json=None, content_text=None, generated_at=GENERATED)


def inline(doc, kind, content_json=None, content_text=None):
    """A result from before object storage: the content sits in the row."""
    return SimpleNamespace(id=f"r-{doc}-{kind}", document_id=doc, storage_bucket=None, storage_key=None, file_size=0,
                           checksum=None, content_json=content_json, content_text=content_text, generated_at=None)


def as_json(value):
    return json.dumps(value, ensure_ascii=False).encode()


MINUTES = {"prompt_version": "minutes-v3", "model": "haiku", "meeting_date": "2026-09-20",
           "minutes": {"summary": "加息预期下降。", "meeting_type": "宏观经济"}, "markdown": "# 会议纪要\n\n## 摘要\n加息预期下降。\n"}


@pytest.fixture(autouse=True)
def storage(monkeypatch):
    MANIFESTS.clear()
    BLOBS.clear()
    monkeypatch.setattr(lookup, "load_manifest", lambda reference: MANIFESTS[reference["storage_key"]])
    monkeypatch.setattr(lookup, "read_object", lambda output: BLOBS[output["storage_key"]])


def database(monkeypatch, results, readable=None):
    """results: {(document_id, kind): result}; readable: the (dataset, document) pairs the tenant may read."""
    monkeypatch.setattr(lookup, "current_results", lambda ids, kinds: {
        key: value for key, value in results.items() if key[0] in set(ids) and key[1] in kinds})
    monkeypatch.setattr(lookup, "_readable", lambda tenant, pairs: {
        pair for pair in pairs if readable is None or pair in readable})


def test_parse_request_keeps_order_and_drops_repeated_types():
    pairs, kinds = lookup.parse_request({
        "documents": [{"dataset_id": " kb1 ", "document_id": "d2"}, {"dataset_id": "kb1", "document_id": "d1"}],
        "types": ["translation", "summary", "translation"],
    })
    assert pairs == [("kb1", "d2"), ("kb1", "d1")]
    assert kinds == ["translation", "summary"]


def test_parse_request_asks_for_every_type_by_default():
    assert lookup.parse_request({"documents": [{"dataset_id": "kb", "document_id": "d"}]})[1] == list(lookup.TYPES)


@pytest.mark.parametrize("body", [
    [],
    {"documents": []},
    {"documents": "d1"},
    {"documents": [{"dataset_id": "kb"}]},
    {"documents": [{"dataset_id": "kb", "document_id": "  "}]},
    {"documents": [{"dataset_id": "kb", "document_id": 7}]},
    {"documents": ["d1"]},
    {"documents": [{"dataset_id": "kb", "document_id": "d"}], "types": []},
    {"documents": [{"dataset_id": "kb", "document_id": "d"}], "types": ["asr_transcript"]},
    {"documents": [{"dataset_id": "kb", "document_id": "d"}], "types": "summary"},
    {"documents": [{"dataset_id": "kb", "document_id": f"d{i}"} for i in range(lookup.MAX_DOCUMENTS + 1)]},
])
def test_parse_request_rejects(body):
    with pytest.raises(ValueError):
        lookup.parse_request(body)


def test_lookup_reads_each_kind_and_keeps_request_order(monkeypatch):
    database(monkeypatch, {
        ("d1", "summary"): stored("d1", "summary", ("self", "application/json", as_json(
            {"summary": "  营收增长 12%。 ", "prompt_version": "summary-v1"}), "result.json")),
        ("d2", "meeting_minutes"): stored("d2", "meeting_minutes", ("self", "application/json", as_json(MINUTES),
                                                                    "result.json")),
        ("d1", "translation"): stored("d1", "translation", ("pdf", "application/pdf", b"%PDF", "报告_中文.pdf")),
    })
    entries = lookup.lookup("t", [("kb2", "d2"), ("kb1", "d1"), ("kb1", "d3")], list(lookup.TYPES))
    assert [entry["document_id"] for entry in entries] == ["d2", "d1", "d3"]
    assert entries[1]["summary"] == {"text": "营收增长 12%。", "result_id": "r-d1-summary",
                                     "generated_at": "2026-10-21T15:30:00"}
    assert entries[1]["translation"] == {"url": "/api/v1/datasets/kb1/documents/d1/translation",
                                         "result_id": "r-d1-translation", "generated_at": "2026-10-21T15:30:00"}
    assert entries[0]["meeting_minutes"] == {
        "markdown": MINUTES["markdown"].strip(), "summary": "加息预期下降。", "meeting_type": "宏观经济",
        "meeting_date": "2026-09-20", "result_id": "r-d2-meeting_minutes", "generated_at": "2026-10-21T15:30:00"}
    assert entries[0]["summary"] is None and entries[1]["meeting_minutes"] is None
    assert entries[2] == {"dataset_id": "kb1", "document_id": "d3", "summary": None, "meeting_minutes": None,
                          "translation": None}


def test_lookup_returns_only_the_types_asked_for(monkeypatch):
    database(monkeypatch, {("d1", "summary"): stored("d1", "summary", ("self", "application/json", as_json(
        {"summary": "摘要"}), "result.json"))})
    assert lookup.lookup("t", [("kb", "d1")], ["summary"]) == [
        {"dataset_id": "kb", "document_id": "d1",
         "summary": {"text": "摘要", "result_id": "r-d1-summary", "generated_at": "2026-10-21T15:30:00"}}]


def test_lookup_hides_what_the_tenant_may_not_read(monkeypatch):
    summary = stored("d1", "summary", ("self", "application/json", as_json({"summary": "摘要"}), "result.json"))
    database(monkeypatch, {("d1", "summary"): summary}, readable={("kb1", "d1")})
    entries = lookup.lookup("t", [("kb2", "d1"), ("kb1", "d1")], ["summary", "translation"])
    assert entries[0]["summary"] is None  # the right document under a dataset it is not in
    assert entries[1]["summary"]["text"] == "摘要"


def test_lookup_reads_results_kept_inline(monkeypatch):
    database(monkeypatch, {
        ("d1", "summary"): inline("d1", "summary", content_json={}, content_text="旧摘要"),
        ("d2", "meeting_minutes"): inline("d2", "meeting_minutes", content_json=MINUTES, content_text="ignored"),
    })
    entries = lookup.lookup("t", [("kb", "d1"), ("kb", "d2")], ["summary", "meeting_minutes"])
    assert entries[0]["summary"] == {"text": "旧摘要", "result_id": "r-d1-summary", "generated_at": None}
    assert entries[1]["meeting_minutes"]["meeting_type"] == "宏观经济"


def test_a_plain_text_summary_is_used_as_is(monkeypatch):
    database(monkeypatch, {("d1", "summary"): stored("d1", "summary", ("self", "text/plain; charset=utf-8",
                                                                       "纯文本摘要".encode(), "result.txt"))})
    assert lookup.lookup("t", [("kb", "d1")], ["summary"])[0]["summary"]["text"] == "纯文本摘要"


def test_one_unreadable_result_does_not_sink_the_page(monkeypatch):
    database(monkeypatch, {
        ("d1", "summary"): stored("d1", "summary", ("self", "application/json", b"{broken", "result.json")),
        ("d2", "summary"): stored("d2", "summary", ("self", "application/json", as_json({"summary": "好的"}),
                                                    "result.json")),
        ("d3", "summary"): stored("d3", "summary", ("pdf", "application/pdf", b"%PDF", "x.pdf")),  # no self output
        ("d4", "summary"): stored("d4", "summary", ("self", "application/json", as_json({"summary": "  "}),
                                                    "result.json")),
    })
    entries = lookup.lookup("t", [("kb", f"d{i}") for i in range(1, 5)], ["summary"])
    assert [entry["summary"] and entry["summary"]["text"] for entry in entries] == [None, "好的", None, None]


def test_minutes_without_markdown_count_as_none(monkeypatch):
    database(monkeypatch, {("d1", "meeting_minutes"): stored("d1", "meeting_minutes", (
        "self", "application/json", as_json({**MINUTES, "markdown": ""}), "result.json"))})
    assert lookup.lookup("t", [("kb", "d1")], ["meeting_minutes"])[0]["meeting_minutes"] is None


def test_translation_pdf_reads_the_pdf_output(monkeypatch):
    database(monkeypatch, {("d1", "translation"): stored("d1", "translation", (
        "self", "application/json", as_json({"job_id": "j"}), "result.json"), (
        "pdf", "application/pdf", b"%PDF-1.7", "报告_中文.pdf"))})
    assert lookup.translation_pdf("d1") == ("报告_中文.pdf", b"%PDF-1.7")
    assert lookup.has_translation("d1") and not lookup.has_translation("d2")
    assert lookup.translation_pdf("d2") is None


def test_an_inline_translation_row_announces_no_pdf(monkeypatch):
    database(monkeypatch, {("d1", "translation"): inline("d1", "translation", content_text="old")})
    assert lookup.lookup("t", [("kb", "d1")], ["translation"])[0]["translation"] is None
    assert not lookup.has_translation("d1") and lookup.translation_pdf("d1") is None


def test_translation_pdf_names_a_nameless_file(monkeypatch):
    database(monkeypatch, {("d1", "translation"): stored("d1", "translation", ("pdf", "application/pdf", b"%PDF", None))})
    assert lookup.translation_pdf("d1") == ("d1_中文.pdf", b"%PDF")


def test_readable_translation_pdf_checks_the_pair(monkeypatch):
    database(monkeypatch, {("d1", "translation"): stored("d1", "translation", ("pdf", "application/pdf", b"%PDF",
                                                                               "a.pdf"))},
             readable={("kb1", "d1")})
    assert lookup.readable_translation_pdf("t", "kb1", "d1") == ("a.pdf", b"%PDF")
    assert lookup.readable_translation_pdf("t", "kb2", "d1") is None
