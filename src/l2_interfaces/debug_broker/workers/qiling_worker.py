"""Isolated Qiling emulation worker."""

from __future__ import annotations

import json
import sys
import traceback
from typing import Any

import qiling
from qiling import Qiling
from qiling.const import QL_ARCH, QL_OS, QL_VERBOSE
from unicorn import UcError


ARCHES = {
    "x86": QL_ARCH.X86,
    "x8664": QL_ARCH.X8664,
    "arm": QL_ARCH.ARM,
    "arm64": QL_ARCH.ARM64,
}
SYSTEMS = {
    "windows": QL_OS.WINDOWS,
    "linux": QL_OS.LINUX,
    "freebsd": QL_OS.FREEBSD,
    "macos": QL_OS.MACOS,
}


def call(operation: str, arguments: dict[str, Any]) -> Any:
    if operation == "inspect_environment":
        return {
            "version": qiling.__version__,
            "architectures": sorted(ARCHES),
            "operating_systems": sorted(SYSTEMS),
        }
    if operation == "emulate_shellcode":
        code = bytes.fromhex(str(arguments["hex_code"]))
        architecture = str(arguments["arch"]).lower()
        operating_system = str(arguments["os"]).lower()
        budget = int(arguments.get("max_instructions", 10000))
        ql = Qiling(
            code=code,
            archtype=ARCHES[architecture],
            ostype=SYSTEMS[operating_system],
            verbose=QL_VERBOSE.DISABLED,
        )
        trace: list[str] = []
        counter = 0

        def on_code(runtime: Qiling, address: int, size: int) -> None:
            nonlocal counter
            counter += 1
            if len(trace) < 1000:
                trace.append(hex(address))
            if counter >= budget:
                runtime.stop()

        ql.hook_code(on_code)
        stop_reason = "completed"
        try:
            ql.run()
        except UcError as exc:
            # An emulation fault is analysis output, not a worker transport crash.
            # Agents need the partial trace and exact stop reason to investigate it.
            stop_reason = f"emulation_fault:{exc}"
        pc_name = "eip" if architecture == "x86" else "rip"
        pc = getattr(ql.arch.regs, pc_name, None)
        return {
            "instructions": counter,
            "trace": trace,
            "pc": hex(int(pc)) if pc is not None else None,
            "stop_reason": stop_reason,
        }
    raise KeyError(f"Unsupported Qiling operation: {operation}")


def main() -> None:
    for raw in sys.stdin:
        try:
            request = json.loads(raw)
            action = request.get("action")
            if action == "ping":
                result = {"version": qiling.__version__}
            elif action == "start":
                result = {"ready": True}
            elif action == "call":
                result = call(
                    str(request["operation"]), dict(request.get("arguments", {}))
                )
            elif action == "close":
                result = {"closed": True}
            else:
                raise KeyError(f"Unknown worker action: {action}")
            response = {"ok": True, "result": result}
        except Exception as exc:
            response = {
                "ok": False,
                "error": str(exc),
                "error_type": type(exc).__name__,
                "traceback": traceback.format_exc(limit=8),
            }
        print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
