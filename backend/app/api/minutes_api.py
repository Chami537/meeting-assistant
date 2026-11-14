"""Minutes of a transcript someone else recognized: RAGFlow's audio parser posts the segments its Speech2Text
model returned (start and end in seconds, as SYSTRAN gives them) and the file name, and gets the minutes back
as the summary job writes them (summary.write_minutes), with a Markdown copy for its knowledge base. Each
request gets a request_id, in the log and the reply, that the caller files the minutes under (RAGFlow's
submit_result), so a stored result can be found in this service's log.

Nothing is stored and nothing of the transcript is logged. There is no authentication: the service is only
reachable on a private network, like the transcription service it sits next to.
"""

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from functools import lru_cache
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..deps import llm_client
from ..postprocess import minutes as mm
from ..postprocess.providers import LlmProvider
from ..postprocess.summary import describe, try_later, write_minutes

log = logging.getLogger(__name__)
router = APIRouter()

TIMEOUT_SECONDS = 600  # a request and its one retry when thin: well under RAGFlow's 600 s wait for us
# The gateway answers 502 when several long requests run at once; more wait their turn here.
_RUNNING = asyncio.Semaphore(2)


class Segment(BaseModel):
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    text: str = ""
    speaker: str | None = None


class MinutesRequest(BaseModel):
    title: str | None = Field(None, description="the recording's file name")
    segments: list[Segment] = Field(max_length=50_000)


@lru_cache
def get_llm() -> LlmProvider:
    return llm_client()


def _llm() -> LlmProvider:
    try:
        return get_llm()
    except RuntimeError as exc:  # LLM_* not configured
        raise HTTPException(503, f"minutes model not configured: {exc}") from None


def numbered(segments: list[Segment]) -> list[dict[str, Any]]:
    """As the summary job stores a transcript: in time order, cleaned, empty ones dropped, numbered."""
    timed = [
        {
            "start_ms": round(s.start * 1000),
            "end_ms": round(s.end * 1000),
            "speaker": s.speaker,
            "text": s.text,
        }
        for s in segments
    ]
    return mm.number_segments(timed)[0]


@router.post("/v1/minutes")
async def write(body: MinutesRequest, llm: Annotated[LlmProvider, Depends(_llm)]) -> dict[str, Any]:
    request_id = uuid.uuid4().hex
    segments = numbered(body.segments)
    if not segments:
        raise HTTPException(422, "no text in the segments")
    title = mm.meeting_title(body.title)
    async with _RUNNING:
        deadline = asyncio.get_running_loop().time() + TIMEOUT_SECONDS
        try:
            written = await write_minutes(llm, segments, title=title, deadline=deadline, label="minutes API")
        except mm.TranscriptTooLong as exc:
            raise HTTPException(413, f"transcript too long: {exc}") from None
        except Exception as exc:
            error = describe(exc)
            log.warning("minutes API %s: %d segments, failed: %s", request_id, len(segments), error)
            if try_later(exc):  # the model's service, not these minutes: 503, as for an unconfigured model
                raise HTTPException(503, f"minutes model unavailable: {error}") from None
            raise HTTPException(502, f"minutes failed: {error}") from None
    minutes = written.minutes
    meeting_date, date_source = mm.resolve_meeting_date(
        scheduled_at=None, title=title, said=minutes.meeting_date, uploaded_at=datetime.now(UTC)
    )
    columns = mm.as_columns(minutes)
    log.info("minutes API %s: %d segments, %d items", request_id, len(segments), len(minutes.cited()))
    return {
        "request_id": request_id,
        "prompt_version": mm.PROMPT_VERSION,
        "model": llm.model,
        "meeting_date": meeting_date.isoformat() if meeting_date else None,
        "meeting_date_source": date_source,
        "minutes": columns,
        "markdown": mm.to_markdown(columns, title=title, meeting_date=meeting_date, date_source=date_source),
    }
