"""LLM client for any OpenAI-compatible chat completions endpoint (design doc §7.5, §10 G1a).

Domestic filed models (混元, DeepSeek, 通义 in compatible mode, ...) expose this API, so switching vendor is
a configuration change. JSON mode (`response_format: json_object`) is the common denominator; the schema goes
into the system message and the reply is validated by the caller. Error messages never include response
bodies: providers may echo the prompt, which is the transcript.
"""

import json
import re
from typing import Any

import httpx

from ..postprocess.providers import REPLY_TRUNCATED, LlmReply, ProviderError

# The request itself is wrong (too long, bad parameter, refused by the provider's content check): sending it
# again cannot succeed. 401/403/429/5xx may clear up and are retried.
_PERMANENT_STATUS = {400, 413, 422}
# finish_reason values of a reply that a content check stopped (OpenAI, and domestic vendors' variants).
_FILTERED = {"content_filter", "sensitive"}
_OWN_FIELDS = {"model", "messages", "response_format", "temperature", "max_tokens"}


class OpenAICompatibleLlm:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        provider: str,
        region: str,
        temperature: float = 0.2,
        max_output_tokens: int | None = None,
        extra_body: dict[str, Any] | None = None,
        http: httpx.AsyncClient | None = None,
    ):
        self.name = provider
        self.model = model
        self.region = region
        self._key = api_key
        self._temperature = temperature
        self._max_output_tokens = max_output_tokens
        # Vendor switches (e.g. DeepSeek's thinking mode); they never replace the fields set here.
        self._extra_body = {k: v for k, v in (extra_body or {}).items() if k not in _OWN_FIELDS}
        # A long reply (up to max_tokens) arrives in one piece after minutes; the job's own timeout bounds it.
        timeout = httpx.Timeout(connect=10, read=900, write=30, pool=10)
        self._http = http or httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def complete_json(self, *, system: str, user: str, schema: dict[str, Any]) -> LlmReply:
        shape = json.dumps(schema, ensure_ascii=False)
        instructions = f"{system}\n\n只输出一个 JSON 对象，结构符合这个 JSON Schema：\n{shape}"
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
            "temperature": self._temperature,
        }
        if self._max_output_tokens:
            body["max_tokens"] = self._max_output_tokens
        body.update(self._extra_body)
        try:
            resp = await self._http.post(
                "/chat/completions", json=body, headers={"Authorization": f"Bearer {self._key}"}
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"transport: {type(exc).__name__}") from None
        if resp.status_code != 200:
            code = _error_code(resp)
            raise ProviderError(
                f"HTTP {resp.status_code}" + (f" {code}" if code else ""),
                permanent=resp.status_code in _PERMANENT_STATUS,
            )
        try:
            data = resp.json()
            choice = data["choices"][0]
            content = choice["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise ProviderError("unexpected response shape") from None
        finish_reason = choice.get("finish_reason")
        if finish_reason == "length":
            raise ProviderError(REPLY_TRUNCATED)  # the caller decides once it happens twice
        if finish_reason in _FILTERED:
            raise ProviderError(f"reply blocked ({finish_reason})", permanent=True)
        try:
            parsed = json.loads(_unfenced(content or ""))
        except ValueError:
            raise ProviderError("reply is not JSON") from None
        if not isinstance(parsed, dict):
            raise ProviderError("reply is not a JSON object")
        usage = data.get("usage") or {}
        return LlmReply(
            parsed, input_tokens=usage.get("prompt_tokens"), output_tokens=usage.get("completion_tokens")
        )


def _error_code(resp: httpx.Response) -> str:
    """The vendor's error code or type (e.g. invalid_request_error), which tells a bad max_tokens from a
    refused prompt. Only identifier-like values: the error message may quote the prompt."""
    try:
        data = resp.json()
    except ValueError:
        return ""
    error = data.get("error") if isinstance(data, dict) else None
    for value in (
        *((error.get("code"), error.get("type")) if isinstance(error, dict) else ()),
        data.get("code") if isinstance(data, dict) else None,
    ):
        if isinstance(value, (str, int)) and re.fullmatch(r"[A-Za-z0-9_.\-]{1,40}", str(value)):
            return str(value)
    return ""


def _unfenced(text: str) -> str:
    """Some models wrap JSON in ```json fences even in JSON mode."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    return text
