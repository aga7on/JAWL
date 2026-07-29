"""Isolated Triton symbolic execution worker."""

from __future__ import annotations

import json
import sys
import traceback
from typing import Any

import triton
from triton import ARCH, Instruction, TritonContext


ARCHES = {
    "x86": ARCH.X86,
    "x86_64": ARCH.X86_64,
    "arm32": ARCH.ARM32,
    "aarch64": ARCH.AARCH64,
}


def _integer(value: Any) -> int:
    return int(str(value), 0)


def _register(context: TritonContext, name: str) -> Any:
    register = getattr(context.registers, name.lower(), None)
    if register is None:
        raise ValueError(f"Unknown Triton register: {name}")
    return register


def _execute(
    context: TritonContext, instructions: list[str], base: int = 0x100000
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    address = base
    for encoded in instructions:
        instruction = Instruction(bytes.fromhex(encoded))
        instruction.setAddress(address)
        context.processing(instruction)
        records.append(
            {
                "address": hex(address),
                "opcode": encoded.lower(),
                "disassembly": instruction.getDisassembly(),
                "type": int(instruction.getType()),
            }
        )
        address += instruction.getSize()
    return records


def call(operation: str, arguments: dict[str, Any]) -> Any:
    architecture = str(arguments.get("architecture", "x86_64"))
    context = TritonContext(ARCHES[architecture])
    if operation == "execute_instructions":
        registers = dict(arguments.get("registers", {}))
        for name, value in registers.items():
            context.setConcreteRegisterValue(_register(context, name), _integer(value))
        records = _execute(context, list(arguments["instructions"]))
        final = {
            name: hex(context.getConcreteRegisterValue(_register(context, name)))
            for name in registers
        }
        for name in ("rax", "rbx", "rcx", "rdx", "rip", "eax", "ebx", "ecx", "edx", "eip"):
            if getattr(context.registers, name, None) is not None:
                final.setdefault(
                    name,
                    hex(context.getConcreteRegisterValue(_register(context, name))),
                )
        return {"instructions": records, "registers": final}
    if operation == "solve_register":
        source = _register(context, str(arguments["symbolic_register"]))
        result = _register(context, str(arguments["result_register"]))
        symbolic = context.symbolizeRegister(source, "jawl_input")
        records = _execute(context, list(arguments["instructions"]))
        expression = context.getSymbolicRegister(result)
        if expression is None:
            raise RuntimeError("Result register is not symbolic after execution")
        target = _integer(arguments["target_value"])
        model = context.getModel(expression.getAst() == target)
        solved = {
            str(variable_id): {
                "name": item.getVariable().getName(),
                "value": hex(item.getValue()),
            }
            for variable_id, item in model.items()
        }
        return {
            "symbolic_variable": symbolic.getName(),
            "target": hex(target),
            "model": solved,
            "instructions": records,
        }
    raise KeyError(f"Unsupported Triton operation: {operation}")


def main() -> None:
    for raw in sys.stdin:
        try:
            request = json.loads(raw)
            action = request.get("action")
            if action == "ping":
                result = {"version": getattr(triton, "__version__", "unknown")}
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
