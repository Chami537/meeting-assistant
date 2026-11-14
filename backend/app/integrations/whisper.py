"""Self-hosted ASR services with an asynchronous job API: a faster-whisper job service (ASR_PROVIDER=whisper)
and our own, asr_service/, running Qwen3-ASR (ASR_PROVIDER=qwen) or Fun-ASR (ASR_PROVIDER=funasr).

    GET    /health/ready                    status, model, device, queue_available
    POST   /v1/audio/transcriptions/jobs    multipart file + model + language (+ prompt) -> 202 {id, status}
    GET    /v1/audio/transcriptions/jobs/ID {status: queued | running | completed | failed, segments, error}
    DELETE /v1/audio/transcriptions/jobs/ID removes the job and its transcript from the server

The service cannot fetch a URL, so the presigned URL the jobs hand over is downloaded to a temporary file and
uploaded. Whisper does not separate speakers; ours labels each segment with a speaker ("发言人1", ...).
For Qwen the meeting title is sent as the prompt; our service passes it on as context with the glossary's
short list after it: names come out right (a company's name instead of a homophone, a project's instead of a
mishearing). Whisper only gets the configured prompt.
Our service can also time every sentence (`sentences`, for a page that plays a recording from the sentence
clicked) and put a job before those waiting (`priority`, someone waits for it); the other callers ask for
neither.
A service that is down, still loading or has a full queue is busy (the meeting waits, no attempt is used); a
job it no longer knows (it restarted) is LOST and submitted again. Calls to the service bypass any HTTP proxy
from the environment: it is on a private network. Error texts never quote response bodies, which may hold the
transcript.
"""

import logging
import tempfile
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import httpx

from ..postprocess.providers import (
    AsrResult,
    AsrSegment,
    AsrSentence,
    AsrState,
    ProviderBusy,
    ProviderError,
)

log = logging.getLogger(__name__)

JOBS = "/v1/audio/transcriptions/jobs"
MAX_AUDIO_BYTES = 1 << 30  # the jobs upload converted 16 kHz mono AAC: hours of audio are tens of MB
_BUSY_STATUS = {429, 503}
_QUEUED, _RUNNING, _DONE, _FAILED = "queued", "running", "completed", "failed"  # as observed on the service
# Whisper loops on noise: text that compresses this well is repetition (whisper's own threshold). Silence is
# already cut by the service's VAD, and in batched mode the confidence scores are shared by a whole ~30 s
# window, so they are not used to drop segments.
_MAX_COMPRESSION = 2.4
# Health, polls and deletes are small: a hung service must not hold the jobs loop for minutes.
_SHORT = httpx.Timeout(20, connect=10)
_TRANSFER = httpx.Timeout(connect=10, read=300, write=300, pool=10)  # audio download and upload


class WhisperJobsAsr:
    # Recognition runs at ~8-20x real time; a job still unfinished after this is stuck (a worker died).
    poll_deadline = timedelta(hours=6)

    def __init__(
        self,
        base_url: str,
        *,
        model: str,
        language: str | None = "zh",
        prompt: str | None = None,
        name: str = "whisper",  # recorded as the jobs' provider; "funasr" for asr_service/
        # The prompt made from the meeting title (Qwen: the title itself); Whisper did worse with one.
        context: Callable[[str | None], str | None] | None = None,
        sentences: bool = False,  # our service: timed sentences in every segment
        priority: bool = False,  # our service: before the jobs waiting
        http: httpx.AsyncClient | None = None,
    ):
        self.name = name
        self.context = context
        self._flags = {key: "1" for key, on in (("sentences", sentences), ("priority", priority)) if on}
        self._model = model
        self._language = language
        self._prompt = prompt
        base = httpx.URL(base_url.rstrip("/"))
        # NO_PROXY patterns such as 10.* are not understood by httpx: route the service itself directly.
        direct = {f"all://{base.netloc.decode()}": None}
        self._http = http or httpx.AsyncClient(base_url=base, timeout=_SHORT, mounts=direct)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def ready(self) -> dict:
        """The service's health report (model, device, queue). No answer, a server error or a status other
        than "ready" (loading the model after a restart) is ProviderBusy; a 4xx (wrong URL) is not."""
        try:
            data = await self._json("GET", "/health/ready", ok=(200,))
        except _NotFound:
            raise
        except ProviderError as exc:
            if isinstance(exc, ProviderBusy) or not _client_error(exc):
                raise ProviderBusy(f"not ready: {exc}") from None
            raise
        if data.get("status") != "ready":
            raise ProviderBusy(f"not ready: {str(data.get('status'))[:20]}")
        return data

    async def check_capacity(self) -> None:
        """Before any expensive work (merge, download): busy when the queue has no room."""
        if (await self.ready()).get("queue_available") == 0:
            raise ProviderBusy("queue full")

    async def submit(self, audio_url: str, title: str | None = None) -> str:
        """`title`, the meeting's, makes the prompt when this service takes a context."""
        await self.check_capacity()
        with tempfile.TemporaryDirectory(prefix="whisper-") as tmp:
            audio = Path(tmp) / "audio.m4a"
            await self._download(audio_url, audio)
            data = {"model": self._model, **self._flags}
            if self._language:
                data["language"] = self._language
            prompt = (self.context(title) if self.context else None) or self._prompt
            if prompt:
                data["prompt"] = prompt
            with audio.open("rb") as f:
                job = await self._json(
                    "POST",
                    JOBS,
                    ok=(200, 201, 202),
                    data=data,
                    files={"file": ("audio.m4a", f, "audio/mp4")},
                    timeout=_TRANSFER,
                )
        job_id = job.get("id")
        if not (isinstance(job_id, str) and job_id):
            raise ProviderError("submit returned no job id")
        return job_id

    async def poll(self, task_id: str) -> AsrResult:
        try:
            data = await self._json("GET", f"{JOBS}/{task_id}", ok=(200,))
        except _NotFound:
            return AsrResult(AsrState.LOST, error="job not found")
        status = data.get("status")
        if status in (_QUEUED, _RUNNING):
            return AsrResult(AsrState.RUNNING)
        if status == _FAILED:
            error = str(data.get("error") or "failed")[:180]
            return AsrResult(AsrState.FAILED, error=f"{self.name}: {error}")
        if status != _DONE:
            return AsrResult(AsrState.FAILED, error=f"{self.name}: status {str(status)[:20]}")
        raw = data.get("segments") or []
        try:
            segments = [_segment(s) for s in raw if not _repetition(s)]
        except (KeyError, TypeError, ValueError, AttributeError):
            # The same reply comes back on every poll: asking again does not help
            raise ProviderError("unexpected result shape", permanent=True) from None
        if len(segments) < len(raw):
            log.info(
                "%s job %s: %d of %d segments dropped as repetition",
                self.name,
                task_id,
                len(raw) - len(segments),
                len(raw),
            )
        return AsrResult(AsrState.DONE, segments=segments)  # not billed: no billed_seconds

    async def discard(self, task_id: str) -> None:
        """Remove the job and its transcript from the server once the result is stored here (or abandoned)."""
        try:
            await self._json("DELETE", f"{JOBS}/{task_id}", ok=(200, 204))
        except _NotFound:
            pass

    async def _download(self, url: str, dest: Path) -> None:
        size = 0
        try:
            async with self._http.stream("GET", url, timeout=_TRANSFER) as resp:
                if resp.status_code != 200:
                    raise ProviderError(f"audio download HTTP {resp.status_code}")
                with dest.open("wb") as f:
                    async for block in resp.aiter_bytes():
                        size += len(block)
                        if size > MAX_AUDIO_BYTES:
                            raise ProviderError("audio larger than the upload limit")
                        f.write(block)
        except httpx.HTTPError as exc:
            raise ProviderError(f"audio download: {type(exc).__name__}") from None

    async def _json(self, method: str, path: str, *, ok: tuple[int, ...], **kwargs) -> dict:
        kwargs.setdefault("timeout", _SHORT)  # per request: holds for an injected client too
        try:
            resp = await self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise ProviderError(f"transport: {type(exc).__name__}") from None
        if resp.status_code == 404:
            raise _NotFound()
        if resp.status_code in _BUSY_STATUS:
            raise ProviderBusy(f"HTTP {resp.status_code}")
        if resp.status_code not in ok:
            raise ProviderError(f"HTTP {resp.status_code}")
        if resp.status_code == 204 or not resp.content:
            return {}
        try:
            data = resp.json()
        except ValueError:
            raise ProviderError("response is not JSON") from None
        if not isinstance(data, dict):
            raise ProviderError("response is not a JSON object")
        return data


class _NotFound(ProviderError):
    def __init__(self) -> None:
        super().__init__("HTTP 404")


def _client_error(exc: ProviderError) -> bool:
    """HTTP 4xx: the request is wrong (a misconfigured URL), waiting will not fix it."""
    parts = str(exc).split()
    return len(parts) == 2 and parts[0] == "HTTP" and parts[1].startswith("4")


def _repetition(seg: dict) -> bool:
    ratio = seg.get("compression_ratio")
    return isinstance(ratio, (int, float)) and ratio > _MAX_COMPRESSION


def _segment(seg: dict) -> AsrSegment:
    speaker = seg.get("speaker")
    return AsrSegment(
        start_ms=round(float(seg["start"]) * 1000),
        end_ms=round(float(seg["end"]) * 1000),
        text=str(seg.get("text") or ""),
        speaker=None if speaker is None else str(speaker)[:20],
        sentences=_sentences(seg.get("sentences")),
    )


def _sentences(raw) -> tuple[AsrSentence, ...]:
    """A segment's timed sentences, or none at all when any of them is malformed (the segment's own times
    are used instead)."""
    if not isinstance(raw, list):
        return ()
    try:
        return tuple(
            AsrSentence(
                start_ms=round(float(s["start"]) * 1000),
                end_ms=round(float(s["end"]) * 1000),
                text=str(s.get("text") or ""),
            )
            for s in raw
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        return ()
