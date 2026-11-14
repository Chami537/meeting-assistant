"""A short summary of an article RAGFlow has parsed: its summary worker posts the article's chunks in page
order and the file name, and gets the summary back (article_summary.write_summary) with a request_id it files
the summary under (submit_result), so a stored summary can be found in this service's log.

The minutes API's model and its two places are shared: a long article asks the model once per group of
chunks and once more for the summary, and each of those requests waits for a place on its own, so one long
article does not hold the minutes back. Nothing is stored and nothing of the article is logged.
"""

import asyncio
import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..postprocess import article_summary as asum
from ..postprocess import minutes as mm
from ..postprocess.providers import LlmProvider, LlmReply
from ..postprocess.summary import describe, try_later
from .minutes_api import _RUNNING, _llm

log = logging.getLogger(__name__)
router = APIRouter()

TIMEOUT_SECONDS = 1200  # ten groups and the summary, each waiting for a place; the worker waits 25 minutes


class Chunk(BaseModel):
    text: str = ""
    page: int | None = None


class SummaryRequest(BaseModel):
    title: str | None = Field(None, description="the article's file name")
    chunks: list[Chunk] = Field(max_length=20_000)


class _Queued:
    """The model, each request waiting for one of the places the minutes API shares."""

    def __init__(self, llm: LlmProvider):
        self.llm = llm
        self.name, self.model, self.region = llm.name, llm.model, llm.region

    async def complete_json(self, *, system: str, user: str, schema: dict[str, Any]) -> LlmReply:
        async with _RUNNING:
            return await self.llm.complete_json(system=system, user=user, schema=schema)


@router.post("/v1/summary")
async def write(body: SummaryRequest, llm: Annotated[LlmProvider, Depends(_llm)]) -> dict[str, Any]:
    request_id = uuid.uuid4().hex
    chunks = [c.text for c in body.chunks]
    if not any(mm.clean_text(t).strip() for t in chunks):
        raise HTTPException(422, "no text in the chunks")
    deadline = asyncio.get_running_loop().time() + TIMEOUT_SECONDS
    try:
        written = await asum.write_summary(
            _Queued(llm), chunks, title=mm.meeting_title(body.title), deadline=deadline, label="summary API"
        )
    except asum.SummaryRejected as exc:
        log.warning("summary API %s: %d chunks, rejected: %s", request_id, len(chunks), exc)
        raise HTTPException(502, f"summary rejected: {exc}") from None
    except Exception as exc:
        error = describe(exc)
        log.warning("summary API %s: %d chunks, failed: %s", request_id, len(chunks), error)
        # 503 when the model's service failed (rate limited, a 5xx, unreachable): the summary worker then
        # ends its round without holding it against this article, which a 502 counts towards giving up.
        if try_later(exc):
            raise HTTPException(503, f"summary model unavailable: {error}") from None
        raise HTTPException(502, f"summary failed: {error}") from None
    log.info(
        "summary API %s: %d chunks, %s, %d groups, %d of %d chars%s",
        request_id,
        len(chunks),
        written.mode,
        written.groups,
        written.used_chars,
        written.input_chars,
        ", truncated" if written.truncated else "",
    )
    return {
        "request_id": request_id,
        "prompt_version": asum.PROMPT_VERSION,
        "model": llm.model,
        "summary": written.summary,
        "mode": written.mode,
        "groups": written.groups,
        "truncated": written.truncated,
        "input_chars": written.input_chars,
        "used_chars": written.used_chars,
    }
