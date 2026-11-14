#
#  Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
"""Chinese translations of English PDFs by RetainPDF, filed with the enrichment ingestion (submit_result,
artifact "translation", the translated PDF as an attachment that is stored, not indexed).

Every TRANSLATE_INTERVAL seconds it looks in the knowledge bases named by TRANSLATE_KB_IDS (or every one, with
TRANSLATE_KB_IDS=all) for PDFs uploaded since TRANSLATE_SINCE whose parse finished and that have no translation
yet (or a stale or failed one). An article is English when CJK characters are under a tenth of the letters in
its first chunks.

RetainPDF (RETAINPDF_URL) runs one job at a time for everyone, so this keeps at most one job of its own there
and submits only when RetainPDF is idle, or after waiting YIELD_SECONDS for it to be. RetainPDF's own job list
is the record of what was submitted: the uploaded file is named after the document and its content hash, so
after a timeout or a restart the next round finds the job instead of submitting it again (each job costs
model calls). That only holds for one process at a time, so a process that submits holds LOCK: the daemon waits
for it, a --once run started next to the daemon exits. A finished job's PDF is downloaded and submitted; a
document whose jobs failed MAX_FAILURES times is left alone. When RetainPDF cannot be reached the round ends; the
next one tries again. With TRANSLATE_KB_IDS, TRANSLATE_SINCE or RETAINPDF_URL empty it does nothing (and waits,
so that entrypoint.sh does not restart it).

A document left alone for a reason a retry would not change (not English, too many pages or bytes, an upload
RetainPDF refused, MAX_FAILURES failed jobs) is recorded with its content hash in SKIP_TABLE, a table of its own in
RAGFlow's database, so that a restart (every deploy restarts this) does not look at it again; a new file is looked
at afresh. Deleting a row has the document looked at again (one whose jobs failed is recorded again for as long as
RetainPDF lists those jobs). The language is read from the chunks there are: a file replaced under the same
document without a new parse is judged by its old chunks, and its row has to be deleted after the parse. Failures
of this process's own (chunk reads, downloads, submissions) are remembered only until it restarts. The ingestion files a translation against the document's content when it persists it, in
the background: a file replaced in the seconds between our last check and that would keep the old translation.

    python rag/svr/translate_worker.py [--once] [--dry-run]
"""

import argparse
import fcntl
import logging
import os
import re
import signal
import sys
import threading
import time
from datetime import datetime

import requests

INTERVAL = int(os.environ.get("TRANSLATE_INTERVAL", "60"))
URL = os.environ.get("RETAINPDF_URL", "").rstrip("/")
KB_IDS = [k.strip() for k in os.environ.get("TRANSLATE_KB_IDS", "").split(",") if k.strip()]
ALL_KBS = KB_IDS == ["all"]
SINCE = os.environ.get("TRANSLATE_SINCE", "").strip()  # ISO time, e.g. 2026-10-27T10:00:00+08:00
SERVICE_VERSION = os.environ.get("TRANSLATE_SERVICE_VERSION", "1.0.0")
YIELD_SECONDS = 2 * 3600
RECENT_SECONDS = 3600
MAX_FAILURES = 2  # RetainPDF jobs that failed, counted from its job list
MAX_LOCAL_FAILURES = 3  # downloads or submissions that failed here, until a restart
MAX_CJK_SHARE = 0.10
SAMPLE_CHUNKS = 20
READ_CHUNKS = 128  # one chunk_list request, in page order: the article's first SAMPLE_CHUNKS are among them
SKIP_TABLE = "translate_worker_skip"
MAX_PAGES = 300  # RetainPDF's upload limit
MAX_BYTES = 64 * 1024 * 1024  # the ingestion's limit for one result; a translation is not smaller than its source
CANDIDATES = 50
JOB_PAGE, MAX_JOBS = 100, 5000
CLOCK_SKEW_MS = 3600 * 1000  # RAGFlow's and RetainPDF's clocks are different machines'
# RetainPDF stores the upload under its name and the result as <stem>-translated.pdf: one path segment each
MAX_NAME_BYTES = 240
# The one upload 503 that is the document's own (its PDF repair ran out of time); the others are the service's
REPAIR_TIMEOUT = "PDF repair timed out"
LANGUAGE = "zh-cn"
NOT_THE_ARTICLE = {"summary", "meeting_minutes"}
LOCK = "/tmp/ragflow_translate_worker.lock"
# The key RetainPDF's translation model expects; the maintainers hand it out with the deployment. There is no
# default: without RETAINPDF_MODEL_API_KEY the worker treats its configuration as incomplete and does nothing.
MODEL_API_KEY = os.environ.get("RETAINPDF_MODEL_API_KEY", "").strip()
# The job request in RetainPDF's integration guide: full OCR, translation and layout. The translation model's
# key goes in per request (MODEL_API_KEY, from RETAINPDF_MODEL_API_KEY): it must not live in code or logs.
JOB = {
    "workflow": "book",
    "ocr": {"provider": "dots_mocr", "poll_timeout": 7200},
    "translation": {
        "model": "hymt", "base_url": "http://127.0.0.1:41011/v1", "mode": "fast",
        "math_mode": "placeholder", "batch_size": 1, "workers": 1, "rule_profile_name": "general_sci",
    },
    "render": {"render_mode": "auto", "compile_workers": 4},
    "runtime": {"timeout_seconds": 14400},
}
JOB_NAME = re.compile(r"^ragflow-([0-9a-f]{32})-([0-9a-f]{8})-")
CJK = re.compile(r"[㐀-鿿豈-﫿]")
LATIN = re.compile(r"[A-Za-z]")

stop_event = threading.Event()


class Unavailable(Exception):
    """RetainPDF cannot be reached or is busy refusing requests: try again next round."""


class Skip(Exception):
    """RetainPDF will not take this document (too many pages, rejected upload): leave it alone."""


class Changed(Exception):
    """The document's file changed since this round read it: look again next round."""


class State:
    """What a round needs from earlier ones, in this process."""

    def __init__(self):
        self.submitted = {}  # document id -> when its translation was submitted to the ingestion
        self.failures = {}  # (document id, content hash) -> chunk reads, downloads or submissions that failed
        # document id -> content hash left alone until a restart: failed here, or a lasting reason SKIP_TABLE did
        # not take (a dry run, the table not there, a database error)
        self.skipped = {}
        self.skip_table = False  # whether SKIP_TABLE is there to read and write
        self.dry_run = False
        self.english = set()  # (document id, content hash) found English
        self.reported = set()  # job ids whose failure was logged
        self.waiting_since = None  # since when a document waits for RetainPDF to be idle


def since_ms(value):
    return int(datetime.fromisoformat(value).timestamp() * 1000) if value else None


def content_key(doc):
    return (doc["content_hash"] or "0" * 8)[:8]


def job_file_name(doc):
    """The name the PDF is uploaded under, which ties the job to the document and its content."""
    prefix = f"ragflow-{doc['id']}-{content_key(doc)}-"
    stem = re.sub(r"\.pdf$", "", doc["name"], flags=re.I)
    room = MAX_NAME_BYTES - len(prefix) - len(".pdf")
    return prefix + stem.encode("utf-8")[:room].decode("utf-8", errors="ignore") + ".pdf"


def our_jobs(items):
    """RetainPDF's translation jobs for RAGFlow documents, newest first, by (document id, content hash)."""
    out = {}
    for item in items:
        match = JOB_NAME.match(item.get("source_file_name") or item.get("display_name") or "")
        if item.get("workflow") == "book" and match:
            out.setdefault(match.groups(), []).append(item)
    return out


def cjk_share(texts):
    cjk = sum(len(CJK.findall(t)) for t in texts)
    letters = cjk + sum(len(LATIN.findall(t)) for t in texts)
    return cjk / letters if letters else 1.0


def article_texts(rows):
    """The article's own chunks' text, as chunk_list returns them in page order."""
    return [
        row.get("content_with_weight") or ""
        for row in rows
        if row.get("doc_type_kwd") not in NOT_THE_ARTICLE and not row.get("enrichment_slot_kwd")
    ][:SAMPLE_CHUNKS]


def translated_name(name):
    stem = re.sub(r"\.pdf$", "", name, flags=re.I).replace("/", "_").replace("\\", "_")
    return f"{stem[:200]}_中文.pdf"


def submission(doc, job, pdf):
    """submit_result's arguments: the translated PDF, stored and not indexed, and where it came from."""
    return {
        "request_id": f"retainpdf-{job['job_id']}",
        "document_id": doc["id"],
        "artifact_type": "translation",
        "payload": {"job_id": job["job_id"], "source_file_name": doc["name"], "page_count": job.get("page_count")},
        "content_outputs": [{"field": "SELF", "store": True, "index": False}],
        "attachments": [{"role": "pdf", "file_name": translated_name(doc["name"]), "mime_type": "application/pdf",
                         "content": pdf, "store": True, "index": False}],
        "language": LANGUAGE,
        "service_name": "retainpdf",
        "service_version": SERVICE_VERSION,
        "model_name": "hymt",
        "schema_version": "1",
    }


def _call(method, path, **kwargs):
    try:
        response = getattr(requests, method)(f"{URL}{path}", allow_redirects=False, **kwargs)
    except (requests.ConnectionError, requests.Timeout) as e:
        raise Unavailable(f"RetainPDF unreachable: {e}") from e
    if response.status_code in (429, 502, 503, 504):
        raise Unavailable(f"RetainPDF HTTP {response.status_code}: {response.text[:200]}")
    return response


def _data(response):
    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict) or response.status_code != 200 or body.get("code") != 0:
        message = body.get("message") if isinstance(body, dict) else response.text[:200]
        raise RuntimeError(f"RetainPDF HTTP {response.status_code}: {message}")
    return body["data"]


def health():
    return _data(_call("get", "/health", timeout=(10, 30)))


def updated_ms(item):
    return int(datetime.fromisoformat(item["updated_at"].replace("Z", "+00:00")).timestamp() * 1000)


def list_jobs(since):
    """RetainPDF's jobs, last updated first (its order), back to the first one last updated before since: that
    one and the rest were also created before it. A list cut short would hide jobs and so pay for them again:
    past MAX_JOBS the round ends."""
    items = []
    while True:
        page = _data(_call("get", "/api/v1/jobs", params={"limit": JOB_PAGE, "offset": len(items)}, timeout=(10, 30)))
        items.extend(page["items"])
        if len(page["items"]) < JOB_PAGE or updated_ms(page["items"][-1]) < since:
            return items
        if len(items) >= MAX_JOBS:
            logging.error("translate: RetainPDF's job list is longer than %d items back to %d; submitting nothing",
                          MAX_JOBS, since)
            raise Unavailable("job list too long")


def job_reason(job_id):
    try:
        job = _data(_call("get", f"/api/v1/jobs/{job_id}", timeout=(10, 30)))
    except Exception as e:
        return f"(reason not read: {e})"
    return (job.get("failure") or {}).get("summary") or job.get("error")


def upload_and_create(doc, pdf):
    """Upload the PDF and create its job. Neither is retried: a job that timed out may exist all the same,
    and the next round's job list shows it."""
    response = _call("post", "/api/v1/uploads", files={"file": (job_file_name(doc), pdf, "application/pdf")},
                     timeout=(10, 300))
    if response.status_code in (400, 413):
        raise Skip(f"upload refused, HTTP {response.status_code}: {response.text[:200]}")
    upload = _data(response)
    if (upload.get("page_count") or 0) > MAX_PAGES:
        raise Skip(f"{upload['page_count']} pages")
    job = _data(_call("post", "/api/v1/jobs",
                      json={**JOB, "translation": {**JOB["translation"], "api_key": MODEL_API_KEY},
                            "source": {"upload_id": upload["upload_id"]}},
                      timeout=(10, 300)))
    return job["job_id"]


def download(job_id):
    response = _call("get", f"/api/v1/jobs/{job_id}/pdf", timeout=(10, 300))
    if response.status_code != 200:
        _data(response)
        raise RuntimeError(f"RetainPDF PDF HTTP {response.status_code}")
    pdf = response.content
    if not pdf.startswith(b"%PDF-"):
        raise RuntimeError("RetainPDF's download is not a PDF")
    if len(pdf) > MAX_BYTES:
        raise Skip(f"translated PDF is {len(pdf)} bytes")
    return pdf


_skip_model = None


def skip_model():
    """SKIP_TABLE's model, bound to RAGFlow's database. Built on first use: RAGFlow's modules are only there in
    its container."""
    global _skip_model
    if _skip_model is None:
        from peewee import BigIntegerField, CharField, Model

        from api.db.db_models import DB

        class TranslateWorkerSkip(Model):
            doc_id = CharField(max_length=32, primary_key=True)
            content_hash = CharField(max_length=32, null=True)  # as Document.content_hash, which may be empty
            reason = CharField(max_length=255)
            skipped_at = BigIntegerField()  # ms, like Document.create_time

            class Meta:
                database = DB
                table_name = SKIP_TABLE

        _skip_model = TranslateWorkerSkip
    return _skip_model


def prepare_skip_table(create):
    """Whether SKIP_TABLE is there and joins with RAGFlow's documents; created first when create (a dry run only
    looks, it writes nothing). A join that fails (a collation unlike the document table's would, on every round) is
    logged and the table left unused: documents are then left alone until a restart, as without it."""
    from peewee import JOIN, fn

    from api.db.db_models import DB, Document

    with DB.connection_context():
        if create:
            DB.create_tables([skip_model()], safe=True)
        elif not DB.table_exists(SKIP_TABLE):
            return False
        s = skip_model()
        try:
            list(Document.select(Document.id).join(s, JOIN.LEFT_OUTER, on=(
                (s.doc_id == Document.id) & (fn.COALESCE(s.content_hash, "") == fn.COALESCE(Document.content_hash, ""))
            )).limit(1))
        except Exception as e:
            logging.error("translate: %s does not join with the documents, not using it: %s", SKIP_TABLE, str(e)[:300])
            return False
        return True


def record_left_alone(doc, reason):
    """The document's row in SKIP_TABLE, replacing one an older content left."""
    from api.db.db_models import DB

    model = skip_model()
    with DB.connection_context():
        model.insert(doc_id=doc["id"], content_hash=doc["content_hash"], reason=str(reason)[:255],
                     skipped_at=int(time.time() * 1000)).on_conflict_replace().execute()


def candidates(skipped, use_table, limit=CANDIDATES):
    """Parsed PDFs uploaded since SINCE to KB_IDS (to every knowledge base still in use, with ALL_KBS) without
    a current translation, oldest first, less the ones left alone with the same content: in SKIP_TABLE (with
    use_table) or in skipped."""
    from peewee import JOIN, Tuple, fn

    from api.db.db_models import DB, Document, DocumentEnrichment, Knowledgebase
    from common.constants import StatusEnum, TaskStatus

    e = DocumentEnrichment
    with DB.connection_context():
        query = (
            Document.select(Document.id, Document.kb_id, Document.name, Document.content_hash,
                            Document.create_time, Knowledgebase.tenant_id)
            .join(Knowledgebase, on=(Knowledgebase.id == Document.kb_id))
            .switch(Document)
            .join(
                e,
                JOIN.LEFT_OUTER,
                on=((e.document_id == Document.id) & (e.artifact_type == "translation") & (e.language == LANGUAGE)),
            )
        )
        left_alone = []
        if use_table:
            s = skip_model()
            # A row for an older content does not count: an empty or missing hash matches only another one
            query = query.switch(Document).join(
                s,
                JOIN.LEFT_OUTER,
                on=((s.doc_id == Document.id)
                    & (fn.COALESCE(s.content_hash, "") == fn.COALESCE(Document.content_hash, ""))),
            )
            left_alone = [s.doc_id.is_null()]
        query = (
            query.where(
                Knowledgebase.status == StatusEnum.VALID.value,
                *([] if ALL_KBS else [Document.kb_id.in_(KB_IDS)]),
                *([Tuple(Document.id, Document.content_hash).not_in(list(skipped.items()))] if skipped else []),
                *left_alone,
                Document.create_time >= since_ms(SINCE),
                Document.suffix == "pdf",
                Document.run == TaskStatus.DONE.value,
                Document.progress == 1,
                Document.status == StatusEnum.VALID.value,
                Document.chunk_num > 0,
                e.id.is_null() | e.status.in_(["stale", "failed"]),
            )
            .order_by(Document.create_time)
            .limit(limit)
            .dicts()
        )
        return list(query)


def read_texts(doc):
    from common import settings

    rows = settings.retriever.chunk_list(
        doc["id"],
        doc["tenant_id"],
        [doc["kb_id"]],
        max_count=READ_CHUNKS,
        fields=["content_with_weight", "doc_type_kwd", "enrichment_slot_kwd"],
        sort_by_position=True,
    )
    return article_texts(rows)


def read_pdf(doc):
    """The document's file, as long as it is still the content this round read (its hash is xxh128)."""
    import xxhash

    from api.db.services.file2document_service import File2DocumentService
    from common import settings

    bucket, name = File2DocumentService.get_storage_address(doc_id=doc["id"])
    pdf = settings.STORAGE_IMPL.get(bucket, name)
    if not pdf:  # the storage returns None for any error
        raise RuntimeError(f"source PDF not read from storage: {bucket}/{name}")
    if doc["content_hash"] and xxhash.xxh128(pdf).hexdigest() != doc["content_hash"]:
        raise Changed("its file was replaced")
    return pdf


def current_hash(doc):
    from api.db.db_models import DB, Document

    with DB.connection_context():
        found = Document.get_or_none(Document.id == doc["id"])
        return found.content_hash if found else None


def collect(doc, job, state, dry_run):
    """File a finished job's PDF with the ingestion."""
    from api.db.services.document_enrichment_ingestion_service import submit_result

    if dry_run:
        logging.info("translate: would file job %s for %s (%s)", job["job_id"], doc["id"], doc["name"])
        return
    try:
        pdf = download(job["job_id"])
        if current_hash(doc) != doc["content_hash"]:
            logging.info("translate: %s (%s) changed since this round began; job %s not filed", doc["id"],
                         doc["name"], job["job_id"])
            return
        submit_result(**submission(doc, job, pdf))
    except Skip as e:
        give_up(doc, state, e)
        return
    except Unavailable:
        raise
    except Exception as e:
        failed_here(doc, state, f"filing job {job['job_id']}", e)
        return
    state.failures.pop((doc["id"], doc["content_hash"]), None)
    state.submitted[doc["id"]] = time.time()
    logging.info("translate: job %s for %s submitted (%s)", job["job_id"], doc["id"], doc["name"])


def give_up(doc, state, reason, level=logging.WARNING, lasting=True):
    """Leave the document alone: for good in SKIP_TABLE when the reason is lasting, else until a restart."""
    recorded = False
    if lasting and state.skip_table and not state.dry_run:
        try:
            record_left_alone(doc, reason)
            recorded = True
        except Exception as e:
            logging.warning("translate: %s not recorded in %s, left alone until a restart: %s", doc["id"],
                            SKIP_TABLE, str(e)[:200])
    if recorded:
        state.skipped.pop(doc["id"], None)  # the table excludes it from now on
    else:
        state.skipped[doc["id"]] = doc["content_hash"]
    logging.log(level, "translate: %s (%s) left alone: %s", doc["id"], doc["name"], reason)


def failed_here(doc, state, what, e):
    key = (doc["id"], doc["content_hash"])
    state.failures[key] = state.failures.get(key, 0) + 1
    logging.warning("translate: %s %s failed (%d of %d): %s", what, doc["id"], state.failures[key],
                    MAX_LOCAL_FAILURES, str(e)[:200])
    if state.failures[key] >= MAX_LOCAL_FAILURES:
        give_up(doc, state, f"failed {MAX_LOCAL_FAILURES} times here", lasting=False)


def needs_translation(doc, state):
    """Whether the document is English (the answer is kept when it is not); None when that was not found out."""
    texts = read_texts(doc)
    if not texts:  # parsed with chunks, so the index did not answer: try again next round
        failed_here(doc, state, "reading the chunks of", "no article chunks")
        return None
    share = cjk_share(texts)
    if share < MAX_CJK_SHARE:
        return True
    give_up(doc, state, f"not English (CJK {share:.0%})", logging.INFO)
    return False


def is_english(doc, state):
    if (doc["id"], doc["content_hash"]) in state.english:
        return True
    english = needs_translation(doc, state)
    if english:
        state.english.add((doc["id"], doc["content_hash"]))
    return english


def run_round(state, dry_run=False):
    state.dry_run = dry_run
    if not state.skip_table:  # looked for again each round until it is there (created, unless a dry run)
        state.skip_table = prepare_skip_table(create=not dry_run)
    now = time.time()
    docs, pending = [], False  # pending: a document whose language could not be found out this round
    for doc in candidates(state.skipped, state.skip_table):
        if stop_event.is_set():
            return
        english = is_english(doc, state)
        if english:
            docs.append(doc)
        pending = pending or english is None
    if not docs:
        if not pending:
            state.waiting_since = None
        return
    status = health()
    # A document's jobs are not older than the document: the list is read back to the oldest English candidate.
    jobs = our_jobs(list_jobs(min(doc["create_time"] for doc in docs) - CLOCK_SKEW_MS))
    busy = any(job["status"] in ("queued", "running") for found in jobs.values() for job in found)
    waiting = None
    for doc in docs:
        if stop_event.is_set():
            return
        found = jobs.get((doc["id"], content_key(doc)), [])
        latest = found[0]["status"] if found else None
        if latest in ("queued", "running"):
            continue
        if latest == "succeeded":
            if now - state.submitted.get(doc["id"], -RECENT_SECONDS) >= RECENT_SECONDS:
                collect(doc, found[0], state, dry_run)
            continue
        failed = [job for job in found if job["status"] in ("failed", "canceled")]
        for job in failed:
            if job["job_id"] not in state.reported:
                state.reported.add(job["job_id"])
                logging.warning("translate: job %s for %s %s: %s", job["job_id"], doc["id"], job["status"],
                                str(job_reason(job["job_id"]))[:200])
        if len(failed) >= MAX_FAILURES:
            give_up(doc, state, f"{len(failed)} RetainPDF jobs failed")
        elif not waiting:
            waiting = doc
    if waiting and not busy:
        submit(waiting, status, state, now, dry_run)
    elif busy or not (waiting or pending):  # the wait is for others' jobs, while something of ours is ready
        state.waiting_since = None


def submit(doc, status, state, now, dry_run):
    """Submit one document to RetainPDF, once it is idle or YIELD_SECONDS have passed waiting for that."""
    if dry_run:
        logging.info("translate: would submit %s (%s)", doc["id"], doc["name"])
        return
    if state.waiting_since is None:
        state.waiting_since = now
    if (status.get("queue_depth") or status.get("running_jobs")) and now - state.waiting_since < YIELD_SECONDS:
        logging.info("translate: RetainPDF busy, %s waits", doc["id"])
        return
    try:
        pdf = read_pdf(doc)
        if len(pdf) > MAX_BYTES:
            raise Skip(f"source PDF is {len(pdf)} bytes, its translation could not be filed")
        job_id = upload_and_create(doc, pdf)
    except Changed as e:
        logging.info("translate: %s (%s) not submitted: %s", doc["id"], doc["name"], e)
        return
    except Skip as e:
        give_up(doc, state, e)
        return
    except Unavailable as e:
        if REPAIR_TIMEOUT not in str(e):
            raise  # the service's trouble: the round ends, the wait is kept
        failed_here(doc, state, "submitting", e)
        return
    except Exception as e:
        failed_here(doc, state, "submitting", e)
        return
    state.failures.pop((doc["id"], doc["content_hash"]), None)
    state.waiting_since = None
    logging.info("translate: %s (%s) submitted as job %s", doc["id"], doc["name"], job_id)


def hold_lock(wait):
    """The lock that makes this the only process submitting; None when another holds it and wait is False."""
    handle = open(LOCK, "w")
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except BlockingIOError:
            if not wait or stop_event.wait(INTERVAL):
                handle.close()
                return None


def signal_handler(sig, frame):
    logging.info("Received interrupt signal, shutting down...")
    stop_event.set()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--once", action="store_true", help="one round, then exit")
    parser.add_argument("--dry-run", action="store_true", help="log what would be done; reads RetainPDF only")
    args = parser.parse_args()

    from common import settings

    settings.init_settings()
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    # The daemon waits rather than exits on a configuration it cannot use: entrypoint.sh would start it again a
    # second later. A --once or --dry-run run says so and exits.
    problem = None
    if not (KB_IDS and SINCE and URL and MODEL_API_KEY):
        problem = "TRANSLATE_KB_IDS, TRANSLATE_SINCE, RETAINPDF_URL or RETAINPDF_MODEL_API_KEY is empty"
    else:
        try:
            since_ms(SINCE)
        except ValueError:
            problem = f"TRANSLATE_SINCE {SINCE!r} is not an ISO time"
    if problem:
        logging.warning("translate worker: %s, nothing to do", problem)
        if args.once or args.dry_run:
            return 1
        stop_event.wait()
        return
    lock = None if args.dry_run else hold_lock(wait=not args.once)
    if not (args.dry_run or lock):
        if stop_event.is_set():  # stopped while waiting for it
            return
        logging.error("translate worker: another one holds %s (the daemon?); not running", LOCK)
        return 1
    logging.info("translate worker: %s, every %ds, since %s, kbs %s", URL, INTERVAL, SINCE, KB_IDS)
    state = State()
    while not stop_event.is_set():
        try:
            run_round(state, dry_run=args.dry_run)
        except Unavailable as e:
            logging.warning("translate: %s; ending this round", e)
        except Exception:
            logging.exception("translate round failed")
        if args.once or args.dry_run:
            return
        for k in [k for k, t in state.submitted.items() if time.time() - t > RECENT_SECONDS]:
            del state.submitted[k]
        stop_event.wait(INTERVAL)


if __name__ == "__main__":
    from common.log_utils import init_root_logger

    init_root_logger("translate_worker")
    sys.exit(main())
