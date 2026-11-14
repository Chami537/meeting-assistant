"""Client of the Qwen3-ASR server (qwen-asr-serve: vLLM, OpenAI-style API), another container on the same
Docker network, to which the Qwen engine sends its chunks.

Chat completions, not /v1/audio/transcriptions: that endpoint drops the request's prompt (vLLM's
get_generation_prompt for Qwen3-ASR never reads it), and the meeting title as context is what gets names
right. On one earnings call the minutes misspelled the company in 6 of 7 runs without it, in none with it.
The chunk goes as a base64 WAV in the user message, the context as the system
message (as qwen_asr does it), decoding at temperature 0 (the transcription endpoint's default).

Every chunk is at most 25 s, so the server never cuts one; `concurrency` requests run at once and the server
batches them. Its text starts with "language Chinese<asr_text>" ("language None" for silence). Errors carry
the HTTP status, never the body: it holds the transcript."""

import base64
import re
from concurrent.futures import ThreadPoolExecutor

import httpx

from .jobs import Unavailable

CHAT = "/v1/chat/completions"
_PREFIX = re.compile(r"language [A-Za-z]*<asr_text>")


class QwenError(Exception):
    pass


class QwenUnavailable(QwenError, Unavailable):
    """The server does not answer, or answers that it cannot now: the job waits instead of failing."""


_UNAVAILABLE = {502, 503, 504}  # not 500: a request the server cannot handle would wait forever


class QwenClient:
    def __init__(
        self,
        base_url: str,
        *,
        model: str = "qwen3-asr-0.6b",
        concurrency: int = 8,
        http: httpx.Client | None = None,
    ):
        self.model, self.concurrency = model, concurrency
        timeout = httpx.Timeout(120, connect=5)  # one chunk takes seconds; a hung server must not hold a job
        self._http = http or httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout, trust_env=False)

    def healthy(self) -> bool:
        try:
            return self._http.get("/health", timeout=3).status_code == 200
        except httpx.HTTPError:
            return False

    def transcribe_many(self, wavs: list[bytes], context: str | None = None) -> list[str]:
        """Texts in the order of the chunks. The first failure is raised and the chunks not started are
        dropped (the job is tried again as a whole)."""
        pool = ThreadPoolExecutor(self.concurrency)
        try:
            return list(pool.map(lambda wav: self.transcribe(wav, context), wavs))
        finally:
            pool.shutdown(wait=True, cancel_futures=True)

    def transcribe(self, wav: bytes, context: str | None = None) -> str:
        url = "data:audio/wav;base64," + base64.b64encode(wav).decode()
        messages = [{"role": "system", "content": context}] if context else []
        messages.append({"role": "user", "content": [{"type": "audio_url", "audio_url": {"url": url}}]})
        body = {"model": self.model, "messages": messages, "temperature": 0.0, "max_tokens": 1024}
        try:
            reply = self._http.post(CHAT, json=body)
        except httpx.HTTPError as exc:
            raise QwenUnavailable(f"Qwen server: {type(exc).__name__}") from None
        if reply.status_code in _UNAVAILABLE:
            raise QwenUnavailable(f"Qwen server: HTTP {reply.status_code}")
        if reply.status_code != 200:
            raise QwenError(f"Qwen server: HTTP {reply.status_code}")
        try:
            text = reply.json()["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError):
            raise QwenError("Qwen server: unexpected reply") from None
        return _PREFIX.sub("", str(text)).strip()
