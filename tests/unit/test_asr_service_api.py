"""The Fun-ASR service's job API, with a fake engine in place of the models. The backend's client for it is
tested in test_whisper.py; here: queueing, the job lifecycle, cleanup of uploaded audio, and failures."""

import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from asr_service.api import JOBS, create_app
from asr_service.jobs import AudioError, Jobs

SEGMENT = {"start": 0.0, "end": 1.5, "text": "收入3529万元", "speaker": "发言人1", "compression_ratio": 1.1}
AUDIO = b"\x00\x00\x00\x20ftypM4A " + b"a" * 100


class FakeEngine:
    def __init__(self):
        self.calls: list[tuple[bytes, dict]] = []
        self.gate = threading.Event()
        self.gate.set()
        self.fail: Exception | None = None
        self.fail_once = False

    def transcribe(self, audio: Path, options: dict) -> list[dict]:
        self.calls.append((audio.read_bytes(), options))
        assert self.gate.wait(5)
        if self.fail is not None:
            fail, self.fail = self.fail, None if self.fail_once else self.fail
            raise fail
        return [SEGMENT]


def wait_for(check, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


@contextmanager
def service(tmp_path, engine=None, load=None, **kw):
    engine = engine or FakeEngine()
    kw.setdefault("retry_seconds", 0.01)
    app = create_app(
        load or (lambda: engine), workdir=tmp_path / "jobs", model="fun-asr-nano-2512", device="cpu", **kw
    )
    with TestClient(app) as client:
        if load is None:
            wait_for(lambda: client.get("/health/ready").json()["status"] == "ready")
        yield client


def submit(client, data=None, audio=AUDIO):
    return client.post(JOBS, files={"file": ("audio.m4a", audio, "audio/mp4")}, data=data or {"model": "m"})


def status(client, job_id):
    return client.get(f"{JOBS}/{job_id}").json()["status"]


def audio_files(tmp_path) -> list[Path]:
    return list((tmp_path / "jobs").glob("*.audio"))


def test_a_job_runs_and_its_transcript_is_kept_until_deleted(tmp_path):
    engine = FakeEngine()
    with service(tmp_path, engine) as client:
        health = client.get("/health/ready").json()
        assert health == {
            "status": "ready",
            "model": "fun-asr-nano-2512",
            "device": "cpu",
            "queue_available": 4,
            "sentences": False,  # this engine cannot time sentences
        }
        reply = submit(
            client, {"model": "faster-whisper-large-v3", "language": "zh", "hotwords": "晨川科技，北辰一号"}
        )
        assert reply.status_code == 202 and reply.json()["status"] == "queued"
        job_id = reply.json()["id"]
        wait_for(lambda: status(client, job_id) == "completed")
        done = client.get(f"{JOBS}/{job_id}").json()
        assert done["segments"] == [SEGMENT] and "error" not in done
        assert engine.calls == [
            (
                AUDIO,
                {"language": "中文", "hotwords": ["晨川科技", "北辰一号"], "context": None, "sentences": False},
            )
        ]
        assert audio_files(tmp_path) == []  # the upload went as soon as the job ended
        assert client.delete(f"{JOBS}/{job_id}").json() == {"deleted": True}
        assert client.get(f"{JOBS}/{job_id}").status_code == 404
        assert client.delete(f"{JOBS}/{job_id}").status_code == 404


def test_a_full_queue_is_429_and_keeps_no_upload(tmp_path):
    engine = FakeEngine()
    engine.gate.clear()
    with service(tmp_path, engine, capacity=1) as client:
        first = submit(client).json()["id"]
        wait_for(lambda: status(client, first) == "running")
        assert client.get("/health/ready").json()["queue_available"] == 0
        full = submit(client)
        assert full.status_code == 429 and full.json() == {"detail": "queue is full"}
        assert full.headers["retry-after"] == "30"  # RAGFlow's client waits on 429, fails on 503
        assert len(audio_files(tmp_path)) == 1  # only the running job's
        engine.gate.set()
        wait_for(lambda: status(client, first) == "completed")
        assert submit(client).status_code == 202


def test_deleting_a_queued_job_drops_its_audio_and_it_never_runs(tmp_path):
    engine = FakeEngine()
    engine.gate.clear()
    with service(tmp_path, engine) as client:
        running = submit(client).json()["id"]
        wait_for(lambda: status(client, running) == "running")
        queued = submit(client, audio=b"second").json()["id"]
        assert status(client, queued) == "queued" and len(audio_files(tmp_path)) == 2
        assert client.delete(f"{JOBS}/{queued}").status_code == 200
        assert len(audio_files(tmp_path)) == 1
        client.delete(f"{JOBS}/{running}")  # mid-run: it finishes, its result is dropped
        engine.gate.set()
        third = submit(client, audio=b"third").json()["id"]
        wait_for(lambda: status(client, third) == "completed")
        assert [audio for audio, _ in engine.calls] == [AUDIO, b"third"]
        assert client.get(f"{JOBS}/{running}").status_code == 404
        assert audio_files(tmp_path) == []


def test_failures_keep_no_audio_text_and_the_next_job_runs(tmp_path, caplog):
    engine = FakeEngine()
    with service(tmp_path, engine) as client:
        engine.fail = AudioError("audio could not be decoded")
        bad = submit(client).json()["id"]
        wait_for(lambda: status(client, bad) == "failed")
        assert client.get(f"{JOBS}/{bad}").json()["error"] == "audio could not be decoded"
        engine.fail = RuntimeError("我们决定下周三上线")  # could hold transcript text
        crashed = submit(client).json()["id"]
        wait_for(lambda: status(client, crashed) == "failed")
        reply = client.get(f"{JOBS}/{crashed}").json()
        assert reply["error"] == "transcription failed (RuntimeError)" and "segments" not in reply
        assert len(engine.calls) == 5  # the crash was tried four times
        engine.fail = None
        good = submit(client).json()["id"]
        wait_for(lambda: status(client, good) == "completed")
        assert audio_files(tmp_path) == []
    assert "RuntimeError" in caplog.text and "下周三" not in caplog.text  # logs never quote the audio


def test_a_passing_failure_is_tried_once_more(tmp_path):
    engine = FakeEngine()
    engine.fail, engine.fail_once = RuntimeError("CUDA out of memory"), True
    with service(tmp_path, engine) as client:
        job_id = submit(client).json()["id"]
        wait_for(lambda: status(client, job_id) == "completed")
        assert [audio for audio, _ in engine.calls] == [AUDIO, AUDIO]  # the upload was kept for the retry
        assert audio_files(tmp_path) == []


def test_a_deleted_running_job_keeps_its_place_until_it_ends(tmp_path):
    """The GPU is still busy with it: accepting another job would put one more in line than the capacity."""
    engine = FakeEngine()
    engine.gate.clear()
    with service(tmp_path, engine, capacity=1) as client:
        running = submit(client).json()["id"]
        wait_for(lambda: status(client, running) == "running")
        client.delete(f"{JOBS}/{running}")
        assert client.get("/health/ready").json()["queue_available"] == 0
        assert submit(client).status_code == 429
        engine.gate.set()
        wait_for(lambda: client.get("/health/ready").json()["queue_available"] == 1)


def test_not_ready_while_loading_or_after_a_failed_load(tmp_path):
    loaded = threading.Event()

    def slow_load():
        assert loaded.wait(5)
        return FakeEngine()

    with service(tmp_path, load=slow_load) as client:
        assert client.get("/health/ready").json()["status"] == "loading"
        assert submit(client).json() == {"detail": "not ready: loading"}
        loaded.set()
        wait_for(lambda: client.get("/health/ready").json()["status"] == "ready")

    attempts = []

    def flaky_load():  # the shared card was full at boot, then had room
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("CUDA out of memory")
        return FakeEngine()

    with service(tmp_path, load=flaky_load, retry_seconds=0.2) as client:
        wait_for(lambda: client.get("/health/ready").json()["status"] == "failed")
        assert submit(client).status_code == 503
        wait_for(lambda: client.get("/health/ready").json()["status"] == "ready")
        assert len(attempts) == 3 and submit(client).status_code == 202
    assert audio_files(tmp_path) == []


def test_bad_uploads_are_refused_and_leave_nothing(tmp_path):
    with service(tmp_path, max_bytes=50) as client:
        assert submit(client).status_code == 413  # 108 bytes
        assert submit(client, audio=b"").status_code == 400
        assert client.post(JOBS, data={"model": "m"}).status_code == 422  # no file
    assert audio_files(tmp_path) == []


def test_uploads_left_by_a_restart_are_removed_at_start(tmp_path):
    (tmp_path / "jobs").mkdir()
    (tmp_path / "jobs" / "lost.audio").write_bytes(b"x")
    with service(tmp_path):
        assert audio_files(tmp_path) == []


@pytest.mark.parametrize(
    ("language", "expected"),
    [(None, "中文"), ("zh", "中文"), ("EN", "英文"), ("日文", "日文"), ("fr", "中文")],
)
def test_language_codes(tmp_path, language, expected):
    engine = FakeEngine()
    with service(tmp_path, engine) as client:
        job_id = submit(client, {"language": language} if language else None).json()["id"]
        wait_for(lambda: status(client, job_id) == "completed")
    assert engine.calls[0][1]["language"] == expected


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        (None, None),
        ("", None),
        ("  \n ", None),
        (" 03-晨川科技\n20260318 ", "03-晨川科技 20260318"),
        ("会" * 900, "会" * 200),  # a title, not a document
    ],
)
def test_the_prompt_is_the_context_on_one_line_and_capped(tmp_path, prompt, expected):
    engine = FakeEngine()
    with service(tmp_path, engine) as client:
        job_id = submit(client, {"prompt": prompt} if prompt is not None else None).json()["id"]
        wait_for(lambda: status(client, job_id) == "completed")
    assert engine.calls[0][1]["context"] == expected


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        ("03-晨川科技20260318", "03-晨川科技20260318 术语：募投、IR"),
        (None, "术语：募投、IR"),  # no title: the terms still help
    ],
)
def test_the_glossary_follows_the_title(tmp_path, prompt, expected):
    engine = FakeEngine()
    with service(tmp_path, engine, terms=["募投", "IR"]) as client:
        job_id = submit(client, {"prompt": prompt} if prompt is not None else None).json()["id"]
        wait_for(lambda: status(client, job_id) == "completed")
    assert engine.calls[0][1]["context"] == expected


def test_the_context_stops_at_a_whole_term():
    from asr_service.api import MAX_CONTEXT_CHARS, _context

    terms = [f"词{i:03d}" for i in range(200)]
    context = _context("标" * 300, terms)
    assert len(context) <= MAX_CONTEXT_CHARS
    assert context.startswith("标" * 200 + " 术语：词000、") and context.endswith(tuple(terms))


def test_the_shipped_glossary_gives_the_recognizer_its_short_list():
    from asr_service.glossary import DEFAULT, recognizer_terms

    terms = recognizer_terms(DEFAULT)
    assert "募投" in terms and "IR" in terms and "CPO" in terms  # the spoken spelling, not 投资者关系
    assert "投资者关系" not in terms and "资本开支" in terms and "毛利率" not in terms
    assert "新质算法" not in terms and "存算一体" not in terms  # rejected, not reviewed yet


def test_finished_jobs_expire_and_only_active_ones_take_room(tmp_path):
    now = [0.0]
    jobs = Jobs(capacity=2, keep_seconds=60, clock=lambda: now[0])
    first = jobs.add(tmp_path / "a.audio", {})
    jobs.add(tmp_path / "b.audio", {})
    assert jobs.available == 0
    first.status, first.finished_at = "completed", 0.0
    assert jobs.available == 1 and jobs.get(first.id) is first
    now[0] = 61.0
    assert jobs.get(first.id) is None


def test_an_unreachable_qwen_server_makes_the_service_not_ready(tmp_path):
    """The backend then waits (busy) instead of sending meetings that would fail."""
    engine, up = FakeEngine(), [True]
    engine.healthy = lambda: up[0]
    with service(tmp_path, engine) as client:
        assert client.get("/health/ready").json()["status"] == "ready"
        up[0] = False
        assert client.get("/health/ready").json()["status"] == "upstream unavailable"
        assert submit(client).json() == {"detail": "not ready: upstream unavailable"}
        up[0] = True
        assert submit(client).status_code == 202


def test_a_job_waits_for_an_unavailable_qwen_server_without_using_attempts(tmp_path):
    """More failures than MAX_ATTEMPTS, yet the job completes: waiting is not failing."""
    from asr_service.jobs import MAX_ATTEMPTS, Unavailable

    class FlakyUpstream(FakeEngine):
        def __init__(self, failures):
            super().__init__()
            self.failures = failures

        def transcribe(self, audio, options):
            if self.failures:
                self.failures -= 1
                self.calls.append((audio.read_bytes(), options))
                raise Unavailable("Qwen server: not answering")
            return super().transcribe(audio, options)

    engine = FlakyUpstream(MAX_ATTEMPTS + 2)
    with service(tmp_path, engine) as client:
        job_id = submit(client).json()["id"]
        wait_for(lambda: status(client, job_id) == "completed")
        assert len(engine.calls) == MAX_ATTEMPTS + 3
        assert audio_files(tmp_path) == []


def test_a_job_deleted_while_waiting_for_the_qwen_server_leaves_no_audio(tmp_path):
    from asr_service.jobs import Unavailable

    engine = FakeEngine()
    engine.fail = Unavailable("Qwen server: not answering")
    with service(tmp_path, engine, retry_seconds=0.2) as client:
        job_id = submit(client).json()["id"]
        wait_for(lambda: len(engine.calls) >= 1)
        assert client.delete(f"{JOBS}/{job_id}").status_code == 200
        wait_for(lambda: audio_files(tmp_path) == [])


def test_sentences_is_an_option_and_the_health_report_says_whether_the_engine_has_them(tmp_path):
    engine = FakeEngine()
    engine.sentences = True  # the Qwen engine with its aligner loaded
    with service(tmp_path, engine) as client:
        assert client.get("/health/ready").json()["sentences"] is True
        for value, wanted in (("1", True), ("true", True), ("0", False), ("", False)):
            job = submit(client, {"model": "m", "sentences": value}).json()["id"]
            wait_for(lambda job=job: status(client, job) == "completed")
            assert engine.calls[-1][1]["sentences"] is wanted
        job = submit(client).json()["id"]  # RAGFlow and the backend send no such field
        wait_for(lambda: status(client, job) == "completed")
        assert engine.calls[-1][1]["sentences"] is False


def test_a_job_with_priority_runs_before_those_already_waiting(tmp_path):
    engine = FakeEngine()
    engine.gate.clear()
    with service(tmp_path, engine) as client:
        running = submit(client, audio=b"running").json()["id"]
        wait_for(lambda: status(client, running) == "running")
        first = submit(client, audio=b"ragflow 1").json()["id"]
        second = submit(client, audio=b"ragflow 2").json()["id"]
        urgent = submit(client, {"model": "m", "priority": "1"}, audio=b"meeting page").json()["id"]
        engine.gate.set()
        for job in (running, first, second, urgent):
            wait_for(lambda job=job: status(client, job) == "completed")
        assert [audio for audio, _ in engine.calls] == [
            b"running",
            b"meeting page",
            b"ragflow 1",
            b"ragflow 2",
        ]
        assert all("priority" not in options for _, options in engine.calls)  # the queue's order only
