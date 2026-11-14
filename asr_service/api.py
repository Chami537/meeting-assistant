"""HTTP API of our transcription service (Qwen3-ASR by default, Fun-ASR as an option, see engine.py). It is
the job API of a faster-whisper service, so the backend's client
(backend/app/integrations/whisper.py) works with either:

    GET    /health/ready                    {status: loading | ready | failed | ..., model, device,
                                             queue_available, sentences}
    POST   /v1/audio/transcriptions/jobs    multipart file (+ prompt, language, hotwords, sentences, priority)
                                            -> 202 {id, status}
    GET    /v1/audio/transcriptions/jobs/ID {id, status: queued|running|completed|failed, segments, error}
    DELETE /v1/audio/transcriptions/jobs/ID {deleted: true}

A segment is {start, end (seconds), text, speaker ("发言人1", "发言人2", ...; null without speaker labels),
compression_ratio}.
A job the service does not know (it restarted) is 404; a full queue is 429 with Retry-After (RAGFlow's client
waits and tries again on 429 only, a 503 fails the document); a model still loading or a Qwen server that does
not answer is 503 (and not "ready" in the health report). The backend waits on either. `prompt` is the
context the Qwen engine passes on with every chunk: the meeting title (the backend and RAGFlow send it), then
the glossary's short list (glossary.py); `model`, which the Whisper client sends, is ignored.
`sentences=1` (the Qwen engine with its aligner loaded: `sentences` true in the health report) gives every
segment its sentences [{start, end, text}] with times of their own, for a page that plays a recording from the
sentence clicked; it adds about a minute per hour of audio, so only the page that plays recordings asks for
it.
`priority=1` puts the job before the others waiting: someone is looking at the page, while RAGFlow's imports
queue up to 24 recordings at a time.
No authentication: private network only.

    uvicorn asr_service.api:app --host 0.0.0.0 --port 8000      # settings from ASR_* variables, see README.md
"""

import asyncio
import logging
import os
import re
import uuid
from collections.abc import Callable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Protocol

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from .glossary import DEFAULT, recognizer_terms
from .jobs import Jobs, QueueFull

log = logging.getLogger(__name__)

JOBS = "/v1/audio/transcriptions/jobs"
LANGUAGES = {"zh": "中文", "en": "英文", "ja": "日文"}  # Fun-ASR-Nano-2512 knows these three
MAX_HOTWORDS, MAX_HOTWORD_CHARS, MAX_TITLE_CHARS, MAX_CONTEXT_CHARS = 50, 30, 200, 500
_CHUNK = 1 << 20


class Engine(Protocol):
    """Optionally also `healthy() -> bool`: whether what it relies on (the Qwen server) answers, and
    `sentences: bool`: whether it can time sentences."""

    def transcribe(self, audio: Path, options: dict[str, Any]) -> list[dict[str, Any]]: ...


def create_app(
    load_engine: Callable[[], Engine],
    *,
    workdir: Path,
    model: str,
    device: str,
    capacity: int = 4,
    keep_seconds: float = 24 * 3600,
    max_bytes: int = 1 << 30,
    retry_seconds: float = 30,
    terms: Sequence[str] = (),
) -> FastAPI:
    jobs = Jobs(capacity=capacity, keep_seconds=keep_seconds, retry_seconds=retry_seconds)
    state: dict[str, Any] = {"status": "loading", "engine": None}

    async def status() -> str:
        healthy = getattr(state["engine"], "healthy", None)
        if state["status"] == "ready" and healthy is not None and not await asyncio.to_thread(healthy):
            return "upstream unavailable"
        return state["status"]

    async def start() -> None:
        while True:  # a full card at boot must not leave the service failed until someone restarts it
            try:
                engine = await asyncio.to_thread(load_engine)
                break
            except Exception:
                log.exception("loading the models failed; trying again in %.0fs", retry_seconds)
                state["status"] = "failed"
                await asyncio.sleep(retry_seconds)
        state["status"], state["engine"] = "ready", engine
        await jobs.run(engine.transcribe)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        workdir.mkdir(parents=True, exist_ok=True)
        for leftover in workdir.glob("*.audio"):  # uploads of jobs lost in a restart
            leftover.unlink(missing_ok=True)
        task = asyncio.create_task(start())
        yield
        task.cancel()

    app = FastAPI(title="Transcription service", lifespan=lifespan)
    app.state.jobs = jobs

    @app.middleware("http")
    async def refuse_oversized_uploads(request: Request, call_next):
        """Before the multipart body is spooled to disk."""
        length = request.headers.get("content-length")
        if request.url.path == JOBS and length and length.isdigit() and int(length) > max_bytes + _CHUNK:
            return JSONResponse({"detail": "audio file too large"}, status_code=413)
        return await call_next(request)

    @app.get("/health/ready")
    async def ready() -> dict[str, Any]:
        return {
            "status": await status(),
            "model": model,
            "device": device,
            "queue_available": jobs.available,
            "sentences": bool(getattr(state["engine"], "sentences", False)),
        }

    @app.post(JOBS, status_code=202)
    async def submit(
        file: UploadFile,
        language: str | None = Form(None),
        hotwords: str | None = Form(None),
        model: str | None = Form(None),  # sent by the Whisper client: ignored
        prompt: str | None = Form(None),
        sentences: str | None = Form(None),
        priority: str | None = Form(None),
    ) -> dict[str, Any]:
        if (current := await status()) != "ready":
            raise HTTPException(503, f"not ready: {current}")
        if jobs.available == 0:
            raise _queue_full()
        options = {
            "language": _language(language),
            "hotwords": _hotwords(hotwords),
            "context": _context(prompt, terms),
            "sentences": _flag(sentences),
        }
        path = workdir / f"{uuid.uuid4().hex}.audio"
        try:
            await _save(file, path, max_bytes)
            job = jobs.add(path, options, priority=_flag(priority))
        except QueueFull:
            path.unlink(missing_ok=True)
            raise _queue_full() from None
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return job.public()

    @app.get(JOBS + "/{job_id}")
    async def poll(job_id: str) -> dict[str, Any]:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "Job not found")
        return job.public()

    @app.delete(JOBS + "/{job_id}")
    async def delete(job_id: str) -> dict[str, Any]:
        if not jobs.delete(job_id):
            raise HTTPException(404, "Job not found")
        return {"deleted": True}

    return app


def _queue_full() -> HTTPException:
    return HTTPException(429, "queue is full", headers={"Retry-After": "30"})


async def _save(file: UploadFile, path: Path, max_bytes: int) -> None:
    size = 0
    with path.open("wb") as out:
        while block := await file.read(_CHUNK):
            size += len(block)
            if size > max_bytes:
                raise HTTPException(413, "audio file too large")
            out.write(block)
    if size == 0:
        raise HTTPException(400, "empty audio file")


def _language(value: str | None) -> str:
    value = (value or "zh").strip()
    return LANGUAGES.get(value.lower(), value if value in LANGUAGES.values() else "中文")


def _context(value: str | None, terms: Sequence[str]) -> str | None:
    """The title on one line (at most MAX_TITLE_CHARS), then "术语：" and as many terms as fit
    MAX_CONTEXT_CHARS: "03-晨川科技20260318 术语：募投、国谈、…"."""
    context = " ".join((value or "").split())[:MAX_TITLE_CHARS]
    if terms:
        context = f"{context} 术语：" if context else "术语："
        for i, term in enumerate(terms):
            longer = context + ("、" if i else "") + term
            if len(longer) > MAX_CONTEXT_CHARS:
                break
            context = longer
    return context or None


def _flag(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _hotwords(value: str | None) -> list[str]:
    words = [w.strip() for w in re.split(r"[,，、\n]", value or "")]
    return [w[:MAX_HOTWORD_CHARS] for w in words if w][:MAX_HOTWORDS]


def _from_env() -> FastAPI:
    env = os.environ.get
    engine = env("ASR_ENGINE", "qwen")
    qwen = engine != "funasr"  # the offline stand-in takes the title and terms as Qwen does

    def load() -> Engine:
        if engine == "fake":  # offline mode: no GPU, no model (fake_engine.py)
            from .fake_engine import FakeEngine

            return FakeEngine(delay=float(env("ASR_FAKE_DELAY", "0")))
        from .engine import from_env  # torch and funasr: only in the service image

        return from_env()

    model = {"fake": "offline-fake-asr", "funasr": "fun-asr-nano-2512"}.get(engine, "qwen3-asr-0.6b")
    return create_app(
        load,
        workdir=Path(env("ASR_WORKDIR", "/tmp/asr-jobs")),
        model=model,
        device="cpu" if engine == "fake" else env("ASR_DEVICE", "cuda:0"),
        capacity=int(env("ASR_QUEUE", "32")),  # RAGFlow alone has up to 24 recordings in flight
        keep_seconds=float(env("ASR_KEEP_HOURS", "24")) * 3600,
        max_bytes=int(env("ASR_MAX_MB", "1024")) << 20,
        terms=recognizer_terms(Path(env("ASR_GLOSSARY") or DEFAULT)) if qwen else (),
    )


app = _from_env()
