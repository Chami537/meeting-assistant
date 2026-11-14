"""Transcription jobs: one GPU, one job at a time, the rest wait in a bounded queue: jobs with priority (the
AI meeting page, where someone waits for the result) before the others (RAGFlow's imports), each in turn.

Jobs and transcripts live only in this process: a restart loses them and the backend, seeing 404 for its job,
submits the audio again. Uploaded audio is deleted as soon as its job ends (or is deleted); a finished job's
transcript is kept until the backend deletes it, or `keep_seconds` after it finished at the latest.
A job that fails unexpectedly (CUDA out of memory while others use the shared card) is tried again after
`retry_seconds`, 4x longer each time (30 s, 2 min, 8 min): a failed job fails the meeting for good. Errors
stored on a job or logged are fixed texts or exception class names, never transcript text."""

import asyncio
import itertools
import logging
import time
import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

QUEUED, RUNNING, COMPLETED, FAILED = "queued", "running", "completed", "failed"
MAX_ATTEMPTS = 4


class QueueFull(Exception):
    pass


class AudioError(Exception):
    """The uploaded file is not audio we can decode: the message is safe to show the client."""


class Unavailable(Exception):
    """What the engine relies on does not answer (the Qwen server): the job waits for it, `retry_seconds` at a
    time, without using an attempt. A failed job fails the meeting for good, and the server may be down for a
    while (stopped by someone, or not starting on a full card)."""


@dataclass
class Job:
    id: str
    audio: Path
    options: dict[str, Any]
    status: str = QUEUED
    finished_at: float | None = None
    seconds: float | None = None  # transcription time
    segments: list[dict[str, Any]] | None = None
    error: str | None = None
    attempts: int = 0
    priority: bool = False
    deleted: bool = field(default=False, repr=False)

    def public(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "status": self.status}
        if self.status == COMPLETED:
            out |= {"segments": self.segments, "seconds": self.seconds}
        elif self.status == FAILED:
            out["error"] = self.error
        return out


Transcribe = Callable[[Path, dict[str, Any]], list[dict[str, Any]]]


class Jobs:
    def __init__(
        self,
        *,
        capacity: int,
        keep_seconds: float,
        retry_seconds: float = 30,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.capacity = capacity
        self.keep_seconds = keep_seconds
        self.retry_seconds = retry_seconds
        self._clock = clock
        self._jobs: dict[str, Job] = {}
        # (0 with priority, else 1; the order put in; the job): a job is never compared
        self._queue: asyncio.PriorityQueue[tuple[int, int, Job]] = asyncio.PriorityQueue()
        self._order = itertools.count()
        self._running: Job | None = None  # still counted after it is deleted: the GPU is still on it

    @property
    def available(self) -> int:
        self.expire()
        queued = sum(j.status == QUEUED for j in self._jobs.values())
        return max(0, self.capacity - queued - (self._running is not None))

    def add(self, audio: Path, options: dict[str, Any], *, priority: bool = False) -> Job:
        """Queue a job for an audio file the caller has saved (it now belongs to the job). QueueFull when
        there is no room: the file is not taken."""
        if self.available == 0:
            raise QueueFull
        job = Job(uuid.uuid4().hex, audio, options, priority=priority)
        self._jobs[job.id] = job
        self._put(job)
        return job

    def _put(self, job: Job) -> None:
        self._queue.put_nowait((0 if job.priority else 1, next(self._order), job))

    def get(self, job_id: str) -> Job | None:
        self.expire()
        return self._jobs.get(job_id)

    def delete(self, job_id: str) -> bool:
        """Forget a job. A queued one's audio goes now; a running one finishes and its result is dropped."""
        job = self._jobs.pop(job_id, None)
        if job is None:
            return False
        job.deleted = True
        if job.status == QUEUED:
            job.audio.unlink(missing_ok=True)
        return True

    def expire(self) -> None:
        now = self._clock()
        for job in [j for j in self._jobs.values() if j.finished_at is not None]:
            if now - job.finished_at > self.keep_seconds:
                self._jobs.pop(job.id, None)

    async def run(self, transcribe: Transcribe) -> None:
        """Work through the queue forever, one job at a time; the blocking model call runs in a thread."""
        while True:
            *_, job = await self._queue.get()
            if job.deleted:
                continue
            job.status, job.attempts, self._running = RUNNING, job.attempts + 1, job
            started = self._clock()
            try:
                job.segments = await asyncio.to_thread(transcribe, job.audio, job.options)
                job.status = COMPLETED
            except AudioError as exc:
                job.status, job.error = FAILED, str(exc)
            except Unavailable as exc:
                log.warning("job %s waits: %s", job.id, type(exc).__name__)
                if job.deleted:  # nobody waits for it: end it, so its audio goes
                    job.status, job.error = FAILED, "deleted"
                else:
                    job.status, job.attempts = QUEUED, job.attempts - 1
                    asyncio.get_running_loop().call_later(self.retry_seconds, self._put, job)
            except Exception as exc:  # the next job must still run
                # The message could quote the audio's text: log the class and where it was raised
                where = "".join(traceback.format_tb(exc.__traceback__))
                log.error("job %s attempt %d: %s\n%s", job.id, job.attempts, type(exc).__name__, where)
                if job.attempts < MAX_ATTEMPTS and not job.deleted:
                    job.status = QUEUED
                    delay = self.retry_seconds * 4 ** (job.attempts - 1)
                    asyncio.get_running_loop().call_later(delay, self._put, job)
                else:
                    job.status, job.error = FAILED, f"transcription failed ({type(exc).__name__})"
            finally:
                self._running = None
                if job.status != QUEUED:
                    job.audio.unlink(missing_ok=True)
                    job.finished_at = self._clock()
                    job.seconds = round(job.finished_at - started, 1)
            if job.status == QUEUED:
                continue
            if job.deleted:
                job.segments = None
            log.info(
                "job %s %s in %.1fs, %d segments", job.id, job.status, job.seconds, len(job.segments or [])
            )
