"""Jobs for a meetings page (a web front end with a Go backend): a recording turned into its transcript, timed
by the sentence, and key points, by this service. The Go backend already polls a job API shaped this way
(ParseResult schema 1.0, defined by its original implementation); this is the same API, so pointing the
backend's parser URL here is the switch, and pointing it back is the way back.

A job, while its status is "processing":
    queued        waiting for a slot here or for room in our transcription service's queue
    transcribing  in the transcription service, with sentence times and priority over bulk imports
    summarizing   the minutes, as /v1/minutes writes them, from the transcript's segments
and then "done" with the page's result (postprocess/meeting_result.py) or "failed" with a message in Chinese
the page shows. No minutes (too long a transcript, the model failing) still makes a result: the transcript and
the player are what the page is for, and the overview says why the key points are missing.

The Go backend gives up after 35 minutes, so a job fails here at 33, with a reason. Jobs are kept in SQLite:
the Go backend polls again after its own restart, and a job cut off by a restart here is taken up again if
there is time left for it, its transcription job polled rather than submitted again. Finished jobs are deleted
after 7 days. Nothing of a transcript or a link is logged: job ids and counts only.
"""

import asyncio
import contextlib
import json
import logging
import sqlite3
import time
import traceback
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ..postprocess import meeting_result
from ..postprocess import minutes as mm
from ..postprocess.providers import AsrResult, AsrSegment, AsrState, LlmProvider, ProviderBusy, ProviderError
from ..postprocess.summary import describe, try_later, write_minutes

log = logging.getLogger(__name__)

PROCESSING, DONE, FAILED = "processing", "done", "failed"
QUEUED, TRANSCRIBING, SUMMARIZING = "queued", "transcribing", "summarizing"
MAX_ERROR_CHARS = 500  # the page's column holds 512


class JobFailed(Exception):
    """A job's end, with the message the page shows."""


class NoMinutes(Exception):
    """Why a job has no key points; it still has its transcript."""


class JobStore:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        # One process, its event loop the only user: autocommit, no thread checks
        self._db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS meeting_jobs (
                job_id TEXT PRIMARY KEY, meeting_id INTEGER, title TEXT, audio_url TEXT, asr_task TEXT,
                status TEXT NOT NULL, phase TEXT, result TEXT, error TEXT,
                created_at REAL NOT NULL, finished_at REAL)"""
        )

    def close(self) -> None:
        self._db.close()

    def add(self, job_id: str, meeting_id: int, title: str | None, audio_url: str, now: float) -> bool:
        """False when the job is known already."""
        try:
            self._db.execute(
                "INSERT INTO meeting_jobs (job_id, meeting_id, title, audio_url, status, phase, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (job_id, meeting_id, title, audio_url, PROCESSING, QUEUED, now),
            )
        except sqlite3.IntegrityError:
            return False
        return True

    def get(self, job_id: str) -> sqlite3.Row | None:
        return self._db.execute("SELECT * FROM meeting_jobs WHERE job_id = ?", (job_id,)).fetchone()

    def phase(self, job_id: str, phase: str) -> None:
        self._db.execute(
            "UPDATE meeting_jobs SET phase = ? WHERE job_id = ? AND status = ?", (phase, job_id, PROCESSING)
        )

    def set_task(self, job_id: str, task_id: str | None) -> None:
        """The transcription service's job: polled again, not submitted again, after a restart here."""
        self._db.execute("UPDATE meeting_jobs SET asr_task = ? WHERE job_id = ?", (task_id, job_id))

    def finish(
        self, job_id: str, now: float, *, result: dict | None = None, error: str | None = None
    ) -> None:
        """Done with `result`, or failed with `error`; the audio link is not kept past the job."""
        self._db.execute(
            "UPDATE meeting_jobs SET status = ?, phase = NULL, result = ?, error = ?, audio_url = NULL,"
            " finished_at = ? WHERE job_id = ?",
            (
                FAILED if error is not None else DONE,
                None if result is None else json.dumps(result, ensure_ascii=False),
                None if error is None else error[:MAX_ERROR_CHARS],
                now,
                job_id,
            ),
        )

    def unfinished(self) -> list[sqlite3.Row]:
        return self._db.execute(
            "SELECT * FROM meeting_jobs WHERE status = ? ORDER BY created_at", (PROCESSING,)
        ).fetchall()

    def purge(self, before: float) -> int:
        return self._db.execute("DELETE FROM meeting_jobs WHERE finished_at < ?", (before,)).rowcount


class MeetingParser:
    """`asr`: our transcription service's client, asking for sentences and priority (WhisperJobsAsr).
    `llm`: the minutes model, made when first needed (RuntimeError when not configured). `minutes_slots`: the
    minutes API's own limit on requests at once, shared with it (the gateway fails several long ones), and
    `minutes_timeout` its limit on one request, so that a job holds a slot no longer than RAGFlow's do."""

    def __init__(
        self,
        store: JobStore,
        *,
        asr: Any,
        llm: Callable[[], LlmProvider],
        minutes_slots: asyncio.Semaphore,
        minutes_timeout: float = 600,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        jobs_at_once: int = 2,
        budget_seconds: float = 33 * 60,
        poll_seconds: float = 5,
        busy_seconds: float = 30,
        submit_tries: int = 3,
        poll_errors: int = 60,
        minutes_tries: int = 3,
        minutes_wait_seconds: float = 60,
        keep_seconds: float = 7 * 86400,
    ):
        self.store, self.asr, self._llm = store, asr, llm
        self._minutes_slots, self.minutes_timeout = minutes_slots, minutes_timeout
        self._clock, self._sleep = clock, sleep
        self._slots = asyncio.Semaphore(jobs_at_once)
        self.budget, self.poll_seconds, self.busy_seconds = budget_seconds, poll_seconds, busy_seconds
        self.submit_tries, self.poll_errors = submit_tries, poll_errors
        self.minutes_tries, self.minutes_wait = minutes_tries, minutes_wait_seconds
        self.keep_seconds = keep_seconds
        self._tasks: set[asyncio.Task] = set()

    # The API

    def submit(self, job_id: str, meeting_id: int, audio_url: str, title: str | None) -> None:
        """Start the job; a job id seen before is left as it is (the Go backend never posts one twice)."""
        self._purge()
        if self.store.add(job_id, meeting_id, title, audio_url, self._clock()):
            log.info("meeting job %s (meeting %d) queued", job_id, meeting_id)
            self._start(job_id)

    def status(self, job_id: str) -> dict[str, Any] | None:
        row = self.store.get(job_id)
        if row is None:
            return None
        result = json.loads(row["result"]) if row["result"] else None
        return {"status": row["status"], "phase": row["phase"], "result": result, "error": row["error"]}

    def resume(self) -> None:
        """At start: forget old jobs, take up those a restart cut off while there is time left for them."""
        self._purge()
        now = self._clock()
        for row in self.store.unfinished():
            if now >= row["created_at"] + self.budget:  # the page's backend has given up on it by now
                self._end(row["job_id"], error="解析服务重启，任务中断，请重新导入")
            else:
                log.info("meeting job %s taken up again", row["job_id"])
                self._start(row["job_id"])

    async def close(self) -> None:
        """Running jobs stop and stay "processing": the next start takes them up, polling their transcription
        job if it was submitted."""
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        with contextlib.suppress(Exception):
            await self.asr.aclose()
        self.store.close()

    # A job

    def _start(self, job_id: str) -> None:
        task = asyncio.create_task(self._run(job_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _purge(self) -> None:
        try:
            if purged := self.store.purge(self._clock() - self.keep_seconds):
                log.info(
                    "meeting jobs: %d finished more than %.0f days ago deleted",
                    purged,
                    self.keep_seconds / 86400,
                )
        except sqlite3.Error as exc:
            log.error("meeting jobs: deleting old ones failed (%s)", type(exc).__name__)

    def _end(self, job_id: str, *, result: dict | None = None, error: str | None = None) -> None:
        try:
            self.store.finish(job_id, self._clock(), result=result, error=error)
        except sqlite3.Error as exc:
            log.error("meeting job %s: storing how it ended failed (%s)", job_id, type(exc).__name__)

    async def _run(self, job_id: str) -> None:
        row = self.store.get(job_id)
        deadline = row["created_at"] + self.budget
        try:
            async with self._slots:
                if self._clock() >= deadline:
                    raise JobFailed("解析服务繁忙，排队等待超时")
                segments, task_id = await self._transcribe(job_id, row, deadline)
                result = await self._result(job_id, row, segments, task_id, deadline)
        except JobFailed as exc:
            log.warning("meeting job %s failed: %s", job_id, exc)
            self._end(job_id, error=str(exc))
        except Exception as exc:  # anything else: the job still ends, with the error's kind only
            # Its message could quote the transcript or the link: the class and where it was raised
            where = "".join(traceback.format_tb(exc.__traceback__))
            log.error("meeting job %s: unexpected %s\n%s", job_id, type(exc).__name__, where)
            self._end(job_id, error=f"解析失败（{describe(exc)}）")
        else:
            self._end(job_id, result=result)
            log.info("meeting job %s done: %d paragraphs", job_id, len(result["transcription"]["paragraphs"]))

    async def _transcribe(
        self, job_id: str, row: sqlite3.Row, deadline: float
    ) -> tuple[list[AsrSegment], str]:
        task_id = row["asr_task"]  # set when a restart here cut the job off during its transcription
        while True:
            if task_id is None:
                task_id = await self._submit(row, deadline)
                self.store.set_task(job_id, task_id)
            self.store.phase(job_id, TRANSCRIBING)
            try:
                result = await self._wait(task_id, deadline)
            except JobFailed:
                await self._discard(task_id)
                raise
            if result.state is not AsrState.LOST:
                break
            log.info("meeting job %s: the transcription service lost it, submitting again", job_id)
            task_id = None
            self.store.set_task(job_id, None)
            self.store.phase(job_id, QUEUED)
        await self._discard(task_id)
        if result.state is AsrState.FAILED:
            raise JobFailed(f"语音识别失败（{result.error}）")
        if not any(seg.text.strip() for seg in result.segments):
            raise JobFailed("录音里没有识别出说话内容")
        return result.segments, task_id

    async def _submit(self, row: sqlite3.Row, deadline: float) -> str:
        """A busy service (queue full, loading, down) is waited for; other errors (the audio link refused) are
        tried a few times."""
        errors = 0
        while True:
            if self._clock() >= deadline:
                raise JobFailed("语音识别服务一直很忙，等待超时")
            try:
                return await self.asr.submit(row["audio_url"], row["title"])
            except ProviderBusy:
                pass
            except ProviderError as exc:
                errors += 1
                if errors >= self.submit_tries:
                    raise JobFailed(f"提交语音识别失败（{exc}）") from None
            await self._sleep(self.busy_seconds)

    async def _wait(self, task_id: str, deadline: float) -> AsrResult:
        errors = 0
        while True:
            if self._clock() > deadline:
                raise JobFailed("语音识别超时")
            await self._sleep(self.poll_seconds)
            try:
                result = await self.asr.poll(task_id)
            except ProviderError as exc:
                errors += 1  # the service restarting, a moment's trouble: ask again, for a while
                if exc.permanent or errors >= self.poll_errors:
                    raise JobFailed(f"读取语音识别结果失败（{exc}）") from None
                continue
            errors = 0
            if result.state is not AsrState.RUNNING:
                return result

    async def _discard(self, task_id: str) -> None:
        with contextlib.suppress(Exception):  # it expires there in 24 h anyway
            await self.asr.discard(task_id)

    async def _result(
        self, job_id: str, row: sqlite3.Row, segments: list[AsrSegment], task_id: str, deadline: float
    ) -> dict[str, Any]:
        self.store.phase(job_id, SUMMARIZING)
        numbered, sources = mm.number_segments(
            [
                {"start_ms": s.start_ms, "end_ms": s.end_ms, "speaker": s.speaker, "text": s.text}
                for s in segments
            ]
        )
        columns, failure = None, None
        try:
            columns = await self._minutes(job_id, numbered, row["title"], deadline)
        except NoMinutes as exc:
            log.warning("meeting job %s: no minutes: %s", job_id, exc)
            failure = str(exc)
        return meeting_result.parse_result(
            segments, sources, columns, title=row["title"], task_id=task_id, failure=failure
        )

    async def _minutes(self, job_id: str, numbered: list[dict], title: str | None, deadline: float) -> dict:
        try:
            llm = self._llm()
        except RuntimeError:
            raise NoMinutes("纪要模型没有配置") from None
        loop = asyncio.get_running_loop()
        for attempt in range(1, self.minutes_tries + 1):
            until = loop.time() + max(0.0, deadline - self._clock())  # the job's deadline on the loop's clock
            try:
                async with asyncio.timeout_at(until):
                    await self._minutes_slots.acquire()
            except TimeoutError:
                raise NoMinutes("等待写纪要超时") from None
            error = None
            try:
                written = await write_minutes(
                    llm,
                    numbered,
                    title=mm.meeting_title(title),
                    deadline=min(until, loop.time() + self.minutes_timeout),
                    label=f"meeting job {job_id}",
                )
            except mm.TranscriptTooLong:
                raise NoMinutes("录音太长，纪要写不下") from None
            except TimeoutError:
                raise NoMinutes("写纪要超时") from None
            except Exception as exc:
                error = exc
            finally:
                self._minutes_slots.release()  # not held while waiting to ask again
            if error is None:
                return mm.as_columns(written.minutes)
            later = attempt < self.minutes_tries and self._clock() + self.minutes_wait < deadline
            if not (try_later(error) and later):
                raise NoMinutes(f"写纪要失败（{describe(error)}）")
            log.warning(
                "meeting job %s: minutes model unavailable (%s), again later", job_id, describe(error)
            )
            await self._sleep(self.minutes_wait)
        raise NoMinutes("写纪要失败")  # minutes_tries 0
