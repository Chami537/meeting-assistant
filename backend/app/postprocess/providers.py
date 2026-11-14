"""Interfaces to the external post-processing services. Real clients (Tencent Cloud ASR, the chosen LLM)
implement these; tests use fakes. Design doc §7.5."""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

REPLY_TRUNCATED = "reply truncated"  # the model hit its output cap; a runaway reply may not happen again


class ProviderError(Exception):
    """A provider failure whose message is safe to store: codes and counts, never transcript text.
    `permanent`: the same request would fail the same way (truncated, refused, rejected as invalid), so
    retrying only costs money."""

    def __init__(self, message: str, *, permanent: bool = False):
        super().__init__(message)
        self.permanent = permanent


class ProviderBusy(ProviderError):
    """The provider is at capacity and took nothing: try again later, without counting it as a failure."""


class AsrState(StrEnum):
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"
    LOST = "LOST"  # the provider no longer knows the task (a self-hosted service restarted): submit again


@dataclass(frozen=True)
class AsrSentence:
    start_ms: int
    end_ms: int
    text: str


@dataclass(frozen=True)
class AsrSegment:
    start_ms: int
    end_ms: int
    text: str
    speaker: str | None = None
    # Timed sentences of the segment, when the recognizer was asked for them (our service's aligner)
    sentences: tuple[AsrSentence, ...] = ()


@dataclass(frozen=True)
class AsrResult:
    state: AsrState
    segments: list[AsrSegment] = field(default_factory=list)
    billed_seconds: int | None = None
    error: str | None = None  # provider error code only, never transcript text


class AsrProvider(Protocol):
    """Optionally also `async def discard(task_id)`: delete the provider's copy once the result is stored."""

    name: str

    async def submit(self, audio_url: str, title: str | None = None) -> str:
        """Start recognition of one audio file; returns the provider's task id. Raises ProviderBusy when the
        provider is full and nothing was submitted. `title` is the meeting's, for a recognizer that takes
        context (it gets names right); the others ignore it."""
        ...

    async def poll(self, task_id: str) -> AsrResult: ...


@dataclass(frozen=True)
class LlmReply:
    content: dict[str, Any]
    input_tokens: int | None = None
    output_tokens: int | None = None


class LlmProvider(Protocol):
    name: str
    model: str
    region: str

    async def complete_json(self, *, system: str, user: str, schema: dict[str, Any]) -> LlmReply:
        """Return a JSON object that the provider was asked to shape after `schema`."""
        ...
