"""A meetings page's job API (a web front end with a Go backend; its parser URL points at this service +
/meeting), the one its original implementation served, so that either can be behind the page
(services/meeting_parse.py):

    POST /meeting/parse {job_id, meeting_id, audio_url, title?} -> 200 {ok: true}, at once
    GET  /meeting/status/{job_id} -> {status: processing | done | failed, phase, result, error}
                                     (404 when unknown)

The Go backend waits 30 s for the first and counts only 200 as accepted. `title` (the meeting's: what was
typed at upload, else the file name) is this service's; the original implementation ignores it. Not
configured (MEETING_PARSE_DB unset) both answer 503. No authentication: on a private network only, like the
minutes API next to it.
"""

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from ..config import Settings
from ..integrations.whisper import WhisperJobsAsr
from ..services.meeting_parse import JobStore, MeetingParser
from . import minutes_api

log = logging.getLogger(__name__)
router = APIRouter(prefix="/meeting")


class ParseRequest(BaseModel):
    job_id: str = Field(min_length=1, max_length=64)
    meeting_id: int = 0  # the Go backend's, for the log only
    audio_url: str = Field(min_length=1, max_length=8192)
    title: str | None = None

    @field_validator("audio_url")
    @classmethod
    def _http(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("audio_url is not an http(s) link")
        return value

    @field_validator("title")
    @classmethod
    def _one_line(cls, value: str | None) -> str | None:
        return " ".join((value or "").split())[:255] or None


def start(settings: Settings) -> MeetingParser | None:
    """The parser for the app's lifetime, its unfinished jobs taken up; None when not configured."""
    if not settings.meeting_parse_db:
        return None
    if not settings.asr_qwen_url:
        log.error("MEETING_PARSE_DB is set but ASR_QWEN_URL is not: the AI meeting page's jobs are off")
        return None
    asr = WhisperJobsAsr(
        settings.asr_qwen_url,
        model="qwen3-asr-0.6b",
        language="zh",
        name="qwen",
        context=lambda title: title,  # the service adds the glossary
        sentences=True,
        priority=True,
    )
    parser = MeetingParser(
        JobStore(settings.meeting_parse_db),
        asr=asr,
        llm=minutes_api.get_llm,
        minutes_slots=minutes_api._RUNNING,
        minutes_timeout=minutes_api.TIMEOUT_SECONDS,
    )
    parser.resume()
    return parser


def _parser(request: Request) -> MeetingParser:
    parser = getattr(request.app.state, "meeting_parser", None)
    if parser is None:
        raise HTTPException(503, "meeting parsing is not configured")
    return parser


@router.post("/parse")
async def parse(body: ParseRequest, parser: Annotated[MeetingParser, Depends(_parser)]) -> dict[str, bool]:
    parser.submit(body.job_id, body.meeting_id, body.audio_url, body.title)
    return {"ok": True}


@router.get("/status/{job_id}")
async def status(job_id: str, parser: Annotated[MeetingParser, Depends(_parser)]) -> dict[str, Any]:
    job = parser.status(job_id)
    if job is None:
        raise HTTPException(404, "未找到该任务")
    return job
