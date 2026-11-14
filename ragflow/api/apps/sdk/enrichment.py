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
"""Summaries, meeting minutes and translations of search results, for the report search page (API key)."""

import logging
from urllib.parse import quote

from quart import make_response

from api.apps.services import enrichment_lookup_service as lookup
from api.utils.api_utils import get_error_data_result, get_request_json, get_result, server_error_response, token_required
from common.constants import RetCode
from common.misc_utils import thread_pool_exec


@manager.route("/reports/enrichments", methods=["POST"])  # noqa: F821
@token_required
async def report_enrichments(tenant_id):
    """
    The current summary, meeting minutes and translation of up to 100 documents.
    Body: {"documents": [{"dataset_id": ..., "document_id": ...}], "types": ["summary", "meeting_minutes", "translation"]}
    (types optional, all by default). One entry per document in request order; an enrichment the document does not
    have, or that the key may not read, is null. A translation is a link to
    GET /datasets/<dataset_id>/documents/<document_id>/translation.
    """
    try:
        body = await get_request_json()
    except Exception:  # malformed JSON, or JSON that is not an object
        body = None
    try:
        pairs, types = lookup.parse_request(body)
    except ValueError as exc:
        return get_error_data_result(message=str(exc), code=RetCode.BAD_REQUEST)
    try:
        return get_result(data={"documents": await thread_pool_exec(lookup.lookup, tenant_id, pairs, types)})
    except Exception as exc:
        logging.exception("Report enrichment lookup failed")
        return server_error_response(exc)


@manager.route("/datasets/<dataset_id>/documents/<document_id>/translation", methods=["GET"])  # noqa: F821
@token_required
async def document_translation(tenant_id, dataset_id, document_id):
    """The document's current Chinese translation as a PDF, shown inline. Failures carry a real HTTP status (404,
    500), not only a code in the JSON, so a proxy that streams the body does not pass an error off as the PDF."""
    try:
        found = await thread_pool_exec(lookup.readable_translation_pdf, tenant_id, dataset_id, document_id)
        if found is None:
            return get_error_data_result(message="Translation not found", code=RetCode.NOT_FOUND), 404
        file_name, binary = found
        response = await make_response(binary)
        response.headers.set("Content-Type", "application/pdf")
        response.headers.set("Content-Disposition", f"inline; filename*=UTF-8''{quote(file_name, safe='')}")
        response.headers.set("Content-Length", str(len(binary)))
        response.headers.set("X-Content-Type-Options", "nosniff")
        return response
    except Exception as exc:
        logging.exception("Document translation failed")
        return server_error_response(exc), 500
