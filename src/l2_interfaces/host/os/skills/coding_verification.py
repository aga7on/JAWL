"""Workspace-aware, persistent verification runs for coding tasks."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Sequence, Tuple

import psutil

from src.l2_interfaces.host.os.client import HostOSAccessLevel, HostOSClient
from src.l2_interfaces.host.os.decorators import require_access
from src.l2_interfaces.host.os.skills.coding_workspaces import (
    HostOSCodingWorkspaces,
)
from src.l3_agent.skills.registry import SkillResult, skill
from src.l3_agent.swarm.roles import Subagents
from src.utils._tools import redact_sensitive_text
from src.utils.logger import main_logger


VerificationCheck = Literal[
    "auto",
    "git_diff_check",
    "python_compile",
    "pytest",
    "npm_test",
    "cargo_test",
    "go_test",
    "dotnet_test",
    "maven_test",
    "gradle_test",
]


class HostOSCodingVerification:
    """Runs bounded standard checks against an exact task-workspace state."""

    _ALLOWED_CHECKS = {
        "auto",
        "git_diff_check",
        "python_compile",
        "pytest",
        "npm_test",
        "cargo_test",
        "go_test",
        "dotnet_test",
        "maven_test",
        "gradle_test",
    }
    _PYTHON_SYNTAX_CHECK = r"""import pathlib
import sys

ignored = {
    '.git', '.mypy_cache', '.pytest_cache', '.ruff_cache', '__pycache__',
    'node_modules', 'venv', '.venv'
}
errors = []
for path in pathlib.Path('.').rglob('*.py'):
    if any(part in ignored for part in path.parts):
        continue
    try:
        compile(path.read_bytes(), str(path), 'exec')
    except Exception as exc:
        errors.append(f'{path}: {exc}')
if errors:
    print('\n'.join(errors), file=sys.stderr)
    raise SystemExit(1)
"""

    def __init__(
        self,
        host_os_client: HostOSClient,
        workspaces: HostOSCodingWorkspaces,
    ) -> None:
        self.host_os = host_os_client
        self.workspaces = workspaces
        self.session_id = uuid.uuid4().hex

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _reconcile_interrupted_runs(entry: Dict[str, Any], session_id: str) -> None:
        for run in entry.get("verification_runs", []):
            if run.get("state") == "running" and run.get("session_id") != session_id:
                run["state"] = "interrupted"
                run["finished_at"] = HostOSCodingVerification._utc_now()

    async def _resolve_task(
        self, task_id: str
    ) -> Tuple[str, Dict[str, Any], Path]:
        normalized = self.workspaces._validate_task_id(task_id)
        async with self.workspaces._lock:
            registry = self.workspaces._load_registry()
            entry = self.workspaces._get_entry(registry, normalized)
            self._reconcile_interrupted_runs(entry, self.session_id)
            _, workspace = self.workspaces._entry_paths(entry)
            if not workspace.is_dir():
                raise ValueError("Coding workspace directory is missing.")
            self.workspaces._save_registry(registry)
            return normalized, dict(entry), workspace

    async def _detect_checks(self, workspace: Path) -> List[str]:
        checks = ["git_diff_check"]
        code, python_files, _ = await self.workspaces._run_git(
            workspace, "ls-files", "--", "*.py"
        )
        if code == 0 and python_files:
            checks.append("python_compile")
            if (workspace / "tests").is_dir() or any(
                (workspace / filename).is_file()
                for filename in ("pytest.ini", "tox.ini", "conftest.py")
            ):
                checks.append("pytest")
        if (workspace / "package.json").is_file():
            checks.append("npm_test")
        if (workspace / "Cargo.toml").is_file():
            checks.append("cargo_test")
        if (workspace / "go.mod").is_file():
            checks.append("go_test")
        if list(workspace.glob("*.sln")) or list(workspace.glob("*.csproj")):
            checks.append("dotnet_test")
        if (workspace / "pom.xml").is_file():
            checks.append("maven_test")
        if (workspace / "gradlew").is_file() or (workspace / "gradlew.bat").is_file():
            checks.append("gradle_test")
        return checks

    async def _normalize_checks(
        self, workspace: Path, checks: Optional[List[VerificationCheck]]
    ) -> List[str]:
        requested = list(checks or ["auto"])
        if not requested or len(requested) > 10:
            raise ValueError("checks must contain between 1 and 10 profiles.")
        invalid = [check for check in requested if check not in self._ALLOWED_CHECKS]
        if invalid:
            raise ValueError(f"Unsupported verification checks: {', '.join(invalid)}")
        if "auto" in requested:
            if len(requested) != 1:
                raise ValueError("'auto' cannot be combined with explicit checks.")
            return await self._detect_checks(workspace)
        return list(dict.fromkeys(requested))

    @staticmethod
    def _resolve_executable(name: str) -> str:
        executable = shutil.which(name)
        if not executable:
            raise FileNotFoundError(
                f"Verification executable '{name}' was not found on PATH."
            )
        return executable

    def _command_for(self, check: str, workspace: Path) -> List[str]:
        if check == "git_diff_check":
            return [self._resolve_executable("git"), "diff", "--check", "HEAD", "--"]
        if check == "python_compile":
            return [sys.executable, "-c", self._PYTHON_SYNTAX_CHECK]
        if check == "pytest":
            return [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
        if check == "npm_test":
            return [self._resolve_executable("npm"), "test"]
        if check == "cargo_test":
            return [self._resolve_executable("cargo"), "test", "--all-targets"]
        if check == "go_test":
            return [self._resolve_executable("go"), "test", "./..."]
        if check == "dotnet_test":
            return [self._resolve_executable("dotnet"), "test", "--nologo"]
        if check == "maven_test":
            return [self._resolve_executable("mvn"), "test", "--batch-mode"]
        if check == "gradle_test":
            wrapper = workspace / ("gradlew.bat" if os.name == "nt" else "gradlew")
            if not wrapper.is_file():
                raise FileNotFoundError("Gradle wrapper was not found in the workspace.")
            return [str(wrapper), "test", "--no-daemon"]
        raise ValueError(f"Unsupported verification check: {check}")

    @staticmethod
    async def _read_stream_tail(
        stream: asyncio.StreamReader, max_bytes: int = 16_000
    ) -> Tuple[str, bool]:
        tail = bytearray()
        total = 0
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            total += len(chunk)
            tail.extend(chunk)
            if len(tail) > max_bytes:
                del tail[: len(tail) - max_bytes]
        text = redact_sensitive_text(bytes(tail).decode("utf-8", errors="replace"))
        return text, total > max_bytes

    @staticmethod
    def _kill_process_tree_sync(pid: int) -> None:
        try:
            parent = psutil.Process(pid)
        except psutil.Error:
            return
        processes = parent.children(recursive=True)
        processes.append(parent)
        for process in reversed(processes):
            try:
                process.kill()
            except psutil.Error:
                pass
        psutil.wait_procs(processes, timeout=5)

    @staticmethod
    def _windows_batch_command(command: Sequence[str]) -> List[str]:
        if os.name != "nt" or Path(command[0]).suffix.lower() not in {".bat", ".cmd"}:
            return list(command)
        command_processor = os.environ.get("COMSPEC", "cmd.exe")
        return [
            command_processor,
            "/d",
            "/s",
            "/c",
            subprocess.list2cmdline(list(command)),
        ]

    async def _run_command(
        self, check: str, command: List[str], workspace: Path, timeout_sec: int
    ) -> Dict[str, Any]:
        started_at = self._utc_now()
        started_monotonic = time.monotonic()
        actual_command = self._windows_batch_command(command)
        env = os.environ.copy()
        env.update(
            {
                "CI": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONUNBUFFERED": "1",
            }
        )
        process = await asyncio.create_subprocess_exec(
            *actual_command,
            cwd=str(workspace),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert process.stdout is not None and process.stderr is not None
        stdout_task = asyncio.create_task(self._read_stream_tail(process.stdout))
        stderr_task = asyncio.create_task(self._read_stream_tail(process.stderr))
        timed_out = False
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout_sec)
        except asyncio.TimeoutError:
            timed_out = True
            await asyncio.to_thread(self._kill_process_tree_sync, process.pid)
            await process.wait()
        except asyncio.CancelledError:
            await asyncio.to_thread(self._kill_process_tree_sync, process.pid)
            await process.wait()
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            raise
        stdout_result, stderr_result = await asyncio.gather(
            stdout_task, stderr_task
        )
        stdout, stdout_truncated = stdout_result
        stderr, stderr_truncated = stderr_result
        return {
            "check": check,
            "command": command,
            "started_at": started_at,
            "finished_at": self._utc_now(),
            "duration_sec": round(time.monotonic() - started_monotonic, 3),
            "exit_code": process.returncode,
            "timed_out": timed_out,
            "stdout": stdout,
            "stderr": stderr,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
            "passed": not timed_out and process.returncode == 0,
        }

    async def _persist_run(
        self,
        task_id: str,
        run: Dict[str, Any],
        final: bool = False,
        start: bool = False,
    ) -> None:
        async with self.workspaces._lock:
            registry = self.workspaces._load_registry()
            entry = self.workspaces._get_entry(registry, task_id)
            runs = entry.setdefault("verification_runs", [])
            if start and any(item.get("state") == "running" for item in runs):
                raise ValueError(
                    f"A verification run is already active for task '{task_id}'."
                )
            existing = next(
                (item for item in runs if item.get("run_id") == run["run_id"]), None
            )
            if existing is None:
                runs.append(run)
            else:
                existing.clear()
                existing.update(run)
            del runs[:-10]
            if final:
                entry["last_verification"] = run
            self.workspaces._save_registry(registry)

    @skill(swarm=[Subagents.CODER, Subagents.QA_ENGINEER, Subagents.SYSADMIN])
    @require_access(HostOSAccessLevel.OBSERVER)
    async def run_coding_verification(
        self,
        task_id: str,
        checks: Optional[List[VerificationCheck]] = None,
        timeout_sec: int = 300,
        stop_on_failure: bool = True,
    ) -> SkillResult:
        """Run standard verification profiles in a task worktree.

        ``auto`` detects Python, Node, Rust, Go, .NET, Maven, and Gradle projects.
        Output is bounded, process trees are terminated on timeout/cancellation,
        and the successful result is tied to an exact workspace fingerprint.
        """

        if timeout_sec < 1 or timeout_sec > 1800:
            return SkillResult.fail("timeout_sec must be between 1 and 1800.")
        run_persisted = False
        try:
            task_id, entry, workspace = await self._resolve_task(task_id)
            normalized_checks = await self._normalize_checks(workspace, checks)
            fingerprint_before = await self.workspaces.workspace_fingerprint(workspace)
            run = {
                "run_id": uuid.uuid4().hex,
                "session_id": self.session_id,
                "task_id": task_id,
                "state": "running",
                "checks": normalized_checks,
                "results": [],
                "started_at": self._utc_now(),
                "finished_at": None,
                "head_before": fingerprint_before["head"],
                "fingerprint_before": fingerprint_before["fingerprint"],
            }
            await self._persist_run(task_id, run, start=True)
            run_persisted = True

            try:
                for check in normalized_checks:
                    try:
                        command = self._command_for(check, workspace)
                        result = await self._run_command(
                            check, command, workspace, timeout_sec
                        )
                    except FileNotFoundError as exc:
                        result = {
                            "check": check,
                            "command": [],
                            "started_at": self._utc_now(),
                            "finished_at": self._utc_now(),
                            "duration_sec": 0.0,
                            "exit_code": None,
                            "timed_out": False,
                            "stdout": "",
                            "stderr": str(exc),
                            "stdout_truncated": False,
                            "stderr_truncated": False,
                            "passed": False,
                        }
                    except Exception as exc:
                        result = {
                            "check": check,
                            "command": [],
                            "started_at": self._utc_now(),
                            "finished_at": self._utc_now(),
                            "duration_sec": 0.0,
                            "exit_code": None,
                            "timed_out": False,
                            "stdout": "",
                            "stderr": f"Verification process error: {exc}",
                            "stdout_truncated": False,
                            "stderr_truncated": False,
                            "passed": False,
                        }
                    run["results"].append(result)
                    await self._persist_run(task_id, run)
                    if stop_on_failure and not result["passed"]:
                        break
            except asyncio.CancelledError:
                run["state"] = "cancelled"
                run["finished_at"] = self._utc_now()
                await asyncio.shield(self._persist_run(task_id, run, final=True))
                raise

            fingerprint_after = await self.workspaces.workspace_fingerprint(workspace)
            run["finished_at"] = self._utc_now()
            run["fingerprint_after"] = fingerprint_after["fingerprint"]
            run["head_after"] = fingerprint_after["head"]
            all_passed = len(run["results"]) == len(normalized_checks) and all(
                result["passed"] for result in run["results"]
            )
            unchanged = fingerprint_before == fingerprint_after
            if all_passed and unchanged:
                run["state"] = "passed"
            elif not unchanged:
                run["state"] = "stale"
                run["error"] = (
                    "Workspace changed during verification; run checks again on "
                    "the final state."
                )
            else:
                run["state"] = "failed"
            await self._persist_run(task_id, run, final=True)

            main_logger.info(
                f"[Host OS] Coding verification for '{task_id}' finished: "
                f"{run['state']}."
            )
            payload = json.dumps(run, ensure_ascii=False)
            return (
                SkillResult.ok(payload)
                if run["state"] == "passed"
                else SkillResult.fail(payload)
            )
        except (
            PermissionError,
            FileNotFoundError,
            TimeoutError,
            ValueError,
            KeyError,
        ) as exc:
            if run_persisted and run.get("state") == "running":
                run["state"] = "error"
                run["finished_at"] = self._utc_now()
                run["error"] = str(exc)
                await asyncio.shield(self._persist_run(task_id, run, final=True))
            return SkillResult.fail(str(exc))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if run_persisted and run.get("state") == "running":
                run["state"] = "error"
                run["finished_at"] = self._utc_now()
                run["error"] = str(exc)
                await asyncio.shield(self._persist_run(task_id, run, final=True))
            return SkillResult.fail(f"Error running coding verification: {exc}")

    @skill(swarm=[Subagents.CODER, Subagents.QA_ENGINEER, Subagents.SYSADMIN])
    @require_access(HostOSAccessLevel.OBSERVER)
    async def get_coding_verification_status(self, task_id: str) -> SkillResult:
        """Report verification history and whether it matches current workspace state."""

        try:
            task_id, _, workspace = await self._resolve_task(task_id)
            current = await self.workspaces.workspace_fingerprint(workspace)
            async with self.workspaces._lock:
                registry = self.workspaces._load_registry()
                entry = self.workspaces._get_entry(registry, task_id)
                self._reconcile_interrupted_runs(entry, self.session_id)
                last = entry.get("last_verification")
                is_current = bool(
                    last
                    and last.get("state") == "passed"
                    and (
                        (
                            last.get("head_before") == current["head"]
                            and last.get("fingerprint_after")
                            == current["fingerprint"]
                        )
                        or (
                            last.get("committed_as") == current["head"]
                            and last.get("post_commit_fingerprint")
                            == current["fingerprint"]
                        )
                    )
                )
                payload = {
                    "task_id": task_id,
                    "is_current": is_current,
                    "current": current,
                    "last_verification": last,
                    "verification_runs": entry.get("verification_runs", []),
                }
                self.workspaces._save_registry(registry)
            return SkillResult.ok(json.dumps(payload, ensure_ascii=False))
        except (PermissionError, FileNotFoundError, TimeoutError, ValueError, KeyError) as exc:
            return SkillResult.fail(str(exc))
        except Exception as exc:
            return SkillResult.fail(f"Error reading coding verification status: {exc}")
