"""Provider contract suite driven by a real OpenAI-compatible HTTP server.

Unlike ``test_provider_contract.py`` (which substitutes the OpenAI SDK), every
test here serves genuine HTTP: real sockets, real headers, real SSE framing and
real status codes. That keeps the OpenAI-compatible adapter honest about the
wire protocol instead of about our mock's shape, and proves that no QWB
endpoint, header or payload extension is required to drive JAWL.
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from src.l3_agent.llm.api_keys.rotator import APIKeyRotator
from src.l3_agent.llm.client import LLMClient
from src.l3_agent.llm.executor import LLMExecutor
from src.l3_agent.llm.providers import (
    LLMMessage,
    LLMRequest,
    LLMToolDefinition,
    OpenAICompatibleProvider,
    ProviderCapabilities,
    ProviderError,
    RetryPolicy,
)
from src.utils.token_tracker import TokenTracker


class _Scenario:
    """Server-side script shared with the request handler thread."""

    def __init__(self) -> None:
        self.responses: list[dict] = []
        self.requests: list[dict] = []
        self.headers: list[dict] = []
        self.index = 0
        self.lock = threading.Lock()

    def next_response(self, request: dict, headers: dict) -> dict:
        with self.lock:
            self.requests.append(request)
            self.headers.append(headers)
            position = min(self.index, len(self.responses) - 1)
            self.index += 1
            return self.responses[position]


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    scenario: _Scenario

    def log_message(self, *_args) -> None:  # keep pytest output clean
        return None

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        if self.path.endswith("/models"):
            self._send_json(200, {"object": "list", "data": [{"id": "test-model"}]})
            return
        self._send_json(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            payload = {"__unparsed__": raw.decode("utf-8", "replace")}
        headers = {key.lower(): value for key, value in self.headers.items()}
        plan = self.scenario.next_response(payload, headers)

        delay = plan.get("delay")
        if delay:
            # Simulate a slow upstream so timeout and cancellation are testable.
            deadline = threading.Event()
            deadline.wait(float(delay))

        if plan.get("raw_body") is not None:
            body = plan["raw_body"].encode("utf-8")
            self.send_response(plan.get("status", 200))
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if plan.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for chunk in plan["chunks"]:
                block = f"data: {json.dumps(chunk)}\n\n".encode("utf-8")
                self.wfile.write(f"{len(block):X}\r\n".encode("ascii"))
                self.wfile.write(block + b"\r\n")
                self.wfile.flush()
            done = b"data: [DONE]\n\n"
            self.wfile.write(f"{len(done):X}\r\n".encode("ascii"))
            self.wfile.write(done + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            return

        self._send_json(
            plan.get("status", 200),
            plan.get("body", {}),
            extra_headers=plan.get("headers") or {},
        )

    def _send_json(self, status: int, body: dict, extra_headers: dict | None = None) -> None:
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(encoded)

    # Streaming replies are chunked, so suppress the implicit Content-Length.
    def send_response(self, code, message=None):
        self.log_request(code)
        self.send_response_only(code, message)
        self.send_header("Server", "openai-compatible-test")
        self.send_header("Date", self.date_time_string())


def _completion(
    content: str = "ok",
    *,
    tool_calls: list[dict] | None = None,
    usage: dict | None = None,
    reasoning: str | None = None,
) -> dict:
    message: dict = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    return {
        "id": "chatcmpl-http",
        "object": "chat.completion",
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if tool_calls else "stop",
            }
        ],
        "usage": usage
        or {
            "prompt_tokens": 31,
            "completion_tokens": 7,
            "total_tokens": 38,
            "prompt_tokens_details": {"cached_tokens": 11},
            "completion_tokens_details": {"reasoning_tokens": 3},
        },
    }


@pytest.fixture
def openai_server():
    """Run a throwaway OpenAI-compatible server on an ephemeral local port."""

    scenario = _Scenario()
    handler = type("_ScopedHandler", (_Handler,), {"scenario": scenario})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    scenario.base_url = f"http://{host}:{port}/v1"
    try:
        yield scenario
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _provider(
    scenario,
    *,
    capabilities: ProviderCapabilities | None = None,
    keys: list[str] | None = None,
    request_timeout_seconds: float | None = None,
) -> OpenAICompatibleProvider:
    client = LLMClient(
        api_url=scenario.base_url,
        api_keys_rotator=APIKeyRotator(keys or ["contract-key-1"]),
        connect_timeout=5.0,
        read_timeout=30.0,
    )
    return OpenAICompatibleProvider(
        client,
        capabilities
        or ProviderCapabilities(
            native_tools=True,
            json_schema=True,
            streaming=True,
            context_window=8192,
        ),
        request_timeout_seconds=request_timeout_seconds,
    )


def _request(**overrides) -> LLMRequest:
    fields: dict = {
        "model": "test-model",
        "messages": (LLMMessage(role="user", content="hello"),),
    }
    fields.update(overrides)
    return LLMRequest(**fields)


@pytest.mark.asyncio
async def test_plain_text_over_real_http_reports_usage_and_sends_no_qwb_fields(
    openai_server,
):
    openai_server.responses = [
        {"body": _completion("plain answer", reasoning="private thoughts")}
    ]
    provider = _provider(openai_server)

    try:
        result = await provider.complete(_request())
    finally:
        await provider.close()

    assert result.content == "plain answer"
    assert result.reasoning == "private thoughts"
    assert result.model == "test-model"
    assert result.response_id == "chatcmpl-http"
    assert result.usage.prompt_tokens == 31
    assert result.usage.completion_tokens == 7
    assert result.usage.total_tokens == 38
    assert result.usage.cached_tokens == 11
    assert result.usage.reasoning_tokens == 3

    sent = openai_server.requests[0]
    assert sent["messages"] == [{"role": "user", "content": "hello"}]
    # No QWB transport extensions may reach a standard provider.
    assert "enable_thinking" not in sent
    assert "chat_id" not in sent
    headers = openai_server.headers[0]
    assert "x-session-id" not in headers
    assert headers["authorization"] == "Bearer contract-key-1"


@pytest.mark.asyncio
async def test_json_action_plan_survives_a_provider_without_native_tools(
    openai_server,
):
    plan = {
        "observation": "inspected the fixture",
        "reasoning": "single edit is enough",
        "reflection": "",
        "actions": [
            {"tool_name": "HostOSEditor.write", "parameters": {"path": "a.txt"}}
        ],
    }
    openai_server.responses = [{"body": _completion(json.dumps(plan))}]
    provider = _provider(
        openai_server,
        capabilities=ProviderCapabilities(json_schema=True, streaming=True),
    )

    try:
        assert provider.resolve_tool_transport("auto") == "json_envelope"
        result = await provider.complete(
            _request(
                tools=(LLMToolDefinition(name="execute_skill"),),
                tool_choice="required",
                tool_transport="json_envelope",
            )
        )
    finally:
        await provider.close()

    assert json.loads(result.content)["actions"][0]["tool_name"] == "HostOSEditor.write"
    sent = openai_server.requests[0]
    # A text-only provider must not be handed native tool parameters at all.
    assert "tools" not in sent
    assert "tool_choice" not in sent


@pytest.mark.asyncio
async def test_native_tool_call_round_trips_over_real_http(openai_server):
    openai_server.responses = [
        {
            "body": _completion(
                "",
                tool_calls=[
                    {
                        "id": "call_http_1",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path":"README.md"}',
                        },
                    }
                ],
            )
        }
    ]
    provider = _provider(openai_server)

    try:
        result = await provider.complete(
            _request(
                tools=(
                    LLMToolDefinition(
                        name="read_file",
                        description="Read a file",
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
        )
    finally:
        await provider.close()

    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "read_file"
    assert json.loads(result.tool_calls[0].arguments) == {"path": "README.md"}
    assert result.finish_reason == "tool_calls"
    sent = openai_server.requests[0]
    function = sent["tools"][0]["function"]
    assert function["name"] == "read_file"
    # The JSON Schema must survive the SDK untouched, including `required`.
    assert function["parameters"]["required"] == ["path"]
    assert function["parameters"]["properties"] == {"path": {"type": "string"}}
    assert sent["tool_choice"] == "auto"


@pytest.mark.asyncio
async def test_tool_and_assistant_roles_are_accepted_by_a_standard_server(
    openai_server,
):
    openai_server.responses = [{"body": _completion("continued after tool result")}]
    provider = _provider(openai_server)
    history = (
        LLMMessage(role="system", content="you are a coding agent"),
        LLMMessage(role="user", content="read it"),
        LLMMessage.from_openai_dict(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_http_1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    }
                ],
            }
        ),
        LLMMessage(role="tool", content="file body", tool_call_id="call_http_1"),
    )

    try:
        result = await provider.complete(_request(messages=history))
    finally:
        await provider.close()

    assert result.content == "continued after tool result"
    roles = [message["role"] for message in openai_server.requests[0]["messages"]]
    assert roles == ["system", "user", "assistant", "tool"]
    assert openai_server.requests[0]["messages"][3]["tool_call_id"] == "call_http_1"


@pytest.mark.asyncio
async def test_real_sse_stream_aggregates_text_tool_calls_and_trailing_usage(
    openai_server,
):
    openai_server.responses = [
        {
            "stream": True,
            "chunks": [
                {
                    "id": "chatcmpl-stream",
                    "model": "test-model",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": "par"},
                            "finish_reason": None,
                        }
                    ],
                },
                {
                    "id": "chatcmpl-stream",
                    "model": "test-model",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "content": "tial",
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_stream",
                                        "type": "function",
                                        "function": {
                                            "name": "read_",
                                            "arguments": '{"path"',
                                        },
                                    }
                                ],
                            },
                            "finish_reason": None,
                        }
                    ],
                },
                {
                    "id": "chatcmpl-stream",
                    "model": "test-model",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "function": {
                                            "name": "file",
                                            "arguments": ':"a.txt"}',
                                        },
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                },
                {
                    "id": "chatcmpl-stream",
                    "model": "test-model",
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 12,
                        "completion_tokens": 9,
                        "total_tokens": 21,
                    },
                },
            ],
        }
    ]
    provider = _provider(openai_server)

    try:
        result = await provider.complete(_request(stream=True))
    finally:
        await provider.close()

    assert result.content == "partial"
    assert len(result.tool_calls) == 1
    # Fragmented names and arguments must be concatenated in index order.
    assert result.tool_calls[0].name == "read_file"
    assert json.loads(result.tool_calls[0].arguments) == {"path": "a.txt"}
    assert result.finish_reason == "tool_calls"
    assert result.usage.total_tokens == 21
    assert result.provider_metadata["stream_events"] == 4
    assert openai_server.requests[0]["stream"] is True


@pytest.mark.asyncio
async def test_timeout_is_classified_and_cancellation_is_never_swallowed(
    openai_server,
):
    openai_server.responses = [{"delay": 5, "body": _completion("too late")}]
    provider = _provider(openai_server, request_timeout_seconds=0.4)

    try:
        with pytest.raises(ProviderError) as timed_out:
            await provider.complete(_request())
        assert timed_out.value.category == "timeout"
        assert timed_out.value.retryable is True

        provider.request_timeout_seconds = None
        task = asyncio.create_task(provider.complete(_request()))
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_rate_limit_uses_retry_after_and_rotates_local_credentials(
    openai_server,
):
    openai_server.responses = [
        {
            "status": 429,
            "headers": {"Retry-After": "3"},
            "body": {"error": {"message": "slow down", "code": "rate_limit_exceeded"}},
        },
        {"body": _completion("recovered")},
    ]
    provider = _provider(openai_server, keys=["key-a", "key-b"])
    executor = LLMExecutor(
        provider,
        TokenTracker(),
        RetryPolicy(provider_retries=2, base_delay_seconds=0),
    )

    try:
        answer = await executor.execute(
            model_name="test-model",
            messages=[{"role": "user", "content": "hello"}],
            temperature=0.2,
            logger=__import__("logging").getLogger("contract"),
            log_prefix="[HTTP]",
            max_retries=3,
        )
    finally:
        await provider.close()

    assert answer == "recovered"
    metrics = executor.last_call_metrics
    assert metrics["status"] == "completed"
    assert metrics["attempts"] == 2
    assert metrics["retry_counters"]["provider"] == 1
    # A second local key exists, so the adapter rotates instead of sleeping 3s.
    assert len(openai_server.requests) == 2
    assert {entry["authorization"] for entry in openai_server.headers} == {
        "Bearer key-a",
        "Bearer key-b",
    }


@pytest.mark.parametrize(
    ("status", "body", "category", "retryable"),
    [
        (400, {"error": {"message": "bad input"}}, "input_rejected", True),
        (
            400,
            {"error": {"message": "unknown model", "code": "model_not_found"}},
            "configuration",
            False,
        ),
        (401, {"error": {"message": "bad key"}}, "authentication", True),
        (403, {"error": {"message": "forbidden"}}, "authorization", False),
        (408, {"error": {"message": "request timeout"}}, "provider", True),
        (409, {"error": {"message": "conflict"}}, "provider", True),
        (500, {"error": {"message": "boom"}}, "provider", True),
        (503, {"error": {"message": "unavailable"}}, "provider", True),
    ],
)
@pytest.mark.asyncio
async def test_real_status_codes_are_classified_without_qwb_knowledge(
    openai_server, status, body, category, retryable
):
    openai_server.responses = [{"status": status, "body": body}]
    provider = _provider(openai_server)

    try:
        with pytest.raises(ProviderError) as caught:
            await provider.complete(_request())
    finally:
        await provider.close()

    assert caught.value.category == category
    assert caught.value.retryable is retryable
    assert caught.value.status_code == status


@pytest.mark.asyncio
async def test_malformed_and_empty_bodies_become_repairable_invalid_responses(
    openai_server,
):
    openai_server.responses = [
        {"raw_body": "this is not json at all"},
        {"body": {"id": "chatcmpl-empty", "choices": []}},
        {"body": _completion("")},
    ]
    provider = _provider(openai_server)

    try:
        with pytest.raises(ProviderError) as unparsable:
            await provider.complete(_request())
        assert unparsable.value.category in {"provider", "invalid_response"}

        with pytest.raises(ProviderError) as no_choices:
            await provider.complete(_request())
        assert no_choices.value.category == "invalid_response"
        assert no_choices.value.retryable is True

        with pytest.raises(ProviderError) as empty:
            await provider.complete(_request())
        assert empty.value.category == "invalid_response"
        assert empty.value.code == "empty_response"
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_invalid_response_repair_has_its_own_retry_budget(openai_server):
    openai_server.responses = [
        {"body": _completion("")},
        {"body": _completion("")},
        {"body": _completion("finally a real answer")},
    ]
    provider = _provider(openai_server)
    executor = LLMExecutor(
        provider,
        TokenTracker(),
        RetryPolicy(invalid_response_retries=2, base_delay_seconds=0),
    )

    try:
        answer = await executor.execute(
            model_name="test-model",
            messages=[{"role": "user", "content": "hello"}],
            temperature=0.2,
            logger=__import__("logging").getLogger("contract"),
            log_prefix="[HTTP]",
        )
    finally:
        await provider.close()

    assert answer == "finally a real answer"
    counters = executor.last_call_metrics["retry_counters"]
    assert counters["invalid_response"] == 2
    # Repairing an invalid response must not consume the transport budget.
    assert counters["transport"] == 0
    assert counters["provider"] == 0


@pytest.mark.asyncio
async def test_static_context_is_hashed_and_dynamic_context_stays_budgeted(
    openai_server,
):
    openai_server.responses = [{"body": _completion("first")}, {"body": _completion("second")}]
    provider = _provider(openai_server)
    executor = LLMExecutor(provider, TokenTracker())
    system_block = {"role": "system", "content": "durable goal and personality"}
    logger = __import__("logging").getLogger("contract")

    try:
        await executor.execute(
            model_name="test-model",
            messages=[system_block, {"role": "user", "content": "step one"}],
            temperature=0.2,
            logger=logger,
            log_prefix="[HTTP]",
        )
        first = executor.last_call_metrics
        await executor.execute(
            model_name="test-model",
            messages=[system_block, {"role": "user", "content": "step two differs"}],
            temperature=0.2,
            logger=logger,
            log_prefix="[HTTP]",
        )
        second = executor.last_call_metrics
    finally:
        await provider.close()

    # Identical static context keeps a stable cache key across ticks.
    assert first["static_context_hash"] == second["static_context_hash"]
    assert first["static_context_chars"] == second["static_context_chars"]
    assert first["provider_prompt_tokens"] == 31
    assert first["provider_cached_tokens"] == 11
    # The dynamic tail changed, so the local estimate must not be reused blindly.
    assert first["estimated_input_tokens"] != second["estimated_input_tokens"]


@pytest.mark.asyncio
async def test_health_reports_latency_and_capabilities_from_the_models_endpoint(
    openai_server,
):
    openai_server.responses = [{"body": _completion("unused")}]
    provider = _provider(openai_server)

    try:
        health = await provider.health()
    finally:
        await provider.close()

    public = health.public()
    assert public["status"] == "ok"
    assert public["latency_ms"] >= 0
    assert public["capabilities"]["native_tools"] is True
    assert public["capabilities"]["context_window"] == 8192
    # A standard provider exposes no server-side conversation or account pool.
    assert public["capabilities"]["server_side_conversation"] is False
    assert "accounts" not in public["metadata"]


@pytest.mark.asyncio
async def test_offline_endpoint_is_reported_without_leaking_the_api_key(
    openai_server,
):
    client = LLMClient(
        api_url="http://127.0.0.1:1/v1",
        api_keys_rotator=APIKeyRotator(["super-secret-key"]),
        connect_timeout=2.0,
        read_timeout=2.0,
    )
    provider = OpenAICompatibleProvider(
        client,
        ProviderCapabilities(native_tools=True, streaming=True),
    )

    try:
        health = await provider.health()
    finally:
        await provider.close()

    assert health.status in {"offline", "invalid"}
    assert "super-secret-key" not in repr(health.public())
