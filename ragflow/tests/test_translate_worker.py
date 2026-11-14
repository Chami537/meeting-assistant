"""The translate worker's own logic with RAGFlow and RetainPDF stubbed out: run with `python -m pytest ragflow/tests`."""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

SUBMITTED = []


class ConnectionError_(Exception):
    pass


class Timeout_(Exception):
    pass


requests = types.ModuleType("requests")
requests.ConnectionError = ConnectionError_
requests.Timeout = Timeout_
requests.get = requests.post = None
sys.modules["requests"] = requests


def submit_result(**kwargs):
    SUBMITTED.append(kwargs)
    return {"status": "accepted"}


ingestion = types.ModuleType("api.db.services.document_enrichment_ingestion_service")
ingestion.submit_result = submit_result

spec = importlib.util.spec_from_file_location("translate_worker", Path(__file__).parents[1] / "rag/svr/translate_worker.py")
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)

ID = [f"{i:032x}" for i in range(4)]
HASH = "abcdef0123456789abcdef0123456789"
UPLOADED = 1_793_070_000_000  # 2026-10-27 03:00 UTC, in ms like Document.create_time
DOCS = [{"id": ID[i], "kb_id": "kb", "tenant_id": "t", "name": f"Report {i}.pdf", "content_hash": HASH,
         "create_time": UPLOADED} for i in range(3)]
ENGLISH = ["Revenue grew 12% on strong AI cloud demand."] * 3
CHINESE = ["收入同比增长百分之十二，云业务需求强劲。"] * 3


class Response:
    def __init__(self, status, body=None, content=b""):
        self.status_code, self.body, self.content = status, body, content
        self.text = str(body)

    def json(self):
        if self.body is None:
            raise ValueError("not JSON")
        return self.body


def ok(data):
    return Response(200, {"code": 0, "message": "ok", "data": data})


class RetainPDF:
    """Jobs as RetainPDF lists them (newest first), and every request made."""

    def __init__(self):
        self.jobs, self.calls, self.health = [], [], {"queue_depth": 0, "running_jobs": 0}
        self.pdf, self.pages, self.down, self.uploads_down = b"%PDF-1.7 translated", 8, False, False
        self.upload_status, self.upload_message = 200, "PDF repair timed out"

    def job(self, doc, status, job_id=None, workflow="book", name=None):
        job_id = job_id or f"job{len(self.jobs)}"
        self.jobs.insert(0, {"job_id": job_id, "workflow": workflow, "status": status, "created_at": "2026-10-27T03:00:00Z",
                             "updated_at": "2026-10-27T03:00:00Z", "source_file_name": name or worker.job_file_name(doc),
                             "page_count": 8})
        return job_id

    def get(self, url, params=None, timeout=None, allow_redirects=None):
        path = url.removeprefix(worker.URL)
        self.calls.append(("GET", path, params))
        if self.down:
            raise ConnectionError_("refused")
        if path == "/health":
            return ok(self.health)
        if path == "/api/v1/jobs":
            return ok({"items": self.jobs[params["offset"]:params["offset"] + params["limit"]]})
        if path.endswith("/pdf"):
            return Response(200, content=self.pdf)
        return ok({"failure": {"summary": "OCR timed out"}, "error": None})

    def post(self, url, files=None, json=None, timeout=None, allow_redirects=None):
        path = url.removeprefix(worker.URL)
        self.calls.append(("POST", path, files["file"][0] if files else json))
        if self.down or self.uploads_down:
            raise ConnectionError_("refused")
        if path == "/api/v1/uploads":
            if self.upload_status != 200:
                return Response(self.upload_status, {"code": 50300, "message": self.upload_message})
            self.uploaded = files["file"][0]
            return ok({"upload_id": "up1", "page_count": self.pages})
        job_id = f"new{len(self.jobs)}"  # listed from now on, as RetainPDF does
        self.jobs.insert(0, {"job_id": job_id, "workflow": "book", "status": "queued", "created_at": "2026-10-27T03:00:00Z",
                             "updated_at": "2026-10-27T03:00:00Z", "source_file_name": self.uploaded})
        return ok({"job_id": job_id, "status": "queued"})

    def posts(self):
        return [c for c in self.calls if c[0] == "POST"]

    # SKIP_TABLE: document id -> content hash, and how the worker got at it
    table, table_there, table_broken, prepared = None, True, False, None

    def prepare_table(self, create):
        self.prepared.append(create)
        return self.table_there

    def record(self, doc, reason):
        if self.table_broken:
            raise RuntimeError("MySQL server has gone away")
        self.table[doc["id"]] = doc["content_hash"]


@pytest.fixture
def api(monkeypatch):
    fake = RetainPDF()
    texts = {ID[0]: ENGLISH, ID[1]: ENGLISH, ID[2]: CHINESE}
    reads = []
    monkeypatch.setitem(sys.modules, "api.db.services.document_enrichment_ingestion_service", ingestion)
    monkeypatch.setattr(worker, "URL", "http://retainpdf")
    monkeypatch.setattr(worker, "SINCE", "2026-10-27T10:00:00+08:00")
    monkeypatch.setattr(requests, "get", fake.get)
    monkeypatch.setattr(requests, "post", fake.post)
    # SKIP_TABLE as the candidate query sees it: a row counts only for the same content
    monkeypatch.setattr(worker, "candidates", lambda skipped, use_table: [
        d for d in fake.docs
        if skipped.get(d["id"]) != d["content_hash"] and not (use_table and fake.table.get(d["id"]) == d["content_hash"])
    ])
    monkeypatch.setattr(worker, "prepare_skip_table", fake.prepare_table)
    monkeypatch.setattr(worker, "record_left_alone", fake.record)
    monkeypatch.setattr(worker, "read_texts", lambda doc: reads.append(doc["id"]) or texts[doc["id"]])
    monkeypatch.setattr(worker, "read_pdf", lambda doc: fake.source if not fake.replaced else fake.replace())
    monkeypatch.setattr(worker, "current_hash", lambda doc: fake.hashes.get(doc["id"], doc["content_hash"]))
    fake.docs, fake.reads, fake.texts, fake.hashes, fake.source = DOCS[:2], reads, texts, {}, b"%PDF-1.4 original"
    fake.replaced, fake.table, fake.prepared = False, {}, []

    def replace():
        raise worker.Changed("its file was replaced")

    fake.replace = replace
    SUBMITTED.clear()
    return fake


def test_only_book_jobs_named_after_a_document_are_ours():
    items = [
        {"workflow": "book", "source_file_name": f"ragflow-{ID[0]}-abcdef01-A.pdf", "job_id": "new"},
        {"workflow": "ocr", "source_file_name": f"ragflow-{ID[0]}-abcdef01-A.pdf", "job_id": "new-ocr"},
        {"workflow": "book", "source_file_name": "01-Bernstein-Global Logistics.pdf", "job_id": "someone's"},
        {"workflow": "book", "source_file_name": f"ragflow-{ID[0]}-abcdef01-A.pdf", "job_id": "old"},
    ]
    assert {k: [j["job_id"] for j in v] for k, v in worker.our_jobs(items).items()} == {(ID[0], "abcdef01"): ["new", "old"]}


def test_an_english_report_with_a_chinese_summary_chunk_is_english():
    rows = [{"content_with_weight": "这是一篇英文研报的中文摘要，" * 20, "doc_type_kwd": "summary"},
            {"content_with_weight": "这是一份摩根士丹利发布的投资者材料。"},
            *[{"content_with_weight": t} for t in ENGLISH * 5]]
    texts = worker.article_texts(rows)
    assert len(texts) == 16 and worker.cjk_share(texts) < worker.MAX_CJK_SHARE
    assert worker.cjk_share(CHINESE) > 0.9 and worker.cjk_share([]) == 1.0


def test_translated_name_is_a_plain_file_name():
    assert worker.translated_name("A/B: Outlook.PDF") == "A_B: Outlook_中文.pdf"


def test_one_english_document_is_submitted_when_retainpdf_is_idle(api):
    worker.run_round(worker.State())
    assert api.posts() == [
        ("POST", "/api/v1/uploads", f"ragflow-{ID[0]}-abcdef01-Report 0.pdf"),
        ("POST", "/api/v1/jobs", {**worker.JOB, "translation": {**worker.JOB["translation"], "api_key": worker.MODEL_API_KEY},
                                   "source": {"upload_id": "up1"}}),
    ]
    assert api.reads == [ID[0], ID[1]] and SUBMITTED == []  # the second document waits for the next round


def test_the_job_request_is_the_one_retainpdfs_integration_guide_gives(api, monkeypatch):
    # Spelled out rather than compared with worker.JOB, so a wrong value in JOB fails here. The translation
    # model's key goes in per request, from RETAINPDF_MODEL_API_KEY: it must never have a default in the code.
    monkeypatch.setattr(worker, "MODEL_API_KEY", "example-retainpdf-key")
    worker.run_round(worker.State())
    assert api.posts()[1][2] == {
        "workflow": "book",
        "source": {"upload_id": "up1"},
        "ocr": {"provider": "dots_mocr", "poll_timeout": 7200},
        "translation": {
            "model": "hymt", "base_url": "http://127.0.0.1:41011/v1", "api_key": "example-retainpdf-key",
            "mode": "fast", "math_mode": "placeholder", "batch_size": 1, "workers": 1, "rule_profile_name": "general_sci",
        },
        "render": {"render_mode": "auto", "compile_workers": 4},
        "runtime": {"timeout_seconds": 14400},
    }


def test_an_empty_model_key_never_reaches_retainpdf(api, monkeypatch):
    # With no RETAINPDF_MODEL_API_KEY the worker would not run (main() refuses), and nothing built from JOB
    # carries a key of its own: the template must stay keyless.
    monkeypatch.setattr(worker, "MODEL_API_KEY", "")
    assert "api_key" not in worker.JOB["translation"]


def test_nothing_new_while_one_of_ours_runs(api):
    api.job(DOCS[0], "running")
    state = worker.State()
    worker.run_round(state)
    worker.run_round(state)
    assert api.posts() == [] and api.reads == [ID[0], ID[1]]  # each document's language is found out once


def test_a_finished_job_is_downloaded_and_filed_once_within_the_hour(api):
    job_id = api.job(DOCS[0], "succeeded")
    api.job(DOCS[1], "running")
    state = worker.State()
    worker.run_round(state)
    assert SUBMITTED == [{
        "request_id": f"retainpdf-{job_id}", "document_id": ID[0], "artifact_type": "translation",
        "payload": {"job_id": job_id, "source_file_name": "Report 0.pdf", "page_count": 8},
        "content_outputs": [{"field": "SELF", "store": True, "index": False}],
        "attachments": [{"role": "pdf", "file_name": "Report 0_中文.pdf", "mime_type": "application/pdf",
                         "content": b"%PDF-1.7 translated", "store": True, "index": False}],
        "language": "zh-cn", "service_name": "retainpdf", "service_version": "1.0.0", "model_name": "hymt",
        "schema_version": "1",
    }]
    worker.run_round(state)  # the ingestion files it in the background: not again within the hour
    assert len(SUBMITTED) == 1 and api.posts() == []


def test_a_job_for_older_content_is_not_this_documents(api):
    api.job(DOCS[0], "succeeded", name=f"ragflow-{ID[0]}-00000000-Report 0.pdf")
    worker.run_round(worker.State())
    assert SUBMITTED == [] and api.posts()[0][2] == f"ragflow-{ID[0]}-abcdef01-Report 0.pdf"


def test_a_chinese_document_is_read_once_and_left_alone(api):
    api.docs = [DOCS[2], DOCS[0]]
    state = worker.State()
    worker.run_round(state)
    assert api.table == {ID[2]: HASH} and state.skipped == {}
    assert api.posts()[0][2].startswith(f"ragflow-{ID[0]}")
    worker.run_round(state)
    assert api.reads == [ID[2], ID[0]]


def test_two_failed_jobs_and_it_is_left_alone(api):
    api.job(DOCS[0], "failed", "f1")
    state = worker.State()
    worker.run_round(state)
    assert api.posts()[0][2].startswith(f"ragflow-{ID[0]}")  # one failure: submitted again
    api.calls.clear()
    api.jobs[0]["status"] = "canceled"  # the job just created
    worker.run_round(state)
    assert ID[0] in api.table and api.posts()[0][2].startswith(f"ragflow-{ID[1]}")
    reasons = [c[1] for c in api.calls if c[0] == "GET" and c[1].startswith("/api/v1/jobs/")]
    assert reasons == ["/api/v1/jobs/new1"]  # f1's was read in the first round


def test_a_busy_retainpdf_is_waited_for_two_hours(api, monkeypatch):
    api.health = {"queue_depth": 1, "running_jobs": 1}
    state = worker.State()
    clock = [1000.0]
    monkeypatch.setattr(worker.time, "time", lambda: clock[0])
    worker.run_round(state)
    clock[0] += worker.YIELD_SECONDS - 1
    worker.run_round(state)
    assert api.posts() == [] and state.waiting_since == 1000.0
    clock[0] += 1
    worker.run_round(state)
    assert len(api.posts()) == 2 and state.waiting_since is None


def test_an_unreachable_retainpdf_is_unavailable(api):
    api.down = True
    with pytest.raises(worker.Unavailable):
        worker.run_round(worker.State())


def test_a_download_that_is_not_a_pdf_fails_three_times_then_is_left_alone(api):
    api.job(DOCS[0], "succeeded")
    api.job(DOCS[1], "running")
    api.pdf = b"<html>gateway error</html>"
    state = worker.State()
    for _ in range(3):
        worker.run_round(state)
    assert SUBMITTED == [] and state.failures == {(ID[0], HASH): 3} and state.skipped == {ID[0]: HASH}
    assert api.table == {}  # failed here: tried again after a restart


def test_too_many_pages_creates_no_job(api):
    api.pages = 301
    state = worker.State()
    worker.run_round(state)
    assert [c[1] for c in api.posts()] == ["/api/v1/uploads"] and api.table == {ID[0]: HASH}


def test_the_job_list_is_read_back_to_the_first_job_updated_before_since():
    pages = []

    def get(url, params, timeout, allow_redirects):
        pages.append(params["offset"])
        # RetainPDF lists by updated_at: an old job rerun today sorts among today's, its created_at long before
        updated = "2026-10-27T03:00:00Z" if params["offset"] < 200 else "2026-10-20T00:00:00Z"
        return ok({"items": [{"created_at": "2026-10-01T00:00:00Z", "updated_at": updated}] * params["limit"]})

    requests.get = get
    try:
        items = worker.list_jobs(worker.since_ms("2026-10-27T10:00:00+08:00"))
        with pytest.raises(worker.Unavailable):  # cut short, it would hide jobs: nothing is submitted then
            worker.list_jobs(worker.since_ms("2026-10-01T00:00:00+08:00") - 10**12)
    finally:
        requests.get = None
    assert pages[:3] == [0, 100, 200] and len(items) == 300


def test_dry_run_reads_retainpdf_only(api):
    api.job(DOCS[0], "succeeded")
    worker.run_round(worker.State(), dry_run=True)
    assert api.posts() == [] and SUBMITTED == []


def test_a_failed_attempt_after_two_hours_keeps_the_wait(api, monkeypatch):
    api.health = {"queue_depth": 1, "running_jobs": 1}
    state = worker.State()
    clock = [1000.0]
    monkeypatch.setattr(worker.time, "time", lambda: clock[0])
    worker.run_round(state)
    clock[0] += worker.YIELD_SECONDS
    api.uploads_down = True
    with pytest.raises(worker.Unavailable):
        worker.run_round(state)
    assert [c[1] for c in api.posts()] == ["/api/v1/uploads"] and state.waiting_since == 1000.0
    api.uploads_down = False
    clock[0] += 60
    worker.run_round(state)
    assert len(api.posts()) == 3  # the failed upload, then upload and job right away


def test_a_source_too_big_to_file_is_not_sent(api):
    api.source = b"%PDF-" + b"0" * worker.MAX_BYTES
    state = worker.State()
    worker.run_round(state)
    assert api.posts() == [] and api.table == {ID[0]: HASH}


def test_no_chunks_counts_as_a_failure_not_as_chinese(api):
    api.texts[ID[0]] = []
    state = worker.State()
    worker.run_round(state)
    assert state.failures == {(ID[0], HASH): 1} and ID[0] not in state.skipped and api.table == {}
    assert api.posts()[0][2].startswith(f"ragflow-{ID[1]}")


def test_new_content_is_looked_at_again(api):
    api.docs = [DOCS[2]]
    state = worker.State()
    worker.run_round(state)
    assert api.table == {ID[2]: HASH} and api.posts() == []
    api.docs = [{**DOCS[2], "content_hash": "f" * 32}]
    api.texts[ID[2]] = ENGLISH
    worker.run_round(state)
    assert api.posts()[0][2] == f"ragflow-{ID[2]}-ffffffff-Report 2.pdf"


def test_a_job_is_not_filed_when_the_file_changed_during_the_round(api):
    api.job(DOCS[0], "succeeded")
    api.job(DOCS[1], "running")
    api.hashes[ID[0]] = "f" * 32
    state = worker.State()
    worker.run_round(state)
    assert SUBMITTED == [] and state.failures == {} and state.skipped == {} and api.table == {}


def test_only_one_process_holds_the_lock(monkeypatch, tmp_path):
    monkeypatch.setattr(worker, "LOCK", str(tmp_path / "lock"))
    first = worker.hold_lock(wait=False)
    try:
        assert first is not None and worker.hold_lock(wait=False) is None
    finally:
        first.close()
    second = worker.hold_lock(wait=False)
    assert second is not None
    second.close()


def test_the_job_list_is_read_back_to_the_oldest_candidate(api):
    assert worker.since_ms("2026-10-27T03:00:00+00:00") == UPLOADED  # the fake jobs are as new as the documents
    api.job(DOCS[0], "running")  # ours, on the second page under others' newer ones
    others = [{"job_id": f"o{i}", "workflow": "book", "status": "succeeded", "source_file_name": "x.pdf",
               "created_at": "2026-10-27T04:00:00Z", "updated_at": "2026-10-27T04:00:00Z"} for i in range(150)]
    older = [{**others[0], "updated_at": "2026-10-20T00:00:00Z"}] * 300
    api.jobs = others + api.jobs + older
    worker.run_round(worker.State())
    assert [c[2]["offset"] for c in api.calls if c[:2] == ("GET", "/api/v1/jobs")] == [0, 100]
    assert api.posts() == []  # ours was seen running


def test_a_repair_timeout_counts_against_the_document(api):
    api.upload_status = 503
    state = worker.State()
    for i in range(worker.MAX_LOCAL_FAILURES):
        worker.run_round(state)
    assert state.skipped == {ID[0]: HASH} and api.table == {}
    api.upload_status = 200
    worker.run_round(state)
    assert api.posts()[-2][2].startswith(f"ragflow-{ID[1]}")  # the next document is not held up


@pytest.mark.parametrize("status, message", [(503, "PDF processing capacity is busy; please retry"),
                                             (503, "insufficient storage"), (429, "queue_full")])
def test_the_services_own_trouble_counts_against_no_document(api, status, message):
    api.upload_status, api.upload_message = status, message
    state = worker.State()
    for i in range(worker.MAX_LOCAL_FAILURES + 1):
        with pytest.raises(worker.Unavailable):
            worker.run_round(state)
    assert state.failures == {} and state.skipped == {} and api.table == {}


def test_a_file_replaced_before_upload_is_not_sent_under_the_old_hash(api):
    api.replaced = True
    state = worker.State()
    worker.run_round(state)
    assert api.posts() == [] and state.failures == {} and state.skipped == {} and api.table == {}


def test_the_wait_is_kept_when_the_waiting_document_cannot_be_read_or_is_refused(api, monkeypatch):
    api.health = {"queue_depth": 1, "running_jobs": 1}
    state = worker.State()
    clock = [1000.0]
    monkeypatch.setattr(worker.time, "time", lambda: clock[0])
    worker.run_round(state)
    clock[0] += 60
    api.texts[ID[0]] = []  # no chunks this round: unknown, still pending
    api.texts[ID[1]] = []
    worker.run_round(state)
    assert state.waiting_since == 1000.0
    api.texts[ID[0]] = api.texts[ID[1]] = ENGLISH
    clock[0] += worker.YIELD_SECONDS
    api.pages = 301  # refused after the wait: the next document does not start a new one
    worker.run_round(state)
    api.pages = 8
    clock[0] += 60
    worker.run_round(state)
    assert [c[2] for c in api.posts() if c[1] == "/api/v1/uploads"][-1].startswith(f"ragflow-{ID[1]}")


def test_failures_are_per_content_and_cleared_by_success(api):
    state = worker.State()
    state.failures[(ID[0], "old")] = 3  # the previous content's
    state.failures[(ID[0], HASH)] = 2
    worker.run_round(state)
    assert api.posts() and (ID[0], HASH) not in state.failures


def test_a_long_name_is_cut_to_one_path_segment():
    doc = {**DOCS[0], "name": "很长的研报标题" * 30 + ".PDF"}
    name = worker.job_file_name(doc)
    assert len(name.encode("utf-8")) <= worker.MAX_NAME_BYTES and name.endswith(".pdf")
    assert len((name[:-4] + "-translated.pdf").encode("utf-8")) <= 255  # RetainPDF's name for the result
    assert worker.our_jobs([{"workflow": "book", "source_file_name": name}]) == {(ID[0], "abcdef01"): [
        {"workflow": "book", "source_file_name": name}]}


def test_the_job_list_is_read_back_to_the_oldest_english_candidate(api):
    old_chinese = {**DOCS[2], "create_time": UPLOADED - 10 * 86400 * 1000}
    api.docs = [old_chinese, DOCS[0]]
    api.jobs = [{"job_id": f"o{i}", "workflow": "book", "status": "succeeded", "source_file_name": "x.pdf",
                 "created_at": "2026-10-25T00:00:00Z", "updated_at": "2026-10-25T00:00:00Z"} for i in range(150)]
    worker.run_round(worker.State())
    assert [c[2]["offset"] for c in api.calls if c[:2] == ("GET", "/api/v1/jobs")] == [0]


@pytest.mark.parametrize("since, kbs", [("2026-02-30T10:00:00+08:00", ["kb"]), ("", ["kb"]), ("2026-10-27", [])])
@pytest.mark.parametrize("flag", ["--once", "--dry-run"])
def test_a_one_off_run_with_an_unusable_configuration_exits(monkeypatch, since, kbs, flag):
    common = types.ModuleType("common")
    common.settings = types.SimpleNamespace(init_settings=lambda: None)
    monkeypatch.setitem(sys.modules, "common", common)
    monkeypatch.setattr(sys, "argv", ["translate_worker.py", flag])
    monkeypatch.setattr(worker, "SINCE", since)
    monkeypatch.setattr(worker, "KB_IDS", kbs)
    monkeypatch.setattr(worker, "URL", "http://retainpdf")
    monkeypatch.setattr(worker.signal, "signal", lambda *a: None)
    assert worker.main() == 1


def test_lasting_reasons_are_not_looked_at_again_after_a_restart(api):
    api.docs = [DOCS[2], DOCS[0]]
    api.pages = 301
    worker.run_round(worker.State())
    assert api.table == {ID[2]: HASH, ID[0]: HASH}  # not English; too many pages
    api.calls.clear()
    worker.run_round(worker.State())  # a deploy restarts the worker
    assert api.reads == [ID[2], ID[0]] and api.posts() == []


def test_a_translation_too_big_to_file_is_recorded(api):
    api.job(DOCS[0], "succeeded")
    api.job(DOCS[1], "running")
    api.pdf = b"%PDF-" + b"0" * worker.MAX_BYTES
    worker.run_round(worker.State())
    assert SUBMITTED == [] and api.table == {ID[0]: HASH}


def test_failures_here_are_tried_again_after_a_restart(api):
    api.job(DOCS[0], "succeeded")
    api.job(DOCS[1], "running")
    api.pdf = b"<html>gateway error</html>"
    state = worker.State()
    for _ in range(3):
        worker.run_round(state)
    assert state.skipped == {ID[0]: HASH} and api.table == {}
    api.pdf = b"%PDF-1.7 translated"
    worker.run_round(worker.State())
    assert [s["document_id"] for s in SUBMITTED] == [ID[0]]


def test_a_dry_run_records_nothing_and_creates_no_table(api):
    api.docs = [DOCS[2]]
    state = worker.State()
    worker.run_round(state, dry_run=True)
    assert api.prepared == [False] and api.table == {} and state.skipped == {ID[2]: HASH}


def test_the_table_is_created_once(api):
    state = worker.State()
    worker.run_round(state)
    worker.run_round(state)
    assert api.prepared == [True]


def test_without_the_table_documents_are_left_alone_until_a_restart(api):
    api.table_there = False
    api.docs = [DOCS[2]]
    state = worker.State()
    worker.run_round(state, dry_run=True)
    worker.run_round(state, dry_run=True)
    assert api.prepared == [False, False]  # looked for again each round
    assert state.skipped == {ID[2]: HASH} and api.reads == [ID[2]]


def test_a_row_that_cannot_be_written_is_kept_until_a_restart(api, caplog):
    api.table_broken = True
    api.docs = [DOCS[2]]
    state = worker.State()
    worker.run_round(state)
    worker.run_round(state)
    assert state.skipped == {ID[2]: HASH} and api.reads == [ID[2]]
    assert "not recorded in translate_worker_skip" in caplog.text


def test_the_language_is_read_from_one_request_of_chunks(monkeypatch):
    asked = []

    def chunk_list(doc_id, tenant_id, kb_ids, **kwargs):
        asked.append(kwargs)
        return [{"content_with_weight": t} for t in ENGLISH]

    common = types.ModuleType("common")
    common.settings = types.SimpleNamespace(retriever=types.SimpleNamespace(chunk_list=chunk_list))
    monkeypatch.setitem(sys.modules, "common", common)
    assert worker.read_texts(DOCS[0]) == ENGLISH
    assert asked[0]["max_count"] == worker.READ_CHUNKS == 128 and asked[0]["sort_by_position"] is True


@pytest.fixture
def ragflow_db(monkeypatch, tmp_path):
    """RAGFlow's three tables as the candidate query uses them, in SQLite, with the real query run on them. A file,
    not :memory: — connection_context() closes the connection, which would drop an in-memory database."""
    peewee = pytest.importorskip("peewee")
    db = peewee.SqliteDatabase(str(tmp_path / "ragflow.db"))

    class Base(peewee.Model):
        class Meta:
            database = db

    class Knowledgebase(Base):
        id = peewee.CharField(primary_key=True)
        tenant_id = peewee.CharField()
        status = peewee.CharField(default="1")

    class Document(Base):
        id = peewee.CharField(primary_key=True)
        kb_id = peewee.CharField()
        name = peewee.CharField()
        content_hash = peewee.CharField(null=True, default="")
        create_time = peewee.BigIntegerField()
        suffix = peewee.CharField(default="pdf")
        run = peewee.CharField(default="3")
        progress = peewee.FloatField(default=1)
        status = peewee.CharField(default="1")
        chunk_num = peewee.IntegerField(default=5)

    class DocumentEnrichment(Base):
        id = peewee.CharField(primary_key=True)
        document_id = peewee.CharField()
        artifact_type = peewee.CharField()
        language = peewee.CharField()
        status = peewee.CharField()

    db.create_tables([Knowledgebase, Document, DocumentEnrichment])
    models = types.ModuleType("api.db.db_models")
    models.DB, models.Document, models.DocumentEnrichment, models.Knowledgebase = (
        db, Document, DocumentEnrichment, Knowledgebase)
    constants = types.ModuleType("common.constants")
    constants.StatusEnum = types.SimpleNamespace(VALID=types.SimpleNamespace(value="1"))
    constants.TaskStatus = types.SimpleNamespace(DONE=types.SimpleNamespace(value="3"))
    monkeypatch.setitem(sys.modules, "api.db.db_models", models)
    monkeypatch.setitem(sys.modules, "common.constants", constants)
    monkeypatch.setattr(worker, "_skip_model", None)  # bound to this test's database
    monkeypatch.setattr(worker, "KB_IDS", ["kb"])
    monkeypatch.setattr(worker, "ALL_KBS", False)
    monkeypatch.setattr(worker, "SINCE", "2026-10-27T10:00:00+08:00")
    Knowledgebase.create(id="kb", tenant_id="t")
    return Document, DocumentEnrichment


def test_the_candidate_query_leaves_out_recorded_documents_of_the_same_content(ragflow_db):
    Document, DocumentEnrichment = ragflow_db
    since = worker.since_ms(worker.SINCE)
    for i, (doc_hash, offset) in enumerate([("h0", 1), ("h1", 2), ("new", 3), ("h3", 4), ("h4", 5), (None, 6),
                                            ("h6", -1)]):
        Document.create(id=f"d{i}", kb_id="kb", name=f"d{i}.pdf", content_hash=doc_hash, create_time=since + offset)
    DocumentEnrichment.create(id="e3", document_id="d3", artifact_type="translation", language="zh-cn",
                              status="succeeded")
    DocumentEnrichment.create(id="e4", document_id="d4", artifact_type="translation", language="zh-cn",
                              status="failed")
    assert worker.prepare_skip_table(create=False) is False  # a dry run does not create it
    assert worker.prepare_skip_table(create=True) is True
    assert worker.prepare_skip_table(create=False) is True
    worker.record_left_alone({"id": "d1", "content_hash": "h1"}, "not English (CJK 95%)")
    worker.record_left_alone({"id": "d2", "content_hash": "old"}, "301 pages")  # the file since replaced
    worker.record_left_alone({"id": "d5", "content_hash": None}, "not English")
    ids = lambda rows: [r["id"] for r in rows]
    assert ids(worker.candidates({}, use_table=True)) == ["d0", "d2", "d4"]
    assert ids(worker.candidates({}, use_table=False)) == ["d0", "d1", "d2", "d4", "d5"]
    worker.record_left_alone({"id": "d2", "content_hash": "new"}, "not English")  # replaces the old row
    assert ids(worker.candidates({}, use_table=True)) == ["d0", "d4"]
    assert worker.candidates({}, use_table=True)[0] == {
        "id": "d0", "kb_id": "kb", "name": "d0.pdf", "content_hash": "h0", "create_time": since + 1, "tenant_id": "t"}


def test_a_table_that_does_not_join_is_not_used(ragflow_db, monkeypatch, caplog):
    Document, _ = ragflow_db

    class Unjoinable:
        id = Document.id

        @staticmethod
        def select(*fields):
            raise RuntimeError("(1267, 'Illegal mix of collations')")

    monkeypatch.setattr(sys.modules["api.db.db_models"], "Document", Unjoinable)
    assert worker.prepare_skip_table(create=True) is False
    assert "does not join with the documents" in caplog.text
