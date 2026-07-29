from __future__ import annotations

import os
from pathlib import Path

import psutil
import pytest

from src.l2_interfaces.debug_broker.client import DebugBrokerClient
from src.utils.settings import load_config


pytestmark = pytest.mark.skipif(
    os.environ.get("JAWL_DEBUG_BROKER_LIVE") != "1",
    reason="set JAWL_DEBUG_BROKER_LIVE=1 to run the installed RE toolchain",
)

TARGET = Path(r"G:\RE\Workspaces\live-tests\dotnet\jawl_live_target.exe")


async def invoke(
    client: DebugBrokerClient,
    provider: str,
    operation: str,
    arguments: dict,
    session_id: str | None = None,
):
    spec = client.catalog[(provider, operation)]
    return await client.call_operation(
        provider,
        operation,
        arguments,
        spec.schema_sha256,
        session_id,
    )


@pytest.mark.asyncio
async def test_all_installed_providers_on_a_real_target() -> None:
    assert TARGET.is_file(), "build the JAWL live target before this test"
    _, interfaces = load_config()
    client = DebugBrokerClient(interfaces.debug_broker, Path.cwd())
    providers = {row["provider"]: row for row in client.provider_snapshot()}
    assert set(providers) == {
        "x64dbg",
        "ghidra",
        "frida",
        "windbg",
        "radare2",
        "qiling",
        "triton",
    }
    assert all(row["available"] for row in providers.values())

    session = await client.start_session("radare2", str(TARGET))
    try:
        info = await invoke(client, "radare2", "info", {}, session["session_id"])
        functions = await invoke(
            client,
            "radare2",
            "list_functions",
            {"limit": 5},
            session["session_id"],
        )
        assert info["result"]["core"]["format"] == "pe64"
        assert isinstance(functions["result"], list) and functions["result"]
    finally:
        await client.stop_session(session["session_id"])

    session = await client.start_session("ghidra", str(TARGET))
    try:
        analysis = await invoke(
            client,
            "ghidra",
            "analyze",
            {"analysis_timeout_sec": 180},
            session["session_id"],
        )
        assert analysis["result"]["format"] == "Portable Executable (PE)"
        assert analysis["result"]["function_count_exported"] > 0
    finally:
        await client.stop_session(session["session_id"])

    dbgeng = await invoke(client, "windbg", "dbgeng_status", {})
    assert dbgeng["result"]["engine_version"]
    assert dbgeng["result"]["all_components_installed"] is True

    ttd = await invoke(client, "windbg", "ttd_status", {})
    assert ttd["result"]["installed"] is True
    assert ttd["result"]["agent_can_accept_eula"] is False
    assert ttd["result"]["agent_can_request_elevation"] is False

    crash = await client.start_session("windbg", str(TARGET))
    try:
        result = await invoke(
            client,
            "windbg",
            "analyze_crash",
            {"arguments": ["crash"], "timeout_sec": 90},
            crash["session_id"],
        )
        assert "Access violation" in result["result"]["output"]
        assert "Program.Crash" in result["result"]["output"]
    finally:
        await client.stop_session(crash["session_id"])

    frida = await client.start_session(
        "frida", str(TARGET), {"arguments": ["loop"]}
    )
    owned_pid = frida["metadata"]["provider"]["pid"]
    try:
        modules = await invoke(
            client,
            "frida",
            "enumerate_modules",
            {"limit": 20},
            frida["session_id"],
        )
        assert modules["result"]
        await invoke(client, "frida", "resume", {}, frida["session_id"])
    finally:
        await client.stop_session(frida["session_id"])
    assert not psutil.pid_exists(owned_pid)

    environment = await invoke(client, "qiling", "inspect_environment", {})
    assert environment["result"]["version"]
    qiling = await client.start_session("qiling")
    try:
        emulation = await invoke(
            client,
            "qiling",
            "emulate_shellcode",
            {
                "hex_code": "90ebfd",
                "arch": "x86",
                "os": "windows",
                "max_instructions": 8,
            },
            qiling["session_id"],
        )
        assert emulation["result"]["instructions"] == 8
    finally:
        await client.stop_session(qiling["session_id"])

    triton = await client.start_session("triton")
    try:
        symbolic = await invoke(
            client,
            "triton",
            "solve_register",
            {
                "architecture": "x86_64",
                "symbolic_register": "rax",
                "result_register": "rax",
                "target_value": "0x2a",
                "instructions": ["4883c001"],
            },
            triton["session_id"],
        )
        assert next(iter(symbolic["result"]["model"].values()))["value"] == "0x29"
    finally:
        await client.stop_session(triton["session_id"])

    x64dbg = await client.start_session("x64dbg", str(TARGET))
    try:
        modules = await invoke(
            client, "x64dbg", "get_modules", {}, x64dbg["session_id"]
        )
        assert Path(modules["result"][0]["path"]).resolve() == TARGET.resolve()
        state = await invoke(
            client, "x64dbg", "get_state", {}, x64dbg["session_id"]
        )
        assert state["result"]["isDebugging"] is True
    finally:
        await client.stop_session(x64dbg["session_id"])
