"""task_effects execution: DISCARD_AUDIO deletes and keeps retrying, NOTIFY_USER gives up, claims are fenced."""

from datetime import timedelta

from app.domain.state_machine import Trigger, TriggerType
from app.services import effects
from app.services.effects import run_effects
from app.services.task_transitions import run_trigger
from factories import NOW, make_task
from meeting_protocol import TaskStatus
from sqlalchemy import text


class Storage:
    def __init__(self, fail=False):
        self.fail = fail
        self.prefixes: list[str] = []

    async def delete_prefix(self, prefix):
        if self.fail:
            raise ConnectionError("cos down")
        self.prefixes.append(prefix)


async def canceled_with_audio(engine):
    async with engine.begin() as conn:
        task_id = await make_task(conn, TaskStatus.RECORDING, has_audio=True, ready_segment=True)
    await run_trigger(engine, task_id, Trigger(TriggerType.CANCEL_REQUESTED), now=NOW, actor="test")
    return task_id


async def effect_rows(engine):
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT effect, status, attempts, next_attempt_at FROM task_effects ORDER BY next_attempt_at, id"
                )
            )
        ).all()


async def test_discard_audio_deletes_now_and_again_later(engine, clean):
    storage = Storage()
    task_id = await canceled_with_audio(engine)
    await run_effects(engine, now=NOW, storage=storage)
    assert storage.prefixes == [f"tasks/{task_id}/audio/"]
    async with engine.connect() as conn:
        statuses = (
            (await conn.execute(text("SELECT DISTINCT upload_status FROM audio_assets"))).scalars().all()
        )
    assert statuses == ["DELETED"]
    late = [r for r in await effect_rows(engine) if r.status == "PENDING"]
    assert [(r.effect, r.next_attempt_at) for r in late] == [("DISCARD_AUDIO", NOW + timedelta(minutes=35))]
    await run_effects(engine, now=NOW + timedelta(minutes=35), storage=storage)
    assert storage.prefixes == [f"tasks/{task_id}/audio/"] * 2


async def test_discard_audio_never_gives_up(engine, clean):
    storage = Storage(fail=True)
    await canceled_with_audio(engine)
    at = NOW
    for _ in range(effects.MAX_EFFECT_ATTEMPTS + 3):
        await run_effects(engine, now=at, storage=storage)
        at += timedelta(hours=1)
    discards = [r for r in await effect_rows(engine) if r.effect == "DISCARD_AUDIO"]
    assert all(r.status == "PENDING" for r in discards)
    assert max(r.attempts for r in discards) > effects.MAX_EFFECT_ATTEMPTS
    first = discards[-1]
    assert first.next_attempt_at - (at - timedelta(hours=1)) <= effects.MAX_BACKOFF


async def test_notice_gives_up_after_the_last_attempt(engine, clean, monkeypatch):
    async def broken(*args, **kwargs):
        raise RuntimeError("bug")

    monkeypatch.setattr(effects, "_notify", broken)
    async with engine.begin() as conn:
        task_id = await make_task(conn, TaskStatus.SCHEDULED)
    await run_trigger(engine, task_id, Trigger(TriggerType.CANCEL_REQUESTED), now=NOW, actor="test")
    at = NOW
    for _ in range(effects.MAX_EFFECT_ATTEMPTS):
        await run_effects(engine, now=at, storage=None)
        at += timedelta(hours=1)
    [row] = await effect_rows(engine)
    assert (row.effect, row.status, row.attempts) == ("NOTIFY_USER", "FAILED", effects.MAX_EFFECT_ATTEMPTS)


async def test_a_reclaimed_effect_is_not_overwritten(engine, clean, monkeypatch):
    storage = Storage()
    await canceled_with_audio(engine)
    original = effects._discard_audio

    async def slow(engine_, storage_, task_id, *, now):
        async with engine.begin() as conn:  # meanwhile the lease ran out and another runner claimed it
            await conn.execute(
                text("UPDATE task_effects SET attempts = attempts + 1 WHERE status = 'RUNNING'")
            )
        await original(engine_, storage_, task_id, now=now)

    monkeypatch.setattr(effects, "_discard_audio", slow)
    await run_effects(engine, now=NOW, storage=storage)
    async with engine.connect() as conn:
        claimed = (
            (
                await conn.execute(
                    text("SELECT status FROM task_effects WHERE effect = 'DISCARD_AUDIO' AND attempts = 2")
                )
            )
            .scalars()
            .all()
        )
    assert claimed == ["RUNNING"]  # left to its new owner
