import asyncio
import json
import sys

import pytest

from src.l2_interfaces.host.os.skills.coding_context import HostOSCodingContext
from src.l2_interfaces.host.os.skills.coding_lsp import HostOSCodingLanguageServer


FAKE_LSP_SOURCE = r'''import json
import sys


def record(event):
    if len(sys.argv) < 3:
        return
    with open(sys.argv[2], "a", encoding="utf-8") as stream:
        stream.write(event + "\n")


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


record("process_start")
while True:
    message = receive()
    method = message.get("method")
    record(method or "response")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": message["id"], "result": {"capabilities": {}}})
    elif method == "textDocument/definition":
        if len(sys.argv) > 3 and sys.argv[3] == "hang":
            continue
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
    assert resolver._lsp_character("😀target", 2) == 2
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

    try:
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
        assert payload["session_mode"] == "incremental"
        assert payload["session_reused"] is False
    finally:
        await resolver.stop()


@pytest.mark.asyncio
async def test_lsp_reuses_session_syncs_changes_and_stops_process(os_client, tmp_path):
    repository = os_client.sandbox_dir / "incremental_repo"
    repository.mkdir()
    source_file = repository / "main.py"
    definition_file = repository / "lib.py"
    source_file.write_text("from lib import target\ntarget()\n", encoding="utf-8")
    definition_file.write_text("def target():\n    return 1\n", encoding="utf-8")
    server = tmp_path / "fake_incremental_lsp.py"
    event_log = tmp_path / "lsp_events.log"
    server.write_text(FAKE_LSP_SOURCE, encoding="utf-8")
    resolver = HostOSCodingLanguageServer(
        os_client,
        HostOSCodingContext(os_client),
        server_commands={
            ".py": (
                sys.executable,
                str(server),
                definition_file.as_uri(),
                str(event_log),
            )
        },
    )

    first = await resolver.resolve_code_symbol(
        "sandbox/incremental_repo/main.py",
        line=2,
        column=2,
        project_root="sandbox/incremental_repo",
        timeout_sec=10,
    )
    second = await resolver.resolve_code_symbol(
        "sandbox/incremental_repo/main.py",
        line=2,
        column=2,
        project_root="sandbox/incremental_repo",
        timeout_sec=10,
    )
    source_file.write_text(
        "from lib import target\ntarget()\n# changed\n", encoding="utf-8"
    )
    third = await resolver.resolve_code_symbol(
        "sandbox/incremental_repo/main.py",
        line=2,
        column=2,
        project_root="sandbox/incremental_repo",
        timeout_sec=10,
    )
    fourth = await resolver.resolve_code_symbol(
        "sandbox/incremental_repo/main.py",
        line=2,
        column=2,
        project_root="sandbox/incremental_repo",
        timeout_sec=10,
        restart_session=True,
    )

    payloads = [
        json.loads(item.message) for item in (first, second, third, fourth)
    ]
    assert all(item.is_success for item in (first, second, third, fourth))
    assert [item["session_reused"] for item in payloads] == [
        False,
        True,
        True,
        False,
    ]
    assert [item["document_sync"] for item in payloads] == [
        "opened",
        "unchanged",
        "changed",
        "opened",
    ]
    assert payloads[-1]["session_reset_requested"] is True
    status = json.loads((await resolver.get_lsp_session_status()).message)
    assert status["session_count"] == 1
    assert status["sessions"][0]["open_documents"] == 1
    events = event_log.read_text(encoding="utf-8").splitlines()
    assert events.count("process_start") == 2
    assert events.count("initialize") == 2
    assert events.count("textDocument/didOpen") == 2
    assert events.count("textDocument/didChange") == 1
    assert events.count("textDocument/definition") == 4
    assert events.count("shutdown") == 1
    assert events.count("exit") == 1

    await resolver.stop()
    stopped = json.loads((await resolver.get_lsp_session_status()).message)
    assert stopped["accepting_queries"] is False
    assert stopped["session_count"] == 0
    events = event_log.read_text(encoding="utf-8").splitlines()
    assert events[-2:] == ["shutdown", "exit"]


@pytest.mark.asyncio
async def test_lsp_evicts_idle_lru_session_at_process_bound(os_client, tmp_path):
    first_root = os_client.sandbox_dir / "lru_one"
    second_root = os_client.sandbox_dir / "lru_two"
    first_root.mkdir()
    second_root.mkdir()
    first_source = first_root / "main.py"
    second_source = second_root / "main.py"
    definition_file = first_root / "lib.py"
    first_source.write_text("from lib import target\ntarget()\n", encoding="utf-8")
    second_source.write_text(
        "def target():\n    return 2\n\ntarget()\n", encoding="utf-8"
    )
    definition_file.write_text("def target():\n    return 1\n", encoding="utf-8")
    server = tmp_path / "fake_bounded_lsp.py"
    event_log = tmp_path / "bounded_lsp_events.log"
    server.write_text(FAKE_LSP_SOURCE, encoding="utf-8")
    resolver = HostOSCodingLanguageServer(
        os_client,
        HostOSCodingContext(os_client),
        server_commands={
            ".py": (
                sys.executable,
                str(server),
                definition_file.as_uri(),
                str(event_log),
            )
        },
        max_sessions=1,
    )

    try:
        first = await resolver.resolve_code_symbol(
            "sandbox/lru_one/main.py",
            line=2,
            column=2,
            project_root="sandbox/lru_one",
            timeout_sec=10,
        )
        second = await resolver.resolve_code_symbol(
            "sandbox/lru_two/main.py",
            line=4,
            column=2,
            project_root="sandbox/lru_two",
            timeout_sec=10,
        )
        assert first.is_success is True
        assert second.is_success is True
        status = json.loads((await resolver.get_lsp_session_status()).message)
        assert status["session_count"] == 1
        assert status["max_sessions"] == 1
        assert status["sessions"][0]["root"] == "sandbox/lru_two"
        events = event_log.read_text(encoding="utf-8").splitlines()
        assert events.count("process_start") == 2
        assert events.count("shutdown") == 1
        assert events.count("exit") == 1
    finally:
        await resolver.stop()


@pytest.mark.asyncio
async def test_lsp_timeout_discards_session_and_preserves_fallback(os_client, tmp_path):
    repository = os_client.sandbox_dir / "timeout_repo"
    repository.mkdir()
    source_file = repository / "main.py"
    source_file.write_text(
        "def target():\n    return 1\n\ntarget()\n", encoding="utf-8"
    )
    server = tmp_path / "fake_hanging_lsp.py"
    event_log = tmp_path / "hanging_lsp_events.log"
    server.write_text(FAKE_LSP_SOURCE, encoding="utf-8")
    resolver = HostOSCodingLanguageServer(
        os_client,
        HostOSCodingContext(os_client),
        server_commands={
            ".py": (
                sys.executable,
                str(server),
                source_file.as_uri(),
                str(event_log),
                "hang",
            )
        },
        idle_timeout_sec=1,
    )

    await resolver.start()
    try:
        started = asyncio.get_running_loop().time()
        result = await resolver.resolve_code_symbol(
            "sandbox/timeout_repo/main.py",
            line=4,
            column=2,
            project_root="sandbox/timeout_repo",
            timeout_sec=2,
        )
        elapsed = asyncio.get_running_loop().time() - started
        payload = json.loads(result.message)
        assert result.is_success is True
        assert elapsed >= 1.8
        assert payload["backend"] == "syntax_fallback"
        assert "timed out after 2 seconds" in payload["fallback_reason"]
        status = json.loads((await resolver.get_lsp_session_status()).message)
        assert status["session_count"] == 0
        events = event_log.read_text(encoding="utf-8").splitlines()
        assert events[-2:] == ["shutdown", "exit"]
    finally:
        await resolver.stop()


@pytest.mark.asyncio
async def test_lsp_lifecycle_reaper_closes_truly_idle_session(os_client, tmp_path):
    repository = os_client.sandbox_dir / "idle_repo"
    repository.mkdir()
    source_file = repository / "main.py"
    source_file.write_text("def target():\n    return 1\ntarget()\n", encoding="utf-8")
    server = tmp_path / "fake_idle_lsp.py"
    event_log = tmp_path / "idle_lsp_events.log"
    server.write_text(FAKE_LSP_SOURCE, encoding="utf-8")
    resolver = HostOSCodingLanguageServer(
        os_client,
        HostOSCodingContext(os_client),
        server_commands={
            ".py": (
                sys.executable,
                str(server),
                source_file.as_uri(),
                str(event_log),
            )
        },
        idle_timeout_sec=1,
    )

    await resolver.start()
    try:
        result = await resolver.resolve_code_symbol(
            "sandbox/idle_repo/main.py",
            line=3,
            column=2,
            project_root="sandbox/idle_repo",
            timeout_sec=10,
        )
        assert result.is_success is True
        assert json.loads((await resolver.get_lsp_session_status()).message)[
            "session_count"
        ] == 1
        await asyncio.sleep(1.8)
        status = json.loads((await resolver.get_lsp_session_status()).message)
        assert status["session_count"] == 0
        events = event_log.read_text(encoding="utf-8").splitlines()
        assert events[-2:] == ["shutdown", "exit"]
    finally:
        await resolver.stop()


@pytest.mark.asyncio
async def test_lsp_bounds_open_documents_with_did_close(os_client, tmp_path):
    repository = os_client.sandbox_dir / "document_lru_repo"
    repository.mkdir()
    first_source = repository / "first.py"
    second_source = repository / "second.py"
    definition_file = repository / "lib.py"
    first_source.write_text("from lib import target\ntarget()\n", encoding="utf-8")
    second_source.write_text("from lib import target\ntarget()\n", encoding="utf-8")
    definition_file.write_text("def target():\n    return 1\n", encoding="utf-8")
    server = tmp_path / "fake_document_lru_lsp.py"
    event_log = tmp_path / "document_lru_events.log"
    server.write_text(FAKE_LSP_SOURCE, encoding="utf-8")
    resolver = HostOSCodingLanguageServer(
        os_client,
        HostOSCodingContext(os_client),
        server_commands={
            ".py": (
                sys.executable,
                str(server),
                definition_file.as_uri(),
                str(event_log),
            )
        },
        max_open_documents=1,
    )

    try:
        first = await resolver.resolve_code_symbol(
            "sandbox/document_lru_repo/first.py",
            line=2,
            column=2,
            project_root="sandbox/document_lru_repo",
            timeout_sec=10,
        )
        second = await resolver.resolve_code_symbol(
            "sandbox/document_lru_repo/second.py",
            line=2,
            column=2,
            project_root="sandbox/document_lru_repo",
            timeout_sec=10,
        )
        assert first.is_success is True
        assert second.is_success is True
        status = json.loads((await resolver.get_lsp_session_status()).message)
        assert status["session_count"] == 1
        assert status["max_open_documents_per_session"] == 1
        assert status["sessions"][0]["open_documents"] == 1
        events = event_log.read_text(encoding="utf-8").splitlines()
        assert events.count("process_start") == 1
        assert events.count("textDocument/didOpen") == 2
        assert events.count("textDocument/didClose") == 1
    finally:
        await resolver.stop()
