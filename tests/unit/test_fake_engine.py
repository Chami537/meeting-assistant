"""The offline stand-in engine (ASR_ENGINE=fake): synthetic segments for the real job API, so the whole
upload-to-minutes loop runs with no GPU. The audio's length comes from the WAV header (ffprobe when it is
not a WAV), the text from the fixed script with numerals through the real conversion."""

import io
import wave
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from asr_service.api import create_app
from asr_service.fake_engine import FakeEngine

JOBS = "/v1/audio/transcriptions/jobs"


def wav_bytes(seconds=12.0, rate=8000):
    """A minimal 8 kHz mono WAV of `seconds`: the engine only reads its header."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(seconds * rate))
    return buffer.getvalue()


def wav(tmp_path, seconds=12.0, rate=8000):
    path = tmp_path / "sample.wav"
    path.write_bytes(wav_bytes(seconds, rate))
    return path


def test_the_duration_comes_from_the_wav_header(tmp_path):
    engine = FakeEngine()
    segments = engine.transcribe(wav(tmp_path, seconds=13.0), {})
    assert segments[0]["start"] == 0.0
    assert segments[-1]["end"] == pytest.approx(13.0)
    assert [s["speaker"] for s in segments[:2]] == ["发言人1", "发言人2"]


def test_a_file_that_is_not_audio_is_an_error(tmp_path):
    import shutil

    from asr_service.jobs import AudioError
    if shutil.which("ffprobe") is None:
        pytest.skip("ffprobe not installed")
    path = Path(__file__).with_name("test_fake_engine.py")
    engine = FakeEngine()
    with pytest.raises(AudioError):
        engine.transcribe(path, {})


def test_the_script_is_filled_and_numerals_are_converted(tmp_path):
    segments = FakeEngine().transcribe(wav(tmp_path, seconds=40.0), {"sentences": True})
    text = "".join(s["text"] for s in segments)
    assert "1.2亿元" in text and "15%" in text and "2026年" in text  # itn ran on the script
    assert "二〇二六" not in text
    for s in segments:
        assert s["end"] - s["start"] <= 6.01
        assert s["sentences"] and s["sentences"][0]["start"] == s["start"]


@contextmanager
def service(tmp_path, engine, **kwargs):
    app = create_app(
        lambda: engine,
        workdir=tmp_path,
        model="offline-fake-asr",
        device="cpu",
        capacity=4,
        terms=kwargs.get("terms", ()),
    )
    with TestClient(app) as client:
        yield client


def submit(client, extra=None, file=None):
    return client.post(JOBS, data={"language": "zh"} | (extra or {}),
                       files={"file": ("a.wav", file or wav_bytes(12.0), "audio/wav")})


def wait_for(client, job_id, expect, timeout=10):
    import time
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        body = client.get(f"{JOBS}/{job_id}").json()
        if body["status"] == expect:
            return body
        time.sleep(0.05)
    raise AssertionError(f"job never became {expect}: {body}")


def test_a_job_through_the_real_api_is_completed_with_synthetic_segments(tmp_path):
    with service(tmp_path, FakeEngine()) as client:
        health = client.get("/health/ready").json()
        assert health["status"] == "ready" and health["model"] == "offline-fake-asr" and health["sentences"]
        reply = submit(client, extra={"prompt": "03-晨川科技20260318", "sentences": "1"})
        assert reply.status_code == 202
        job = wait_for(client, reply.json()["id"], "completed")
        assert job["segments"] and job["segments"][0]["speaker"] == "发言人1"
        assert job["segments"][0]["sentences"]  # sentences=1 was honored
        assert client.delete(f"{JOBS}/{reply.json()['id']}").json() == {"deleted": True}


def test_the_fake_engine_holds_jobs_long_enough_to_fill_the_queue(tmp_path):
    with service(tmp_path, FakeEngine(delay=2.0), ) as client:
        job_ids = [submit(client).json()["id"] for _ in range(4)]  # capacity 4: 1 running, 3 queued
        full = submit(client)
        assert full.status_code == 429 and full.headers["retry-after"] == "30"
        for job_id in job_ids:
            wait_for(client, job_id, "completed", timeout=30)
