"""OpenAI-compatible LLM client: request shape, JSON extraction, and errors that never quote the reply."""

import json

import httpx
import pytest
from app.integrations.llm import OpenAICompatibleLlm
from app.postprocess.providers import ProviderError


def llm(handler, **kw):
    http = httpx.AsyncClient(base_url="https://llm.example/v1", transport=httpx.MockTransport(handler))
    return OpenAICompatibleLlm(
        base_url="https://llm.example/v1",
        api_key="sk-x",
        model="m1",
        provider="vendor",
        region="cn",
        http=http,
        **kw,
    )


def reply(content, finish="stop", usage=None):
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": finish}],
            "usage": usage or {"prompt_tokens": 11, "completion_tokens": 7},
        },
    )


async def test_request_and_reply():
    seen = []

    def handler(request):
        seen.append(request)
        return reply('{"summary": "s"}')

    out = await llm(handler).complete_json(
        system="SYS", user="<transcript>x</transcript>", schema={"type": "object"}
    )
    assert (out.content, out.input_tokens, out.output_tokens) == ({"summary": "s"}, 11, 7)
    [req] = seen
    assert req.url.path == "/v1/chat/completions"
    assert req.headers["Authorization"] == "Bearer sk-x"
    body = json.loads(req.content)
    assert body["model"] == "m1"
    assert body["response_format"] == {"type": "json_object"}
    assert "max_tokens" not in body  # 0 / unset: left to the provider
    system, user = body["messages"]
    assert system["role"] == "system" and system["content"].startswith("SYS")
    assert '{"type": "object"}' in system["content"]
    assert user == {"role": "user", "content": "<transcript>x</transcript>"}


async def test_fenced_json_is_accepted():
    out = await llm(lambda r: reply('```json\n{"summary": "s"}\n```')).complete_json(
        system="", user="", schema={}
    )
    assert out.content == {"summary": "s"}


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(500, text="upstream echoed: 我们决定下周三上线"), "HTTP 500"),
        (httpx.Response(200, json={"error": "我们决定下周三上线"}), "unexpected response shape"),
        (reply("我们决定下周三上线"), "reply is not JSON"),
        (reply("[1, 2]"), "reply is not a JSON object"),
        (reply('{"summary": "我们决定', finish="length"), "reply truncated"),
    ],
)
async def test_errors_do_not_quote_content(response, message):
    with pytest.raises(ProviderError) as exc:
        await llm(lambda r: response).complete_json(system="", user="", schema={})
    assert str(exc.value) == message


async def test_transport_error():
    def handler(request):
        raise httpx.ConnectTimeout("timed out talking to https://llm.example")

    with pytest.raises(ProviderError, match="transport: ConnectTimeout"):
        await llm(handler).complete_json(system="", user="", schema={})


async def test_max_output_tokens_is_sent():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return reply('{"summary": "s"}')

    await llm(handler, max_output_tokens=8192).complete_json(system="", user="", schema={})
    assert seen[0]["max_tokens"] == 8192


@pytest.mark.parametrize(
    ("response", "message", "permanent"),
    [
        (
            reply('{"summary": "我们', finish="length"),
            "reply truncated",
            False,
        ),  # the job decides on a repeat
        (reply("", finish="content_filter"), "reply blocked (content_filter)", True),
        (reply(None, finish="sensitive"), "reply blocked (sensitive)", True),
        (httpx.Response(400, json={"error": {"message": "Content Exists Risk"}}), "HTTP 400", True),
        (httpx.Response(413), "HTTP 413", True),
        (
            httpx.Response(400, json={"error": {"type": "invalid_request_error", "message": "max_tokens"}}),
            "HTTP 400 invalid_request_error",
            True,
        ),
        (
            httpx.Response(400, json={"code": "InvalidParameter", "message": "x"}),
            "HTTP 400 InvalidParameter",
            True,
        ),
        (httpx.Response(400, json={"error": {"code": "我们决定下周三上线"}}), "HTTP 400", True),  # not an id
        (httpx.Response(401), "HTTP 401", False),
        (httpx.Response(429), "HTTP 429", False),
        (httpx.Response(503), "HTTP 503", False),
        (reply("not json"), "reply is not JSON", False),
    ],
)
async def test_failures_that_a_retry_cannot_fix_are_permanent(response, message, permanent):
    with pytest.raises(ProviderError) as exc:
        await llm(lambda r: response).complete_json(system="", user="", schema={})
    assert (str(exc.value), exc.value.permanent) == (message, permanent)


async def test_extra_body_adds_vendor_fields_but_never_replaces_ours():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return reply('{"summary": "s"}')

    extra = {"thinking": {"type": "disabled"}, "model": "other", "response_format": {"type": "text"}}
    await llm(handler, extra_body=extra).complete_json(system="", user="", schema={})
    body = seen[0]
    assert body["thinking"] == {"type": "disabled"}
    assert (body["model"], body["response_format"]) == ("m1", {"type": "json_object"})
