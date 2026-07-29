"""Persistent Frida worker isolated from the main JAWL environment."""

from __future__ import annotations

import json
import sys
import time
import traceback
from pathlib import Path
from typing import Any

import frida


class FridaWorker:
    def __init__(self) -> None:
        self.device = frida.get_local_device()
        self.session = None
        self.pid: int | None = None
        self.spawned = False
        self.kill_on_close = True
        self.scripts: dict[str, Any] = {}

    def start(self, target: str | None, options: dict[str, Any]) -> dict[str, Any]:
        if self.session is not None:
            return {"pid": self.pid, "already_attached": True}
        if not target:
            return {"ready": True, "attached": False}
        arguments = [str(item) for item in options.get("arguments", [])]
        path = Path(target)
        should_spawn = bool(options.get("spawn", path.is_file()))
        if should_spawn:
            argv = [str(path), *arguments]
            self.pid = int(self.device.spawn(argv))
            self.spawned = True
            self.kill_on_close = bool(options.get("kill_on_close", True))
            self.session = self.device.attach(self.pid)
            if bool(options.get("resume", False)):
                self.device.resume(self.pid)
        else:
            attach_target: int | str = int(target) if str(target).isdigit() else target
            self.session = self.device.attach(attach_target)
            process = self.device.get_process(attach_target)
            self.pid = int(process.pid)
        return {
            "pid": self.pid,
            "spawned": self.spawned,
            "suspended": self.spawned and not bool(options.get("resume", False)),
        }

    def _require_session(self) -> Any:
        if self.session is None:
            raise RuntimeError("Frida session is not attached")
        return self.session

    def call(self, operation: str, arguments: dict[str, Any]) -> Any:
        if operation == "list_processes":
            limit = int(arguments.get("limit", 200))
            return [
                {"pid": int(process.pid), "name": process.name}
                for process in self.device.enumerate_processes()[:limit]
            ]
        if operation == "enumerate_modules":
            session = self._require_session()
            source = """
                rpc.exports = {
                    list() {
                        return Process.enumerateModules().map(m => ({
                            name: m.name, base: m.base.toString(),
                            size: m.size, path: m.path
                        }));
                    }
                };
            """
            script = session.create_script(source)
            script.load()
            try:
                result = script.exports_sync.list()
                return result[: int(arguments.get("limit", 1000))]
            finally:
                script.unload()
        if operation == "read_memory":
            session = self._require_session()
            address = str(arguments["address"])
            size = int(arguments["size"])
            source = f"""
                rpc.exports = {{
                    read() {{
                        const bytes = new Uint8Array(ptr({json.dumps(address)}).readByteArray({size}));
                        return Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
                    }}
                }};
            """
            script = session.create_script(source)
            script.load()
            try:
                return {"address": address, "size": size, "hex": script.exports_sync.read()}
            finally:
                script.unload()
        if operation == "run_script":
            session = self._require_session()
            messages: list[dict[str, Any]] = []

            def on_message(message: Any, data: Any) -> None:
                record: dict[str, Any] = {"message": message}
                if data is not None:
                    record["data_hex"] = bytes(data).hex()
                messages.append(record)

            script = session.create_script(str(arguments["source"]))
            script.on("message", on_message)
            script.load()
            result = None
            method = str(arguments.get("rpc_method", "")).strip()
            if method:
                rpc = getattr(script.exports_sync, method)
                result = rpc(*list(arguments.get("rpc_arguments", [])))
            settle_ms = int(arguments.get("settle_ms", 0))
            if settle_ms:
                time.sleep(settle_ms / 1000)
            script.unload()
            return {"rpc_result": result, "messages": messages}
        if operation == "resume":
            if not self.spawned or self.pid is None:
                raise RuntimeError("Frida did not spawn this process")
            self.device.resume(self.pid)
            return {"pid": self.pid, "resumed": True}
        raise KeyError(f"Unsupported Frida operation: {operation}")

    def close(self) -> dict[str, Any]:
        pid = self.pid
        killed = False
        for script in list(self.scripts.values()):
            try:
                script.unload()
            except Exception:
                pass
        self.scripts.clear()
        if self.session is not None:
            try:
                self.session.detach()
            finally:
                self.session = None
        if self.spawned and self.kill_on_close and pid is not None:
            try:
                self.device.kill(pid)
                killed = True
            except frida.ProcessNotFoundError:
                pass
        return {"detached": True, "pid": pid, "killed_owned_process": killed}


def main() -> None:
    worker = FridaWorker()
    for raw in sys.stdin:
        try:
            request = json.loads(raw)
            action = request.get("action")
            if action == "ping":
                result = {"version": frida.__version__, "device": worker.device.name}
            elif action == "start":
                result = worker.start(request.get("target"), request.get("options", {}))
            elif action == "call":
                result = worker.call(
                    str(request["operation"]), dict(request.get("arguments", {}))
                )
            elif action == "close":
                result = worker.close()
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
