from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from src.l2_interfaces.debug_broker.catalog import build_catalog
from src.l2_interfaces.debug_broker.client import (
    DebugBrokerClient,
    DebugBrokerError,
)
from src.l2_interfaces.debug_broker.models import DebugSession, schema_digest
from src.utils.settings import DebugBrokerConfig


def make_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    providers: list[str] | None = None,
) -> DebugBrokerClient:
    monkeypatch.setattr(
        DebugBrokerClient, "_discover_windbg", staticmethod(lambda: None)
    )
    config = DebugBrokerConfig(
        enabled=True,
        re_root=str(tmp_path),
        enabled_providers=providers or ["qiling", "x64dbg"],
    )
    return DebugBrokerClient(config, tmp_path)


def test_catalog_is_stable_typed_and_progressively_discoverable() -> None:
    first = build_catalog()
    second = build_catalog()

    assert len(first) >= 30
    assert set(first) == set(second)
    assert all(spec.schema_sha256 == second[key].schema_sha256 for key, spec in first.items())
    assert all(len(spec.schema_sha256) == 64 for spec in first.values())
    assert first[("qiling", "inspect_environment")].session_required is False
    assert first[("x64dbg", "write_memory")].mutating is True
    assert schema_digest({"b": 2, "a": 1}) == schema_digest({"a": 1, "b": 2})


def test_debug_session_public_view_redacts_common_secrets() -> None:
    session = DebugSession(
        provider="frida",
        options={
            "token": "one",
            "password": "two",
            "secret": "three",
            "authorization": "four",
            "arguments": ["loop"],
        },
    )

    assert session.public()["options"] == {"arguments": ["loop"]}


@pytest.mark.asyncio
async def test_call_requires_current_schema_and_valid_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(tmp_path, monkeypatch)
    dispatch = AsyncMock(return_value={"version": "test"})
    monkeypatch.setattr(client, "_dispatch", dispatch)
    spec = client.catalog[("qiling", "inspect_environment")]

    result = await client.call_operation(
        "qiling", "inspect_environment", {}, spec.schema_sha256
    )
    assert result["result"] == {"version": "test"}

    with pytest.raises(DebugBrokerError, match="schema changed"):
        await client.call_operation(
            "qiling", "inspect_environment", {}, "0" * 64
        )
    with pytest.raises(DebugBrokerError, match="Additional properties"):
        await client.call_operation(
            "qiling",
            "inspect_environment",
            {"unexpected": True},
            spec.schema_sha256,
        )


def test_large_json_is_parsed_before_model_facing_truncation() -> None:
    records = [{"index": index, "name": "x" * 200} for index in range(400)]
    encoded = json.dumps(records)

    parsed = DebugBrokerClient._parse_json_output(encoded)

    assert isinstance(parsed, list)
    assert len(parsed) == 400
    assert parsed[-1]["index"] == 399


def test_model_facing_truncation_remains_valid_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(tmp_path, monkeypatch)
    client.config.max_result_chars = 500

    encoded = client.bounded_json({"records": ["x" * 100] * 40})
    result = json.loads(encoded)

    assert len(encoded) <= 500
    assert result["truncated"] is True
    assert result["original_chars"] > 500
    assert "Narrow" in result["hint"]


@pytest.mark.asyncio
async def test_ttd_status_and_recording_safety_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(tmp_path, monkeypatch, providers=["windbg"])
    ttd = tmp_path / "Tools" / "ttd" / "current" / "TTD.exe"
    ttd.parent.mkdir(parents=True)
    ttd.touch()
    target = tmp_path / "target.exe"
    target.touch()
    client._windbg_path = target
    monkeypatch.setattr(client, "_ttd_eula_accepted", lambda: False)
    monkeypatch.setattr(client, "_is_elevated", lambda: False)

    status_spec = client.catalog[("windbg", "ttd_status")]
    status = await client.call_operation(
        "windbg", "ttd_status", {}, status_spec.schema_sha256
    )
    assert status["result"]["installed"] is True
    assert status["result"]["record_ready"] is False
    assert status["result"]["agent_can_accept_eula"] is False

    session = await client.start_session("windbg", str(target))
    record_spec = client.catalog[("windbg", "record_trace")]
    with pytest.raises(DebugBrokerError, match="will not accept legal terms"):
        await client.call_operation(
            "windbg",
            "record_trace",
            {},
            record_spec.schema_sha256,
            session["session_id"],
        )


@pytest.mark.asyncio
async def test_ttd_replay_rejects_non_trace_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(tmp_path, monkeypatch, providers=["windbg"])
    target = tmp_path / "not-a-trace.exe"
    target.touch()
    session = DebugSession(provider="windbg", target=str(target), status="ready")

    with pytest.raises(DebugBrokerError, match=r"existing \.run file"):
        await client._replay_ttd_trace(session, {}, 10)


def test_x64dbg_port_allocator_never_reuses_an_open_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(tmp_path, monkeypatch)
    monkeypatch.setattr(client, "_port_open", lambda port=8888: port in {8888, 8889})

    assert client._free_x64_port() == 8890


def test_x64dbg_repairs_cp1251_mojibake_recursively() -> None:
    result = DebugBrokerClient._repair_x64dbg_text(
        {
            "output": (
                "Р–СѓСЂРЅР°Р» Р±СѓРґРµС‚ РїРµСЂРµРЅР°РїСЂР°РІР»РµРЅ"
            ),
            "nested": ["РќРµРґРѕРїСѓСЃС‚РёРјРѕРµ Р·РЅР°С‡РµРЅРёРµ"],
            "valid": "Русский текст не повреждён",
        }
    )

    assert result["output"] == "Журнал будет перенаправлен"
    assert result["nested"] == ["Недопустимое значение"]
    assert result["valid"] == "Русский текст не повреждён"


def test_invalid_x64dbg_port_ranges_are_rejected() -> None:
    with pytest.raises(ValidationError):
        DebugBrokerConfig(x64dbg_port_start=9000, x64dbg_port_end=8999)
    with pytest.raises(ValidationError):
        DebugBrokerConfig(x64dbg_port_start=8000, x64dbg_port_end=8200)


def test_external_state_namespace_cannot_overwrite_native_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    native = make_client(tmp_path, monkeypatch)
    external = DebugBrokerClient(
        native.config,
        tmp_path,
        state_namespace="mcp-1234",
    )

    assert native.sessions_path.name == "sessions.json"
    assert external.sessions_path.name == "sessions-mcp-1234.json"
    assert native.events_path != external.events_path
    with pytest.raises(ValueError, match="state namespace"):
        DebugBrokerClient(
            native.config,
            tmp_path,
            state_namespace="../escape",
        )


@pytest.mark.asyncio
async def test_context_provider_accepts_registry_event_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(tmp_path, monkeypatch)

    block = await client.get_context_block(
        event_name="HEARTBEAT",
        payload={},
        missed_events=[],
        agent_state=None,
    )

    assert "DEBUG BROKER" in block
    assert "progressive discovery" in block


@pytest.mark.asyncio
async def test_external_mcp_lifespan_owns_broker_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.l2_interfaces.debug_broker import mcp_server

    start = AsyncMock()
    stop = AsyncMock()
    monkeypatch.setattr(mcp_server.client, "start", start)
    monkeypatch.setattr(mcp_server.client, "stop", stop)

    async with mcp_server._broker_lifespan(mcp_server.server):
        assert mcp_server._started is True
        start.assert_awaited_once()

    assert mcp_server._started is False
    stop.assert_awaited_once()
