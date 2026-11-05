import json
import time

import anthropic
import httpx2
import pytest

from app.llm.client import (
    NO_KEY_PLACEHOLDER,
    STUB_MODEL_NAME,
    LLMClient,
    PermanentLLMError,
    RetryableLLMError,
)

SCHEMA = {"type": "object", "properties": {"reply": {"type": "string"}}}


class CountingLimiter:
    def __init__(self) -> None:
        self.calls = 0

    async def acquire(self) -> None:
        self.calls += 1


def _client(settings, handler, **overrides) -> tuple[LLMClient, CountingLimiter]:
    limiter = CountingLimiter()
    http = anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler))
    client = LLMClient(settings.model_copy(update=overrides), limiter, http_client=http)
    return client, limiter


def _message(text: str, stop_reason: str = "end_turn") -> dict:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": [
            {"type": "thinking", "thinking": "", "signature": "sig"},
            {"type": "text", "text": text},
        ],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 20},
    }


def _error(status: int, kind: str, headers: dict | None = None) -> httpx2.Response:
    body = {"type": "error", "error": {"type": kind, "message": kind}}
    return httpx2.Response(status, json=body, headers=headers or {})


async def _call(client: LLMClient):
    return await client.generate_json(
        system="sys", user="question", schema=SCHEMA, stub_response='{"reply": "static"}'
    )


async def test_no_key_mode_sends_real_request_then_returns_static_response(settings):
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return _error(401, "authentication_error")

    client, limiter = _client(
        settings, handler, llm_stub_min_delay_seconds=0.2, llm_stub_max_delay_seconds=0.3
    )
    assert client.stub_mode
    started = time.monotonic()
    result = await _call(client)
    elapsed = time.monotonic() - started

    assert (result.text, result.model, result.stub) == (
        '{"reply": "static"}',
        STUB_MODEL_NAME,
        True,
    )
    assert 0.2 <= elapsed < 1.0  # simulated provider latency
    assert limiter.calls == 1  # stub calls count against the global limit too
    assert len(seen) == 1
    request = seen[0]
    assert request.url.path == "/v1/messages"
    # SDK 2.x sends the key as a bearer token (older versions used x-api-key).
    assert request.headers["authorization"] == f"Bearer {NO_KEY_PLACEHOLDER}"
    body = json.loads(request.content)
    assert body["model"] == settings.llm_model
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["fallbacks"] == "default"


async def test_no_key_mode_survives_network_errors(settings):
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("no route to host")

    client, _ = _client(settings, handler)
    result = await _call(client)
    assert result.stub and result.text == '{"reply": "static"}'


async def test_real_mode_returns_text_block(settings):
    client, limiter = _client(
        settings,
        lambda r: httpx2.Response(200, json=_message('{"reply": "hi"}')),
        anthropic_api_key="sk-test",
    )
    assert not client.stub_mode
    result = await _call(client)
    assert (result.text, result.model, result.stub) == ('{"reply": "hi"}', "claude-opus-5-5", False)
    assert limiter.calls == 1


@pytest.mark.parametrize(
    ("response", "error_type", "retry_after"),
    [
        (_error(429, "rate_limit_error", {"retry-after": "7"}), RetryableLLMError, 7.0),
        (_error(529, "overloaded_error"), RetryableLLMError, None),
        (_error(500, "api_error"), RetryableLLMError, None),
        (_error(400, "invalid_request_error"), PermanentLLMError, None),
        (_error(401, "authentication_error"), PermanentLLMError, None),
    ],
)
async def test_real_mode_classifies_errors(settings, response, error_type, retry_after):
    client, _ = _client(settings, lambda r: response, anthropic_api_key="sk-test")
    with pytest.raises(error_type) as info:
        await _call(client)
    assert info.value.retry_after == retry_after


async def test_real_mode_connection_error_is_retryable(settings):
    def handler(request):
        raise httpx2.ConnectError("down")

    client, _ = _client(settings, handler, anthropic_api_key="sk-test")
    with pytest.raises(RetryableLLMError):
        await _call(client)


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
async def test_unusable_stop_reasons_are_permanent(settings, stop_reason):
    client, _ = _client(
        settings,
        lambda r: httpx2.Response(200, json=_message("{}", stop_reason)),
        anthropic_api_key="sk-test",
    )
    with pytest.raises(PermanentLLMError):
        await _call(client)
