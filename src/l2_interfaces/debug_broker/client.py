"""Stateful local broker for native debugger and RE provider processes."""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

try:
    import winreg
except ImportError:  # pragma: no cover - only reachable on non-Windows hosts
    winreg = None  # type: ignore[assignment]

from jsonschema import ValidationError, validate

from src.l2_interfaces.debug_broker.catalog import build_catalog
from src.l2_interfaces.debug_broker.models import DebugSession, OperationSpec
from src.utils._tools import truncate_text
from src.utils.logger import main_logger
from src.utils.settings import DebugBrokerConfig


class DebugBrokerError(RuntimeError):
    """A bounded, model-safe broker failure."""


class JsonLineWorker:
    """One isolated provider interpreter using a request/response JSON line protocol."""

    def __init__(
        self,
        python: Path,
        script: Path,
        *,
        cwd: Path,
        timeout: float,
    ) -> None:
        self.python = python
        self.script = script
        self.cwd = cwd
        self.timeout = timeout
        self.process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self.process is not None and self.process.returncode is None:
            return
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x08000000 | 0x00000200
        self.process = await asyncio.create_subprocess_exec(
            str(self.python),
            str(self.script),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.cwd),
            **kwargs,
        )
        await self.request({"action": "ping"})

    async def request(self, payload: dict[str, Any]) -> Any:
        async with self._lock:
            if (
                self.process is None
                or self.process.returncode is not None
                or self.process.stdin is None
                or self.process.stdout is None
            ):
                raise DebugBrokerError("Provider worker is not running")
            encoded = (
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
            ).encode("utf-8")
            self.process.stdin.write(encoded)
            await self.process.stdin.drain()
            try:
                raw = await asyncio.wait_for(
                    self.process.stdout.readline(), timeout=self.timeout
                )
            except asyncio.TimeoutError as exc:
                raise DebugBrokerError(
                    f"Provider worker timed out after {self.timeout:.0f}s"
                ) from exc
            if not raw:
                stderr = ""
                if self.process.stderr is not None:
                    try:
                        stderr = (
                            await asyncio.wait_for(
                                self.process.stderr.read(), timeout=0.5
                            )
                        ).decode("utf-8", errors="replace")
                    except asyncio.TimeoutError:
                        pass
                raise DebugBrokerError(
                    "Provider worker exited without a response"
                    + (f": {truncate_text(stderr, 2000)}" if stderr else "")
                )
            try:
                response = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise DebugBrokerError(
                    f"Provider worker returned invalid JSON: {truncate_text(raw.decode('utf-8', errors='replace'), 1000)}"
                ) from exc
            if not response.get("ok"):
                raise DebugBrokerError(
                    f"{response.get('error_type', 'ProviderError')}: "
                    f"{response.get('error', 'unknown provider failure')}"
                )
            return response.get("result")

    async def close(self) -> None:
        process = self.process
        if process is None:
            return
        if process.returncode is None:
            try:
                await self.request({"action": "close"})
            except Exception:
                pass
            if process.stdin is not None:
                process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except asyncio.TimeoutError:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=3)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
        self.process = None


class DebugBrokerClient:
    """Coordinates provider discovery, lifecycle, sessions, and bounded results."""

    _ACTIVE = {"ready", "running", "paused", "attached", "suspended"}

    def __init__(
        self,
        config: DebugBrokerConfig,
        framework_root: Path,
        *,
        state_namespace: str | None = None,
    ) -> None:
        self.config = config
        self.framework_root = framework_root.resolve()
        self.re_root = Path(config.re_root).expanduser().resolve()
        self.state_dir = self.re_root / "Workspaces" / "broker"
        self.artifacts_dir = self.re_root / "Artifacts" / "broker"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        if state_namespace is not None and not re.fullmatch(
            r"[A-Za-z0-9_.-]{1,80}", state_namespace
        ):
            raise ValueError("Invalid Debug Broker state namespace")
        suffix = f"-{state_namespace}" if state_namespace else ""
        self.sessions_path = self.state_dir / f"sessions{suffix}.json"
        self.events_path = self.state_dir / f"events{suffix}.jsonl"
        self.catalog = {
            key: spec
            for key, spec in build_catalog().items()
            if spec.provider in set(config.enabled_providers)
        }
        self.sessions: dict[str, DebugSession] = {}
        self.workers: dict[str, JsonLineWorker] = {}
        self.owned_processes: dict[str, subprocess.Popen[Any]] = {}
        self._windbg_path = self._discover_windbg()
        self._java_home = next(
            iter(
                sorted(
                    (
                        path
                        for path in Path("C:/Program Files/Microsoft").glob("jdk-21*")
                        if (path / "bin" / "java.exe").is_file()
                    ),
                    reverse=True,
                )
            ),
            None,
        )
        self._lock = asyncio.Lock()
        self._load_sessions()

    @staticmethod
    def _discover_windbg() -> Path | None:
        """Resolve the packaged CDB path without enumerating protected WindowsApps."""

        if sys.platform != "win32":
            return None
        try:
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "(Get-AppxPackage Microsoft.WinDbg | "
                    "Select-Object -First 1 -ExpandProperty InstallLocation)",
                ],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=10,
                creationflags=0x08000000,
            )
            location = result.stdout.strip()
            candidate = Path(location) / "amd64" / "cdb.exe"
            return candidate if location and candidate.is_file() else None
        except (OSError, subprocess.SubprocessError):
            return None

    def _load_sessions(self) -> None:
        if not self.sessions_path.is_file():
            return
        try:
            payload = json.loads(self.sessions_path.read_text(encoding="utf-8"))
            for record in payload.get("sessions", []):
                session = DebugSession(**record)
                if session.status in self._ACTIVE:
                    session.touch(
                        status="interrupted",
                        detail="JAWL restarted; provider ownership must be re-established.",
                    )
                self.sessions[session.session_id] = session
        except Exception as exc:
            main_logger.warning(f"[DebugBroker] Ignoring invalid session store: {exc}")

    def _persist(self) -> None:
        payload = {
            "version": 1,
            "sessions": [
                session.public()
                for session in sorted(
                    self.sessions.values(), key=lambda item: item.updated_at
                )[-self.config.max_sessions :]
            ],
        }
        temporary = self.sessions_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, self.sessions_path)

    def _event(
        self,
        event: str,
        *,
        session: DebugSession | None = None,
        detail: Any = None,
    ) -> None:
        record = {
            "at": time.time(),
            "event": event,
            "session_id": session.session_id if session else None,
            "provider": session.provider if session else None,
            "detail": detail,
        }
        with self.events_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")

    async def start(self) -> None:
        self._persist()
        providers = self.provider_snapshot()
        available = sum(1 for item in providers if item["available"])
        main_logger.info(
            f"[DebugBroker] Ready: {available}/{len(providers)} providers available."
        )

    async def stop(self) -> None:
        for worker in list(self.workers.values()):
            await worker.close()
        self.workers.clear()
        for process in list(self.owned_processes.values()):
            if process.poll() is None:
                process.terminate()
        self.owned_processes.clear()
        for session in self.sessions.values():
            if session.status in self._ACTIVE:
                session.touch(status="interrupted", detail="JAWL shutdown")
        self._persist()

    def _tool_paths(self) -> dict[str, Path | None]:
        legacy_ghidra = self.re_root / "ghidra_12.1.2_PUBLIC"
        ghidra_candidates = [
            self.re_root / "Tools" / "ghidra" / "current",
            legacy_ghidra,
        ]
        ghidra = next(
            (
                path / "support" / "analyzeHeadless.bat"
                for path in ghidra_candidates
                if (path / "support" / "analyzeHeadless.bat").is_file()
            ),
            None,
        )
        radare = next(
            iter(
                sorted(
                    (self.re_root / "Tools" / "radare2").glob(
                        "*/**/bin/radare2.exe"
                    ),
                    reverse=True,
                )
            ),
            None,
        )
        x64_root = self.re_root / "Tools" / "x64dbg" / "current"
        if not x64_root.exists():
            x64_root = self.re_root / "x64dbg"
        x32dbg = x64_root / "release" / "x32" / "x32dbg.exe"
        x64dbg = x64_root / "release" / "x64" / "x64dbg.exe"
        workers = (
            self.framework_root
            / "src"
            / "l2_interfaces"
            / "debug_broker"
            / "workers"
        )
        return {
            "ghidra": ghidra,
            "radare2": radare,
            "x32dbg": x32dbg if x32dbg.is_file() else None,
            "x64dbg": x64dbg if x64dbg.is_file() else None,
            "windbg": self._windbg_path,
            "dbgeng": self._existing(
                self._windbg_path.parent / "dbgeng.dll"
                if self._windbg_path is not None
                else Path("__unavailable__")
            ),
            "dbgmodel": self._existing(
                self._windbg_path.parent / "dbgmodel.dll"
                if self._windbg_path is not None
                else Path("__unavailable__")
            ),
            "ttdreplay": self._existing(
                self._windbg_path.parent / "ttd" / "TTDReplay.dll"
                if self._windbg_path is not None
                else Path("__unavailable__")
            ),
            "ttd": self._existing(
                self.re_root / "Tools" / "ttd" / "current" / "TTD.exe"
            ),
            "frida_python": self._existing(
                self.re_root / "Runtimes" / "frida" / "Scripts" / "python.exe"
            ),
            "frida_worker": self._existing(workers / "frida_worker.py"),
            "qiling_python": self._existing(
                self.re_root / "Runtimes" / "qiling" / "Scripts" / "python.exe"
            ),
            "qiling_worker": self._existing(workers / "qiling_worker.py"),
            "triton_python": self._existing(
                self.re_root / "Runtimes" / "triton" / "Scripts" / "python.exe"
            ),
            "triton_worker": self._existing(workers / "triton_worker.py"),
        }

    @staticmethod
    def _existing(path: Path) -> Path | None:
        return path if path.is_file() else None

    def provider_snapshot(self) -> list[dict[str, Any]]:
        paths = self._tool_paths()
        requirements = {
            "x64dbg": [paths["x32dbg"], paths["x64dbg"]],
            "ghidra": [paths["ghidra"]],
            "frida": [paths["frida_python"], paths["frida_worker"]],
            "windbg": [
                paths["windbg"],
                paths["dbgeng"],
                paths["dbgmodel"],
                paths["ttdreplay"],
                paths["ttd"],
            ],
            "radare2": [paths["radare2"]],
            "qiling": [paths["qiling_python"], paths["qiling_worker"]],
            "triton": [paths["triton_python"], paths["triton_worker"]],
        }
        active = {
            session.provider
            for session in self.sessions.values()
            if session.status in self._ACTIVE
        }
        result = []
        for provider in self.config.enabled_providers:
            candidates = requirements[provider]
            if provider == "x64dbg":
                available = any(item is not None for item in candidates)
            elif provider == "windbg":
                # CDB is the core provider; standalone TTD is an optional
                # capability reported separately by windbg.ttd_status.
                available = candidates[0] is not None
            else:
                available = all(item is not None for item in candidates)
            result.append(
                {
                    "provider": provider,
                    "available": available,
                    "active": provider in active,
                    "auto_start": self.config.auto_start,
                    "operations": sum(
                        1 for key in self.catalog if key[0] == provider
                    ),
                    "paths": [str(item) for item in candidates if item is not None],
                }
            )
        return result

    def search_operations(
        self, query: str, provider: str | None = None, limit: int = 12
    ) -> dict[str, Any]:
        words = set(re.findall(r"[a-z0-9_]+", query.lower()))
        scored: list[tuple[int, OperationSpec]] = []
        for spec in self.catalog.values():
            if provider and spec.provider != provider:
                continue
            haystack = f"{spec.provider} {spec.name} {spec.description}".lower()
            score = sum(3 if word in spec.name else 1 for word in words if word in haystack)
            if not words or score:
                scored.append((score, spec))
        scored.sort(key=lambda item: (-item[0], item[1].provider, item[1].name))
        return {
            "query": query,
            "provider": provider,
            "operations": [spec.public() for _, spec in scored[: max(1, min(limit, 50))]],
        }

    async def _make_worker(self, provider: str) -> JsonLineWorker:
        paths = self._tool_paths()
        python = paths.get(f"{provider}_python")
        script = paths.get(f"{provider}_worker")
        if not isinstance(python, Path) or not isinstance(script, Path):
            raise DebugBrokerError(f"{provider} runtime/worker is unavailable")
        worker = JsonLineWorker(
            python,
            script,
            cwd=self.framework_root,
            timeout=self.config.request_timeout_sec,
        )
        await worker.start()
        return worker

    async def start_session(
        self,
        provider: str,
        target: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if provider not in self.config.enabled_providers:
            raise DebugBrokerError(f"Provider is disabled: {provider}")
        snapshot = {item["provider"]: item for item in self.provider_snapshot()}
        if not snapshot[provider]["available"]:
            raise DebugBrokerError(f"Provider is unavailable: {provider}")
        options = dict(options or {})
        session = DebugSession(provider=provider, target=target, options=options)
        async with self._lock:
            if len(self.sessions) >= self.config.max_sessions:
                removable = sorted(
                    (
                        item
                        for item in self.sessions.values()
                        if item.status not in self._ACTIVE
                    ),
                    key=lambda item: item.updated_at,
                )
                if not removable:
                    raise DebugBrokerError("Debug session limit reached")
                self.sessions.pop(removable[0].session_id, None)
            self.sessions[session.session_id] = session
            try:
                if provider in {"frida", "qiling", "triton"}:
                    worker = await self._make_worker(provider)
                    self.workers[session.session_id] = worker
                    started = await worker.request(
                        {"action": "start", "target": target, "options": options}
                    )
                    session.provider_pid = (
                        worker.process.pid if worker.process is not None else None
                    )
                    session.metadata["provider"] = started
                    state = (
                        "suspended"
                        if isinstance(started, dict) and started.get("suspended")
                        else "attached"
                        if provider == "frida" and target
                        else "ready"
                    )
                    session.touch(status=state)
                elif provider == "x64dbg":
                    await self._ensure_x64dbg(session)
                    session.touch(status="ready")
                else:
                    session.touch(status="ready")
                self._event("session_started", session=session)
                self._persist()
                return session.public()
            except Exception as exc:
                session.touch(status="failed", detail=str(exc))
                self._event("session_start_failed", session=session, detail=str(exc))
                self._persist()
                raise

    async def stop_session(self, session_id: str) -> dict[str, Any]:
        session = self._session(session_id)
        worker = self.workers.pop(session_id, None)
        if worker is not None:
            await worker.close()
        process = self.owned_processes.pop(session_id, None)
        if process is not None and process.poll() is None:
            process.terminate()
        session.touch(status="closed")
        self._event("session_closed", session=session)
        self._persist()
        return session.public()

    def _session(self, session_id: str) -> DebugSession:
        session = self.sessions.get(session_id)
        if session is None:
            raise DebugBrokerError(f"Unknown debug session: {session_id}")
        return session

    async def call_operation(
        self,
        provider: str,
        operation: str,
        arguments: dict[str, Any],
        expected_schema_sha256: str,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        spec = self.catalog.get((provider, operation))
        if spec is None:
            raise DebugBrokerError(f"Unknown operation: {provider}.{operation}")
        if expected_schema_sha256 != spec.schema_sha256:
            raise DebugBrokerError(
                "Operation schema changed; search_operations must be called again"
            )
        try:
            validate(arguments, spec.input_schema)
        except ValidationError as exc:
            raise DebugBrokerError(
                f"Operation arguments failed schema validation: {exc.message}"
            ) from exc
        session: DebugSession | None = None
        if spec.session_required:
            if not session_id:
                raise DebugBrokerError("session_id is required for this operation")
            session = self._session(session_id)
            if session.provider != provider:
                raise DebugBrokerError(
                    f"Session provider is {session.provider}, not {provider}"
                )
            if session.status in {"failed", "closed", "interrupted"}:
                raise DebugBrokerError(
                    f"Session is not active: {session.status} ({session.detail})"
                )
        started_at = time.monotonic()
        try:
            result = await self._dispatch(provider, operation, arguments, session)
            if session:
                session.touch()
                self._persist()
            duration_ms = round((time.monotonic() - started_at) * 1000, 1)
            self._event(
                "operation_completed",
                session=session,
                detail={"operation": operation, "duration_ms": duration_ms},
            )
            return {
                "provider": provider,
                "operation": operation,
                "session_id": session_id,
                "duration_ms": duration_ms,
                "result": result,
            }
        except Exception as exc:
            self._event(
                "operation_failed",
                session=session,
                detail={"operation": operation, "error": str(exc)},
            )
            raise

    async def _dispatch(
        self,
        provider: str,
        operation: str,
        arguments: dict[str, Any],
        session: DebugSession | None,
    ) -> Any:
        if provider == "windbg":
            if operation == "dbgeng_status":
                return await self._dbgeng_status()
            if operation == "ttd_status":
                return self._ttd_status()
        if provider in {"frida", "qiling", "triton"}:
            if session is None:
                worker = await self._make_worker(provider)
                try:
                    return await worker.request(
                        {
                            "action": "call",
                            "operation": operation,
                            "arguments": arguments,
                        }
                    )
                finally:
                    await worker.close()
            worker = self.workers.get(session.session_id)
            if worker is None:
                raise DebugBrokerError("Provider worker ownership was lost")
            result = await worker.request(
                {"action": "call", "operation": operation, "arguments": arguments}
            )
            if provider == "frida" and operation == "resume":
                session.touch(status="running")
            return result
        if session is None:
            raise DebugBrokerError("Provider operation requires a session")
        if provider == "radare2":
            return await self._call_radare(session, operation, arguments)
        if provider == "ghidra":
            return await self._call_ghidra(session, operation, arguments)
        if provider == "windbg":
            return await self._call_windbg(session, operation, arguments)
        if provider == "x64dbg":
            return await self._call_x64dbg(session, operation, arguments)
        raise DebugBrokerError(f"Unsupported provider dispatch: {provider}")

    async def _run(
        self,
        argv: list[str],
        *,
        cwd: Path | None = None,
        timeout: float | None = None,
        env: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x08000000
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd or self.framework_root),
            env=env,
            **kwargs,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout or self.config.request_timeout_sec,
            )
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.wait()
            raise DebugBrokerError(
                f"Provider command timed out after {timeout or self.config.request_timeout_sec:.0f}s"
            ) from exc
        output = stdout.decode("utf-8", errors="replace")
        error = stderr.decode("utf-8", errors="replace")
        capture_limit = max(1_048_576, self.config.max_result_chars * 64)
        if len(output) > capture_limit or len(error) > capture_limit:
            raise DebugBrokerError(
                "Provider output exceeded the broker capture limit "
                f"({capture_limit} characters); narrow the query or lower its record limit"
            )
        if process.returncode != 0:
            raise DebugBrokerError(
                f"Provider exited with code {process.returncode}: "
                f"{truncate_text(error or output, 4000)}"
            )
        return {
            "returncode": process.returncode,
            # Structured CLIs such as radare2 emit one large JSON document.  Parse
            # the complete bounded capture first; truncating here corrupts JSON
            # before per-operation record limits can be applied.
            "stdout": output,
            "stderr": error,
        }

    @staticmethod
    def _parse_json_output(output: str) -> Any:
        stripped = output.strip()
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            for marker in ("[", "{"):
                position = stripped.find(marker)
                if position >= 0:
                    try:
                        return json.loads(stripped[position:])
                    except json.JSONDecodeError:
                        continue
        return {"text": stripped}

    async def _call_radare(
        self, session: DebugSession, operation: str, arguments: dict[str, Any]
    ) -> Any:
        if not session.target or not Path(session.target).is_file():
            raise DebugBrokerError("radare2 session target must be an existing file")
        executable = self._tool_paths()["radare2"]
        if not isinstance(executable, Path):
            raise DebugBrokerError("radare2 executable is unavailable")
        if operation == "info":
            command = "ij"
        elif operation == "analyze":
            command = {
                "basic": "aa;ij",
                "standard": "aaa;ij",
                "deep": "aaaa;ij",
            }.get(str(arguments.get("level", "standard")), "aaa;ij")
        elif operation == "list_functions":
            command = "aaa;aflj"
        elif operation == "strings":
            command = "izzj"
        elif operation == "disassemble":
            command = (
                f"aaa;pdj {int(arguments.get('count', 32))} @ {arguments['address']}"
            )
        elif operation == "xrefs":
            command = f"aaa;axtj @ {arguments['address']}"
        elif operation == "raw_command":
            command = str(arguments.get("command", ""))
        else:
            raise DebugBrokerError(f"Unsupported radare2 operation: {operation}")
        result = await self._run(
            [str(executable), "-q", "-2", "-c", command, str(session.target)]
        )
        parsed = self._parse_json_output(result["stdout"])
        limit = int(arguments.get("limit", 0))
        if limit and isinstance(parsed, list):
            parsed = parsed[:limit]
        return parsed

    async def _call_ghidra(
        self, session: DebugSession, operation: str, arguments: dict[str, Any]
    ) -> Any:
        if not session.target or not Path(session.target).is_file():
            raise DebugBrokerError("Ghidra session target must be an existing file")
        executable = self._tool_paths()["ghidra"]
        if not isinstance(executable, Path):
            raise DebugBrokerError("Ghidra headless analyzer is unavailable")
        project_dir = self.state_dir / "ghidra-projects"
        project_dir.mkdir(parents=True, exist_ok=True)
        output = self.artifacts_dir / f"{session.session_id}-ghidra.json"
        script_dir = (
            self.framework_root
            / "src"
            / "l2_interfaces"
            / "debug_broker"
            / "ghidra_scripts"
        )
        max_functions = int(arguments.get("max_functions", 10000))
        timeout = float(
            arguments.get(
                "analysis_timeout_sec", self.config.request_timeout_sec
            )
        )
        argv = [
            str(executable),
            str(project_dir),
            f"jawl-{session.session_id}",
            "-import",
            str(Path(session.target).resolve()),
            "-scriptPath",
            str(script_dir),
            "-postScript",
            "JawlExportProgram.java",
            str(output),
            str(max_functions),
        ]
        if bool(arguments.get("overwrite", False)):
            argv.append("-overwrite")
        if operation == "export_program" and output.is_file():
            return json.loads(output.read_text(encoding="utf-8"))
        environment = os.environ.copy()
        if self._java_home is not None:
            environment["JAVA_HOME"] = str(self._java_home)
            environment["PATH"] = (
                str(self._java_home / "bin")
                + os.pathsep
                + environment.get("PATH", "")
            )
        result = await self._run(argv, timeout=timeout, env=environment)
        if not output.is_file():
            raise DebugBrokerError(
                "Ghidra analysis completed without the expected JSON artifact: "
                + truncate_text(
                    result.get("stderr") or result.get("stdout", ""), 5000
                )
            )
        snapshot = json.loads(output.read_text(encoding="utf-8"))
        session.metadata["ghidra_artifact"] = str(output)
        if operation == "analyze":
            return {
                "artifact": str(output),
                "program": snapshot.get("program"),
                "format": snapshot.get("format"),
                "language": snapshot.get("language"),
                "function_count_exported": snapshot.get("function_count_exported"),
                "log_tail": result["stdout"][-4000:],
            }
        return snapshot

    async def _call_windbg(
        self, session: DebugSession, operation: str, arguments: dict[str, Any]
    ) -> Any:
        if not session.target or not Path(session.target).is_file():
            raise DebugBrokerError("WinDbg session target must be an existing file")
        executable = self._tool_paths()["windbg"]
        if not isinstance(executable, Path):
            raise DebugBrokerError("CDB is unavailable")
        timeout = float(arguments.get("timeout_sec", self.config.request_timeout_sec))
        if operation == "record_trace":
            return await self._record_ttd_trace(session, arguments, timeout)
        if operation == "replay_trace":
            return await self._replay_ttd_trace(session, arguments, timeout)
        if operation == "analyze_crash":
            commands = [
                ".symfix",
                "sxe av",
                "sxe sbo",
                "g",
                ".ecxr",
                "r",
                "k",
            ]
            if bool(arguments.get("include_extension_analysis", False)):
                commands.append("!analyze -v")
            commands.append("q")
        else:
            commands = [str(item) for item in arguments["commands"]]
            if not any(item.strip().lower() in {"q", "qd"} for item in commands):
                commands.append("q")
        command_text = ";".join(commands)
        debuggee_args = [str(item) for item in arguments.get("arguments", [])]
        result = await self._run(
            [
                str(executable),
                "-o",
                "-G",
                "-c",
                command_text,
                str(Path(session.target).resolve()),
                *debuggee_args,
            ],
            cwd=Path(session.target).resolve().parent,
            timeout=timeout,
        )
        return {
            "commands": commands,
            "output": result["stdout"],
            "stderr": result["stderr"],
        }

    @staticmethod
    def _is_elevated() -> bool:
        if sys.platform != "win32":
            return False
        try:
            import ctypes

            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except (AttributeError, OSError):
            return False

    @staticmethod
    def _ttd_eula_accepted() -> bool:
        if sys.platform != "win32" or winreg is None:
            return False
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, r"Software\Microsoft\TTD"
            ) as key:
                value, _ = winreg.QueryValueEx(key, "EULASigned")
                return bool(int(value))
        except (FileNotFoundError, OSError, TypeError, ValueError):
            return False

    def _ttd_status(self) -> dict[str, Any]:
        executable = self._tool_paths().get("ttd")
        installed = isinstance(executable, Path)
        elevated = self._is_elevated()
        eula_accepted = self._ttd_eula_accepted()
        version = None
        if installed:
            parent = executable.parent
            if parent.name.lower() == "current":
                try:
                    parent = parent.resolve()
                except OSError:
                    pass
            version = parent.name
        return {
            "installed": installed,
            "path": str(executable) if installed else None,
            "version": version,
            "elevated": elevated,
            "eula_accepted": eula_accepted,
            "record_ready": installed and elevated and eula_accepted,
            "agent_can_accept_eula": False,
            "agent_can_request_elevation": False,
            "warning": (
                "TTD recording is invasive, slows the target, and trace files can "
                "contain memory, file paths, registry data, credentials, or other "
                "sensitive information."
            ),
            "remediation": (
                "Read the Microsoft TTD EULA, accept it manually, then launch JAWL "
                "from an elevated terminal."
                if not (elevated and eula_accepted)
                else None
            ),
        }

    async def _dbgeng_status(self) -> dict[str, Any]:
        paths = self._tool_paths()
        executable = paths["windbg"]
        if not isinstance(executable, Path):
            raise DebugBrokerError("CDB/DbgEng is unavailable")
        result = await self._run([str(executable), "-version"], timeout=30)
        version_text = result["stdout"].strip()
        version_match = re.search(r"cdb version\s+([0-9.]+)", version_text, re.I)
        components = {
            "dbgeng": paths["dbgeng"],
            "dbgmodel": paths["dbgmodel"],
            "ttd_replay": paths["ttdreplay"],
        }
        return {
            "engine_probe": "cdb -version",
            "engine_version": version_match.group(1) if version_match else None,
            "output": version_text,
            "cdb_path": str(executable),
            "components": {
                name: {
                    "installed": isinstance(path, Path),
                    "path": str(path) if isinstance(path, Path) else None,
                }
                for name, path in components.items()
            },
            "all_components_installed": all(
                isinstance(path, Path) for path in components.values()
            ),
        }

    async def _record_ttd_trace(
        self,
        session: DebugSession,
        arguments: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        status = self._ttd_status()
        if not status["installed"]:
            raise DebugBrokerError("Standalone Microsoft TTD recorder is unavailable")
        if not status["eula_accepted"]:
            raise DebugBrokerError(
                "TTD EULA is not accepted. The broker will not accept legal terms "
                "on the user's behalf; review and accept it manually first."
            )
        if not status["elevated"]:
            raise DebugBrokerError(
                "TTD recording requires an elevated JAWL process. The broker will "
                "not trigger UAC automatically."
            )
        if not session.target or not Path(session.target).is_file():
            raise DebugBrokerError("TTD session target must be an existing executable")

        artifact_dir = self.artifacts_dir / "ttd"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        requested_name = str(arguments.get("artifact_name") or "").strip()
        if requested_name:
            safe_name = Path(requested_name).name
            if safe_name != requested_name or not safe_name.lower().endswith(".run"):
                raise DebugBrokerError(
                    "artifact_name must be a safe basename ending in .run"
                )
        else:
            safe_name = f"{session.session_id}-{int(time.time())}.run"
        output = artifact_dir / safe_name
        if output.exists():
            raise DebugBrokerError(f"TTD artifact already exists: {output}")

        executable = self._tool_paths()["ttd"]
        assert isinstance(executable, Path)
        argv = [str(executable), "-noUI", "-out", str(output)]
        if bool(arguments.get("ring", True)):
            argv.append("-ring")
        if "max_file_mb" in arguments:
            argv.extend(["-maxFile", str(int(arguments["max_file_mb"]))])
        argv.extend(
            [
                "-launch",
                str(Path(session.target).resolve()),
                *[str(item) for item in arguments.get("arguments", [])],
            ]
        )
        result = await self._run(
            argv,
            cwd=Path(session.target).resolve().parent,
            timeout=timeout,
        )
        if not output.is_file():
            raise DebugBrokerError(
                "TTD exited without creating the expected trace artifact: "
                + truncate_text(result.get("stderr") or result.get("stdout", ""), 4000)
            )
        session.metadata["ttd_artifact"] = str(output)
        return {
            "artifact": str(output),
            "size_bytes": output.stat().st_size,
            "ring": bool(arguments.get("ring", True)),
            "output": result["stdout"],
            "stderr": result["stderr"],
            "privacy_warning": status["warning"],
        }

    async def _replay_ttd_trace(
        self,
        session: DebugSession,
        arguments: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        if not session.target:
            raise DebugBrokerError("TTD replay requires a trace target")
        trace = Path(session.target).resolve()
        if not trace.is_file() or trace.suffix.lower() != ".run":
            raise DebugBrokerError("TTD replay target must be an existing .run file")
        executable = self._tool_paths()["windbg"]
        if not isinstance(executable, Path):
            raise DebugBrokerError("CDB/DbgEng is unavailable")
        commands = [
            str(item)
            for item in arguments.get(
                "commands",
                ["!index", "dx @$curprocess.TTD.Position", "r", "k"],
            )
        ]
        if not any(item.strip().lower() in {"q", "qd"} for item in commands):
            commands.append("q")
        result = await self._run(
            [
                str(executable),
                "-z",
                str(trace),
                "-c",
                ";".join(commands),
            ],
            cwd=trace.parent,
            timeout=timeout,
        )
        index = trace.with_suffix(".idx")
        session.metadata["ttd_trace"] = str(trace)
        session.metadata["ttd_index"] = str(index) if index.is_file() else None
        return {
            "trace": str(trace),
            "index": str(index) if index.is_file() else None,
            "commands": commands,
            "output": result["stdout"],
            "stderr": result["stderr"],
        }

    @staticmethod
    def _port_open(port: int = 8888) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            return False

    def _x64_server_target(self, port: int) -> Path | None:
        """Return the main module path exposed by one x64dbg server."""

        try:
            modules = self._x64_http("/GetModuleList", None, port=port)
        except DebugBrokerError:
            return None
        if not isinstance(modules, list) or not modules:
            return None
        path = modules[0].get("path") if isinstance(modules[0], dict) else None
        return Path(path).resolve() if isinstance(path, str) and path else None

    @staticmethod
    def _same_windows_path(left: Path, right: Path) -> bool:
        return os.path.normcase(str(left.resolve())) == os.path.normcase(
            str(right.resolve())
        )

    def _free_x64_port(self) -> int:
        for port in range(
            self.config.x64dbg_port_start, self.config.x64dbg_port_end + 1
        ):
            if not self._port_open(port):
                return port
        raise DebugBrokerError(
            "No free x64dbg broker port is available in "
            f"{self.config.x64dbg_port_start}-{self.config.x64dbg_port_end}"
        )

    @staticmethod
    def _pe_architecture(path: Path) -> str:
        try:
            with path.open("rb") as stream:
                if stream.read(2) != b"MZ":
                    return "x64"
                stream.seek(0x3C)
                pe_offset = int.from_bytes(stream.read(4), "little")
                stream.seek(pe_offset + 4)
                machine = int.from_bytes(stream.read(2), "little")
                return "x64" if machine == 0x8664 else "x86"
        except OSError:
            return "x64"

    async def _ensure_x64dbg(self, session: DebugSession) -> None:
        requested_port = int(
            session.options.get("port", self.config.x64dbg_port_start)
        )
        if not (
            self.config.x64dbg_port_start
            <= requested_port
            <= self.config.x64dbg_port_end
        ):
            raise DebugBrokerError(
                "x64dbg port must be inside the configured broker port range"
            )
        port = requested_port
        if self._port_open(port):
            expected = Path(session.target).resolve() if session.target else None
            actual = self._x64_server_target(port)
            if expected is None or (
                actual is not None and self._same_windows_path(actual, expected)
            ):
                session.metadata.update(
                    {
                        "port": port,
                        "reused_server": True,
                        "server_target": str(actual) if actual else None,
                    }
                )
                return
            # Never redirect a new target session into an unrelated user
            # debugger. A freshly launched broker-owned instance gets another
            # loopback port instead.
            port = self._free_x64_port()
            session.metadata["port_collision"] = {
                "port": requested_port,
                "existing_target": str(actual) if actual else "unknown",
            }
        if not self.config.auto_start:
            raise DebugBrokerError("x64dbg plugin is offline and auto_start is disabled")
        if not session.target or not Path(session.target).is_file():
            raise DebugBrokerError(
                "x64dbg auto-start requires an existing PE target"
            )
        paths = self._tool_paths()
        architecture = str(
            session.options.get(
                "architecture", self._pe_architecture(Path(session.target))
            )
        ).lower()
        executable = paths["x32dbg"] if architecture == "x86" else paths["x64dbg"]
        if not isinstance(executable, Path):
            raise DebugBrokerError(f"{architecture} x64dbg executable is unavailable")
        environment = os.environ.copy()
        environment["JAWL_X64DBG_PORT"] = str(port)
        process = subprocess.Popen(
            [
                str(executable),
                str(Path(session.target).resolve()),
                *[str(item) for item in session.options.get("arguments", [])],
            ],
            cwd=str(Path(session.target).resolve().parent),
            env=environment,
        )
        self.owned_processes[session.session_id] = process
        session.provider_pid = process.pid
        deadline = time.monotonic() + self.config.startup_timeout_sec
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise DebugBrokerError(
                    f"x64dbg exited during startup with code {process.returncode}"
                )
            if self._port_open(port):
                session.metadata.update(
                    {"architecture": architecture, "port": port}
                )
                return
            await asyncio.sleep(0.25)
        raise DebugBrokerError(
            f"x64dbg plugin did not open port {port} before timeout"
        )

    def _x64_http(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
        *,
        port: int = 8888,
    ) -> Any:
        query = urllib.parse.urlencode(params or {})
        url = f"http://127.0.0.1:{port}{endpoint}" + (
            f"?{query}" if query else ""
        )
        try:
            with urllib.request.urlopen(
                url, timeout=self.config.request_timeout_sec
            ) as response:
                text = response.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError) as exc:
            raise DebugBrokerError(f"x64dbg HTTP request failed: {exc}") from exc
        try:
            result = json.loads(text)
        except json.JSONDecodeError:
            result = text
        if isinstance(result, str) and result.lstrip().startswith(("[", "{")):
            try:
                result = json.loads(result)
            except json.JSONDecodeError:
                pass
        result = self._repair_x64dbg_text(result)
        if isinstance(result, dict) and (
            result.get("success") is False or result.get("error")
        ):
            raise DebugBrokerError(f"x64dbg operation failed: {result}")
        if isinstance(result, str) and re.search(
            r"(?i)(?:error|failed|invalid|not found|недопуст|ошибк|не существует)",
            result,
        ):
            raise DebugBrokerError(f"x64dbg operation failed: {result}")
        return result

    @classmethod
    def _repair_x64dbg_text(cls, value: Any) -> Any:
        """Repair UTF-8 text accidentally decoded as Windows-1251 by x64dbg."""
        if isinstance(value, dict):
            return {
                key: cls._repair_x64dbg_text(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [cls._repair_x64dbg_text(item) for item in value]
        if not isinstance(value, str):
            return value

        suspicious = re.compile(
            r"(?:Р[°±²µ¶·ё№»јЅѕї]|С[ЂЃ‚ѓ„…†‡€‰Љ‹ЊЌЋЏђ‘’“”•–—™љ›њќћџ])"
        )
        before_score = len(suspicious.findall(value))
        if before_score < 2:
            return value
        try:
            repaired = value.encode("cp1251").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return value
        return (
            repaired
            if len(suspicious.findall(repaired)) < before_score
            else value
        )

    async def _call_x64dbg(
        self, session: DebugSession, operation: str, arguments: dict[str, Any]
    ) -> Any:
        port = int(session.metadata.get("port", self.config.x64dbg_port_start))
        if not self._port_open(port):
            await self._ensure_x64dbg(session)
            port = int(session.metadata.get("port", self.config.x64dbg_port_start))
        endpoints: dict[str, tuple[str, dict[str, Any] | None]] = {
            "get_state": ("/GetState", None),
            "get_registers": ("/GetRegisters", None),
            "get_modules": ("/GetModuleList", None),
            "list_breakpoints": ("/ExecCommand", {"cmd": "bplist"}),
            "read_memory": (
                "/Memory/Read",
                {
                    "addr": arguments.get("address"),
                    "size": str(arguments.get("size")),
                },
            ),
            "write_memory": (
                "/Memory/Write",
                {
                    "addr": arguments.get("address"),
                    "data": arguments.get("hex_data"),
                },
            ),
            "disassemble": (
                "/Disasm/GetInstructionRange",
                {
                    "addr": arguments.get("address"),
                    "count": str(arguments.get("count", 32)),
                },
            ),
            "set_breakpoint": (
                "/Debug/SetBreakpoint",
                {"addr": arguments.get("address")},
            ),
            "delete_breakpoint": (
                "/Debug/DeleteBreakpoint",
                {"addr": arguments.get("address")},
            ),
            "raw_command": (
                "/ExecCommand",
                {"cmd": arguments.get("command")},
            ),
        }
        if operation == "control":
            endpoint = {
                "run": "/Debug/Run",
                "pause": "/Debug/Pause",
                "stop": "/Debug/Stop",
                "step_in": "/Debug/StepIn",
                "step_over": "/Debug/StepOver",
                "step_out": "/Debug/StepOut",
            }[str(arguments["action"])]
            endpoints[operation] = (endpoint, None)
        endpoint, params = endpoints[operation]
        result = await asyncio.to_thread(
            self._x64_http, endpoint, params, port=port
        )
        if operation == "control":
            state = {
                "run": "running",
                "pause": "paused",
                "stop": "closed",
                "step_in": "paused",
                "step_over": "paused",
                "step_out": "paused",
            }[str(arguments["action"])]
            session.touch(status=state)
        return result

    async def wait_session(
        self, session_id: str, timeout_seconds: float = 30
    ) -> dict[str, Any]:
        session = self._session(session_id)
        started = time.monotonic()
        initial = session.status
        while time.monotonic() - started < timeout_seconds:
            port = int(
                session.metadata.get("port", self.config.x64dbg_port_start)
            )
            if session.provider == "x64dbg" and self._port_open(port):
                try:
                    state = await asyncio.to_thread(
                        self._x64_http, "/GetState", None, port=port
                    )
                    if isinstance(state, dict):
                        if state.get("isPaused"):
                            session.touch(status="paused")
                        elif state.get("isRunning"):
                            session.touch(status="running")
                        elif state.get("isDebugging") is False:
                            session.touch(status="closed")
                        session.metadata["last_state"] = state
                except DebugBrokerError as exc:
                    session.touch(status="interrupted", detail=str(exc))
            process = self.owned_processes.get(session_id)
            if process is not None and process.poll() is not None:
                session.touch(status="closed", detail=f"process exit {process.returncode}")
            worker = self.workers.get(session_id)
            if (
                worker is not None
                and worker.process is not None
                and worker.process.returncode is not None
            ):
                session.touch(
                    status="interrupted",
                    detail=f"provider worker exit {worker.process.returncode}",
                )
            if session.status != initial or session.status not in self._ACTIVE:
                break
            await asyncio.sleep(0.25)
        self._persist()
        return {
            "timed_out": session.status == initial
            and session.status in self._ACTIVE
            and time.monotonic() - started >= timeout_seconds,
            "waited_seconds": round(time.monotonic() - started, 2),
            "session": session.public(),
        }

    def session_snapshot(self, session_id: str | None = None) -> dict[str, Any]:
        if session_id:
            return {"session": self._session(session_id).public()}
        return {
            "providers": self.provider_snapshot(),
            "sessions": [
                item.public()
                for item in sorted(
                    self.sessions.values(),
                    key=lambda value: value.updated_at,
                    reverse=True,
                )[: self.config.max_sessions]
            ],
        }

    def bounded_json(self, payload: Any) -> str:
        encoded = json.dumps(payload, ensure_ascii=False, indent=2)
        if len(encoded) <= self.config.max_result_chars:
            return encoded
        limit = self.config.max_result_chars
        preview_budget = max(0, limit - 180)
        while True:
            envelope = {
                "truncated": True,
                "original_chars": len(encoded),
                "preview": truncate_text(encoded, preview_budget),
                "hint": "Narrow the operation query or request a smaller record limit.",
            }
            result = json.dumps(envelope, ensure_ascii=False)
            if len(result) <= limit or preview_budget == 0:
                return result
            preview_budget = max(0, preview_budget - (len(result) - limit) - 8)

    async def get_context_block(self, **_: Any) -> str:
        active = [
            session
            for session in self.sessions.values()
            if session.status in self._ACTIVE
        ]
        providers = self.provider_snapshot()
        available = ", ".join(
            item["provider"] for item in providers if item["available"]
        )
        lines = [
            "### DEBUG BROKER [ON]",
            f"Available providers: {available or 'none'}.",
            (
                "Use progressive discovery: DebugBroker.search_operations, "
                "start_session, call_operation, wait_session, stop_session."
            ),
        ]
        if active:
            lines.append("Active sessions:")
            lines.extend(
                f"- {item.session_id}: {item.provider} {item.status} target={item.target}"
                for item in active[-5:]
            )
        return "\n".join(lines)
