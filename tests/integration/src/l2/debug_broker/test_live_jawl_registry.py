"""Full installed-toolchain smoke test through a running JAWL skill registry."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from src.cli.control_client import request_control


pytestmark = pytest.mark.skipif(
    os.environ.get("JAWL_DEBUG_BROKER_JAWL_LIVE") != "1",
    reason="set JAWL_DEBUG_BROKER_JAWL_LIVE=1 and start JAWL first",
)

TARGET = Path(r"G:\RE\Workspaces\live-tests\dotnet\jawl_live_target.exe")
TARGET_X86 = Path(r"G:\RE\Workspaces\live-tests\dotnet-x86\jawl_live_target.exe")
PROVIDERS = ["radare2", "ghidra", "windbg", "frida", "qiling", "triton", "x64dbg"]


def skill(
    name: str,
    arguments: dict[str, Any],
    *,
    timeout: float = 420,
    expect_success: bool = True,
) -> dict[str, Any]:
    response = request_control(
        "debug.skill",
        {"skill": name, "arguments": arguments},
        timeout=timeout,
    )
    if expect_success:
        assert response["is_success"], response["message"]
    return json.loads(response["message"]) if response["is_success"] else response


def test_every_debug_operation_through_running_jawl() -> None:
    assert TARGET.is_file()
    installed = skill("DebugBroker.list_providers", {})
    assert all(item["available"] for item in installed["providers"])

    operations: dict[tuple[str, str], dict[str, Any]] = {}
    for provider in PROVIDERS:
        discovered = skill(
            "DebugBroker.search_operations",
            {"query": "", "provider": provider, "limit": 50},
        )
        for item in discovered["operations"]:
            operations[(provider, item["operation"])] = item
    assert len(operations) >= 33

    sessions: list[str] = []

    def start(
        provider: str,
        target: Path | None = None,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {"provider": provider}
        if target is not None:
            arguments["target"] = str(target)
        if options is not None:
            arguments["options"] = options
        session = skill("DebugBroker.start_session", arguments)
        sessions.append(session["session_id"])
        return session

    def stop(session_id: str) -> None:
        try:
            skill("DebugBroker.stop_session", {"session_id": session_id}, timeout=60)
        finally:
            if session_id in sessions:
                sessions.remove(session_id)

    def call(
        provider: str,
        operation: str,
        arguments: dict[str, Any],
        session_id: str | None = None,
        *,
        timeout: float = 420,
        expect_success: bool = True,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "provider": provider,
            "operation": operation,
            "arguments": arguments,
            "expected_schema_sha256": operations[
                (provider, operation)
            ]["schema_sha256"],
        }
        if session_id is not None:
            payload["session_id"] = session_id
        return skill(
            "DebugBroker.call_operation",
            payload,
            timeout=timeout,
            expect_success=expect_success,
        )

    try:
        assert call("qiling", "inspect_environment", {})["result"]["version"]
        ttd_status = call("windbg", "ttd_status", {})["result"]
        assert ttd_status["installed"]
        assert not ttd_status["agent_can_accept_eula"]
        assert not ttd_status["agent_can_request_elevation"]

        session = start("radare2", TARGET)
        session_id = session["session_id"]
        assert call("radare2", "info", {}, session_id)["result"]["core"]["format"] == "pe64"
        call("radare2", "analyze", {"level": "basic"}, session_id)
        assert call(
            "radare2", "list_functions", {"limit": 8}, session_id
        )["result"]
        call("radare2", "strings", {"limit": 8}, session_id)
        call(
            "radare2",
            "disassemble",
            {"address": "entry0", "count": 8},
            session_id,
        )
        call("radare2", "xrefs", {"address": "entry0"}, session_id)
        call(
            "radare2",
            "raw_command",
            {"command": "ij", "json": True},
            session_id,
        )
        skill("DebugBroker.session_snapshot", {"session_id": session_id})
        stop(session_id)

        session = start("ghidra", TARGET)
        session_id = session["session_id"]
        analysis = call(
            "ghidra",
            "analyze",
            {"analysis_timeout_sec": 180},
            session_id,
            timeout=300,
        )
        assert analysis["result"]["function_count_exported"] > 0
        exported = call(
            "ghidra",
            "export_program",
            {"max_functions": 20},
            session_id,
        )
        # A large export is deliberately a valid truncation envelope.
        assert exported.get("truncated") is True or exported["result"]
        stop(session_id)

        session = start("windbg", TARGET)
        session_id = session["session_id"]
        call(
            "windbg",
            "run_commands",
            {"commands": ["r", "lm", "q"], "timeout_sec": 60},
            session_id,
            timeout=90,
        )
        crash = call(
            "windbg",
            "analyze_crash",
            {"arguments": ["crash"], "timeout_sec": 90},
            session_id,
            timeout=120,
        )
        assert "Program.Crash" in crash["result"]["output"]
        if not ttd_status["record_ready"]:
            refused = call(
                "windbg",
                "record_trace",
                {
                    "arguments": ["normal"],
                    "ring": True,
                    "max_file_mb": 8,
                    "artifact_name": "jawl-e2e-guard.run",
                    "timeout_sec": 60,
                },
                session_id,
                timeout=90,
                expect_success=False,
            )
            assert not refused["is_success"]
            assert "EULA" in refused["message"] or "elevated" in refused["message"]
        stop(session_id)

        assert call("frida", "list_processes", {"limit": 10})["result"]
        session = start("frida", TARGET, {"arguments": ["loop"]})
        session_id = session["session_id"]
        modules = call(
            "frida", "enumerate_modules", {"limit": 10}, session_id
        )["result"]
        memory = call(
            "frida",
            "read_memory",
            {"address": modules[0]["base"], "size": 2},
            session_id,
        )
        assert memory["result"]["hex"].lower() == "4d5a"
        rpc = call(
            "frida",
            "run_script",
            {
                "source": (
                    'rpc.exports = { add(a, b) { return a + b; } }; '
                    'send({kind: "jawl-e2e"});'
                ),
                "rpc_method": "add",
                "rpc_arguments": [20, 22],
                "settle_ms": 100,
            },
            session_id,
        )
        assert rpc["result"]["rpc_result"] == 42
        call("frida", "resume", {}, session_id)
        stop(session_id)

        session = start("qiling")
        session_id = session["session_id"]
        emulated = call(
            "qiling",
            "emulate_shellcode",
            {
                "hex_code": "90ebfd",
                "arch": "x86",
                "os": "windows",
                "max_instructions": 8,
            },
            session_id,
        )
        assert emulated["result"]["instructions"] == 8
        skill(
            "DebugBroker.wait_session",
            {"session_id": session_id, "timeout_seconds": 0},
        )
        stop(session_id)

        session = start("triton")
        session_id = session["session_id"]
        call(
            "triton",
            "execute_instructions",
            {
                "architecture": "x86_64",
                "instructions": ["48c7c029000000", "4883c001"],
            },
            session_id,
        )
        solved = call(
            "triton",
            "solve_register",
            {
                "architecture": "x86_64",
                "symbolic_register": "rax",
                "result_register": "rax",
                "target_value": "0x2a",
                "instructions": ["4883c001"],
            },
            session_id,
        )
        assert next(iter(solved["result"]["model"].values()))["value"] == "0x29"
        stop(session_id)

        session = start("x64dbg", TARGET)
        session_id = session["session_id"]
        assert call("x64dbg", "get_state", {}, session_id)["result"]["isDebugging"]
        call("x64dbg", "get_registers", {}, session_id)
        modules = call("x64dbg", "get_modules", {}, session_id)["result"]
        base = modules[0]["base"]
        call("x64dbg", "list_breakpoints", {}, session_id)
        memory = call(
            "x64dbg", "read_memory", {"address": base, "size": 1}, session_id
        )["result"]
        same_byte: Any = (
            memory.get("data") or memory.get("hex") or memory.get("bytes")
        )
        if isinstance(same_byte, list):
            same_byte = f"{same_byte[0]:02x}"
        same_byte = str(same_byte).replace(" ", "")[:2]
        assert len(same_byte) == 2
        call(
            "x64dbg",
            "write_memory",
            {"address": base, "hex_data": same_byte},
            session_id,
        )
        call(
            "x64dbg",
            "disassemble",
            {"address": base, "count": 5},
            session_id,
        )
        call("x64dbg", "set_breakpoint", {"address": base}, session_id)
        call("x64dbg", "delete_breakpoint", {"address": base}, session_id)
        call("x64dbg", "control", {"action": "step_in"}, session_id)
        call("x64dbg", "raw_command", {"command": "cip"}, session_id)
        stop(session_id)

        if TARGET_X86.is_file():
            session = start("x64dbg", TARGET_X86)
            session_id = session["session_id"]
            assert session["metadata"]["architecture"] == "x86"
            modules = call("x64dbg", "get_modules", {}, session_id)["result"]
            assert Path(modules[0]["path"]).resolve() == TARGET_X86.resolve()
            stop(session_id)

        snapshot = skill("DebugBroker.session_snapshot", {})
        assert not any(item["active"] for item in snapshot["providers"])
    finally:
        for session_id in sessions[::-1]:
            stop(session_id)
