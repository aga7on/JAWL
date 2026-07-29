"""Bounded, inspectable short-lived script execution sessions."""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from src.l2_interfaces.host.os.client import HostOSAccessLevel, HostOSClient
from src.l2_interfaces.host.os.decorators import require_access
from src.l2_interfaces.host.os.skills.execution import HostOSExecution
from src.l3_agent.skills.registry import SkillResult, skill
from src.l3_agent.swarm.roles import Subagents
from src.utils._tools import redact_sensitive_text
from src.utils.event.bus import EventBus
from src.utils.event.registry import Events
from src.utils.logger import main_logger


class HostOSProcessSessions:
    """Runs finite Python scripts without holding one ReAct tool call open."""

    _ACTIVE = {"running"}
    _TERMINAL = {"completed", "failed", "timed_out", "cancelled", "interrupted"}
    _ID = re.compile(r"^[a-f0-9]{12}$")
    _MAX_RECORDS = 100

    def __init__(
        self,
        host_os_client: HostOSClient,
        execution: HostOSExecution,
        event_bus: Optional[EventBus] = None,
    ) -> None:
        self.host_os = host_os_client
        self.execution = execution
        self.event_bus = event_bus
        self.registry_path = self.host_os.system_dir / "process_sessions.json"
        self.logs_dir = self.host_os.system_dir / "process_sessions"
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self._processes: dict[str, subprocess.Popen] = {}
        self._monitors: dict[str, asyncio.Task] = {}
        self._log_streams: dict[str, Any] = {}
        self._completion: dict[str, asyncio.Event] = {}
        self._lock = asyncio.Lock()
        if not self.registry_path.exists():
            self._write({"version": 1, "sessions": {}})

    async def start(self) -> None:
        """Classify unfinished records from a prior JAWL process as interrupted."""

        async with self._lock:
            payload = self._read()
            changed = False
            now = time.time()
            for record in payload["sessions"].values():
                if record.get("status") == "running":
                    record["status"] = "interrupted"
                    record["finished_at"] = now
                    record["detail"] = "JAWL restarted before the process result was observed."
                    changed = True
            if changed:
                self._write(payload)

    async def stop(self) -> None:
        """Terminate only exact current-process session handles during shutdown."""

        for session_id, process in list(self._processes.items()):
            if process.poll() is None:
                self.execution._kill_process_tree(process.pid)
            await self._finish(session_id, "cancelled", detail="JAWL shutdown")
        tasks = [task for task in self._monitors.values() if not task.done()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    @skill(swarm=[Subagents.CODER, Subagents.QA_ENGINEER, Subagents.SYSADMIN])
    @require_access(HostOSAccessLevel.OBSERVER)
    async def start_script_session(
        self,
        filepath: str,
        arguments: Optional[list[str]] = None,
        timeout_seconds: int = 300,
    ) -> SkillResult:
        """Starts a finite Python script and returns an ID for status/wait calls."""

        if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool):
            return SkillResult.fail("timeout_seconds must be an integer between 1 and 3600.")
        if timeout_seconds < 1 or timeout_seconds > 3600:
            return SkillResult.fail("timeout_seconds must be between 1 and 3600.")
        arguments = [] if arguments is None else arguments
        if not isinstance(arguments, list) or len(arguments) > 32:
            return SkillResult.fail("arguments must be a list containing at most 32 strings.")
        if any(not isinstance(item, str) or len(item) > 4096 for item in arguments):
            return SkillResult.fail("Every script argument must be a string of at most 4096 characters.")

        try:
            safe_path = self.host_os.validate_path(filepath, is_write=False)
            if (
                self.host_os.access_level < HostOSAccessLevel.OPERATOR
                and not safe_path.is_relative_to(self.host_os.sandbox_dir)
            ):
                return SkillResult.fail(
                    "Access denied: scripts must be inside sandbox/ at this access level."
                )
            if not safe_path.is_file() or safe_path.suffix.lower() != ".py":
                return SkillResult.fail("Only existing Python scripts are supported by finite sessions.")

            runner = (
                self.host_os.framework_dir
                / "src"
                / "utils"
                / "templates"
                / "sandbox_runner.py"
            )
            guarded = self.host_os.access_level < HostOSAccessLevel.OPERATOR
            if guarded and not runner.is_file():
                return SkillResult.fail("Sandbox runner is unavailable; refusing unguarded execution.")
            command = (
                [sys.executable, str(runner)]
                if guarded
                else [sys.executable, str(safe_path), *arguments]
            )
            env = self.execution._build_isolated_env()
            if guarded:
                env["JAWL_TARGET_SCRIPT"] = str(safe_path)
                env["JAWL_SCRIPT_ARGS"] = json.dumps(arguments, ensure_ascii=False)

            session_id = uuid.uuid4().hex[:12]
            log_path = self.logs_dir / f"{session_id}.log"
            log_stream = open(log_path, "ab", buffering=0)
            kwargs: dict[str, Any] = {}
            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | 0x08000000
            else:
                kwargs["start_new_session"] = True
            process = subprocess.Popen(
                command,
                stdout=log_stream,
                stderr=subprocess.STDOUT,
                cwd=str(safe_path.parent),
                env=env,
                **kwargs,
            )
            record = {
                "id": session_id,
                "status": "running",
                "pid": process.pid,
                "filepath": self._display_path(safe_path),
                "arguments_count": len(arguments),
                "execution_mode": "sandbox" if guarded else "host",
                "timeout_seconds": timeout_seconds,
                "started_at": time.time(),
                "finished_at": None,
                "exit_code": None,
                "log_path": self._display_path(log_path),
            }
            async with self._lock:
                payload = self._read()
                payload["sessions"][session_id] = record
                self._prune(payload)
                self._write(payload)
            self._processes[session_id] = process
            self._log_streams[session_id] = log_stream
            self._completion[session_id] = asyncio.Event()
            monitor = asyncio.create_task(
                self._monitor(session_id, process, timeout_seconds)
            )
            self._monitors[session_id] = monitor
            return SkillResult.ok(json.dumps(record, ensure_ascii=False, indent=2))
        except PermissionError as exc:
            return SkillResult.fail(str(exc))
        except Exception as exc:
            return SkillResult.fail(f"Could not start script session: {exc}")

    @skill(swarm=[Subagents.CODER, Subagents.QA_ENGINEER, Subagents.SYSADMIN])
    @require_access(HostOSAccessLevel.OBSERVER)
    async def get_script_session(
        self, session_id: str, log_tail_chars: int = 4000
    ) -> SkillResult:
        """Returns exact status, exit code, and a bounded tail of session output."""

        if not isinstance(log_tail_chars, int) or isinstance(log_tail_chars, bool):
            return SkillResult.fail("log_tail_chars must be an integer between 0 and 20000.")
        if log_tail_chars < 0 or log_tail_chars > 20000:
            return SkillResult.fail("log_tail_chars must be between 0 and 20000.")
        record = await self._get_record(session_id)
        if isinstance(record, SkillResult):
            return record
        return SkillResult.ok(
            json.dumps(
                {**record, "log_tail": self._read_log_tail(record, log_tail_chars)},
                ensure_ascii=False,
                indent=2,
            )
        )

    @skill(swarm=[Subagents.CODER, Subagents.QA_ENGINEER, Subagents.SYSADMIN])
    @require_access(HostOSAccessLevel.OBSERVER)
    async def wait_for_script_session(
        self,
        session_id: str,
        wait_seconds: int = 30,
        log_tail_chars: int = 4000,
    ) -> SkillResult:
        """Waits at most 60 seconds, then returns current exact session state."""

        if not isinstance(wait_seconds, int) or isinstance(wait_seconds, bool):
            return SkillResult.fail("wait_seconds must be an integer between 0 and 60.")
        if wait_seconds < 0 or wait_seconds > 60:
            return SkillResult.fail("wait_seconds must be between 0 and 60.")
        record = await self._get_record(session_id)
        if isinstance(record, SkillResult):
            return record
        event = self._completion.get(session_id)
        if record.get("status") == "running" and event is not None and wait_seconds:
            try:
                await asyncio.wait_for(event.wait(), timeout=wait_seconds)
            except asyncio.TimeoutError:
                pass
        return await self.get_script_session(session_id, log_tail_chars)

    @skill(swarm=[Subagents.SYSADMIN])
    @require_access(HostOSAccessLevel.OBSERVER)
    async def cancel_script_session(self, session_id: str) -> SkillResult:
        """Cancels one exact current-process script session and its process tree."""

        record = await self._get_record(session_id)
        if isinstance(record, SkillResult):
            return record
        if record.get("status") != "running":
            return SkillResult.fail(
                f"Script session {session_id} is already {record.get('status')}."
            )
        process = self._processes.get(session_id)
        if process is None or process.poll() is not None:
            return SkillResult.fail("No exact active process handle exists for this session.")
        self.execution._kill_process_tree(process.pid)
        await self._finish(session_id, "cancelled", detail="Cancelled by agent")
        return SkillResult.ok(f"Script session {session_id} cancelled.")

    async def _monitor(
        self, session_id: str, process: subprocess.Popen, timeout_seconds: int
    ) -> None:
        deadline = time.monotonic() + timeout_seconds
        try:
            while process.poll() is None and time.monotonic() < deadline:
                await asyncio.sleep(0.2)
            if process.poll() is None:
                self.execution._kill_process_tree(process.pid)
                await self._finish(session_id, "timed_out", detail="Session timeout reached")
            else:
                status = "completed" if process.returncode == 0 else "failed"
                await self._finish(session_id, status, exit_code=process.returncode)
        except asyncio.CancelledError:
            if process.poll() is None:
                self.execution._kill_process_tree(process.pid)
            await asyncio.shield(
                self._finish(session_id, "cancelled", detail="Session monitor cancelled")
            )
            raise
        except Exception as exc:
            await self._finish(
                session_id,
                "failed",
                detail=f"Monitor error: {type(exc).__name__}",
            )

    async def _finish(
        self,
        session_id: str,
        status: str,
        *,
        exit_code: Optional[int] = None,
        detail: str = "",
    ) -> None:
        stream = self._log_streams.pop(session_id, None)
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass
        async with self._lock:
            payload = self._read()
            record = payload["sessions"].get(session_id)
            if record is None or record.get("status") != "running":
                event = self._completion.get(session_id)
                if event is not None:
                    event.set()
                return
            record["status"] = status
            record["finished_at"] = time.time()
            record["exit_code"] = exit_code
            if detail:
                record["detail"] = redact_sensitive_text(detail)[:500]
            self._write(payload)
        self._processes.pop(session_id, None)
        event = self._completion.get(session_id)
        if event is not None:
            event.set()
        main_logger.info(f"[Host OS] Script session {session_id} finished: {status}")
        if self.event_bus is not None:
            try:
                await self.event_bus.publish(
                    Events.HOST_OS_SANDBOX_EVENT,
                    message=f"Script session '{session_id}' finished with status '{status}'.",
                    session_id=session_id,
                    status=status,
                )
            except Exception as exc:
                main_logger.error(f"[Host OS] Could not publish script session event: {exc}")

    async def _get_record(self, session_id: str) -> dict[str, Any] | SkillResult:
        normalized = str(session_id).strip().lower()
        if not self._ID.fullmatch(normalized):
            return SkillResult.fail("Invalid script session ID.")
        async with self._lock:
            record = self._read()["sessions"].get(normalized)
        if record is None:
            return SkillResult.fail(f"Script session {normalized} was not found.")
        return dict(record)

    def _read_log_tail(self, record: dict[str, Any], limit: int) -> str:
        if limit == 0:
            return ""
        path = (self.host_os.framework_dir / str(record.get("log_path", ""))).resolve()
        if not path.is_relative_to(self.logs_dir.resolve()) or not path.is_file():
            return ""
        # Read a bounded suffix rather than loading an arbitrarily large job log.
        # Four bytes per requested character covers the widest UTF-8 code point.
        byte_limit = max(4_096, min(200_000, limit * 4))
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - byte_limit), os.SEEK_SET)
            data = stream.read(byte_limit)
        return data.decode("utf-8", errors="replace")[-limit:]

    def _display_path(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.host_os.framework_dir).as_posix()
        except ValueError:
            return path.resolve().as_posix()

    def _read(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Process session registry is unreadable: {exc}") from exc
        if payload.get("version") != 1 or not isinstance(payload.get("sessions"), dict):
            raise ValueError("Process session registry has an unsupported format.")
        return payload

    def _write(self, payload: dict[str, Any]) -> None:
        descriptor, name = tempfile.mkstemp(
            prefix=f".{self.registry_path.name}.",
            suffix=".tmp",
            dir=self.registry_path.parent,
        )
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.registry_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _prune(self, payload: dict[str, Any]) -> None:
        sessions = payload["sessions"]
        terminal = sorted(
            (record for record in sessions.values() if record.get("status") in self._TERMINAL),
            key=lambda record: float(record.get("finished_at") or 0),
        )
        while len(sessions) > self._MAX_RECORDS and terminal:
            sessions.pop(terminal.pop(0)["id"], None)
