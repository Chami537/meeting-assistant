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
"""Short summaries of parsed PDFs, filed with the enrichment ingestion (submit_result, artifact "summary").

Every SUMMARY_INTERVAL seconds it looks in the knowledge bases named by SUMMARY_KB_IDS (or in every one, with
SUMMARY_KB_IDS=all, so that new knowledge bases are covered without a change) for PDFs whose parse
finished and that have no summary yet (or a stale or failed one), reads each one's chunks in page order,
asks the meeting assistant's summary API (SUMMARY_URL, /v1/summary) and submits the summary. The ingestion
stores it and indexes it next to the article's own chunks.

What it remembers lives in this process: the documents submitted in the last hour (the ingestion files them
in the background, so the next round would not see them yet) and how often each one failed (three times and
it is left alone until a restart). When the summary API cannot be reached the round ends; the next one tries
again. With SUMMARY_KB_IDS empty it does nothing (and waits, so that entrypoint.sh does not restart it).

    python rag/svr/summary_worker.py [--once] [--dry-run]
"""

import argparse
import logging
import os
import signal
import sys
import threading
import time

import requests

INTERVAL = int(os.environ.get("SUMMARY_INTERVAL", "300"))
BATCH = int(os.environ.get("SUMMARY_BATCH", "10"))
URL = os.environ.get("SUMMARY_URL","").rstrip("/")
KB_IDS = [k.strip() for k in os.environ.get("SUMMARY_KB_IDS", "").split(",") if k.strip()]
ALL_KBS = KB_IDS == ["all"]
SERVICE_VERSION = os.environ.get("SUMMARY_SERVICE_VERSION", "1.0.0")  # the program's; the prompt's is in the payload
TIMEOUT = 1500  # the API gives itself 20 minutes for a long article
RECENT_SECONDS = 3600
MAX_FAILURES = 3
LANGUAGE = "zh-cn"
# Chunks the ingestion indexed from enrichments (minutes, a summary) are not the article.
NOT_THE_ARTICLE = {"summary", "meeting_minutes"}

stop_event = threading.Event()


class Unavailable(Exception):
    """The summary API cannot be reached or has no model: try again next round."""


def pick(rows, submitted, failures, now, batch=BATCH):
    """The documents to summarize this round, from the query's rows in order: not submitted in the last
    hour, not failed MAX_FAILURES times."""
    out = []
    for row in rows:
        if now - submitted.get(row["id"], -RECENT_SECONDS) < RECENT_SECONDS:
            continue
        if failures.get(row["id"], 0) >= MAX_FAILURES:
            continue
        out.append(row)
        if len(out) == batch:
            break
    return out


def article_chunks(rows):
    """The article's own chunks, as chunk_list returns them in page order, for the summary API."""
    out = []
    for row in rows:
        if row.get("doc_type_kwd") in NOT_THE_ARTICLE or row.get("enrichment_slot_kwd"):
            continue
        pages = row.get("page_num_int") or [None]
        out.append({"text": row.get("content_with_weight") or "", "page": pages[0]})
    return out


def summarize(doc, chunks, post=requests.post):
    """The summary API's reply for one document; Unavailable when the service is down."""
    try:
        response = post(f"{URL}/v1/summary", json={"title": doc["name"], "chunks": chunks}, timeout=TIMEOUT)
    except requests.ConnectionError as e:
        raise Unavailable(f"summary API unreachable: {e}") from e
    if response.status_code == 503:
        raise Unavailable(f"summary API: {response.text[:200]}")
    if response.status_code != 200:
        raise RuntimeError(f"summary API HTTP {response.status_code}: {response.text[:200]}")
    return response.json()


PAYLOAD_FIELDS = ("summary", "prompt_version", "mode", "groups", "truncated", "input_chars", "used_chars")


def submission(doc, reply):
    """submit_result's arguments for a summary the API wrote: the whole reply is kept (how the summary was
    written goes with it), the summary text alone is indexed, next to the article's own chunks."""
    return {
        "request_id": reply["request_id"],
        "document_id": doc["id"],
        "artifact_type": "summary",
        "payload": {k: reply[k] for k in PAYLOAD_FIELDS},
        "content_outputs": [{"field": "SELF", "store": True, "index": True, "index_fields": ["summary"]}],
        "language": LANGUAGE,
        "service_name": "article-summary-service",
        "service_version": SERVICE_VERSION,
        "model_name": reply["model"],
        "schema_version": "1",
    }


def candidates(limit):
    """Parsed PDFs in KB_IDS (in every knowledge base still in use, with ALL_KBS) without a current summary,
    oldest first."""
    from peewee import JOIN

    from api.db.db_models import DB, Document, DocumentEnrichment, Knowledgebase
    from common.constants import StatusEnum, TaskStatus

    e = DocumentEnrichment
    with DB.connection_context():
        query = (
            Document.select(Document.id, Document.kb_id, Document.name, Document.token_num, Knowledgebase.tenant_id)
            .join(Knowledgebase, on=(Knowledgebase.id == Document.kb_id))
            .switch(Document)
            .join(
                e,
                JOIN.LEFT_OUTER,
                on=((e.document_id == Document.id) & (e.artifact_type == "summary") & (e.language == LANGUAGE)),
            )
            .where(
                Knowledgebase.status == StatusEnum.VALID.value,
                *([] if ALL_KBS else [Document.kb_id.in_(KB_IDS)]),
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


def read_chunks(doc):
    from common import settings

    rows = settings.retriever.chunk_list(
        doc["id"],
        doc["tenant_id"],
        [doc["kb_id"]],
        max_count=10000,
        fields=["content_with_weight", "page_num_int", "doc_type_kwd", "enrichment_slot_kwd"],
        sort_by_position=True,
    )
    return article_chunks(rows)


def run_round(submitted, failures, dry_run=False):
    from api.db.services.document_enrichment_ingestion_service import submit_result

    now = time.time()
    docs = pick(candidates(BATCH + len(submitted) + len(failures)), submitted, failures, now)
    for doc in docs:
        if stop_event.is_set():
            return
        chunks = read_chunks(doc)
        chars = sum(len(c["text"]) for c in chunks)
        if dry_run:
            logging.info("summary: would summarize %s (%s, %d chunks, %d chars)", doc["id"], doc["name"], len(chunks), chars)
            continue
        try:
            reply = summarize(doc, chunks)
            submit_result(**submission(doc, reply))
        except Unavailable as e:
            logging.warning("summary: %s; ending this round", e)
            return
        except Exception as e:
            failures[doc["id"]] = failures.get(doc["id"], 0) + 1
            logging.warning("summary of %s failed (%d of %d): %s", doc["id"], failures[doc["id"]], MAX_FAILURES, str(e)[:200])
            continue
        submitted[doc["id"]] = time.time()
        logging.info(
            "summary of %s submitted (%s): %s, %d groups, %d chars%s",
            doc["id"], reply["request_id"], reply.get("mode"), reply.get("groups", 1), chars,
            ", truncated" if reply.get("truncated") else "",
        )


def signal_handler(sig, frame):
    logging.info("Received interrupt signal, shutting down...")
    stop_event.set()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--once", action="store_true", help="one round, then exit")
    parser.add_argument("--dry-run", action="store_true", help="list what would be summarized; no API calls")
    args = parser.parse_args()

    from common import settings

    settings.init_settings()
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    if not KB_IDS:  # waits rather than exits: entrypoint.sh would start it again a second later
        logging.info("summary worker: SUMMARY_KB_IDS is empty, nothing to do")
        stop_event.wait()
        return
    logging.info("summary worker: %s, every %ds, %d per round, kbs %s", URL, INTERVAL, BATCH, KB_IDS)
    submitted, failures = {}, {}
    while not stop_event.is_set():
        try:
            run_round(submitted, failures, dry_run=args.dry_run)
        except Exception:
            logging.exception("summary round failed")
        if args.once or args.dry_run:
            return
        for k in [k for k, t in submitted.items() if time.time() - t > RECENT_SECONDS]:
            del submitted[k]
        stop_event.wait(INTERVAL)


if __name__ == "__main__":
    from common.log_utils import init_root_logger

    init_root_logger("summary_worker")
    sys.exit(main())
