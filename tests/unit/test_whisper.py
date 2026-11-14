"""Self-hosted faster-whisper job service client. The fake mirrors the responses recorded from the real
service (10/2026): 202 {id, status: queued}, then {status: completed, segments: [{start, end, text, ...}]}."""

import httpx
import pytest
from app.integrations.whisper import WhisperJobsAsr
from app.postprocess.providers import AsrState, ProviderBusy, ProviderError

AUDIO_URL = "https://bucket.cos.ap-guangzhou.myqcloud.com/tasks/t/audio/upload-v1.m4a?q-sign=x"
SPEECH = {"start": 0.0, "end": 2.12, "text": "同比增长超过89%", "avg_logprob": -0.1, "no_speech_prob": 0.61}


class FakeWhisper:
    def __init__(self):
        self.ready = {"status": "ready", "model": "large-v3", "device": "cuda", "queue_available": 4}
        self.jobs: dict[str, dict] = {}
        self.uploads: list[httpx.Request] = []
        self.audio = b"\x00\x00\x00\x20ftypM4A " + b"a" * 1000
        self.submit_status = 202

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if request.url.host.endswith("myqcloud.com"):
            return httpx.Response(200, content=self.audio)
        if path == "/health/ready":
            return httpx.Response(200, json=self.ready)
        if path == "/v1/audio/transcriptions/jobs" and method == "POST":
            self.uploads.append(request)
            if self.submit_status != 202:
                return httpx.Response(self.submit_status, json={"detail": "queue is full"})
            job_id = f"j{len(self.jobs) + 1}"
            self.jobs[job_id] = {"id": job_id, "status": "queued", "progress": 0.0}
            return httpx.Response(202, json=self.jobs[job_id])
        job_id = path.rsplit("/", 1)[-1]
        if job_id not in self.jobs:
            return httpx.Response(404, json={"detail": "Job not found"})
        if method == "DELETE":
            return httpx.Response(200, json={"deleted": True, "job": self.jobs.pop(job_id)})
        return httpx.Response(200, json=self.jobs[job_id])

    def client(self, **kw) -> WhisperJobsAsr:
        http = httpx.AsyncClient(base_url="http://whisper:18000", transport=httpx.MockTransport(self.handler))
        return WhisperJobsAsr("http://whisper:18000", model="faster-whisper-large-v3", http=http, **kw)


async def test_submit_downloads_the_audio_and_uploads_it_as_a_job():
    fake = FakeWhisper()
    job_id = await fake.client().submit(AUDIO_URL)
    assert job_id == "j1"
    [upload] = fake.uploads
    body = upload.content
    assert b'name="model"\r\n\r\nfaster-whisper-large-v3' in body
    assert b'name="language"\r\n\r\nzh' in body
    assert b'name="prompt"' not in body  # none by default
    assert b'filename="audio.m4a"' in body and fake.audio in body
    assert AUDIO_URL not in body.decode("latin-1")  # the presigned URL never leaves for the service


async def test_prompt_is_sent_when_configured():
    fake = FakeWhisper()
    await fake.client(prompt="会议纪要。").submit(AUDIO_URL)
    assert 'name="prompt"\r\n\r\n会议纪要。'.encode() in fake.uploads[0].content


async def test_the_context_is_the_prompt_only_for_a_service_that_takes_it():
    def context(title):
        return f"{title} 术语：募投" if title else "术语：募投"

    fake = FakeWhisper()
    await fake.client(context=context).submit(AUDIO_URL, title="03-晨川科技20260318")
    await fake.client(context=context, prompt="会议纪要。").submit(AUDIO_URL)  # no title: still the terms
    await fake.client(context=lambda title: None, prompt="会议纪要。").submit(
        AUDIO_URL
    )  # nothing: configured
    await fake.client(prompt="会议纪要。").submit(AUDIO_URL, title="03-晨川科技20260318")  # Whisper
    first, second, third, fourth = (upload.content for upload in fake.uploads)
    assert 'name="prompt"\r\n\r\n03-晨川科技20260318 术语：募投'.encode() in first
    assert 'name="prompt"\r\n\r\n术语：募投'.encode() in second
    assert 'name="prompt"\r\n\r\n会议纪要。'.encode() in third
    assert 'name="prompt"\r\n\r\n会议纪要。'.encode() in fourth and "晨川".encode() not in fourth


async def test_full_queue_is_busy_not_a_failure():
    fake = FakeWhisper()
    fake.ready["queue_available"] = 0
    with pytest.raises(ProviderBusy):
        await fake.client().submit(AUDIO_URL)
    assert fake.uploads == []  # nothing was uploaded
    fake.ready["queue_available"], fake.submit_status = 1, 503  # filled up between the check and the upload
    with pytest.raises(ProviderBusy):
        await fake.client().submit(AUDIO_URL)


async def test_service_down_or_loading_is_busy_but_a_wrong_url_is_not():
    """A restart (connection refused, model loading, 5xx) must not use up the meeting's attempts."""
    fake = FakeWhisper()
    fake.ready = {"status": "loading"}
    with pytest.raises(ProviderBusy, match="not ready: loading"):
        await fake.client().submit(AUDIO_URL)

    def answer(status=None, error=None):
        def handler(request):
            if error:
                raise error
            return httpx.Response(status, json={"detail": "x"})

        http = httpx.AsyncClient(base_url="http://whisper:18000", transport=httpx.MockTransport(handler))
        return WhisperJobsAsr("http://whisper:18000", model="m", http=http)

    with pytest.raises(ProviderBusy, match="not ready: transport: ConnectError"):
        await answer(error=httpx.ConnectError("refused")).check_capacity()
    with pytest.raises(ProviderBusy, match="not ready: HTTP 502"):
        await answer(502).check_capacity()
    for wrong in (404, 401):  # a misconfigured URL or gateway: waiting will not fix it
        with pytest.raises(ProviderError) as exc:
            await answer(wrong).check_capacity()
        assert not isinstance(exc.value, ProviderBusy)
    assert fake.uploads == []


async def test_poll_states():
    fake = FakeWhisper()
    asr = fake.client()
    job_id = await asr.submit(AUDIO_URL)
    assert (await asr.poll(job_id)).state == AsrState.RUNNING
    fake.jobs[job_id]["status"] = "running"
    assert (await asr.poll(job_id)).state == AsrState.RUNNING
    fake.jobs[job_id] |= {
        "status": "completed",
        "segments": [SPEECH, {**SPEECH, "start": 2.12, "end": 6.385}],
    }
    result = await asr.poll(job_id)
    assert result.state == AsrState.DONE and result.billed_seconds is None
    assert [(s.start_ms, s.end_ms, s.text, s.speaker) for s in result.segments] == [
        (0, 2120, "同比增长超过89%", None),
        (2120, 6385, "同比增长超过89%", None),
    ]
    fake.jobs[job_id] = {"status": "failed", "error": "Invalid data found when processing input"}
    failed = await asr.poll(job_id)
    assert (failed.state, failed.error) == (
        AsrState.FAILED,
        "whisper: Invalid data found when processing input",
    )
    assert (await asr.poll("unknown")).state == AsrState.LOST  # the service restarted
    fake.jobs[job_id] = {"status": "cancelled"}
    unknown = await asr.poll(job_id)
    assert (unknown.state, unknown.error) == (AsrState.FAILED, "whisper: status cancelled")


async def test_only_repetition_is_dropped():
    """The service's VAD cuts silence, and in batched mode the confidence scores are shared by a whole window:
    a low score must not drop real speech. Text that compresses like a loop is dropped."""
    fake = FakeWhisper()
    asr = fake.client()
    job_id = await asr.submit(AUDIO_URL)
    unsure = {**SPEECH, "start": 9.0, "end": 12.0, "avg_logprob": -1.05, "no_speech_prob": 0.9}
    loop = {"start": 12.0, "end": 20.0, "text": "好的" * 40, "avg_logprob": -0.2, "compression_ratio": 6.1}
    fake.jobs[job_id] |= {"status": "completed", "segments": [SPEECH, unsure, loop]}
    kept = (await asr.poll(job_id)).segments
    assert [s.start_ms for s in kept] == [0, 9000]


async def test_discard_removes_the_job_and_tolerates_a_missing_one():
    fake = FakeWhisper()
    asr = fake.client()
    job_id = await asr.submit(AUDIO_URL)
    await asr.discard(job_id)
    assert fake.jobs == {}
    await asr.discard(job_id)  # already gone: fine

    def empty(request):
        return httpx.Response(204)

    http = httpx.AsyncClient(base_url="http://whisper:18000", transport=httpx.MockTransport(empty))
    await WhisperJobsAsr("http://whisper:18000", model="m", http=http).discard("j1")  # 204, no body: fine


def test_the_service_is_reached_directly_even_behind_an_http_proxy(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:3128")
    monkeypatch.setenv("NO_PROXY", "10.*")  # a pattern httpx does not understand
    http = WhisperJobsAsr("http://10.0.0.5:18000", model="m")._http
    assert http._transport_for_url(httpx.URL("http://10.0.0.5:18000/health/ready")) is http._transport


async def test_errors_never_quote_response_bodies():
    fake = FakeWhisper()
    asr = fake.client()
    fake.submit_status = 500
    with pytest.raises(ProviderError) as exc:
        await asr.submit(AUDIO_URL)
    assert str(exc.value) == "HTTP 500"
    fake.submit_status = 202
    job_id = await asr.submit(AUDIO_URL)
    fake.jobs[job_id] = {"status": "completed", "segments": [{"text": "我们决定下周三上线"}]}  # no times
    with pytest.raises(ProviderError) as exc:
        await asr.poll(job_id)
    assert str(exc.value) == "unexpected result shape"


async def test_download_problems(monkeypatch):
    from app.integrations import whisper

    fake = FakeWhisper()
    monkeypatch.setattr(whisper, "MAX_AUDIO_BYTES", 100)
    with pytest.raises(ProviderError, match="larger than the upload limit"):
        await fake.client().submit(AUDIO_URL)
    assert fake.uploads == []

    def expired(request):
        if request.url.host.endswith("myqcloud.com"):
            return httpx.Response(403, content=b"<Error><Code>AccessDenied</Code></Error>")
        return fake.handler(request)

    http = httpx.AsyncClient(base_url="http://whisper:18000", transport=httpx.MockTransport(expired))
    with pytest.raises(ProviderError, match="audio download HTTP 403"):
        await WhisperJobsAsr("http://whisper:18000", model="m", http=http).submit(AUDIO_URL)


async def test_long_timeouts_only_for_the_audio_transfer():
    """A hung service must not hold the jobs loop for minutes on a health check, poll or delete."""
    fake = FakeWhisper()
    seen: dict[str, float] = {}

    def handler(request):
        key = "cos" if request.url.host.endswith("myqcloud.com") else f"{request.method} {request.url.path}"
        seen[key] = request.extensions["timeout"]["read"]
        return fake.handler(request)

    http = httpx.AsyncClient(base_url="http://whisper:18000", transport=httpx.MockTransport(handler))
    asr = WhisperJobsAsr("http://whisper:18000", model="m", http=http)
    job_id = await asr.submit(AUDIO_URL)
    await asr.poll(job_id)
    await asr.discard(job_id)
    assert seen["cos"] == seen["POST /v1/audio/transcriptions/jobs"] == 300
    assert seen["GET /health/ready"] == seen[f"GET /v1/audio/transcriptions/jobs/{job_id}"] == 20
    assert seen[f"DELETE /v1/audio/transcriptions/jobs/{job_id}"] == 20


def test_a_stuck_job_is_given_up_long_before_tencent_s_window():
    from app.postprocess.jobs import ASR_POLL_DEADLINE

    assert WhisperJobsAsr.poll_deadline < ASR_POLL_DEADLINE


async def test_fun_asr_speaker_labels_and_provider_name():
    """asr_service/ (ASR_PROVIDER=funasr) speaks the same job API and labels each segment with a speaker."""
    fake = FakeWhisper()
    asr = fake.client(name="funasr")
    job_id = await asr.submit(AUDIO_URL)
    fake.jobs[job_id] |= {
        "status": "completed",
        "segments": [
            {**SPEECH, "speaker": "2", "compression_ratio": 1.1},
            {**SPEECH, "start": 3.0, "end": 4.0, "speaker": 7},
            {**SPEECH, "start": 5.0, "end": 6.0, "speaker": None},
        ],
    }
    assert [s.speaker for s in (await asr.poll(job_id)).segments] == ["2", "7", None]
    fake.jobs[job_id] = {"status": "failed", "error": "audio could not be decoded"}
    assert (await asr.poll(job_id)).error == "funasr: audio could not be decoded"
    assert asr.name == "funasr" and fake.client().name == "whisper"


async def test_our_service_can_be_asked_for_sentence_times_and_priority():
    fake = FakeWhisper()
    await fake.client().submit(AUDIO_URL)
    await fake.client(sentences=True, priority=True).submit(AUDIO_URL)
    plain, asked = (upload.content for upload in fake.uploads)
    assert (
        b'name="sentences"' not in plain and b'name="priority"' not in plain
    )  # RAGFlow's and ours: as before
    assert b'name="sentences"\r\n\r\n1' in asked and b'name="priority"\r\n\r\n1' in asked


async def test_timed_sentences_come_with_their_segment_and_a_malformed_list_is_dropped():
    fake = FakeWhisper()
    asr = fake.client(sentences=True)
    job_id = await asr.submit(AUDIO_URL)
    timed = [
        {"start": 0.0, "end": 1.2, "text": "同比增长。"},
        {"start": 1.25, "end": 2.12, "text": "超过89%"},
    ]
    fake.jobs[job_id] |= {
        "status": "completed",
        "segments": [
            {**SPEECH, "sentences": timed},
            {**SPEECH, "start": 2.12, "end": 3.0, "sentences": [{"start": "soon", "end": 1, "text": "x"}]},
            {**SPEECH, "start": 3.0, "end": 4.0},  # none asked, or this chunk's alignment failed
        ],
    }
    first, broken, untimed = (await asr.poll(job_id)).segments
    assert [(s.start_ms, s.end_ms, s.text) for s in first.sentences] == [
        (0, 1200, "同比增长。"),
        (1250, 2120, "超过89%"),
    ]
    assert broken.sentences == () and untimed.sentences == ()  # the segment's own times are used then
