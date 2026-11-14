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
"""The translated PDF next to /chunk/pdf-preview: same parameters, same checks (the chunk must belong to the
document), so the preview page can switch between the original and its translation."""

import logging
from urllib.parse import quote

from quart import make_response, request

from api.apps.services import enrichment_lookup_service as lookup
from api.apps.services.chunk_pdf_preview_service import ChunkPdfPreviewError, resolve_chunk_pdf_preview_context
from api.utils.api_utils import get_json_result, get_result, server_error_response
from common.constants import RetCode
from common.misc_utils import thread_pool_exec


def _error_response(exc: ChunkPdfPreviewError):
    return get_json_result(code=exc.code, message=exc.message, data={"error_code": exc.error_code})


def _no_store(response):
    response.headers.set("Cache-Control", "no-store")
    return response


async def _document_id() -> str:
    arguments = {key: request.args.get(key) for key in ("dataset_id", "document_id", "doc_id", "chunk_id")}
    context = await thread_pool_exec(resolve_chunk_pdf_preview_context, **arguments)
    return context.document_id


@manager.route("/chunk/pdf-preview/translation", methods=["GET"])  # noqa: F821
async def chunk_pdf_translation():
    """{"available": bool}: whether the document has a translated PDF."""
    try:
        document_id = await _document_id()
        return _no_store(get_result(data={"available": await thread_pool_exec(lookup.has_translation, document_id)}))
    except ChunkPdfPreviewError as exc:
        return _no_store(_error_response(exc))
    except Exception as exc:
        logging.exception("Chunk PDF translation lookup failed")
        return _no_store(server_error_response(exc))


@manager.route("/chunk/pdf-preview/translation/file", methods=["GET"])  # noqa: F821
async def chunk_pdf_translation_file():
    try:
        document_id = await _document_id()
        found = await thread_pool_exec(lookup.translation_pdf, document_id)
        if found is None:
            raise ChunkPdfPreviewError("Translation not found.", code=RetCode.DATA_ERROR, error_code="TRANSLATION_NOT_FOUND")
        file_name, binary = found
        response = await make_response(binary)
        response.headers.set("Content-Type", "application/pdf")
        response.headers.set("Content-Disposition", f"inline; filename*=UTF-8''{quote(file_name, safe='')}")
        response.headers.set("Content-Length", str(len(binary)))
        response.headers.set("X-Content-Type-Options", "nosniff")
        response.headers.set("Cache-Control", "private, max-age=300, must-revalidate")
        return response
    except ChunkPdfPreviewError as exc:
        return _error_response(exc)
    except Exception as exc:
        logging.exception("Chunk PDF translation file failed")
        return server_error_response(exc)
