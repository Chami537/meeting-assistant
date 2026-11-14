"""Writing the minutes of one transcript: the request, the validation, asking once more when the minutes come
out thin, and proofreading them against the glossary (proofread.py). Shared by the summary job (jobs.py,
which retries and stores) and the minutes API (api/minutes_api.py, which answers RAGFlow), so both write the
same minutes."""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from . import minutes as mm
from .proofread import proofread
from .providers import REPLY_TRUNCATED, LlmProvider, ProviderError

log = logging.getLogger(__name__)


@dataclass
class Written:
    minutes: mm.Minutes
    input_tokens: int | None
    output_tokens: int | None


def describe(exc: Exception) -> str:
    """An error as it may be stored or logged. MinutesRejected and ProviderError messages carry codes and
    counts only; anything else is reduced to its type, since an arbitrary message may echo the prompt (the
    transcript) or a presigned URL."""
    if isinstance(exc, (mm.MinutesRejected, ProviderError)):
        return str(exc)[:200]
    return type(exc).__name__


def try_later(exc: Exception) -> bool:
    """The model's service failed, not this request: rate limited, a 5xx, unreachable. Sent again later, the
    same request may well succeed; a rejected or truncated reply, or a request the provider refused, would
    not."""
    return isinstance(exc, ProviderError) and not exc.permanent and str(exc) != REPLY_TRUNCATED


async def write_minutes(
    llm: LlmProvider, segments: list[dict[str, Any]], *, title: str | None, deadline: float, label: str
) -> Written:
    """Minutes of `segments` (numbered as the transcript stores them), every request bounded by `deadline`
    (loop time). The first request's error is raised (TranscriptTooLong before anything is sent); `label`
    names the request in the log."""
    prompt = mm.build_user_prompt(segments, title=title)

    async def ask() -> tuple[mm.Minutes, Any]:
        async with asyncio.timeout_at(deadline):
            reply = await llm.complete_json(
                system=mm.SYSTEM_PROMPT, user=prompt, schema=mm.MINUTES_JSON_SCHEMA
            )
        return mm.validate(reply.content, segments, title=title), reply

    minutes, reply = await ask()
    tokens = [reply.input_tokens, reply.output_tokens]
    if thin := mm.too_thin(minutes, segments):
        # Asked once more right away; the thin minutes are kept if that fails or writes no more, so a valid
        # reply is never thrown away for a later try that may not come.
        log.warning("%s: reply too thin (%s), asking once more", label, thin)
        try:
            again, second = await ask()
        except Exception as exc:
            log.warning("%s: second try failed: %s", label, describe(exc))
        else:
            tokens = [
                None if a is None and b is None else (a or 0) + (b or 0)  # a provider may not report usage
                for a, b in zip(tokens, (second.input_tokens, second.output_tokens), strict=True)
            ]
            if (mm.too_thin(again, segments) is None, len(again.cited())) > (False, len(minutes.cited())):
                minutes = again
    checked = await proofread(llm, minutes, segments, deadline=deadline, label=label)
    tokens = [
        None if a is None and b is None else (a or 0) + (b or 0) for a, b in zip(tokens, checked, strict=True)
    ]
    return Written(minutes, *tokens)
