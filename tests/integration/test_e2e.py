"""End to end without external services: scheduled task -> dispatch -> simulated Worker records and uploads ->
recognition and minutes (fake providers) -> completed, user notified."""

import os
from datetime import timedelta

import httpx
from app import cli
from app.deps import clock, db_engine, keyring, object_storage
from app.main import app
from app.postprocess.jobs import run_jobs
from app.postprocess.providers import AsrResult, AsrSegment, AsrState
from app.security.keyring import Keyring
from app.services.dispatch import scheduler_tick
from app.services.uploads import ObjectStat, UploadTarget
from app.tools.sim_worker import SimWorker
from factories import NOW, make_task
from meeting_protocol import TaskStatus
from sqlalchemy import text
from test_postprocess import FakeAsr, FakeLlm, deps

RING = Keyring({1: os.urandom(32)}, 1)


class Bucket:
    """Presigns URLs on cos.example and receives the PUTs the simulated Worker sends there."""

    def __init__(self):
        self.objects: dict[str, ObjectStat] = {}

    async def presign_put(self, object_key, *, size, sha256, expires_at):
        url = f"https://cos.example/{object_key}?sig=1"
        return UploadTarget(object_key, url, expires_at, headers={"x-cos-meta-sha256": sha256})

    async def presign_get(self, object_key, *, expires_at):
        return f"https://cos.example/{object_key}?get=1"

    async def stat(self, object_key):
        return self.objects.get(object_key)

    async def delete(self, object_key):
        self.objects.pop(object_key, None)

    async def delete_prefix(self, prefix):
        for key in [k for k in self.objects if k.startswith(prefix)]:
            del self.objects[key]

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        key = request.url.path.lstrip("/")
        self.objects[key] = ObjectStat(len(request.content), request.headers["x-cos-meta-sha256"])
        return httpx.Response(200)


async def test_meeting_end_to_end(engine, clean):
    bucket = Bucket()
    app.dependency_overrides.update(
        {db_engine: lambda: engine, clock: lambda: NOW, keyring: lambda: RING, object_storage: lambda: bucket}
    )
    try:
        async with engine.begin() as conn:
            task_id = await make_task(conn, TaskStatus.SCHEDULED)
        token = await cli.add_worker(engine, "sim-01", "windows")
        assert await scheduler_tick(engine, now=NOW)  # DISPATCH_DUE

        api = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        storage = httpx.AsyncClient(transport=httpx.MockTransport(bucket.handler))
        async with api, storage:
            worker = SimWorker(api, storage, token, b"fake audio bytes", segment_ms=30_000)
            lease = await worker.lease()
            assert lease["task"]["task_id"] == str(task_id)
            await worker.attend(segments=2)
        assert worker.log[-1] == "LEFT"
        assert len(bucket.objects) == 2

        async with engine.connect() as conn:
            status = await conn.scalar(text("SELECT status FROM meeting_tasks WHERE id = :t"), {"t": task_id})
        assert status == TaskStatus.TRANSCRIBING

        asr = FakeAsr()
        pipeline = deps(
            asr=asr, llm=FakeLlm({"summary": "演示会议", "decisions": [], "action_items": [], "risks": []})
        )
        await run_jobs(engine, pipeline, now=NOW)
        assert asr.submitted and "merged-v1.m4a" in asr.submitted[0]  # two segments: merged first
        asr.results["asr-1"] = AsrResult(AsrState.DONE, [AsrSegment(0, 2000, "大家好，开始开会。", "0")])
        await run_jobs(engine, pipeline, now=NOW + timedelta(minutes=1))
        await run_jobs(engine, pipeline, now=NOW + timedelta(minutes=2))

        async with engine.connect() as conn:
            status, outcome = (
                await conn.execute(
                    text("SELECT status, recording_outcome FROM meeting_tasks WHERE id = :t"), {"t": task_id}
                )
            ).one()
            summary = await conn.scalar(
                text("SELECT summary FROM meeting_minutes WHERE task_id = :t"), {"t": task_id}
            )
            effects = (
                (
                    await conn.execute(
                        text("SELECT effect FROM task_effects WHERE task_id = :t"), {"t": task_id}
                    )
                )
                .scalars()
                .all()
            )
        assert (status, outcome, summary) == (TaskStatus.COMPLETED, "FULL", "演示会议")
        assert "NOTIFY_USER" in effects
    finally:
        app.dependency_overrides.clear()
