"""Post-processing jobs (design doc §7.5): recognition, minutes, retries, cancellation races."""

import asyncio
import logging
from datetime import timedelta

import pytest
from app.domain.state_machine import Trigger, TriggerType
from app.postprocess import jobs, proofread
from app.postprocess.jobs import PostProcessing, run_jobs
from app.postprocess.providers import (
    REPLY_TRUNCATED,
    AsrResult,
    AsrSegment,
    AsrState,
    LlmReply,
    ProviderBusy,
    ProviderError,
)
from app.services.dispatch import scheduler_tick
from app.services.task_transitions import run_trigger
from app.services.uploads import apply_and_settle
from factories import NOW, make_task
from meeting_protocol import FailureCode, TaskKind, TaskStatus
from sqlalchemy import text

T = TriggerType

SPEECH = [
    AsrSegment(0, 4000, "我们决定下周三上线。", "1"),
    AsrSegment(4000, 5000, "   ", "2"),  # blank: dropped, numbering stays dense
    AsrSegment(5000, 9000, "张三负责写测试，周五前完成。", "2"),
]
GOOD_MINUTES = {
    "summary": "确定上线时间并分配测试工作。",
    "decisions": [{"text": "下周三上线", "evidence_segments": [0]}],
    "action_items": [{"task": "写测试", "owner": "张三", "due": "周五", "evidence_segments": [1]}],
    "risks": [],
}


class Storage:
    async def presign_get(self, object_key, *, expires_at):
        return f"https://cos.example/{object_key}?get"


class FakeAsr:
    name = "fake_asr"

    def __init__(self):
        self.submitted: list[str] = []
        self.titles: list[str | None] = []
        self.polled: list[str] = []
        self.results: dict[str, AsrResult] = {}
        self.submit_errors = 0
        self.on_poll = None

    async def submit(self, audio_url, title=None):
        if self.submit_errors:
            self.submit_errors -= 1
            raise ConnectionError("provider down")
        self.submitted.append(audio_url)
        self.titles.append(title)
        return f"asr-{len(self.submitted)}"

    async def poll(self, task_id):
        self.polled.append(task_id)
        if self.on_poll:
            await self.on_poll()
        return self.results.get(task_id, AsrResult(AsrState.RUNNING))


class FakeLlm:
    name, model, region = "fake_llm", "fake-model", "ap-guangzhou"

    def __init__(self, *replies):
        self.replies = list(replies)
        self.prompts: list[str] = []
        self.proofread: list[str] = []
        self.fixes: list[dict] = []

    async def complete_json(self, *, system, user, schema):
        if schema is proofread.SCHEMA:  # the proofreading request after the minutes: nothing to fix
            self.proofread.append(user)
            return LlmReply({"fixes": self.fixes})
        self.prompts.append(user)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, LlmReply):
            return reply
        return LlmReply(reply, input_tokens=100, output_tokens=50)


class FakeMerger:
    def __init__(self):
        self.calls = []

    async def concat(self, sources, target):
        self.calls.append((sources, target))


def deps(asr=None, llm=None, merger=None, budget=10**9):
    return PostProcessing(
        asr=asr or FakeAsr(),
        llm=llm or FakeLlm(GOOD_MINUTES),
        storage=Storage(),
        merger=merger or FakeMerger(),
        asr_daily_audio_seconds=budget,
    )


async def transcribing_task(engine, *, kind=TaskKind.LIVE_MEETING, extra_segments=0, duration_ms=None):
    """A task that finished uploading, moved to TRANSCRIBING through the state machine (so the ASR job
    row is the one ENQUEUE_ASR writes)."""
    async with engine.begin() as conn:
        task_id = await make_task(
            conn, TaskStatus.UPLOADING, kind=kind, ready_segment=True, active_attempt=False
        )
        if duration_ms:
            await conn.execute(text("UPDATE audio_assets SET duration_ms = :d"), {"d": duration_ms})
        for i in range(extra_segments):
            # recorded earlier than the factory's segment, so it must come first in the merge
            await conn.execute(
                text(
                    """
                    INSERT INTO audio_assets (task_id, source, attempt_id, segment_no, object_key, file_sha256,
                      format, bytes, recorded_started_at, recorded_ended_at, retention_until, upload_status,
                      verified_at)
                    SELECT task_id, source, attempt_id, :n, :k, file_sha256, format, bytes,
                      :rs, :re, retention_until, 'READY', verified_at
                    FROM audio_assets WHERE task_id = :t AND segment_no = 1
                    """
                ),
                {
                    "t": task_id,
                    "n": 10 + i,
                    "k": f"tasks/{task_id}/audio/early-{i}",
                    "rs": NOW - timedelta(minutes=40 - i),
                    "re": NOW - timedelta(minutes=39 - i),
                },
            )
    await run_trigger(engine, task_id, Trigger(T.UPLOAD_COMPLETE), now=NOW, actor="test")
    return task_id


async def task_state(engine, task_id):
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text("SELECT status, failure_code FROM meeting_tasks WHERE id = :t"), {"t": task_id}
            )
        ).one()


async def job_rows(engine, task_id, kind):
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT status, attempts, external_job_id, last_error, next_poll_at, billed_audio_seconds, "
                    "input_tokens, output_tokens FROM processing_jobs WHERE task_id = :t AND kind = :k "
                    "ORDER BY input_version"
                ),
                {"t": task_id, "k": kind},
            )
        ).all()


async def scalar(engine, sql, **params):
    async with engine.connect() as conn:
        return await conn.scalar(text(sql), params)


async def test_meeting_goes_from_audio_to_minutes(engine, clean):
    asr = FakeAsr()
    d = deps(asr=asr)
    task_id = await transcribing_task(engine)
    key = await scalar(engine, "SELECT object_key FROM audio_assets WHERE task_id = :t", t=task_id)

    assert await run_jobs(engine, d, now=NOW) == 1
    assert asr.submitted == [f"https://cos.example/{key}?get"]  # one segment: no merge
    [job] = await job_rows(engine, task_id, "ASR")
    assert (job.status, job.attempts, job.external_job_id) == ("SUBMITTED", 1, "asr-1")

    assert await run_jobs(engine, d, now=NOW + timedelta(seconds=10)) == 0  # not due yet
    assert await run_jobs(engine, d, now=NOW + timedelta(seconds=30)) == 1  # still running
    asr.results["asr-1"] = AsrResult(AsrState.DONE, SPEECH, billed_seconds=9)
    await run_jobs(engine, d, now=NOW + timedelta(minutes=1))
    assert asr.submitted == [f"https://cos.example/{key}?get"]  # polled, never resubmitted
    assert (await task_state(engine, task_id)).status == TaskStatus.SUMMARIZING
    [job] = await job_rows(engine, task_id, "ASR")
    assert (job.status, job.billed_audio_seconds, job.next_poll_at) == ("SUCCEEDED", 9, None)
    body = await scalar(engine, "SELECT body FROM transcripts WHERE task_id = :t", t=task_id)
    assert [(s["i"], s["text"]) for s in body["segments"]] == [
        (0, "我们决定下周三上线。"),
        (1, "张三负责写测试，周五前完成。"),
    ]
    assert (
        await scalar(engine, "SELECT source FROM transcripts WHERE task_id = :t", t=task_id) == "worker_asr"
    )

    await run_jobs(engine, d, now=NOW + timedelta(minutes=1))
    assert (await task_state(engine, task_id)).status == TaskStatus.COMPLETED
    [job] = await job_rows(engine, task_id, "SUMMARY")
    assert (job.status, job.attempts, job.input_tokens, job.output_tokens) == ("SUCCEEDED", 1, 100, 50)
    async with engine.connect() as conn:
        minutes = (
            await conn.execute(
                text(
                    "SELECT summary, decisions, action_items, is_current, model_provider, model_name, "
                    "model_region, prompt_version, meeting_type, meeting_date, meeting_date_source "
                    "FROM meeting_minutes WHERE task_id = :t"
                ),
                {"t": task_id},
            )
        ).one()
    assert minutes.summary == GOOD_MINUTES["summary"]
    assert minutes.action_items[0]["owner"] == "张三"
    assert minutes.is_current
    assert (minutes.model_provider, minutes.model_name, minutes.model_region) == (
        "fake_llm",
        "fake-model",
        "ap-guangzhou",
    )
    assert minutes.prompt_version == "minutes-v3"
    # No type in the reply (another provider's): 其他. A live meeting: the date it was scheduled for.
    assert (minutes.meeting_type, minutes.meeting_date_source) == ("其他", "预约时间")
    assert minutes.meeting_date is not None
    notify = "SELECT count(*) FROM task_effects WHERE task_id = :t AND effect = 'NOTIFY_USER'"
    assert await scalar(engine, notify, t=task_id) == 1
    assert "[1] 00:00:05 2: 张三负责写测试" in d.llm.prompts[0]


async def test_segments_are_merged_in_recording_order(engine, clean):
    asr, merger = FakeAsr(), FakeMerger()
    task_id = await transcribing_task(engine, extra_segments=2)
    await run_jobs(engine, deps(asr=asr, merger=merger), now=NOW)
    [(sources, target)] = merger.calls
    assert sources[:2] == [f"tasks/{task_id}/audio/early-0", f"tasks/{task_id}/audio/early-1"]
    assert len(sources) == 3
    assert target == f"tasks/{task_id}/audio/merged-v1.m4a"
    assert asr.submitted == [f"https://cos.example/{target}?get"]


async def test_upload_task_transcript_source(engine, clean):
    asr = FakeAsr()
    task_id = await transcribing_task(engine, kind=TaskKind.USER_UPLOAD)
    await run_jobs(engine, deps(asr=asr), now=NOW)
    asr.results["asr-1"] = AsrResult(AsrState.DONE, SPEECH)
    await run_jobs(engine, deps(asr=asr), now=NOW + timedelta(minutes=1))
    assert (
        await scalar(engine, "SELECT source FROM transcripts WHERE task_id = :t", t=task_id) == "upload_asr"
    )


async def test_submit_errors_back_off_then_fail_the_task(engine, clean):
    asr = FakeAsr()
    asr.submit_errors = 5
    task_id = await transcribing_task(engine)
    d = deps(asr=asr)
    await run_jobs(engine, d, now=NOW)
    [job] = await job_rows(engine, task_id, "ASR")
    assert (job.status, job.attempts, job.last_error) == ("PENDING", 1, "ConnectionError")
    assert job.next_poll_at == NOW + timedelta(minutes=1)
    await run_jobs(engine, d, now=NOW + timedelta(minutes=1))
    [job] = await job_rows(engine, task_id, "ASR")
    assert (job.attempts, job.next_poll_at) == (2, NOW + timedelta(minutes=3))
    await run_jobs(engine, d, now=NOW + timedelta(minutes=3))
    [job] = await job_rows(engine, task_id, "ASR")
    assert (job.status, job.attempts) == ("FAILED", 3)
    assert tuple(await task_state(engine, task_id)) == (TaskStatus.FAILED, FailureCode.ASR_FAILED)
    assert asr.submitted == []


async def test_run_that_died_mid_submit_is_logged_and_bounded(engine, clean, caplog):
    asr = FakeAsr()
    task_id = await transcribing_task(engine)
    async with engine.begin() as conn:  # attempts committed, then the process died: no error recorded
        await conn.execute(text("UPDATE processing_jobs SET attempts = 1 WHERE task_id = :t"), {"t": task_id})
    with caplog.at_level(logging.WARNING, logger="app.postprocess.jobs"):
        await run_jobs(engine, deps(asr=asr), now=NOW)
    assert "may be billed twice" in caplog.text
    [job] = await job_rows(engine, task_id, "ASR")
    assert (job.status, job.attempts) == ("SUBMITTED", 2)

    other = await transcribing_task(engine)
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE processing_jobs SET attempts = 3 WHERE task_id = :t"), {"t": other})
    await run_jobs(engine, deps(asr=asr), now=NOW)
    assert tuple(await task_state(engine, other)) == (TaskStatus.FAILED, FailureCode.ASR_FAILED)
    assert len(asr.submitted) == 1


@pytest.mark.parametrize(
    ("result", "code"),
    [
        (AsrResult(AsrState.FAILED, error="AudioDecodeFailed"), FailureCode.ASR_FAILED),
        (AsrResult(AsrState.DONE, [AsrSegment(0, 1000, "  ")]), FailureCode.EMPTY_AUDIO),
        (AsrResult(AsrState.DONE, []), FailureCode.EMPTY_AUDIO),
    ],
)
async def test_recognition_outcomes_that_fail_the_task(engine, clean, result, code):
    asr = FakeAsr()
    task_id = await transcribing_task(engine)
    await run_jobs(engine, deps(asr=asr), now=NOW)
    asr.results["asr-1"] = result
    await run_jobs(engine, deps(asr=asr), now=NOW + timedelta(minutes=1))
    assert tuple(await task_state(engine, task_id)) == (TaskStatus.FAILED, code)
    [job] = await job_rows(engine, task_id, "ASR")
    assert job.status == "FAILED"
    assert await scalar(engine, "SELECT count(*) FROM transcripts WHERE task_id = :t", t=task_id) == 0


async def test_polling_ends_at_the_deadline_unless_the_result_is_there(engine, clean):
    asr = FakeAsr()
    task_id = await transcribing_task(engine)
    await run_jobs(engine, deps(asr=asr), now=NOW)
    await run_jobs(engine, deps(asr=asr), now=NOW + jobs.ASR_POLL_DEADLINE)  # still running: give up
    assert asr.polled == ["asr-1"]
    assert tuple(await task_state(engine, task_id)) == (TaskStatus.FAILED, FailureCode.ASR_FAILED)


async def test_a_result_that_finished_while_the_runner_was_down_is_kept(engine, clean):
    asr = SelfHostedAsr()
    asr.poll_deadline = timedelta(hours=6)
    task_id = await transcribing_task(engine)
    await run_jobs(engine, deps(asr=asr), now=NOW)
    asr.results["asr-1"] = AsrResult(AsrState.DONE, SPEECH)  # done minutes later; the runner slept 7 h
    await run_jobs(engine, deps(asr=asr), now=NOW + timedelta(hours=7))
    assert (await task_state(engine, task_id)).status == TaskStatus.SUMMARIZING


async def test_poll_errors_retry_at_the_poll_interval(engine, clean):
    asr = FakeAsr()

    async def broken():
        raise TimeoutError

    task_id = await transcribing_task(engine)
    await run_jobs(engine, deps(asr=asr), now=NOW)
    asr.on_poll = broken
    later = NOW + timedelta(minutes=1)
    await run_jobs(engine, deps(asr=asr), now=later)
    [job] = await job_rows(engine, task_id, "ASR")
    assert (job.status, job.last_error, job.next_poll_at) == (
        "SUBMITTED",
        "TimeoutError",
        later + timedelta(seconds=30),
    )


async def test_cancel_while_recognition_runs_drops_the_result(engine, clean):
    asr = FakeAsr()
    task_id = await transcribing_task(engine)
    await run_jobs(engine, deps(asr=asr), now=NOW)
    asr.results["asr-1"] = AsrResult(AsrState.DONE, SPEECH)

    async def cancel():  # the user cancels between the provider answering and the result being stored
        await run_trigger(engine, task_id, Trigger(T.CANCEL_REQUESTED), now=NOW, actor="test")

    asr.on_poll = cancel
    await run_jobs(engine, deps(asr=asr), now=NOW + timedelta(minutes=1))
    assert (await task_state(engine, task_id)).status == TaskStatus.CANCELED
    [job] = await job_rows(engine, task_id, "ASR")
    assert (job.status, job.last_error) == ("FAILED", "canceled")
    assert await scalar(engine, "SELECT count(*) FROM transcripts WHERE task_id = :t", t=task_id) == 0
    assert await scalar(engine, "SELECT count(*) FROM processing_jobs WHERE kind = 'SUMMARY'") == 0


async def test_job_of_a_task_that_moved_on_is_closed(engine, clean):
    asr = FakeAsr()
    task_id = await transcribing_task(engine)
    async with engine.begin() as conn:  # e.g. an operator failed the task by hand
        await conn.execute(
            text("UPDATE meeting_tasks SET status = 'FAILED', failure_code = 'INTERNAL_ERROR' WHERE id = :t"),
            {"t": task_id},
        )
    await run_jobs(engine, deps(asr=asr), now=NOW)
    [job] = await job_rows(engine, task_id, "ASR")
    assert (job.status, job.last_error) == ("FAILED", "task left TRANSCRIBING")
    assert asr.submitted == []


async def test_daily_budget_defers_submission(engine, clean):
    asr = FakeAsr()
    first = await transcribing_task(engine, duration_ms=3_600_000)
    await run_jobs(engine, deps(asr=asr, budget=7199), now=NOW)
    [job] = await job_rows(engine, first, "ASR")
    assert (job.status, job.billed_audio_seconds) == ("SUBMITTED", 3600)

    second = await transcribing_task(engine, duration_ms=3_600_000)
    await run_jobs(engine, deps(asr=asr, budget=7199), now=NOW)
    [job] = await job_rows(engine, second, "ASR")
    # the first job's hour counts against the rolling window
    assert (job.status, job.attempts, job.last_error) == ("PENDING", 0, "daily budget")
    assert job.next_poll_at == NOW + jobs.BUDGET_RECHECK
    await run_jobs(engine, deps(asr=asr, budget=7200), now=NOW + jobs.BUDGET_RECHECK)
    [job] = await job_rows(engine, second, "ASR")
    assert (job.status, job.billed_audio_seconds) == ("SUBMITTED", 3600)


async def summarizing_task(engine, asr_segments=SPEECH):
    asr = FakeAsr()
    task_id = await transcribing_task(engine)
    await run_jobs(engine, deps(asr=asr), now=NOW)
    asr.results["asr-1"] = AsrResult(AsrState.DONE, asr_segments)
    await run_jobs(engine, deps(asr=asr, llm=FakeLlm()), now=NOW + timedelta(seconds=30))
    assert (await task_state(engine, task_id)).status == TaskStatus.SUMMARIZING
    return task_id


async def test_bad_minutes_are_retried(engine, clean):
    task_id = await summarizing_task(engine)
    # one invented citation only costs its item; most of the reply invented is retried
    bad_citation = {**GOOD_MINUTES, "decisions": [{"text": "x", "evidence_segments": [7]}] * 3}
    llm = FakeLlm(bad_citation, GOOD_MINUTES)
    later = NOW + timedelta(minutes=1)
    await run_jobs(engine, deps(llm=llm), now=later)
    [job] = await job_rows(engine, task_id, "SUMMARY")
    assert (job.status, job.attempts, job.last_error) == (
        "PENDING",
        1,
        "3 of 4 items cite no segment or a missing one (decisions: [7])",
    )
    await run_jobs(engine, deps(llm=llm), now=later + timedelta(minutes=1))
    assert (await task_state(engine, task_id)).status == TaskStatus.COMPLETED


THIN = {"summary": "只有摘要。", "key_points": [{"text": "占位", "evidence_segments": [0]}]}
RICH = {
    **GOOD_MINUTES,
    "key_points": [{"text": f"观点{i}", "evidence_segments": [i % 2]} for i in range(3)],
}


@pytest.mark.parametrize(
    ("second", "kept", "tokens"),
    [
        (RICH, RICH, (200, 100)),  # asked again right away, the fuller reply is stored
        (THIN, THIN, (200, 100)),  # a meeting that really had nothing more to say still gets its minutes
        (RuntimeError("HTTP 502"), THIN, (100, 50)),  # the second try failing does not lose the first
        (ProviderError(REPLY_TRUNCATED), THIN, (100, 50)),
        (LlmReply(RICH), RICH, (100, 50)),  # no usage reported for the second try
    ],
)
async def test_thin_minutes_are_asked_again_within_the_same_try(
    engine, clean, monkeypatch, second, kept, tokens
):
    """A substantial meeting answered with a summary and next to nothing else is asked once more; the thin
    reply is a valid one, so it is stored if the second try fails or writes no more."""
    task_id = await summarizing_task(engine)
    monkeypatch.setattr(jobs.mm, "THIN_TRANSCRIPT_CHARS", 1)
    llm = FakeLlm(THIN, second)
    await run_jobs(engine, deps(llm=llm), now=NOW + timedelta(minutes=1))
    [job] = await job_rows(engine, task_id, "SUMMARY")
    assert (job.status, job.attempts, job.input_tokens, job.output_tokens) == ("SUCCEEDED", 1, *tokens)
    assert len(llm.prompts) == 2
    assert (await task_state(engine, task_id)).status == TaskStatus.COMPLETED
    summary = await scalar(engine, "SELECT summary FROM meeting_minutes WHERE task_id = :t", t=task_id)
    points = await scalar(engine, "SELECT key_points FROM meeting_minutes WHERE task_id = :t", t=task_id)
    shown = [p["text"] for p in kept["key_points"] if p["text"] != "占位"]  # the placeholder is not shown
    assert (summary, [p["text"] for p in points]) == (kept["summary"], shown)


async def test_llm_failures_end_in_llm_failed_without_leaking_the_transcript(engine, clean):
    task_id = await summarizing_task(engine)
    leak = RuntimeError("upstream said: 我们决定下周三上线")
    llm = FakeLlm(leak, {"summary": ""}, leak)
    at = NOW + timedelta(minutes=1)
    for _ in range(3):
        await run_jobs(engine, deps(llm=llm), now=at)
        at += timedelta(minutes=10)
    assert tuple(await task_state(engine, task_id)) == (TaskStatus.FAILED, FailureCode.LLM_FAILED)
    [job] = await job_rows(engine, task_id, "SUMMARY")
    assert (job.status, job.attempts, job.last_error) == ("FAILED", 3, "RuntimeError")
    assert await scalar(engine, "SELECT count(*) FROM meeting_minutes WHERE task_id = :t", t=task_id) == 0


async def test_permanent_llm_failure_is_not_retried(engine, clean):
    task_id = await summarizing_task(engine)
    llm = FakeLlm(ProviderError("HTTP 400 invalid_request_error", permanent=True), GOOD_MINUTES)
    await run_jobs(engine, deps(llm=llm), now=NOW + timedelta(minutes=1))
    assert tuple(await task_state(engine, task_id)) == (TaskStatus.FAILED, FailureCode.LLM_FAILED)
    [job] = await job_rows(engine, task_id, "SUMMARY")
    assert (job.status, job.attempts, job.last_error) == ("FAILED", 1, "HTTP 400 invalid_request_error")
    assert len(llm.prompts) == 1


@pytest.mark.parametrize(
    ("second", "outcome"), [("truncated", TaskStatus.FAILED), ("good", TaskStatus.COMPLETED)]
)
async def test_truncated_reply_gets_one_more_try(engine, clean, second, outcome):
    task_id = await summarizing_task(engine)
    truncated = ProviderError("reply truncated")
    llm = FakeLlm(truncated, truncated if second == "truncated" else GOOD_MINUTES, GOOD_MINUTES)
    at = NOW + timedelta(minutes=1)
    for _ in range(2):
        await run_jobs(engine, deps(llm=llm), now=at)
        at += timedelta(minutes=10)
    assert (await task_state(engine, task_id)).status == outcome
    [job] = await job_rows(engine, task_id, "SUMMARY")
    assert job.attempts == 2 and len(llm.prompts) == 2


async def test_transcript_too_long_fails_without_calling_the_model(engine, clean, monkeypatch):
    task_id = await summarizing_task(engine)
    monkeypatch.setattr(jobs.mm, "MAX_TRANSCRIPT_CHARS", 10)
    llm = FakeLlm()
    await run_jobs(engine, deps(llm=llm), now=NOW + timedelta(minutes=1))
    assert llm.prompts == []
    assert tuple(await task_state(engine, task_id)) == (TaskStatus.FAILED, FailureCode.LLM_FAILED)


async def test_cancel_during_summary_drops_the_minutes(engine, clean):
    task_id = await summarizing_task(engine)

    class CancelingLlm(FakeLlm):
        async def complete_json(self, **kw):
            await run_trigger(engine, task_id, Trigger(T.CANCEL_REQUESTED), now=NOW, actor="test")
            return await super().complete_json(**kw)

    await run_jobs(engine, deps(llm=CancelingLlm(GOOD_MINUTES)), now=NOW + timedelta(minutes=1))
    assert (await task_state(engine, task_id)).status == TaskStatus.CANCELED
    assert await scalar(engine, "SELECT count(*) FROM meeting_minutes WHERE task_id = :t", t=task_id) == 0
    assert await scalar(engine, "SELECT count(*) FROM transcripts WHERE task_id = :t", t=task_id) == 0


async def test_ingest_job_without_upload_tooling_waits_and_the_batch_goes_on(engine, clean):
    asr = FakeAsr()
    odd = await transcribing_task(engine)
    fine = await transcribing_task(engine)
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE processing_jobs SET kind = 'AUDIO_NORMALIZE' WHERE task_id = :t"), {"t": odd}
        )
    assert await run_jobs(engine, deps(asr=asr), now=NOW) == 2
    [job] = await job_rows(engine, odd, "AUDIO_NORMALIZE")
    assert (job.status, job.last_error) == ("PENDING", "upload processing not configured")
    [job] = await job_rows(engine, fine, "ASR")
    assert job.status == "SUBMITTED"


async def test_audio_longer_than_the_daily_budget_fails_at_once(engine, clean):
    task_id = await transcribing_task(engine, duration_ms=3 * 3_600_000)
    await run_jobs(engine, deps(budget=2 * 3600), now=NOW)
    assert tuple(await task_state(engine, task_id)) == (TaskStatus.FAILED, FailureCode.ASR_FAILED)
    [job] = await job_rows(engine, task_id, "ASR")
    assert (job.status, job.last_error) == ("FAILED", "audio exceeds the daily ASR budget")


async def test_budget_wait_is_bounded(engine, clean):
    task_id = await transcribing_task(engine, duration_ms=3_600_000)
    small = deps(budget=3600)
    async with engine.begin() as conn:  # the day's budget is already used up by another meeting
        await conn.execute(
            text(
                "INSERT INTO processing_jobs (task_id, kind, provider, input_version, status, external_job_id, "
                "submitted_at, billed_audio_seconds) VALUES (:t, 'ASR', 'fake_asr', 99, 'SUCCEEDED', 'e', :at, 3600)"
            ),
            {"t": task_id, "at": NOW},
        )
    await run_jobs(engine, small, now=NOW)
    assert (await task_state(engine, task_id)).status == TaskStatus.TRANSCRIBING
    at = NOW + jobs.BUDGET_RECHECK
    while at < NOW + jobs.BUDGET_MAX_WAIT:
        await run_jobs(engine, small, now=at)
        at += jobs.BUDGET_RECHECK
    # the other meeting's hour is still inside the rolling window when the wait cap is reached
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE processing_jobs SET submitted_at = :at WHERE input_version = 99"), {"at": at}
        )
    await run_jobs(engine, small, now=at)
    assert tuple(await task_state(engine, task_id)) == (TaskStatus.FAILED, FailureCode.ASR_FAILED)
    [job] = [j for j in await job_rows(engine, task_id, "ASR") if j.status != "SUCCEEDED"]
    assert job.last_error == "daily budget wait exceeded"


async def test_slow_merge_is_cut_off_inside_the_lease(engine, clean, monkeypatch):
    monkeypatch.setattr(jobs, "STEP_TIMEOUT", timedelta(milliseconds=50))

    class SlowMerger(FakeMerger):
        async def concat(self, sources, target):
            await asyncio.sleep(1)

    asr = FakeAsr()
    task_id = await transcribing_task(engine, extra_segments=1)
    await run_jobs(engine, deps(asr=asr, merger=SlowMerger()), now=NOW)
    [job] = await job_rows(engine, task_id, "ASR")
    assert (job.status, job.attempts, job.last_error) == ("PENDING", 1, "TimeoutError")
    assert asr.submitted == []


async def test_start_of_an_attempt_renews_the_lease(engine, clean):
    asr = FakeAsr()
    later = NOW + timedelta(minutes=5)
    task_id = await transcribing_task(engine)

    async def look():
        [job] = await job_rows(engine, task_id, "ASR")
        seen.append(job.next_poll_at)

    seen = []

    class Watching(FakeAsr):
        async def submit(self, audio_url, title=None):
            await look()
            return await super().submit(audio_url, title)

    async with engine.begin() as conn:  # claimed earlier by a batch: the renewal counts from the attempt
        await conn.execute(text("UPDATE processing_jobs SET next_poll_at = :n"), {"n": NOW})
    await run_jobs(engine, deps(asr=Watching()), now=later)
    assert seen == [later + jobs.CLAIM_LEASE]
    assert asr.submitted == []


async def test_nul_characters_never_reach_the_database(engine, clean):
    asr = FakeAsr()
    task_id = await transcribing_task(engine)
    await run_jobs(engine, deps(asr=asr), now=NOW)
    asr.results["asr-1"] = AsrResult(AsrState.DONE, [AsrSegment(0, 1000, "开会\x00了", "0")])
    await run_jobs(engine, deps(asr=asr), now=NOW + timedelta(minutes=1))
    body = await scalar(engine, "SELECT body FROM transcripts WHERE task_id = :t", t=task_id)
    assert body["segments"][0]["text"] == "开会了"
    llm = FakeLlm({**GOOD_MINUTES, "summary": "确定\x00上线", "decisions": [], "action_items": []})
    await run_jobs(engine, deps(llm=llm), now=NOW + timedelta(minutes=2))
    assert (
        await scalar(engine, "SELECT summary FROM meeting_minutes WHERE task_id = :t", t=task_id)
        == "确定上线"
    )


async def test_uploaded_recording_waits_for_its_upload(engine, clean):
    """No Worker and nothing sealed: the settle sweep must not call an uploaded recording empty."""
    async with engine.begin() as conn:
        waiting = await make_task(conn, TaskStatus.AWAITING_CONSENT, kind=TaskKind.USER_UPLOAD)
        uploading = await make_task(conn, TaskStatus.UPLOADING, kind=TaskKind.USER_UPLOAD, ready_segment=True)
        await conn.execute(
            text("UPDATE meeting_tasks SET consent_at = :n, consent_version = 'v1'"), {"n": NOW}
        )
        await apply_and_settle(
            conn, waiting, Trigger(T.CONSENT_GRANTED, consent_version="v1"), now=NOW, actor="test"
        )
    await scheduler_tick(engine, now=NOW - timedelta(minutes=1))  # before the upload deadline
    assert (await task_state(engine, waiting)).status == TaskStatus.UPLOADING
    assert (await task_state(engine, uploading)).status == TaskStatus.UPLOADING


# ---------------------------------------------------------------- self-hosted provider behaviour


class SelfHostedAsr(FakeAsr):
    """A provider with a queue (busy), that can forget tasks (restart) and keeps results until discarded."""

    name = "self_hosted"

    def __init__(self):
        super().__init__()
        self.busy = 0  # submit answers busy this many times
        self.full = 0  # check_capacity answers busy this many times
        self.discarded: list[str] = []

    async def check_capacity(self):
        if self.full:
            self.full -= 1
            raise ProviderBusy("queue full")

    async def submit(self, audio_url, title=None):
        if self.busy:
            self.busy -= 1
            raise ProviderBusy("queue full")
        return await super().submit(audio_url, title)

    async def discard(self, task_id):
        self.discarded.append(task_id)


async def test_busy_provider_waits_without_using_up_attempts(engine, clean):
    asr = SelfHostedAsr()
    asr.busy = jobs.MAX_ASR_ATTEMPTS + 2  # more busy answers than there are attempts
    task_id = await transcribing_task(engine)
    at = NOW
    for _ in range(jobs.MAX_ASR_ATTEMPTS + 2):
        await run_jobs(engine, deps(asr=asr), now=at)
        [job] = await job_rows(engine, task_id, "ASR")
        assert (job.status, job.attempts, job.last_error) == ("PENDING", 0, "provider busy")
        at += jobs.BUSY_RECHECK
    await run_jobs(engine, deps(asr=asr), now=at)
    [job] = await job_rows(engine, task_id, "ASR")
    assert (job.status, job.attempts, job.external_job_id) == ("SUBMITTED", 1, "asr-1")
    assert await scalar(engine, "SELECT provider FROM processing_jobs WHERE task_id = :t", t=task_id) == (
        "self_hosted"
    )


async def test_busy_wait_is_bounded(engine, clean):
    asr = SelfHostedAsr()
    asr.busy = 10**6
    task_id = await transcribing_task(engine)
    await run_jobs(engine, deps(asr=asr), now=NOW + jobs.BUSY_MAX_WAIT - timedelta(minutes=1))
    assert (await task_state(engine, task_id)).status == TaskStatus.TRANSCRIBING
    await run_jobs(engine, deps(asr=asr), now=NOW + jobs.BUSY_MAX_WAIT + jobs.BUSY_RECHECK)
    assert tuple(await task_state(engine, task_id)) == (TaskStatus.FAILED, FailureCode.ASR_FAILED)
    [job] = await job_rows(engine, task_id, "ASR")
    assert job.last_error == "provider busy too long"


async def test_full_queue_is_noticed_before_the_merge(engine, clean):
    asr, merger = SelfHostedAsr(), FakeMerger()
    asr.full = 1
    task_id = await transcribing_task(engine, extra_segments=1)  # two segments: a merge before the upload
    await run_jobs(engine, deps(asr=asr, merger=merger), now=NOW)
    assert merger.calls == [] and asr.submitted == []
    [job] = await job_rows(engine, task_id, "ASR")
    assert (job.status, job.attempts, job.last_error) == ("PENDING", 0, "provider busy")
    await run_jobs(engine, deps(asr=asr, merger=merger), now=NOW + jobs.BUSY_RECHECK)
    assert len(merger.calls) == 1 and len(asr.submitted) == 1


async def test_recognition_of_a_canceled_meeting_is_removed_from_the_provider(engine, clean):
    asr = SelfHostedAsr()
    task_id = await transcribing_task(engine)
    await run_jobs(engine, deps(asr=asr), now=NOW)
    await run_trigger(engine, task_id, Trigger(T.CANCEL_REQUESTED), now=NOW, actor="test")
    assert await run_jobs(engine, deps(asr=asr), now=NOW + jobs.ASR_POLL_INTERVAL) == 0  # nobody polls it
    assert await jobs.discard_abandoned(engine, deps(asr=asr)) == 1
    assert asr.discarded == ["asr-1"]
    [job] = await job_rows(engine, task_id, "ASR")
    assert (job.status, job.external_job_id) == ("FAILED", None)
    assert await jobs.discard_abandoned(engine, deps(asr=asr)) == 0  # done once
    assert await jobs.discard_abandoned(engine, deps(asr=FakeAsr())) == 0  # a provider that keeps nothing


async def test_stuck_recognition_hits_the_providers_own_deadline(engine, clean):
    asr = SelfHostedAsr()
    asr.poll_deadline = timedelta(hours=1)
    task_id = await transcribing_task(engine)
    await run_jobs(engine, deps(asr=asr), now=NOW)
    await run_jobs(engine, deps(asr=asr), now=NOW + timedelta(hours=1))
    assert tuple(await task_state(engine, task_id)) == (TaskStatus.FAILED, FailureCode.ASR_FAILED)
    [job] = await job_rows(engine, task_id, "ASR")
    assert (job.last_error, job.external_job_id) == ("result window passed", None)
    assert asr.discarded == ["asr-1"]


async def test_lost_task_is_submitted_again_and_its_result_discarded(engine, clean):
    asr = SelfHostedAsr()
    task_id = await transcribing_task(engine)
    await run_jobs(engine, deps(asr=asr), now=NOW)
    asr.results["asr-1"] = AsrResult(AsrState.LOST, error="job not found")  # the service restarted
    await run_jobs(engine, deps(asr=asr), now=NOW + jobs.ASR_POLL_INTERVAL)
    [job] = await job_rows(engine, task_id, "ASR")
    assert (job.status, job.attempts, job.external_job_id, job.last_error) == (
        "PENDING",
        1,
        None,
        "provider lost the task",
    )
    await run_jobs(engine, deps(asr=asr), now=NOW + jobs.ASR_POLL_INTERVAL)
    asr.results["asr-2"] = AsrResult(AsrState.DONE, SPEECH)
    await run_jobs(engine, deps(asr=asr), now=NOW + 2 * jobs.ASR_POLL_INTERVAL)
    assert (await task_state(engine, task_id)).status == TaskStatus.SUMMARIZING
    assert asr.discarded == ["asr-2"]  # the server's copy of the transcript is removed once stored here


async def test_lost_tasks_are_resubmitted_only_within_the_attempt_limit(engine, clean):
    asr = SelfHostedAsr()
    task_id = await transcribing_task(engine)
    at = NOW
    for n in range(1, jobs.MAX_ASR_ATTEMPTS + 1):
        await run_jobs(engine, deps(asr=asr), now=at)  # submit
        asr.results[f"asr-{n}"] = AsrResult(AsrState.LOST)
        at += jobs.ASR_POLL_INTERVAL
        await run_jobs(engine, deps(asr=asr), now=at)  # lost
    await run_jobs(engine, deps(asr=asr), now=at)
    assert tuple(await task_state(engine, task_id)) == (TaskStatus.FAILED, FailureCode.ASR_FAILED)
    assert len(asr.submitted) == jobs.MAX_ASR_ATTEMPTS


async def test_failed_recognition_is_discarded_too(engine, clean):
    asr = SelfHostedAsr()
    task_id = await transcribing_task(engine)
    await run_jobs(engine, deps(asr=asr), now=NOW)
    asr.results["asr-1"] = AsrResult(AsrState.FAILED, error="whisper: Invalid data")
    await run_jobs(engine, deps(asr=asr), now=NOW + jobs.ASR_POLL_INTERVAL)
    assert tuple(await task_state(engine, task_id)) == (TaskStatus.FAILED, FailureCode.ASR_FAILED)
    assert asr.discarded == ["asr-1"]


async def test_switching_provider_resubmits_tasks_in_flight(engine, clean):
    task_id = await transcribing_task(engine)
    await run_jobs(engine, deps(asr=FakeAsr()), now=NOW)  # submitted to fake_asr
    async with engine.begin() as conn:  # on its last allowed try
        await conn.execute(
            text("UPDATE processing_jobs SET attempts = :n WHERE task_id = :t"),
            {"n": jobs.MAX_ASR_ATTEMPTS, "t": task_id},
        )
    other = SelfHostedAsr()
    await run_jobs(engine, deps(asr=other), now=NOW + jobs.ASR_POLL_INTERVAL)
    assert other.polled == []  # never asked about a task id it did not issue
    [job] = await job_rows(engine, task_id, "ASR")
    assert (job.status, job.attempts, job.last_error) == (
        "PENDING",
        jobs.MAX_ASR_ATTEMPTS - 1,
        "submitted to fake_asr",
    )
    await run_jobs(engine, deps(asr=other), now=NOW + jobs.ASR_POLL_INTERVAL)
    [job] = await job_rows(engine, task_id, "ASR")
    assert (job.status, job.external_job_id) == ("SUBMITTED", "asr-1")  # the new provider got its try
    assert await scalar(engine, "SELECT provider FROM processing_jobs WHERE task_id = :t", t=task_id) == (
        "self_hosted"
    )


async def test_daily_budget_counts_each_provider_separately(engine, clean):
    task_id = await transcribing_task(engine, duration_ms=3_600_000)
    async with engine.begin() as conn:  # another provider used a whole day's hour
        await conn.execute(
            text(
                "INSERT INTO processing_jobs (task_id, kind, provider, input_version, status, external_job_id, "
                "submitted_at, billed_audio_seconds) VALUES (:t, 'ASR', 'tencent_asr', 99, 'SUCCEEDED', 'e', "
                ":at, 3600)"
            ),
            {"t": task_id, "at": NOW},
        )
    await run_jobs(engine, deps(asr=SelfHostedAsr(), budget=3600), now=NOW)
    [job] = [j for j in await job_rows(engine, task_id, "ASR") if j.status != "SUCCEEDED"]
    assert job.status == "SUBMITTED"


async def test_an_old_job_still_gets_a_busy_wait_the_first_time(engine, clean):
    asr = SelfHostedAsr()
    asr.full = 2
    task_id = await transcribing_task(engine)
    late = NOW + jobs.BUSY_MAX_WAIT + timedelta(hours=1)  # e.g. it waited on the daily budget all day
    await run_jobs(engine, deps(asr=asr), now=late)
    [job] = await job_rows(engine, task_id, "ASR")
    assert (job.status, job.last_error) == ("PENDING", "provider busy")
    await run_jobs(engine, deps(asr=asr), now=late + jobs.BUSY_RECHECK)  # busy again, and it was waiting
    assert tuple(await task_state(engine, task_id)) == (TaskStatus.FAILED, FailureCode.ASR_FAILED)


class FlakyDiscard(SelfHostedAsr):
    def __init__(self, failing=()):
        super().__init__()
        self.failing = set(failing)
        self.tried: list[str] = []

    async def discard(self, task_id):
        self.tried.append(task_id)
        if task_id in self.failing:
            raise ProviderError("transport: ConnectError")
        await super().discard(task_id)


async def test_a_copy_left_by_a_cancel_during_submit_is_swept_later(engine, clean):
    task_id = await transcribing_task(engine)
    asr = FlakyDiscard(failing={"asr-1"})

    async def submit_then_cancel(audio_url, title=None):
        task = await SelfHostedAsr.submit(asr, audio_url, title)
        await run_trigger(engine, task_id, Trigger(T.CANCEL_REQUESTED), now=NOW, actor="test")
        return task

    asr.submit = submit_then_cancel
    await run_jobs(engine, deps(asr=asr), now=NOW)
    [job] = await job_rows(engine, task_id, "ASR")
    assert (job.status, job.external_job_id) == (
        "FAILED",
        "asr-1",
    )  # the discard failed: the row keeps the id
    asr.failing.clear()
    assert await jobs.discard_abandoned(engine, deps(asr=asr)) == 1
    [job] = await job_rows(engine, task_id, "ASR")
    assert job.external_job_id is None


async def test_the_sweep_stops_at_a_failure_and_does_not_starve_the_rest(engine, clean):
    asr = FlakyDiscard()
    ids = []
    for _ in range(3):
        task_id = await transcribing_task(engine)
        await run_jobs(engine, deps(asr=asr), now=NOW)
        await run_trigger(engine, task_id, Trigger(T.CANCEL_REQUESTED), now=NOW, actor="test")
        ids.append(
            await scalar(engine, "SELECT external_job_id FROM processing_jobs WHERE task_id = :t", t=task_id)
        )
    first = await scalar(
        engine,
        "SELECT external_job_id FROM processing_jobs WHERE external_job_id IS NOT NULL ORDER BY updated_at LIMIT 1",
    )
    asr.failing = {first}  # this copy cannot be deleted (e.g. the service refuses)
    assert await jobs.discard_abandoned(engine, deps(asr=asr)) == 0
    assert asr.tried == [first]  # one failure ends the sweep: no storm against a provider that is down
    assert await jobs.discard_abandoned(engine, deps(asr=asr)) == 2  # the failed one went to the back
    assert set(asr.discarded) == set(ids) - {first}
