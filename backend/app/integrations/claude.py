"""Claude through the official Anthropic SDK (LLM_API=anthropic), for minutes with structured outputs.

The reply is constrained to the schema by the API (output_config.format); constraints the API does not take
(lengths, item counts) are turned into descriptions by anthropic.transform_schema and checked afterwards by
the caller's validator, as for any provider. Adaptive thinking stays on and its depth is set by `effort`; the
models this targets (Haiku 5.5) reject a non-default temperature, so none is sent.

Credentials: an explicit api_key, else whatever the SDK finds in the environment (ANTHROPIC_API_KEY,
ANTHROPIC_AUTH_TOKEN, ANTHROPIC_BASE_URL for a gateway). Error messages carry status codes and stop reasons
only, never response bodies, which may quote the transcript.
"""

import json
from typing import Any

import anthropic

from ..postprocess.providers import REPLY_TRUNCATED, LlmReply, ProviderError

# The request itself is wrong or refused: sending it again cannot succeed (as in llm.py).
_PERMANENT_STATUS = {400, 413, 422}


def build_client(*, api_key: str | None, base_url: str | None) -> anthropic.AsyncAnthropic:
    """RuntimeError (the "not configured" signal deps and the runner understand) when no credential resolves:
    otherwise every summary would fail on its own, three tries each, instead of the jobs waiting."""
    try:
        client = anthropic.AsyncAnthropic(
            **({"api_key": api_key} if api_key else {}),
            **({"base_url": base_url} if base_url else {}),
            timeout=900,  # a long reply arrives in one piece; the job's own timeout bounds it
            max_retries=0,  # retries are the job's (backoff, attempt count), never hidden inside one try
        )
    except anthropic.AnthropicError as exc:  # e.g. a broken ANTHROPIC_PROFILE
        raise RuntimeError(f"Anthropic credentials unusable: {type(exc).__name__}") from None
    if client.api_key is None and client.auth_token is None and getattr(client, "credentials", None) is None:
        raise RuntimeError(
            "LLM_API_KEY or the ANTHROPIC_* credentials are not configured (LLM_API=anthropic)"
        )
    return client


class ClaudeLlm:
    def __init__(
        self,
        *,
        model: str,
        provider: str,
        region: str,
        effort: str = "medium",
        max_output_tokens: int = 16000,  # thinking counts against it: room for both at medium effort
        api_key: str | None = None,
        base_url: str | None = None,
        client: anthropic.AsyncAnthropic | None = None,
    ):
        self.name = provider
        self.model = model
        self.region = region
        self._effort = effort
        self._max_tokens = max_output_tokens
        self._client = client or build_client(api_key=api_key, base_url=base_url)

    async def aclose(self) -> None:
        await self._client.close()

    async def complete_json(self, *, system: str, user: str, schema: dict[str, Any]) -> LlmReply:
        try:
            response = await self._client.messages.create(
                model=self.model,
                max_tokens=self._max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={
                    "effort": self._effort,
                    "format": {"type": "json_schema", "schema": anthropic.transform_schema(schema)},
                },
            )
        except anthropic.APIStatusError as exc:
            status = exc.status_code
            raise ProviderError(
                f"HTTP {status} {type(exc).__name__}", permanent=status in _PERMANENT_STATUS
            ) from None
        except anthropic.APIConnectionError as exc:
            raise ProviderError(f"transport: {type(exc).__name__}") from None
        except anthropic.AnthropicError as exc:  # credentials that stopped resolving, and the like
            raise ProviderError(f"client: {type(exc).__name__}", permanent=True) from None
        if response.stop_reason == "refusal":
            category = response.stop_details.category if response.stop_details else None
            raise ProviderError(f"reply blocked (refusal {category})", permanent=True)
        if response.stop_reason == "max_tokens":
            raise ProviderError(REPLY_TRUNCATED)  # the caller decides once it happens twice
        text = next((block.text for block in response.content if block.type == "text"), None)
        if text is None:
            raise ProviderError("reply has no text")
        try:
            parsed = json.loads(text)
        except ValueError:
            raise ProviderError("reply is not JSON") from None
        if not isinstance(parsed, dict):
            raise ProviderError("reply is not a JSON object")
        usage = response.usage  # a caching gateway moves most of the prompt into the cache counters
        prompt_tokens = (
            usage.input_tokens
            + (usage.cache_creation_input_tokens or 0)
            + (usage.cache_read_input_tokens or 0)
        )
        return LlmReply(parsed, input_tokens=prompt_tokens, output_tokens=usage.output_tokens)
