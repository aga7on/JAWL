import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import openai
import pytest

from src.l3_agent.llm.providers import (
    LLMMessage,
    LLMProvider,
    LLMRequest,
    LLMResult,
    LLMToolCall,
    LLMToolDefinition,
    LLMUsage,
    OpenAICompatibleProvider,
    ProviderCapabilities,
    ProviderError,
    QWBProvider,
    normalize_tool_transport,
)


def _response(*, content="ok", tool_calls=(), usage=None):
    return SimpleNamespace(
        id="chatcmpl-contract",
        model="contract-model",
        choices=[
            SimpleNamespace(
                finish_reason="tool_calls" if tool_calls else "stop",
                message=SimpleNamespace(
                    content=content,
                    tool_calls=list(tool_calls),
                    reasoning_content="private reasoning",
                ),
            )
        ],
        usage=usage,
    )


def _sdk_tool_call(name="read_file", arguments='{"path":"README.md"}'):
    return SimpleNamespace(
        id="call_contract",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _client_with_response(response):
    client = MagicMock()
    client.api_url = "http://provider.test/v1"
    client.rotator = MagicMock()
    client.rotator.total_keys.return_value = 1
    session = MagicMock()
    session.api_key = "test-key"
    session.chat.completions.create = AsyncMock(return_value=response)
    session.models.list = AsyncMock(return_value=SimpleNamespace(data=[]))
    client.get_session.return_value = session
    return client, session


class DeterministicProvider(LLMProvider):
    name = "deterministic"

    def __init__(self, result: LLMResult):
        super().__init__(
            ProviderCapabilities(native_tools=True, json_schema=True, streaming=True)
        )
        self.result = result
        self.requests = []

    async def complete(self, request: LLMRequest) -> LLMResult:
        self.requests.append(request)
        return self.result


def _provider_for(kind: str, response):
    if kind == "fake":
        if isinstance(response, LLMResult):
            return DeterministicProvider(response), None
        raise TypeError("fake provider needs an LLMResult")
    client, session = _client_with_response(response)
    if kind == "qwb":
        return QWBProvider(client), session
    return OpenAICompatibleProvider(client), session


def test_contract_round_trips_all_openai_chat_roles_and_legacy_transport_names():
    calls = (
        LLMToolCall(id="call_1", name="inspect", arguments='{"x":1}'),
    )
    messages = [
        LLMMessage(role="system", content="system"),
        LLMMessage(role="developer", content="developer"),
        LLMMessage(role="user", content=[{"type": "text", "text": "user"}]),
        LLMMessage(role="assistant", content=None, tool_calls=calls),
        LLMMessage(role="tool", content="result", tool_call_id="call_1"),
    ]

    assert [LLMMessage.from_openai_dict(item.as_openai_dict()) for item in messages] == messages
    assert normalize_tool_transport("wrapper") == "json_envelope"
    assert normalize_tool_transport("hybrid") == "auto"
    assert normalize_tool_transport("native") == "native"


@pytest.mark.parametrize("kind", ["fake", "qwb", "openai"])
@pytest.mark.asyncio
async def test_same_text_and_json_action_contract_runs_through_every_provider(kind):
    action = json.dumps(
        {
            "observation": "fixture inspected",
            "reasoning": "",
            "reflection": "done",
            "actions": [],
        }
    )
    provider_response = (
        LLMResult(
            content=action,
            usage=LLMUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )
        if kind == "fake"
        else _response(
            content=action,
            usage=SimpleNamespace(
                prompt_tokens=10,
                completion_tokens=5,
                total_tokens=15,
                prompt_tokens_details=SimpleNamespace(cached_tokens=3),
                completion_tokens_details=SimpleNamespace(reasoning_tokens=2),
            ),
        )
    )
    provider, session = _provider_for(kind, provider_response)
    request = LLMRequest(
        model="contract-model",
        messages=(LLMMessage(role="user", content="finish the fixture"),),
        tool_transport="json_envelope",
    )

    result = await provider.complete(request)

    assert json.loads(result.content)["reflection"] == "done"
    assert result.usage.total_tokens == 15
    if session is not None:
        kwargs = session.chat.completions.create.await_args.kwargs
        assert kwargs["messages"] == [
            {"role": "user", "content": "finish the fixture"}
        ]
        assert "extra_headers" not in kwargs or kind == "qwb"


@pytest.mark.parametrize("kind", ["fake", "qwb", "openai"])
@pytest.mark.asyncio
async def test_same_native_tool_call_contract_runs_through_every_provider(kind):
    call = LLMToolCall(
        id="call_contract",
        name="read_file",
        arguments='{"path":"README.md"}',
    )
    provider_response = (
        LLMResult(content="inspect", tool_calls=(call,))
        if kind == "fake"
        else _response(content="inspect", tool_calls=[_sdk_tool_call()])
    )
    provider, session = _provider_for(kind, provider_response)
    request = LLMRequest(
        model="contract-model",
        messages=(LLMMessage(role="user", content="read it"),),
        tools=(
            LLMToolDefinition(
                name="read_file",
                parameters={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            ),
        ),
        tool_choice="auto",
        tool_transport="native",
    )

    result = await provider.complete(request)

    assert result.tool_calls == (call,)
    if session is not None:
        kwargs = session.chat.completions.create.await_args.kwargs
        assert kwargs["tools"][0]["function"]["name"] == "read_file"
        assert kwargs["tool_choice"] == "auto"


@pytest.mark.asyncio
async def test_openai_json_envelope_omits_tools_when_capability_is_not_native():
    client, session = _client_with_response(_response(content='{"actions":[]}'))
    provider = OpenAICompatibleProvider(
        client,
        ProviderCapabilities(json_schema=True),
    )
    request = LLMRequest(
        model="text-only",
        messages=(LLMMessage(role="user", content="return JSON"),),
        tools=(LLMToolDefinition(name="execute_skill"),),
        tool_choice="required",
        tool_transport="json_envelope",
    )

    await provider.complete(request)

    kwargs = session.chat.completions.create.await_args.kwargs
    assert "tools" not in kwargs
    assert "tool_choice" not in kwargs
    assert provider.resolve_tool_transport("auto") == "json_envelope"


class _AsyncChunks:
    def __init__(self, chunks):
        self._chunks = iter(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._chunks)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


@pytest.mark.asyncio
async def test_openai_stream_aggregates_text_reasoning_tool_calls_and_usage():
    chunks = _AsyncChunks(
        [
            SimpleNamespace(
                id="stream-1",
                model="stream-model",
                usage=None,
                choices=[
                    SimpleNamespace(
                        finish_reason=None,
                        delta=SimpleNamespace(
                            content="hel",
                            reasoning_content="think",
                            tool_calls=[],
                        ),
                    )
                ],
            ),
            SimpleNamespace(
                id="stream-1",
                model="stream-model",
                usage=SimpleNamespace(
                    prompt_tokens=4,
                    completion_tokens=3,
                    total_tokens=7,
                    prompt_tokens_details=None,
                    completion_tokens_details=None,
                ),
                choices=[
                    SimpleNamespace(
                        finish_reason="stop",
                        delta=SimpleNamespace(
                            content="lo",
                            reasoning_content="",
                            tool_calls=[],
                        ),
                    )
                ],
            ),
        ]
    )
    client, session = _client_with_response(chunks)
    provider = OpenAICompatibleProvider(client)

    result = await provider.complete(
        LLMRequest(
            model="stream-model",
            messages=(LLMMessage(role="user", content="hello"),),
            stream=True,
        )
    )

    assert result.content == "hello"
    assert result.reasoning == "think"
    assert result.usage.total_tokens == 7
    assert result.provider_metadata["stream_events"] == 2
    assert session.chat.completions.create.await_args.kwargs["stream"] is True


@pytest.mark.asyncio
async def test_provider_timeout_and_downstream_cancellation_are_distinct():
    started = asyncio.Event()

    async def never_returns(**_kwargs):
        started.set()
        await asyncio.Event().wait()

    client, session = _client_with_response(None)
    session.chat.completions.create.side_effect = never_returns
    provider = OpenAICompatibleProvider(client, request_timeout_seconds=0.01)
    with pytest.raises(ProviderError, match="timed out") as timeout:
        await provider.complete(
            LLMRequest(model="m", messages=(LLMMessage(role="user", content="x"),))
        )
    assert timeout.value.category == "timeout"

    provider.request_timeout_seconds = None
    task = asyncio.create_task(
        provider.complete(
            LLMRequest(model="m", messages=(LLMMessage(role="user", content="x"),))
        )
    )
    await asyncio.wait_for(started.wait(), timeout=0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize(
    ("status", "expected_category", "retryable"),
    [
        (400, "input_rejected", True),
        (401, "authentication", True),
        (403, "authorization", False),
        (408, "provider", True),
        (409, "provider", True),
        (429, "rate_limit", True),
        (500, "provider", True),
    ],
)
def test_openai_status_classification_and_retry_after(status, expected_category, retryable):
    client, _ = _client_with_response(_response())
    provider = OpenAICompatibleProvider(client)
    request = httpx.Request("POST", "http://provider.test/v1/chat/completions")
    response = httpx.Response(status, request=request, headers={"retry-after": "7"})
    error = openai.APIStatusError("upstream", response=response, body={})

    mapped = provider._map_error(error)

    assert mapped.category == expected_category
    assert mapped.retryable is retryable
    assert mapped.retry_after == 7


@pytest.mark.asyncio
async def test_qwb_extensions_and_chat_not_found_stay_inside_qwb_adapter():
    request = httpx.Request("POST", "http://qwb.test/v1/chat/completions")
    response = httpx.Response(400, request=request)
    error = openai.BadRequestError(
        "CHAT_NOT_FOUND: chat is not exist",
        response=response,
        body={"error": {"code": "CHAT_NOT_FOUND"}},
    )
    client, session = _client_with_response(_response())
    session.chat.completions.create.side_effect = error
    provider = QWBProvider(client)

    with pytest.raises(ProviderError) as caught:
        await provider.complete(
            LLMRequest(
                model="qwen",
                messages=(LLMMessage(role="user", content="x"),),
                enable_reasoning=True,
                session_id="goal-safe-lane",
            )
        )

    kwargs = session.chat.completions.create.await_args.kwargs
    assert kwargs["extra_body"] == {"enable_thinking": True}
    assert kwargs["extra_headers"] == {"X-Session-Id": "goal-safe-lane"}
    assert caught.value.category == "session_state"
    assert caught.value.code == "CHAT_NOT_FOUND"


@pytest.mark.asyncio
async def test_qwb_tool_protocol_error_uses_the_tool_repair_budget():
    """A malformed tool envelope is a protocol repair, not an upstream outage."""

    request = httpx.Request("POST", "http://qwb.test/v1/chat/completions")
    response = httpx.Response(502, request=request, headers={"Retry-After": "1"})
    error = openai.APIStatusError(
        "Qwen returned malformed JAWL tool transport; retry from the "
        "authoritative local snapshot",
        response=response,
        body={"error": {"type": "tool_protocol_error"}},
    )
    client, session = _client_with_response(_response())
    session.chat.completions.create.side_effect = error
    provider = QWBProvider(client)

    with pytest.raises(ProviderError) as caught:
        await provider.complete(
            LLMRequest(model="qwen", messages=(LLMMessage(role="user", content="x"),))
        )

    assert caught.value.category == "tool_protocol"
    assert caught.value.retryable is True
    # It must not be charged to the generic provider/transport budgets.
    from src.l3_agent.llm.executor import LLMExecutor

    assert LLMExecutor._retry_bucket(caught.value.category) == "tool_protocol"


@pytest.mark.asyncio
async def test_qwb_empty_answer_is_classified_for_repair():
    client, _ = _client_with_response(_response(content=""))
    provider = QWBProvider(client)

    with pytest.raises(ProviderError) as caught:
        await provider.complete(
            LLMRequest(model="qwen", messages=(LLMMessage(role="user", content="x"),))
        )

    assert caught.value.category == "invalid_response"
    assert caught.value.code == "empty_qwen_answer"


@pytest.mark.asyncio
async def test_qwb_waf_challenge_stops_without_banning_the_bridge_key():
    request = httpx.Request("POST", "http://qwb.test/v1/chat/completions")
    response = httpx.Response(503, request=request)
    error = openai.APIStatusError(
        "Qwen Web anti-bot challenge required; refresh WAF cookies or use CDP transport",
        response=response,
        body={"error": {"type": "upstream_waf_challenge"}},
    )
    client, session = _client_with_response(_response())
    session.chat.completions.create.side_effect = error
    provider = QWBProvider(client)

    with pytest.raises(ProviderError) as caught:
        await provider.complete(
            LLMRequest(model="qwen", messages=(LLMMessage(role="user", content="x"),))
        )

    assert caught.value.category == "authentication"
    assert caught.value.retryable is False
    provider.on_authentication_error(caught.value)
    client.rotator.ban_key.assert_not_called()


@pytest.mark.asyncio
async def test_qwb_health_exposes_account_diagnostics_without_tokens():
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "status": "ok",
        "model": "qwen3.8-max-preview",
        "transport": "direct",
        "accounts": [
            {
                "label": "account-a",
                "fingerprint": "deadbeef",
                "state": "healthy",
                "token": "must-not-leak",
                "inFlight": 0,
            }
        ],
    }
    http = AsyncMock()
    http.get.return_value = response
    context = AsyncMock()
    context.__aenter__.return_value = http
    context.__aexit__.return_value = False
    client, _ = _client_with_response(_response())
    provider = QWBProvider(client)

    with patch(
        "src.l3_agent.llm.providers.qwb.httpx.AsyncClient",
        return_value=context,
    ):
        health = await provider.health()

    public = health.public()
    assert public["status"] == "ok"
    assert public["metadata"]["accounts"][0]["fingerprint"] == "deadbeef"
    assert "must-not-leak" not in repr(public)
