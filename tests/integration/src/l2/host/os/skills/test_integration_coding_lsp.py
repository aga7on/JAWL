import asyncio
import json
import sys

import pytest

from src.l2_interfaces.host.os.skills.coding_context import HostOSCodingContext
from src.l2_interfaces.host.os.skills.coding_lsp import HostOSCodingLanguageServer


FAKE_LSP_SOURCE = r'''import json
import sys


def receive():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if line in (b"\r\n", b""):
            break
        key, value = line.decode("ascii").split(":", 1)
        headers[key.lower()] = value.strip()
    return json.loads(sys.stdin.buffer.read(int(headers["content-length"])))


def send(payload):
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii"))
    sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()


while True:
    message = receive()
    method = message.get("method")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": message["id"], "result": {"capabilities": {}}})
    elif method == "textDocument/definition":
        send({"jsonrpc": "2.0", "id": message["id"], "result": [{
            "uri": sys.argv[1],
            "range": {
                "start": {"line": 0, "character": 4},
                "end": {"line": 0, "character": 10}
            }
        }]})
    elif method == "shutdown":
        send({"jsonrpc": "2.0", "id": message["id"], "result": None})
    elif method == "exit":
        break
'''


@pytest.mark.asyncio
async def test_lsp_resolves_project_definition_and_normalizes_utf16_column(
    os_client, monkeypatch
):
    repository = os_client.sandbox_dir / "lsp_repo"
    repository.mkdir()
    source_file = repository / "main.py"
    definition_file = repository / "lib.py"
    source_file.write_text(
        "from lib import target\nvalue = target()\n", encoding="utf-8"
    )
    definition_file.write_text(
        "emoji = '😀'\ndef target():\n    return 1\n", encoding="utf-8"
    )
    resolver = HostOSCodingLanguageServer(
        os_client,
        HostOSCodingContext(os_client),
        server_commands={".py": ("fake-language-server",)},
    )

    async def fake_query(*args, **kwargs):
        return (
            [
                {
                    "uri": definition_file.as_uri(),
                    "range": {
                        "start": {"line": 1, "character": 4},
                        "end": {"line": 1, "character": 10},
                    },
                }
            ],
            "fake-language-server",
        )

    monkeypatch.setattr(resolver, "_query_server", fake_query)

    result = await resolver.resolve_code_symbol(
        "sandbox/lsp_repo/main.py",
        line=2,
        column=10,
        project_root="sandbox/lsp_repo",
    )
    payload = json.loads(result.message)

    assert result.is_success is True
    assert resolver._python_column("😀target", 2) == 2
    assert payload["backend"] == "lsp"
    assert payload["lsp_available"] is True
    assert payload["server"] == "fake-language-server"
    assert payload["results"] == [
        {
            "path": "lib.py",
            "line": 2,
            "column": 5,
            "lsp_character": 4,
            "preview": "def target():",
            "backend": "lsp",
            "confidence": "semantic",
        }
    ]


@pytest.mark.asyncio
async def test_lsp_unavailable_retains_syntax_aware_fallback(os_client, monkeypatch):
    repository = os_client.sandbox_dir / "fallback_repo"
    repository.mkdir()
    source_file = repository / "main.py"
    source_file.write_text(
        "def target():\n    return 1\n\nvalue = target()\n", encoding="utf-8"
    )
    resolver = HostOSCodingLanguageServer(os_client, HostOSCodingContext(os_client))
    monkeypatch.setattr(resolver, "_server_command", lambda suffix: None)

    result = await resolver.resolve_code_symbol(
        "sandbox/fallback_repo/main.py",
        line=4,
        column=10,
        operation="references",
    )
    payload = json.loads(result.message)

    assert result.is_success is True
    assert payload["backend"] == "syntax_fallback"
    assert payload["lsp_available"] is False
    assert "no allowlisted LSP server" in payload["fallback_reason"]
    assert payload["fallback"]["backend_counts"] == {"python_ast": 1}
    assert payload["fallback"]["definition_count"] == 1
    assert payload["fallback"]["reference_count"] == 1


@pytest.mark.asyncio
async def test_lsp_framing_rejects_oversized_payload_before_reading_body():
    reader = asyncio.StreamReader()
    reader.feed_data(b"Content-Length: 2097153\r\n\r\n")
    reader.feed_eof()

    with pytest.raises(ValueError, match="bounded message size"):
        await HostOSCodingLanguageServer._receive(reader)


@pytest.mark.asyncio
async def test_lsp_acknowledges_server_request_while_waiting_for_response():
    def frame(payload):
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        return f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body

    class FakeWriter:
        def __init__(self):
            self.data = bytearray()

        def write(self, data):
            self.data.extend(data)

        async def drain(self):
            return None

    reader = asyncio.StreamReader()
    reader.feed_data(
        frame(
            {
                "jsonrpc": "2.0",
                "id": 44,
                "method": "client/registerCapability",
                "params": {},
            }
        )
        + frame({"jsonrpc": "2.0", "id": 2, "result": []})
    )
    reader.feed_eof()
    writer = FakeWriter()

    response = await HostOSCodingLanguageServer._receive_response(reader, 2, writer)

    assert response["result"] == []
    assert b'"id":44,"result":null' in writer.data


@pytest.mark.asyncio
async def test_lsp_real_stdio_process_round_trip(os_client, tmp_path):
    repository = os_client.sandbox_dir / "stdio_repo"
    repository.mkdir()
    source_file = repository / "main.py"
    definition_file = repository / "lib.py"
    source_file.write_text("from lib import target\ntarget()\n", encoding="utf-8")
    definition_file.write_text("def target():\n    return 1\n", encoding="utf-8")
    server = tmp_path / "fake_lsp.py"
    server.write_text(FAKE_LSP_SOURCE, encoding="utf-8")
    resolver = HostOSCodingLanguageServer(
        os_client,
        HostOSCodingContext(os_client),
        server_commands={
            ".py": (sys.executable, str(server), definition_file.as_uri())
        },
    )

    result = await resolver.resolve_code_symbol(
        "sandbox/stdio_repo/main.py",
        line=2,
        column=2,
        project_root="sandbox/stdio_repo",
        timeout_sec=10,
    )
    payload = json.loads(result.message)

    assert result.is_success is True
    assert payload["backend"] == "lsp"
    assert payload["results"][0]["path"] == "lib.py"
    assert payload["results"][0]["preview"] == "def target():"
