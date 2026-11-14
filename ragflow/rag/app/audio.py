#
#  Copyright 2025 The InfiniFlow Authors. All Rights Reserved.
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
import logging
import os
import tempfile
import uuid

import requests

from common.constants import LLMType
from api.db.services.llm_service import LLMBundle
from api.db.joint_services.tenant_model_service import get_tenant_default_model_by_type

# A recording goes into the knowledge base as its meeting minutes: the meeting assistant's minutes API
# writes them from the transcript, and they are filed with the recording through the enrichment ingestion
# service (submit_result), which stores them and then replaces the recording's chunks with the minutes'
# Markdown, chunked by the naive parser. So this parser returns no chunks of its own. The recognizer's
# transcript is filed the same way (_store_asr), not put in the knowledge base.
MINUTES_URL = os.environ.get("MEETING_MINUTES_URL","")
# Minutes that cannot be written or filed fail the parse (the transcript is stored by then); there is no
# fallback to the transcript text. Parsing again files them again: the same minutes are not stored twice.


def _document_id(kb_id, filename):
    """The recording's document id. chunk() is not given it, but a knowledge base keeps its document names
    unique (an upload of the same name is renamed), and the task names the document as it is called."""
    from api.db.services.document_service import DocumentService

    docs = DocumentService.query(kb_id=kb_id, name=filename)
    if len(docs) != 1:
        raise RuntimeError(f"{len(docs)} documents named {filename} in this knowledge base")
    return docs[0].id


def _store_asr(document_id, result, segments, model_config):
    """The recognizer's result filed with the recording: as it came (text, language, duration, ...), with the
    segments the minutes are written from, which the ingestion service requires to have text and be in time
    order."""
    from api.db.services.document_enrichment_ingestion_service import submit_result

    segments = sorted(segments, key=lambda s: float(s["start"]))
    text = str(result.get("text") or "").strip() or " ".join(str(s["text"]).strip() for s in segments)
    return submit_result(
        request_id=f"{uuid.uuid4().hex}:asr",
        document_id=document_id,
        artifact_type="asr_transcript",
        payload={**result, "text": text, "segments": segments},
        language=result.get("language") or "",
        service_name="asr-service",
        service_version="1.0.0",
        model_name=model_config.get("llm_name") or "",
        schema_version="1",
        content_outputs=[{"field": "SELF", "store": True, "index": False}],
    )


def _minutes(filename, segments):
    """The minutes from the meeting assistant's minutes API: request_id, prompt_version, model, meeting_date,
    meeting_date_source, minutes (the sections) and markdown."""
    response = requests.post(
        f"{MINUTES_URL.rstrip('/')}/v1/minutes",
        json={
            "title": filename,
            "segments": [
                {"start": s["start"], "end": s["end"], "speaker": s.get("speaker"), "text": s["text"]}
                for s in segments
            ],
        },
        timeout=600,
    )
    if response.status_code != 200:
        raise RuntimeError(f"minutes API HTTP {response.status_code}: {response.text[:200]}")
    return response.json()


def _submit_minutes(document_id, minutes):
    """The minutes filed with the recording, under the id of the minutes request that wrote them."""
    from api.db.services.document_enrichment_ingestion_service import submit_result

    return submit_result(
        request_id=minutes["request_id"],
        document_id=document_id,
        artifact_type="meeting_minutes",
        payload=minutes,
        language="zh-cn",
        service_name="meeting-minutes-service",
        service_version=minutes["prompt_version"],
        model_name=minutes["model"],
        schema_version="1",
        content_outputs=[{"field": "SELF", "store": True, "index": True, "index_fields": ["markdown"]}],
    )


def chunk(filename, binary, tenant_id, lang, callback=None, **kwargs):
    callback = callback or (lambda *args, **kwargs: None)
    tmp_path = ""

    try:
        _, ext = os.path.splitext(filename)
        ext = ext.lower()
        if not ext:
            raise RuntimeError("No extension detected.")

        if ext not in [".wave", ".wav", ".mp3", ".aac", ".flac", ".ogg", ".opus", ".aiff", ".au", ".wma",
                       ".m4a", ".webm", ".mka", ".ape"]:
            raise RuntimeError(f"Extension {ext} is not supported yet.")

        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmpf:
            tmpf.write(binary)
            tmpf.flush()
            tmp_path = os.path.abspath(tmpf.name)

        callback(0.1, "Submitting audio to the configured Speech2Text model")
        seq2txt_model_config = get_tenant_default_model_by_type(tenant_id, LLMType.SPEECH2TEXT)
        seq2txt_mdl = LLMBundle(tenant_id, seq2txt_model_config, lang=lang)

        def report_asr_progress(progress, message):
            callback(0.1 + 0.5 * progress, message)

        # The file name often carries the company or project name that recognition would misspell.
        title = " ".join(os.path.splitext(os.path.basename(filename))[0].split())[:200]
        result = seq2txt_mdl.transcription_with_segments(
            tmp_path,
            progress_callback=report_asr_progress,
            prompt=title or None,
        )
        segments = [s for s in (result.get("segments") or []) if str(s.get("text") or "").strip()]
        if not segments:
            text = str(result.get("text") or "").strip()
            if not text:
                raise RuntimeError("Speech2Text model returned no text")
            segments = [{"start": 0.0, "end": float(result.get("duration") or 0.0), "text": text}]
        callback(0.6, f"Speech2Text produced {len(segments)} segments")

        document_id = _document_id(kwargs.get("kb_id"), filename)
        # First the transcript, so that it is kept even when the minutes fail.
        try:
            _store_asr(document_id, result, segments, seq2txt_model_config)
        except Exception as e:
            logging.exception("filing the transcript of %s failed", filename)
            raise RuntimeError(f"识别原文入库失败：{str(e)[:200]}") from e

        callback(0.65, "Writing the meeting minutes")
        try:
            minutes = _minutes(filename, segments)
        except Exception as e:
            logging.exception("meeting minutes of %s failed", filename)
            raise RuntimeError(f"会议纪要生成失败：{str(e)[:200]}") from e  # reported below, prog=-1
        try:
            _submit_minutes(document_id, minutes)
        except Exception as e:
            logging.exception("filing the meeting minutes of %s failed", filename)
            raise RuntimeError(f"会议纪要入库失败：{str(e)[:200]}") from e
        callback(msg=f"会议纪要已提交入库（{minutes['request_id']}），chunk 由入库服务按纪要生成")
        return []
    except Exception as e:
        callback(prog=-1, msg=str(e))
        raise
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception as e:
                logging.exception(f"Failed to remove temporary file: {tmp_path}, exception: {e}")
                pass
