# Debug Broker

`interfaces.debug_broker` connects JAWL to local reverse-engineering and
debugging tools through one typed, stateful interface. Provider executables do
not need to be running: the broker starts an isolated process or worker when a
session requires it and closes only processes it owns.

```yaml
interfaces:
  debug_broker:
    enabled: true
    re_root: "G:/RE"
    auto_start: true
    startup_timeout_sec: 30
    request_timeout_sec: 300
    max_result_chars: 30000
    max_sessions: 20
    x64dbg_port_start: 8888
    x64dbg_port_end: 8899
    enabled_providers:
      - x64dbg
      - ghidra
      - frida
      - windbg
      - radare2
      - qiling
      - triton
```

The model sees seven stable skills instead of every debugger command:

1. `DebugBroker.list_providers`
2. `DebugBroker.search_operations`
3. `DebugBroker.start_session`
4. `DebugBroker.call_operation`
5. `DebugBroker.wait_session`
6. `DebugBroker.session_snapshot`
7. `DebugBroker.stop_session`

Operation schemas have a SHA-256 revision. The model must discover an operation
and send the returned `schema_sha256` when calling it. This prevents stale or
invented arguments after a broker update.

## Providers

| Provider | Capabilities |
| --- | --- |
| x64dbg | x86/x64 state, registers, modules, memory, disassembly, breakpoints, execution control, raw commands |
| Ghidra | headless analysis and bounded JSON program export |
| Frida | process discovery, spawn/attach, modules, memory, scripts/RPC, resume |
| WinDbg | CDB commands, DbgEng/DbgModel/TTDReplay diagnostics, crash analysis, TTD readiness and controlled recording |
| radare2 | metadata, analysis, functions, strings, disassembly, xrefs, raw commands |
| Qiling | environment inspection and bounded shellcode emulation |
| Triton | instruction execution and symbolic register solving |

All model-facing results are bounded. If a result is too large, the broker
returns a valid JSON envelope with a preview and asks the caller to narrow the
query.

## Time Travel Debugging safety

The broker never accepts the Microsoft TTD EULA and never requests elevation.
`windbg.ttd_status` reports whether the standalone recorder is installed, the
current-user EULA state, elevation, and readiness. `windbg.record_trace` fails
fast until the user has reviewed and accepted the EULA manually and has started
JAWL from an elevated terminal.

TTD recording is invasive and can significantly slow a target. A trace can
contain process memory, paths, registry data, credentials, and other sensitive
information. Broker-created traces are restricted to
`G:/RE/Artifacts/broker/ttd`. Existing `.run` traces can be opened through
`windbg.replay_trace`; indexing remains bounded by the configured timeout and
may create a sibling `.idx` artifact.

## External MCP facade

The same compact API is available over stdio MCP:

```powershell
G:\AI\JAWL-Coding\venv\Scripts\python.exe -m src.l2_interfaces.debug_broker.mcp_server
```

Run it with `G:\AI\JAWL-Coding` as the working directory. The server exposes the
same seven discovery/session skills; provider processes remain lazy. Its MCP
lifespan owns the broker and closes workers and broker-started debugger processes
when the client disconnects or the server exits normally. Each external MCP
process gets an isolated session/event store, so it cannot invalidate live
native JAWL sessions.

## Verification

Unit tests:

```powershell
venv\Scripts\python.exe -m pytest tests/unit/l2/debug_broker -q
```

The live matrix uses the safe test executable and real installed providers:

```powershell
$env:JAWL_DEBUG_BROKER_LIVE = "1"
venv\Scripts\python.exe -m pytest tests/integration/src/l2/debug_broker/test_live_debug_broker.py -q
```

The live test intentionally excludes TTD recording until its legal and
elevation prerequisites are satisfied. It still verifies TTD discovery and the
fail-closed readiness gate.

After explicit EULA acceptance, run JAWL elevated and opt in to the destructive
trace test:

```powershell
$env:JAWL_DEBUG_BROKER_JAWL_LIVE = "1"
$env:JAWL_DEBUG_BROKER_TTD_RECORD = "1"
venv\Scripts\python.exe -m pytest tests/integration/src/l2/debug_broker/test_live_jawl_registry.py -q
```
