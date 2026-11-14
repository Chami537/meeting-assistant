"""Claude minutes client (LLM_API=anthropic): request shape, structured output, and errors that never quote
the reply. The fake answers like the Messages API."""

import json

import anthropic
import httpx2
import pytest
from app.integrations.claude import ClaudeLlm
from app.postprocess import minutes as mm
from app.postprocess.providers import ProviderError

GOOD = {"summary": "s", "decisions": [], "action_items": [], "risks": []}


def message(text, *, stop="end_turn", usage=None, stop_details=None):
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-5-5",
        "content": [{"type": "thinking", "thinking": "", "signature": "x"}, {"type": "text", "text": text}],
        "stop_reason": stop,
        "stop_sequence": None,
        "stop_details": stop_details,
        "usage": usage
        or {
            "input_tokens": 2,
            "output_tokens": 40,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 900,
        },
    }


def llm(handler):
    http = anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler))
    client = anthropic.AsyncAnthropic(
        api_key="k", base_url="https://claude.test", http_client=http, max_retries=0
    )
    return ClaudeLlm(model="claude-haiku-5-5", provider="anthropic", region="global", client=client)


async def test_request_uses_structured_output_and_effort():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx2.Response(200, json=message(json.dumps(GOOD)))

    reply = await llm(handler).complete_json(
        system="SYS", user="<transcript>x</transcript>", schema=mm.MINUTES_JSON_SCHEMA
    )
    assert reply.content == GOOD
    assert (reply.input_tokens, reply.output_tokens) == (902, 40)  # cached prompt tokens counted
    [body] = seen
    assert body["model"] == "claude-haiku-5-5" and body["system"] == "SYS"
    assert body["messages"] == [{"role": "user", "content": "<transcript>x</transcript>"}]
    assert body["output_config"]["effort"] == "medium"
    fmt = body["output_config"]["format"]
    assert fmt["type"] == "json_schema" and fmt["schema"]["additionalProperties"] is False
    assert "temperature" not in body and "thinking" not in body  # Haiku 5.5: defaults only
    assert not _keys(fmt["schema"]) & {"maxLength", "minLength", "maxItems"}  # moved into descriptions
    assert all(o.get("additionalProperties") is False for o in _objects(fmt["schema"]))


def _objects(node):
    if isinstance(node, dict):
        if node.get("type") == "object":
            yield node
        for value in node.values():
            yield from _objects(value)
    elif isinstance(node, list):
        for value in node:
            yield from _objects(value)


def _keys(node) -> set[str]:
    if isinstance(node, dict):
        return set(node) | {k for v in node.values() for k in _keys(v)}
    if isinstance(node, list):
        return {k for v in node for k in _keys(v)}
    return set()


@pytest.mark.parametrize(
    ("answer", "text", "permanent"),
    [
        (
            httpx2.Response(
                400, json={"type": "error", "error": {"type": "invalid_request_error", "message": "x"}}
            ),
            "HTTP 400 BadRequestError",
            True,
        ),
        (
            httpx2.Response(
                529, json={"type": "error", "error": {"type": "overloaded_error", "message": "x"}}
            ),
            "HTTP 529 OverloadedError",
            False,
        ),
        (
            httpx2.Response(
                200,
                json=message(
                    "{}",
                    stop="refusal",
                    stop_details={"type": "refusal", "category": "cyber", "explanation": "e"},
                ),
            ),
            "reply blocked (refusal cyber)",
            True,
        ),
        (
            httpx2.Response(200, json=message('{"summary": "我们决定', stop="max_tokens")),
            "reply truncated",
            False,
        ),
        (httpx2.Response(200, json=message("我们决定下周三上线")), "reply is not JSON", False),
        (
            httpx2.Response(
                413, json={"type": "error", "error": {"type": "request_too_large", "message": "x"}}
            ),
            "HTTP 413 RequestTooLargeError",
            True,
        ),
        (
            httpx2.Response(
                422, json={"type": "error", "error": {"type": "invalid_request_error", "message": "x"}}
            ),
            "HTTP 422 UnprocessableEntityError",
            True,
        ),
    ],
)
async def test_failures_never_quote_the_reply(answer, text, permanent):
    with pytest.raises(ProviderError) as exc:
        await llm(lambda request: answer).complete_json(system="", user="", schema={"type": "object"})
    assert (str(exc.value), exc.value.permanent) == (text, permanent)
    assert "我们决定" not in str(exc.value)


async def test_transport_error():
    def handler(request):
        raise httpx2.ConnectError("refused by https://claude.test")

    with pytest.raises(ProviderError, match="transport: APIConnectionError"):
        await llm(handler).complete_json(system="", user="", schema={"type": "object"})
