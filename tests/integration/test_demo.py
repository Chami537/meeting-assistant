"""The fallback demo tool drives a local recording to a result link (fakes for COS, media, ASR, LLM)."""

from datetime import timedelta

from app.postprocess import jobs
from app.postprocess.providers import AsrResult, AsrSegment, AsrState
from app.tools import demo
from test_fallback_uploads import M4A, RING, Bucket, pipeline
from test_postprocess import FakeAsr, FakeLlm


class InstantAsr(FakeAsr):
    async def poll(self, task_id):
        return AsrResult(AsrState.DONE, [AsrSegment(0, 2000, "我们决定下周三上线。", "0")])


async def test_demo_run(engine, clean, tmp_path, monkeypatch, capsys):
    bucket = Bucket()
    base = pipeline(bucket)
    minutes = {"summary": "确定上线", "decisions": [{"text": "下周三上线", "evidence_segments": [0]}]}
    full = jobs.PostProcessing(
        asr=InstantAsr(),
        llm=FakeLlm(minutes),
        storage=bucket,
        merger=base.merger,
        asr_daily_audio_seconds=10**9,
        media=base.media,
        keyring=RING,
    )
    monkeypatch.setattr(demo, "get_engine", lambda: engine)
    monkeypatch.setattr(demo, "object_storage", lambda: bucket)
    monkeypatch.setattr(demo, "keyring", lambda: RING)
    monkeypatch.setattr(demo, "postprocessing", lambda: full)
    monkeypatch.setattr(demo, "POLL_S", 0)
    monkeypatch.setattr(jobs, "ASR_POLL_INTERVAL", timedelta(0))
    from app import runner

    monkeypatch.setattr(runner, "get_engine", lambda: engine)
    recording = tmp_path / "meeting.m4a"
    recording.write_bytes(M4A)

    assert await demo.main([str(recording), "--run", "--timeout", "30"]) == 0
    out = capsys.readouterr().out
    assert "纪要已生成：http://127.0.0.1:8000/r/" in out
    assert "状态：COMPLETED" in out  # one pass carries it through ingest, recognition and minutes
    # The upload's file name reaches the recognizer and the minutes model as the meeting title, the
    # spelling reference.
    assert full.asr.titles == ["meeting"]
    assert full.llm.prompts[0].startswith("<title>meeting</title>\n<transcript>\n")


async def test_demo_refuses_a_non_recording(engine, clean, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(demo, "get_engine", lambda: engine)
    monkeypatch.setattr(demo, "object_storage", lambda: Bucket())
    monkeypatch.setattr(demo, "keyring", lambda: RING)
    doc = tmp_path / "notes.pdf"
    doc.write_bytes(b"%PDF-1.7\n")
    assert await demo.main([str(doc)]) == 1
    assert "无法开始" in capsys.readouterr().err


async def test_demo_checks_configuration_before_uploading(engine, clean, tmp_path, monkeypatch, capsys):
    bucket = Bucket()

    def not_configured():
        raise RuntimeError("LLM_BASE_URL / LLM_API_KEY / LLM_MODEL / LLM_PROVIDER are not configured")

    monkeypatch.setattr(demo, "get_engine", lambda: engine)
    monkeypatch.setattr(demo, "object_storage", lambda: bucket)
    monkeypatch.setattr(demo, "postprocessing", not_configured)
    recording = tmp_path / "meeting.m4a"
    recording.write_bytes(M4A)
    assert await demo.main([str(recording), "--run"]) == 1
    assert "LLM_BASE_URL" in capsys.readouterr().err
    assert bucket.objects == {}  # nothing was uploaded
