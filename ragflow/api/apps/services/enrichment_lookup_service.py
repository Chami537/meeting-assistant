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
"""A document's current summary, meeting minutes and translated PDF, for the report search page (summary on
hover, a link to the translation, the minutes of a call) and the PDF preview (switch to the translation).

Only a slot's current successful result counts, as in DocumentEnrichmentService.get_current_result: a stale
slot has no current result, so a summary of content that has since changed never shows. The summary text and
the minutes are read from object storage, several documents at a time; the translation is only announced here
and read when someone opens it.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor

from api.db.db_models import DB, Document, DocumentEnrichment, DocumentEnrichmentResult
from api.db.services.document_enrichment_storage import load_manifest, read_object
from api.db.services.knowledgebase_service import KnowledgebaseService
from common.constants import DocumentEnrichmentStatus

TYPES = ("summary", "meeting_minutes", "translation")
MAX_DOCUMENTS = 100
LANGUAGE = "zh-cn"  # preferred when a slot exists in more than one language
READERS = 8  # object storage reads in parallel


def parse_request(body) -> tuple[list[tuple[str, str]], list[str]]:
    """(dataset_id, document_id) pairs in request order, and the types asked for (all by default)."""
    if not isinstance(body, dict):
        raise ValueError("The request body must be a JSON object")
    documents = body.get("documents")
    if not isinstance(documents, list) or not documents:
        raise ValueError("`documents` must be a non-empty list")
    if len(documents) > MAX_DOCUMENTS:
        raise ValueError(f"At most {MAX_DOCUMENTS} documents per request")
    pairs = []
    for item in documents:
        ids = [item.get(key) if isinstance(item, dict) else None for key in ("dataset_id", "document_id")]
        if not all(isinstance(value, str) and value.strip() for value in ids):
            raise ValueError("Each document needs a `dataset_id` and a `document_id`")
        pairs.append((ids[0].strip(), ids[1].strip()))
    types = body.get("types", list(TYPES))
    if not isinstance(types, list) or not types or any(kind not in TYPES for kind in types):
        raise ValueError(f"`types` must be a non-empty list of: {', '.join(TYPES)}")
    return pairs, list(dict.fromkeys(types))


@DB.connection_context()
def current_results(document_ids, types) -> dict[tuple[str, str], DocumentEnrichmentResult]:
    """{(document_id, artifact_type): its current successful result}."""
    document_ids, types = list(document_ids), list(types)
    if not document_ids or not types:
        return {}
    slots = DocumentEnrichment.select(
        DocumentEnrichment.id, DocumentEnrichment.document_id, DocumentEnrichment.artifact_type,
        DocumentEnrichment.language, DocumentEnrichment.current_result_id,
    ).where(
        DocumentEnrichment.document_id.in_(document_ids),
        DocumentEnrichment.artifact_type.in_(types),
        DocumentEnrichment.status == DocumentEnrichmentStatus.SUCCEEDED.value,
        DocumentEnrichment.current_result_id.is_null(False),
    )
    chosen = {}
    for slot in sorted(slots, key=lambda slot: (slot.language != LANGUAGE, slot.language)):
        chosen.setdefault((slot.document_id, slot.artifact_type), slot)
    if not chosen:
        return {}
    results = {
        result.id: result
        for result in DocumentEnrichmentResult.select().where(
            DocumentEnrichmentResult.id.in_([slot.current_result_id for slot in chosen.values()]))
    }
    return {
        key: results[slot.current_result_id]
        for key, slot in chosen.items()
        if slot.current_result_id in results and results[slot.current_result_id].enrichment_id == slot.id
    }


def _output(result, role: str) -> tuple[dict, bytes]:
    manifest = load_manifest({
        "storage_bucket": result.storage_bucket, "storage_key": result.storage_key,
        "file_size": result.file_size, "checksum": result.checksum,
    })
    output = next((item for item in manifest["outputs"] if item.get("role") == role and item.get("store", True)), None)
    if output is None:
        raise LookupError(f"Enrichment result {result.id} keeps no {role} output")
    return output, read_object(output)


def _payload(result):
    """What the service submitted: a JSON object or plain text. Results from before object storage keep it inline."""
    if not result.storage_key:
        return result.content_json or result.content_text or ""
    output, binary = _output(result, "self")
    text = binary.decode("utf-8")
    return json.loads(text) if str(output.get("mime_type", "")).startswith("application/json") else text


def _generated_at(result) -> str | None:
    return result.generated_at.isoformat() if result.generated_at else None


def _summary(result) -> dict | None:
    payload = _payload(result)
    text = payload.get("summary") if isinstance(payload, dict) else payload
    text = text.strip() if isinstance(text, str) else ""
    return {"text": text, "result_id": result.id, "generated_at": _generated_at(result)} if text else None


def _meeting_minutes(result) -> dict | None:
    payload = _payload(result)
    if not isinstance(payload, dict):
        payload = {"markdown": payload}
    markdown = payload.get("markdown")
    if not isinstance(markdown, str) or not markdown.strip():
        return None
    minutes = payload.get("minutes") if isinstance(payload.get("minutes"), dict) else {}
    return {
        "markdown": markdown.strip(),
        "summary": minutes.get("summary"),
        "meeting_type": minutes.get("meeting_type"),
        "meeting_date": payload.get("meeting_date"),
        "result_id": result.id,
        "generated_at": _generated_at(result),
    }


def _translation(dataset_id: str, document_id: str, result) -> dict:
    return {
        "url": f"/api/v1/datasets/{dataset_id}/documents/{document_id}/translation",
        "result_id": result.id,
        "generated_at": _generated_at(result),
    }


READ = {"summary": _summary, "meeting_minutes": _meeting_minutes}


def _read(kind: str, result) -> dict | None:
    try:
        return READ[kind](result)
    except Exception:
        logging.exception("Could not read %s %s of document %s", kind, result.id, result.document_id)
        return None


@DB.connection_context()
def _readable(tenant_id: str, pairs) -> set[tuple[str, str]]:
    """The pairs the tenant may read: the document is in that dataset, and the tenant has joined the dataset."""
    datasets = {dataset_id for dataset_id, _ in pairs}
    joined = {dataset_id for dataset_id in datasets if KnowledgebaseService.accessible(kb_id=dataset_id, user_id=tenant_id)}
    homes = {
        row.id: row.kb_id
        for row in Document.select(Document.id, Document.kb_id).where(Document.id.in_([doc for _, doc in pairs]))
    }
    return {(dataset_id, document_id) for dataset_id, document_id in pairs
            if dataset_id in joined and homes.get(document_id) == dataset_id}


def lookup(tenant_id: str, pairs: list[tuple[str, str]], types: list[str]) -> list[dict]:
    """One entry per requested pair, in order; an enrichment the document lacks, or may not be read, is None."""
    readable = _readable(tenant_id, pairs)
    results = current_results({document_id for _, document_id in readable}, types)
    reads = [(key, result) for key, result in results.items() if key[1] in READ]
    with ThreadPoolExecutor(max_workers=READERS) as pool:
        contents = dict(zip([key for key, _ in reads], pool.map(lambda item: _read(item[0][1], item[1]), reads)))
    entries = []
    for dataset_id, document_id in pairs:
        entry = {"dataset_id": dataset_id, "document_id": document_id}
        for kind in types:
            result = results.get((document_id, kind)) if (dataset_id, document_id) in readable else None
            if result is None:
                entry[kind] = None
            elif kind == "translation":
                entry[kind] = _translation(dataset_id, document_id, result) if result.storage_key else None
            else:
                entry[kind] = contents.get((document_id, kind))
        entries.append(entry)
    return entries


def _current_translation(document_id: str):
    """The current translation result, if it is kept in object storage: only there can it carry the PDF (rows from
    before object storage keep text inline)."""
    result = current_results([document_id], ["translation"]).get((document_id, "translation"))
    return result if result is not None and result.storage_key else None


def has_translation(document_id: str) -> bool:
    return _current_translation(document_id) is not None


def translation_pdf(document_id: str) -> tuple[str, bytes] | None:
    """(file name, PDF) of the document's current translation, or None when it has none."""
    result = _current_translation(document_id)
    if result is None:
        return None
    output, binary = _output(result, "pdf")
    return output.get("file_name") or f"{document_id}_中文.pdf", binary


def readable_translation_pdf(tenant_id: str, dataset_id: str, document_id: str) -> tuple[str, bytes] | None:
    if not _readable(tenant_id, [(dataset_id, document_id)]):
        return None
    return translation_pdf(document_id)
