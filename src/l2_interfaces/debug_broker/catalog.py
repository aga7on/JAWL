"""Compact progressive-discovery catalog for native RE providers."""

from __future__ import annotations

from typing import Any

from src.l2_interfaces.debug_broker.models import OperationSpec


def _object(
    properties: dict[str, Any] | None = None,
    required: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties or {},
        "required": required or [],
        "additionalProperties": False,
    }


def _string(description: str, **extra: Any) -> dict[str, Any]:
    return {"type": "string", "description": description, **extra}


def _integer(description: str, **extra: Any) -> dict[str, Any]:
    return {"type": "integer", "description": description, **extra}


def build_catalog() -> dict[tuple[str, str], OperationSpec]:
    specs = [
        OperationSpec(
            "x64dbg",
            "get_state",
            "Return debugging/running state and current instruction context.",
            _object(),
        ),
        OperationSpec(
            "x64dbg",
            "get_registers",
            "Return current CPU registers.",
            _object(),
        ),
        OperationSpec(
            "x64dbg",
            "get_modules",
            "Return loaded debugger modules.",
            _object(),
        ),
        OperationSpec(
            "x64dbg",
            "list_breakpoints",
            "Return current software, hardware, memory, and exception breakpoints.",
            _object(),
        ),
        OperationSpec(
            "x64dbg",
            "read_memory",
            "Read bounded process memory as hexadecimal bytes.",
            _object(
                {
                    "address": _string("Hexadecimal address."),
                    "size": _integer("Number of bytes.", minimum=1, maximum=65536),
                },
                ["address", "size"],
            ),
        ),
        OperationSpec(
            "x64dbg",
            "write_memory",
            "Write hexadecimal bytes into debuggee memory.",
            _object(
                {
                    "address": _string("Hexadecimal address."),
                    "hex_data": _string("Even-length hexadecimal byte string."),
                },
                ["address", "hex_data"],
            ),
            mutating=True,
        ),
        OperationSpec(
            "x64dbg",
            "disassemble",
            "Disassemble instructions starting at an address.",
            _object(
                {
                    "address": _string("Hexadecimal address."),
                    "count": _integer("Instruction count.", minimum=1, maximum=200),
                },
                ["address"],
            ),
        ),
        OperationSpec(
            "x64dbg",
            "set_breakpoint",
            "Set a software breakpoint at an address.",
            _object({"address": _string("Hexadecimal address.")}, ["address"]),
            mutating=True,
        ),
        OperationSpec(
            "x64dbg",
            "delete_breakpoint",
            "Delete a software breakpoint at an address.",
            _object({"address": _string("Hexadecimal address.")}, ["address"]),
            mutating=True,
        ),
        OperationSpec(
            "x64dbg",
            "control",
            "Run, pause, stop, step into, step over, or step out.",
            _object(
                {
                    "action": _string(
                        "Execution action.",
                        enum=["run", "pause", "stop", "step_in", "step_over", "step_out"],
                    )
                },
                ["action"],
            ),
            mutating=True,
        ),
        OperationSpec(
            "x64dbg",
            "raw_command",
            "Execute an x64dbg command when no typed operation exists.",
            _object({"command": _string("Bounded x64dbg command.")}, ["command"]),
            mutating=True,
        ),
        OperationSpec(
            "ghidra",
            "analyze",
            "Import and analyze the session target in a persistent headless project.",
            _object(
                {
                    "overwrite": {"type": "boolean", "default": False},
                    "analysis_timeout_sec": _integer(
                        "Headless analysis timeout.", minimum=30, maximum=1800
                    ),
                }
            ),
        ),
        OperationSpec(
            "ghidra",
            "export_program",
            "Export functions, entry points, memory blocks, imports, and exports as JSON.",
            _object(
                {
                    "max_functions": _integer(
                        "Maximum function records.", minimum=1, maximum=100000
                    )
                }
            ),
        ),
        OperationSpec("radare2", "info", "Return parsed binary metadata.", _object()),
        OperationSpec(
            "radare2",
            "analyze",
            "Run automatic analysis and return summary information.",
            _object(
                {
                    "level": _string(
                        "Analysis depth.", enum=["basic", "standard", "deep"]
                    )
                }
            ),
        ),
        OperationSpec(
            "radare2",
            "list_functions",
            "Return analyzed functions as structured JSON.",
            _object(
                {"limit": _integer("Maximum records.", minimum=1, maximum=10000)}
            ),
        ),
        OperationSpec(
            "radare2",
            "strings",
            "Return detected strings as structured JSON.",
            _object(
                {"limit": _integer("Maximum records.", minimum=1, maximum=10000)}
            ),
        ),
        OperationSpec(
            "radare2",
            "disassemble",
            "Return structured disassembly at an address or symbol.",
            _object(
                {
                    "address": _string("Address or radare2 symbol."),
                    "count": _integer("Instruction count.", minimum=1, maximum=500),
                },
                ["address"],
            ),
        ),
        OperationSpec(
            "radare2",
            "xrefs",
            "Return cross-references to an address or symbol.",
            _object({"address": _string("Address or radare2 symbol.")}, ["address"]),
        ),
        OperationSpec(
            "radare2",
            "raw_command",
            "Execute one bounded radare2 command and return JSON/text.",
            _object(
                {
                    "command": _string("radare2 command."),
                    "json": {"type": "boolean", "default": False},
                },
                ["command"],
            ),
        ),
        OperationSpec(
            "frida",
            "list_processes",
            "List local processes available to Frida.",
            _object(
                {"limit": _integer("Maximum records.", minimum=1, maximum=1000)}
            ),
            session_required=False,
        ),
        OperationSpec(
            "frida",
            "enumerate_modules",
            "Return modules loaded by the attached process.",
            _object(
                {"limit": _integer("Maximum records.", minimum=1, maximum=5000)}
            ),
        ),
        OperationSpec(
            "frida",
            "read_memory",
            "Read memory through an injected Frida agent.",
            _object(
                {
                    "address": _string("Hexadecimal native pointer."),
                    "size": _integer("Number of bytes.", minimum=1, maximum=1048576),
                },
                ["address", "size"],
            ),
        ),
        OperationSpec(
            "frida",
            "run_script",
            "Load a bounded Frida JavaScript snippet and return messages/RPC result.",
            _object(
                {
                    "source": _string("Frida JavaScript source.", maxLength=100000),
                    "rpc_method": _string("Optional exported RPC method."),
                    "rpc_arguments": {"type": "array", "maxItems": 32},
                    "settle_ms": _integer(
                        "Message collection delay.", minimum=0, maximum=30000
                    ),
                },
                ["source"],
            ),
            mutating=True,
        ),
        OperationSpec(
            "frida",
            "resume",
            "Resume a process spawned in a suspended state by Frida.",
            _object(),
            mutating=True,
        ),
        OperationSpec(
            "windbg",
            "ttd_status",
            (
                "Inspect standalone Time Travel Debugging recorder readiness. "
                "Reports installation, elevation, EULA state, and privacy warnings "
                "without accepting the EULA or changing system state."
            ),
            _object(),
            session_required=False,
        ),
        OperationSpec(
            "windbg",
            "record_trace",
            (
                "Record the session target with Microsoft TTD into the broker "
                "artifact directory. Requires an already accepted EULA and an "
                "elevated JAWL process; traces can contain sensitive memory data."
            ),
            _object(
                {
                    "arguments": {
                        "type": "array",
                        "items": _string("Debuggee argument."),
                        "maxItems": 32,
                    },
                    "ring": {
                        "type": "boolean",
                        "description": "Use a bounded ring-buffer trace.",
                        "default": True,
                    },
                    "max_file_mb": _integer(
                        "Maximum trace size in MB.", minimum=1, maximum=32768
                    ),
                    "artifact_name": _string(
                        "Optional safe trace basename ending in .run.",
                        maxLength=120,
                    ),
                    "timeout_sec": _integer(
                        "Recording timeout.", minimum=1, maximum=1800
                    ),
                }
            ),
            mutating=True,
        ),
        OperationSpec(
            "windbg",
            "run_commands",
            "Launch the target under CDB and execute bounded debugger commands.",
            _object(
                {
                    "commands": {
                        "type": "array",
                        "items": _string("One WinDbg command."),
                        "minItems": 1,
                        "maxItems": 64,
                    },
                    "arguments": {
                        "type": "array",
                        "items": _string("Debuggee argument."),
                        "maxItems": 32,
                    },
                    "timeout_sec": _integer(
                        "Debugger timeout.", minimum=1, maximum=1800
                    ),
                },
                ["commands"],
            ),
        ),
        OperationSpec(
            "windbg",
            "analyze_crash",
            "Run target, break on exceptions, and return registers and stack; optional extension analysis is slower.",
            _object(
                {
                    "arguments": {
                        "type": "array",
                        "items": _string("Debuggee argument."),
                        "maxItems": 32,
                    },
                    "include_extension_analysis": {
                        "type": "boolean",
                        "description": (
                            "Also run !analyze -v. This may download symbols and take "
                            "several minutes; disabled by default."
                        ),
                        "default": False,
                    },
                    "timeout_sec": _integer(
                        "Debugger timeout.", minimum=1, maximum=1800
                    ),
                }
            ),
        ),
        OperationSpec(
            "qiling",
            "emulate_shellcode",
            "Emulate hexadecimal machine code with instruction and memory hooks.",
            _object(
                {
                    "hex_code": _string("Hexadecimal machine code."),
                    "arch": _string(
                        "Architecture.", enum=["x86", "x8664", "arm", "arm64"]
                    ),
                    "os": _string(
                        "Operating system model.",
                        enum=["windows", "linux", "freebsd", "macos"],
                    ),
                    "max_instructions": _integer(
                        "Instruction budget.", minimum=1, maximum=1000000
                    ),
                },
                ["hex_code", "arch", "os"],
            ),
        ),
        OperationSpec(
            "qiling",
            "inspect_environment",
            "Return installed Qiling architectures, operating systems, and version.",
            _object(),
            session_required=False,
        ),
        OperationSpec(
            "triton",
            "execute_instructions",
            "Symbolically/concretely execute hexadecimal instructions.",
            _object(
                {
                    "architecture": _string(
                        "Triton architecture.", enum=["x86", "x86_64", "arm32", "aarch64"]
                    ),
                    "instructions": {
                        "type": "array",
                        "items": _string("Instruction bytes in hexadecimal."),
                        "minItems": 1,
                        "maxItems": 10000,
                    },
                    "registers": {"type": "object"},
                },
                ["architecture", "instructions"],
            ),
        ),
        OperationSpec(
            "triton",
            "solve_register",
            "Symbolize one register, execute instructions, and solve a target register value.",
            _object(
                {
                    "architecture": _string(
                        "Triton architecture.", enum=["x86", "x86_64"]
                    ),
                    "symbolic_register": _string("Input register name."),
                    "result_register": _string("Register constrained after execution."),
                    "target_value": _string("Target integer or hexadecimal value."),
                    "instructions": {
                        "type": "array",
                        "items": _string("Instruction bytes in hexadecimal."),
                        "minItems": 1,
                        "maxItems": 10000,
                    },
                },
                [
                    "architecture",
                    "symbolic_register",
                    "result_register",
                    "target_value",
                    "instructions",
                ],
            ),
        ),
    ]
    return {(spec.provider, spec.name): spec for spec in specs}
