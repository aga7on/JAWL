"""Local runtime discovery must report facts, never guess capabilities."""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from src.l3_agent.llm.providers.discovery import (
    DiscoveredEndpoint,
    DiscoveredModel,
    capabilities_for_model,
    list_models,
    probe_endpoint,
    recommend_coding_model,
    scan_local_runtimes,
    tool_transport_for_model,
)


class _Runtime(BaseHTTPRequestHandler):
    """Serves both the OpenAI surface and a runtime's native metadata API."""

    protocol_version = "HTTP/1.1"
    routes: dict = {}

    def log_message(self, *_args) -> None:
        return None

    def _send(self, status: int, payload) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        route = self.routes.get(("GET", self.path))
        if route is None:
            self._send(404, {"error": "not found"})
            return
        self._send(200, route)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        handler = self.routes.get(("POST", self.path))
        if handler is None:
            self._send(404, {"error": "not found"})
            return
        if callable(handler):
            self._send(200, handler(payload))
            return
        self._send(200, handler)


@pytest.fixture
def runtime_server():
    def start(routes: dict):
        handler = type("_Scoped", (_Runtime,), {"routes": routes})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        started.append((server, thread))
        host, port = server.server_address[:2]
        return f"http://{host}:{port}"

    started: list = []
    try:
        yield start
    finally:
        for server, thread in started:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


def _ollama_show(model: str, capabilities: list[str], context: int) -> dict:
    return {
        "capabilities": capabilities,
        "details": {
            "parameter_size": "11.9B",
            "quantization_level": "Q4_K_M",
            "family": "gemma",
        },
        "model_info": {"gemma.context_length": context},
    }


@pytest.mark.asyncio
async def test_ollama_declared_capabilities_are_used_verbatim(runtime_server):
    root = runtime_server(
        {
            ("GET", "/v1/models"): {
                "object": "list",
                "data": [
                    {"id": "coder-model:latest"},
                    {"id": "plain-model:latest"},
                ],
            },
            ("POST", "/api/show"): lambda payload: (
                _ollama_show(payload["model"], ["completion", "tools", "thinking"], 262144)
                if payload["model"].startswith("coder")
                else _ollama_show(payload["model"], ["completion"], 8192)
            ),
        }
    )

    endpoint = await probe_endpoint(
        "ollama", "Ollama", f"{root}/v1", native_api=f"{root}/api"
    )

    assert endpoint.reachable is True
    by_id = {model.id: model for model in endpoint.models}
    coder = by_id["coder-model:latest"]
    assert coder.native_tools is True
    assert coder.reasoning is True
    assert coder.vision is False
    assert coder.context_window == 262144
    assert coder.parameter_size == "11.9B"
    assert coder.quantization == "Q4_K_M"

    plain = by_id["plain-model:latest"]
    # The runtime listed capabilities and omitted tools, so tools are absent.
    assert plain.native_tools is False
    assert plain.reasoning is False
    assert plain.context_window == 8192
    # The declared coder model must outrank the plain one.
    assert endpoint.models[0].id == "coder-model:latest"


@pytest.mark.asyncio
async def test_undeclared_capabilities_stay_unknown_and_never_enable_tools(
    runtime_server,
):
    root = runtime_server(
        {
            ("GET", "/v1/models"): {
                "object": "list",
                "data": [{"id": "mystery-model"}],
            }
        }
    )

    endpoint = await probe_endpoint("ollama", "Ollama", f"{root}/v1", native_api="")
    model = endpoint.models[0]

    assert model.native_tools is None, "an unprobed capability must stay unknown"
    assert any("did not declare tool support" in item for item in model.warnings)
    # Unknown must degrade to the safe JAWL envelope, never to native tools.
    assert tool_transport_for_model(model) == "json_envelope"
    assert capabilities_for_model(model)["native_tools"] is False
    assert capabilities_for_model(model)["server_side_conversation"] is False


@pytest.mark.asyncio
async def test_lmstudio_metadata_is_parsed_and_embeddings_are_demoted(
    runtime_server,
):
    root = runtime_server(
        {
            ("GET", "/v1/models"): {
                "object": "list",
                "data": [
                    {"id": "qwen-coder-7b"},
                    {"id": "text-embedding-nomic"},
                ],
            },
            ("GET", "/api/v0/models"): {
                "data": [
                    {
                        "id": "qwen-coder-7b",
                        "type": "llm",
                        "arch": "qwen3",
                        "quantization": "Q4_K_M",
                        "max_context_length": 32768,
                        "capabilities": ["tool_use"],
                    },
                    {"id": "text-embedding-nomic", "type": "embeddings"},
                ]
            },
        }
    )

    endpoint = await probe_endpoint(
        "lmstudio", "LM Studio", f"{root}/v1", native_api=f"{root}/api/v0"
    )

    by_id = {model.id: model for model in endpoint.models}
    coder = by_id["qwen-coder-7b"]
    assert coder.native_tools is True
    assert coder.context_window == 32768
    assert coder.family == "qwen3"
    assert coder.coding_score > 0

    embedding = by_id["text-embedding-nomic"]
    assert embedding.coding_score == 0
    assert any("chat model" in item for item in embedding.warnings)
    assert recommend_coding_model([endpoint]).id == "qwen-coder-7b"


def test_model_names_are_tokenised_so_families_are_not_confused():
    """`fable5-composer` must not read as the `e5` embedding family."""

    endpoint = DiscoveredEndpoint(
        runtime="ollama", label="Ollama", base_url="http://x/v1", reachable=True
    )
    from src.l3_agent.llm.providers.discovery import _coding_score, _looks_non_chat

    assert _looks_non_chat("gemma-4-12b-coder-fable5-composer2.5-v1:latest") is False
    assert _coding_score("gemma-4-12b-coder-fable5-composer2.5-v1:latest", "11.9B") > 0
    # Genuine embedding and reranker families are still rejected.
    assert _looks_non_chat("nomic-embed-text:latest") is True
    assert _looks_non_chat("bge-m3:latest") is True
    assert _looks_non_chat("e5-large-v2") is True
    assert _looks_non_chat("bge-reranker-v2") is True
    assert endpoint.public()["models"] == []


def test_recommendation_prefers_declared_tools_and_larger_context():
    base = {"runtime": "ollama", "base_url": "http://x/v1"}
    no_tools = DiscoveredModel(
        id="coder-a", native_tools=False, context_window=131072,
        coding_score=20, **base,
    )
    with_tools = DiscoveredModel(
        id="coder-b", native_tools=True, context_window=32768,
        coding_score=20, **base,
    )
    endpoint = DiscoveredEndpoint(
        runtime="ollama",
        label="Ollama",
        base_url="http://x/v1",
        reachable=True,
        models=[no_tools, with_tools],
    )

    assert recommend_coding_model([endpoint]).id == "coder-b"
    # An unreachable endpoint can never contribute a recommendation.
    offline = DiscoveredEndpoint(
        runtime="lmstudio", label="LM Studio", base_url="http://y/v1",
        reachable=False, models=[with_tools],
    )
    assert recommend_coding_model([offline]) is None


@pytest.mark.asyncio
async def test_scan_reports_unreachable_runtimes_without_raising():
    endpoints = await scan_local_runtimes(
        timeout=0.4,
        enrich=False,
        runtimes=[
            {
                "runtime": "ollama",
                "label": "Ollama",
                "base_url": "http://127.0.0.1:1/v1",
            }
        ],
    )

    assert len(endpoints) == 1
    assert endpoints[0].reachable is False
    assert endpoints[0].detail
    assert recommend_coding_model(endpoints) is None


@pytest.mark.asyncio
async def test_list_models_sends_the_key_only_when_one_is_configured(
    runtime_server,
):
    seen: list[str] = []

    class _AuthRuntime(_Runtime):
        routes = {("GET", "/v1/models"): {"data": [{"id": "m"}]}}

        def do_GET(self):  # noqa: N802
            seen.append(self.headers.get("Authorization") or "")
            super().do_GET()

    server = ThreadingHTTPServer(("127.0.0.1", 0), _AuthRuntime)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    base = f"http://{host}:{port}/v1"
    try:
        assert await list_models(base, "local_dummy_key") == ["m"]
        assert await list_models(base, "") == ["m"]
        assert await list_models(base, "real-secret") == ["m"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    # The local placeholder must never be sent as a real credential.
    assert seen[0] == ""
    assert seen[1] == ""
    assert seen[2] == "Bearer real-secret"
