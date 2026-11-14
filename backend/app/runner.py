"""Background loops (design doc §4). Run one process: `python -m app.runner` (from backend/).

Two loops run side by side so a slow merge or LLM call never holds up the 5-second work:
- core: scheduling, KF reconciliation pull, inbox, effects, outbox, retention purge;
- jobs: post-processing (ASR, minutes), one job at a time.
Every step takes the current time when it starts and is isolated from the others: a failing KF pull does not
stop deletions or notices. A session-level advisory lock keeps a second copy idle; it is re-checked every
tick, and if the lock session is gone the process exits so its supervisor restarts it into the wait.
Every step is also safe under concurrency on its own (row locks, SKIP LOCKED, claim fencing).
"""

import asyncio
import logging
import signal
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from .config import get_settings
from .db import get_engine
from .deps import keyring, object_storage, postprocessing, wecom_client
from .postprocess.jobs import PostProcessing, discard_abandoned, run_jobs
from .services.conversation import process_inbox
from .services.dispatch import scheduler_tick
from .services.effects import run_effects
from .services.kf_sync import sync_account
from .services.outbox import send_outbox
from .services.purge import purge

log = logging.getLogger("app.runner")

TICK = timedelta(seconds=5)
RECONCILE_EVERY = timedelta(minutes=10)  # callbacks are not guaranteed (official doc 90238)
CONFIG_WARN_EVERY = timedelta(minutes=30)  # a missing setting is reported, but not every tick
SWEEP_EVERY = timedelta(minutes=10)  # removal of provider copies left behind (discard_abandoned)
JOBS_PER_PASS = 20
_LOCK_KEY = "app.runner"


def _clock(now: datetime | None) -> Callable[[], datetime]:
    """Tests pass a fixed `now`; in production each step reads the clock when it starts."""
    return (lambda: now) if now is not None else (lambda: datetime.now(UTC))


_last_warned: dict[str, float] = {}
_last_sweep: float | None = None


def _warn_every(key: str, message: str, *args: object) -> None:
    """Log a standing problem (something not configured) once per CONFIG_WARN_EVERY instead of every tick."""
    at = time.monotonic()
    last = _last_warned.get(key)
    if last is None or at - last >= CONFIG_WARN_EVERY.total_seconds():
        _last_warned[key] = at
        log.error(message, *args)


async def _step(report: dict, name: str, work: Callable[[], Awaitable]) -> None:
    try:
        report[name] = await work()
    except Exception as exc:
        log.error("step %s failed: %s", name, type(exc).__name__)
        report[name] = "error"


async def run_once(*, now: datetime | None = None, reconcile: bool) -> dict:
    """One pass of the core loop (everything except post-processing jobs)."""
    settings = get_settings()
    engine, ring, clock = get_engine(), keyring(), _clock(now)
    storage = _storage_or_none()
    report: dict = {}
    await _step(report, "scheduler", lambda: scheduler_tick(engine, now=clock()))
    client = _wecom_or_none() if settings.wecom_corp_id else None
    if client is not None:
        if reconcile and settings.wecom_kf_open_kfid:
            await _step(
                report,
                "synced",
                lambda: sync_account(
                    engine,
                    client,
                    corp_id=settings.wecom_corp_id,
                    open_kfid=settings.wecom_kf_open_kfid,
                    now=clock(),
                    keyring=ring,
                ),
            )
        await _step(
            report,
            "inbox",
            lambda: process_inbox(
                engine, now=clock(), keyring=ring, corp_id=settings.wecom_corp_id, media=client
            ),
        )
    # Notices and deletions do not depend on WeChat being configured or reachable.
    await _step(report, "effects", lambda: run_effects(engine, now=clock(), storage=storage))
    if client is not None:
        await _step(
            report,
            "sent",
            lambda: send_outbox(engine, client, now=clock(), base_url=settings.public_base_url),
        )
    await _step(report, "purge", lambda: purge(engine, storage, now=clock()))
    return report


async def run_jobs_pass(pipeline: PostProcessing, *, now: datetime | None = None) -> int:
    """Up to JOBS_PER_PASS jobs, one claim at a time, so each one's lease starts when its work starts."""
    engine, clock = get_engine(), _clock(now)
    global _last_sweep
    if _last_sweep is None or time.monotonic() - _last_sweep >= SWEEP_EVERY.total_seconds():
        _last_sweep = time.monotonic()
        try:
            await discard_abandoned(engine, pipeline)
        except Exception as exc:  # cleanup must never hold up the jobs themselves
            log.error("discard sweep failed: %s", type(exc).__name__)
    handled = 0
    while handled < JOBS_PER_PASS and await run_jobs(engine, pipeline, now=clock(), limit=1):
        handled += 1
    return handled


def _postprocessing_or_none() -> PostProcessing | None:
    try:
        pipeline = postprocessing()
    except RuntimeError as exc:
        # COS, ASR or LLM not configured yet: jobs wait (the messages name settings, never their values).
        _warn_every("postprocessing", "post-processing jobs are waiting: %s", exc)
        return None
    if pipeline.media is None:  # picked up without a restart once installed
        _warn_every("ffmpeg", "ffmpeg/ffprobe not installed: recordings cannot be merged or converted")
    return pipeline


def _storage_or_none():
    try:
        return object_storage()
    except RuntimeError as exc:
        _warn_every("storage", "audio deletion and purge of stored objects are waiting: %s", exc)
        return None


def _wecom_or_none():
    try:
        return wecom_client()
    except RuntimeError as exc:
        # WECOM_CORP_ID without WECOM_APP_SECRET: skip the WeChat steps, keep deletions and notices going.
        _warn_every("wecom", "WeChat KF steps skipped: %s", exc)
        return None


async def _core_loop(stop: asyncio.Event, lock_alive: Callable[[], Awaitable[bool]]) -> None:
    last_reconcile = datetime.min.replace(tzinfo=UTC)
    while not stop.is_set():
        if not await lock_alive():
            log.error("advisory lock lost; exiting so the supervisor restarts the runner")
            stop.set()
            raise SystemExit(1)
        started = datetime.now(UTC)
        reconcile = started - last_reconcile >= RECONCILE_EVERY
        if reconcile:
            last_reconcile = started  # also after a failed pull: no retry storm every 5 s
        try:
            report = await run_once(reconcile=reconcile)
            if any(v for v in report.values() if isinstance(v, int) or v == "error") or any(
                isinstance(v, dict) and any(v.values()) for v in report.values()
            ):
                log.info("tick %s", report)
        except Exception as exc:
            log.error("tick failed: %s", type(exc).__name__)
        await _sleep(stop, TICK.total_seconds())


async def _jobs_loop(stop: asyncio.Event) -> None:
    while not stop.is_set():
        pipeline = _postprocessing_or_none()
        if pipeline is not None:
            try:
                if handled := await run_jobs_pass(pipeline):
                    log.info("processed %d jobs", handled)
            except Exception as exc:
                log.error("jobs pass failed: %s", type(exc).__name__)
        await _sleep(stop, TICK.total_seconds())


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    for name in ("httpx", "httpcore"):  # request URLs carry WeCom credentials
        logging.getLogger(name).setLevel(logging.WARNING)
    try:
        keyring()  # every KF and upload step needs it: refuse to start rather than fail every tick
    except RuntimeError as exc:  # names the missing settings
        log.error("runner cannot start: %s", exc)
        raise SystemExit(1) from None
    except Exception as exc:  # a malformed key setting: the message is not logged, it could quote a key
        log.error("runner cannot start: DATA_ENCRYPTION_KEYS is invalid (%s)", type(exc).__name__)
        raise SystemExit(1) from None
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    async with get_engine().connect() as lock_conn:
        while not stop.is_set():
            got = await lock_conn.scalar(text("SELECT pg_try_advisory_lock(hashtext(:k))"), {"k": _LOCK_KEY})
            await lock_conn.commit()
            if got:
                break
            log.info("another runner holds the lock; waiting")
            await _sleep(stop, 30)

        async def lock_alive() -> bool:
            try:
                held = await lock_conn.scalar(
                    text(
                        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted "
                        "AND pid = pg_backend_pid()"
                    )
                )
                await lock_conn.commit()
                return bool(held)
            except Exception:
                return False

        await asyncio.gather(_core_loop(stop, lock_alive), _jobs_loop(stop))


async def _sleep(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        pass


if __name__ == "__main__":
    asyncio.run(main())
