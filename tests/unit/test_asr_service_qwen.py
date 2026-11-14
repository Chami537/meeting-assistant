"""The client the Qwen engine uses for the Qwen3-ASR server (asr_service/qwen.py), against a fake server shaped
like the chat replies recorded from qwen-asr-serve (10/2026): {"choices": [{"message": {"content":
"language Chinese<asr_text>…"}}], …}."""

import base64
import json
import threading

import httpx
import pytest

from asr_service.qwen import QwenClient, QwenError


def client(handler, concurrency=4):
    http = httpx.Client(base_url="http://qwen3-asr:80", transport=httpx.MockTransport(handler))
    return QwenClient("http://qwen3-asr:80", concurrency=concurrency, http=http)


def answer(text):
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": text}}]})


def audio_of(body: dict) -> bytes:
    (part,) = body["messages"][-1]["content"]
    prefix, data = part["audio_url"]["url"].split(",", 1)
    assert part["type"] == "audio_url" and prefix == "data:audio/wav;base64"
    return base64.b64decode(data)


def reply_with(texts):
    """A server that answers each chunk (its bytes are the key) with the given text."""

    def handler(request):
        if request.url.path == "/health":
            return httpx.Response(200)
        assert request.url.path == "/v1/chat/completions"
        return answer(texts[audio_of(json.loads(request.content)).decode()])

    return handler


def test_texts_come_back_in_chunk_order_without_the_language_prefix():
    texts = {
        "a": "language Chinese<asr_text>同比增长超过百分之八十九。",
        "b": "language None<asr_text>",  # silence
        "c": "language English<asr_text>FDA",
    }
    assert client(reply_with(texts)).transcribe_many([b"a", b"b", b"c"]) == [
        "同比增长超过百分之八十九。",
        "",
        "FDA",
    ]


def test_requests_run_concurrently():
    inside, most, lock = [0], [0], threading.Lock()
    barrier = threading.Barrier(3, timeout=5)

    def handler(request):
        with lock:
            inside[0] += 1
            most[0] = max(most[0], inside[0])
        barrier.wait()  # only passes when three requests are in flight together
        with lock:
            inside[0] -= 1
        return answer("language Chinese<asr_text>好")

    assert client(handler, concurrency=3).transcribe_many([b"1", b"2", b"3"]) == ["好", "好", "好"]
    assert most[0] == 3


def test_failures_name_the_status_never_the_body():
    def handler(request):
        return httpx.Response(500, json={"detail": "我们决定下周三上线"})

    with pytest.raises(QwenError) as exc:
        client(handler).transcribe_many([b"a"])
    assert str(exc.value) == "Qwen server: HTTP 500"

    def refused(request):
        raise httpx.ConnectError("connection refused")

    with pytest.raises(QwenError, match="ConnectError"):
        client(refused).transcribe(b"a")

    for odd in (
        lambda r: httpx.Response(200, text="我们决定下周三上线"),
        lambda r: httpx.Response(200, json={"choices": []}),
        lambda r: httpx.Response(
            200, json={"text": "我们决定下周三上线"}
        ),  # the transcription endpoint's shape
    ):
        with pytest.raises(QwenError) as exc:
            client(odd).transcribe(b"a")
        assert str(exc.value) == "Qwen server: unexpected reply"


def test_the_context_goes_as_the_system_message_of_every_chunk():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return answer("language Chinese<asr_text>晨川")

    assert client(handler).transcribe_many([b"a", b"b"], "03-晨川科技20260318") == ["晨川", "晨川"]
    assert client(handler).transcribe(b"c") == "晨川"
    for body in bodies[:2]:
        assert body["messages"][0] == {"role": "system", "content": "03-晨川科技20260318"}
    assert [m["role"] for m in bodies[2]["messages"]] == ["user"]  # no context: no system message
    assert sorted(audio_of(body) for body in bodies) == [b"a", b"b", b"c"]
    assert {(b["model"], b["temperature"]) for b in bodies} == {("qwen3-asr-0.6b", 0.0)}


def test_health():
    assert client(lambda r: httpx.Response(200)).healthy()
    assert not client(lambda r: httpx.Response(503)).healthy()

    def down(request):
        raise httpx.ConnectError("refused")

    assert not client(down).healthy()


def test_no_chunks():
    assert client(lambda r: httpx.Response(500)).transcribe_many([]) == []


def test_a_server_that_is_down_or_busy_makes_the_job_wait_but_a_bad_request_fails_it():
    """QwenUnavailable is the job runner's Unavailable: the job waits without using an attempt."""
    from asr_service.jobs import Unavailable
    from asr_service.qwen import QwenUnavailable

    for status in (502, 503, 504):
        with pytest.raises(QwenUnavailable, match=f"HTTP {status}"):
            client(lambda r, status=status: httpx.Response(status)).transcribe(b"a")

    def refused(request):
        raise httpx.ConnectError("connection refused")

    with pytest.raises(Unavailable):
        client(refused).transcribe_many([b"a", b"b"])
    for status in (400, 500):  # the same request would fail the same way: not worth waiting for
        with pytest.raises(QwenError) as exc:
            client(lambda r, status=status: httpx.Response(status)).transcribe(b"a")
        assert not isinstance(exc.value, Unavailable)
