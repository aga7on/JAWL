"""Policy-controlled command execution inside managed coding workspaces."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import psutil

from src.l2_interfaces.host.os.client import HostOSAccessLevel, HostOSClient
from src.l2_interfaces.host.os.decorators import require_access
from src.l2_interfaces.host.os.skills.coding_workspaces import HostOSCodingWorkspaces
from src.l3_agent.skills.registry import SkillResult, skill
from src.l3_agent.swarm.roles import Subagents
from src.utils._tools import redact_sensitive_text, truncate_text
from src.utils.tracing import current_trace


class HostOSCodingExecution:
    """Runs argv-only task commands under a human-selected execution policy."""

    _FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")
    _IMAGE = re.compile(
        r"^(?:[A-Za-z0-9.-]+(?::[0-9]+)?/)?"
        r"[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*"
        r"(?::[A-Za-z0-9._-]+)?(?:@sha256:[0-9a-f]{64})?$"
    )
    _MAX_ARGV_ITEMS = 128
    _MAX_ARG_CHARS = 4096
    _MAX_TOTAL_ARG_CHARS = 32768
    _OUTPUT_BYTES = 32768

    def __init__(
        self,
        host_os_client: HostOSClient,
        workspaces: HostOSCodingWorkspaces,
    ) -> None:
        self.host_os = host_os_client
        self.workspaces = workspaces

    @classmethod
    def _validate_argv(cls, argv: List[str]) -> List[str]:
        if not isinstance(argv, list) or not 1 <= len(argv) <= cls._MAX_ARGV_ITEMS:
            raise ValueError(
                f"argv must contain between 1 and {cls._MAX_ARGV_ITEMS} strings."
            )
        if not all(isinstance(item, str) for item in argv):
            raise ValueError("Every argv item must be a string.")
        if any(not item or "\x00" in item for item in argv):
            raise ValueError("argv items cannot be empty or contain NUL bytes.")
        if any(len(item) > cls._MAX_ARG_CHARS for item in argv):
            raise ValueError(
                f"An argv item cannot exceed {cls._MAX_ARG_CHARS} characters."
            )
        if sum(len(item) for item in argv) > cls._MAX_TOTAL_ARG_CHARS:
            raise ValueError(
                f"Combined argv cannot exceed {cls._MAX_TOTAL_ARG_CHARS} characters."
            )
        return list(argv)

    @classmethod
    def _validate_fingerprint(cls, fingerprint: str) -> str:
        normalized = str(fingerprint).strip().lower()
        if not cls._FINGERPRINT.fullmatch(normalized):
            raise ValueError(
                "expected_workspace_fingerprint must be a 64-character SHA-256 value."
            )
        return normalized

    @staticmethod
    def _host_environment() -> Dict[str, str]:
        allowed = {
            "COMSPEC",
            "LANG",
            "LC_ALL",
            "LC_CTYPE",
            "NUMBER_OF_PROCESSORS",
            "OS",
            "PATH",
            "PATHEXT",
            "SYSTEMDRIVE",
            "SYSTEMROOT",
            "TEMP",
            "TMP",
            "TZ",
            "WINDIR",
        }
        env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
        env.update(
            {
                "GIT_TERMINAL_PROMPT": "0",
                "PYTHONIOENCODING": "utf-8",
                "PYTHONNOUSERSITE": "1",
                "PYTHONUNBUFFERED": "1",
            }
        )
        return env

    @staticmethod
    def _kill_process_tree(pid: int) -> None:
        try:
            parent = psutil.Process(pid)
            processes = parent.children(recursive=True)
            processes.append(parent)
            for process in reversed(processes):
                try:
                    process.kill()
                except (psutil.AccessDenied, psutil.NoSuchProcess):
                    pass
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            pass

    async def _run_bounded(
        self,
        command: List[str],
        cwd: Path,
        timeout: int,
        env: Optional[Dict[str, str]],
    ) -> Tuple[int, str, str, bool, bool]:
        process = None
        readers: List[asyncio.Task[Any]] = []

        async def read_bounded(
            stream: asyncio.StreamReader,
        ) -> Tuple[bytes, bool]:
            retained = bytearray()
            truncated = False
            while True:
                chunk = await stream.read(65536)
                if not chunk:
                    break
                remaining = self._OUTPUT_BYTES - len(retained)
                if remaining > 0:
                    retained.extend(chunk[:remaining])
                if len(chunk) > max(0, remaining):
                    truncated = True
            return bytes(retained), truncated

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(cwd),
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            assert process.stdout is not None and process.stderr is not None
            readers = [
                asyncio.create_task(read_bounded(process.stdout)),
                asyncio.create_task(read_bounded(process.stderr)),
            ]
            wait_task = asyncio.create_task(process.wait())
            stdout_result, stderr_result, return_code = await asyncio.wait_for(
                asyncio.gather(*readers, wait_task), timeout=timeout
            )
            stdout, stdout_truncated = stdout_result
            stderr, stderr_truncated = stderr_result
            return (
                int(return_code),
                stdout.decode("utf-8", errors="replace").strip(),
                stderr.decode("utf-8", errors="replace").strip(),
                stdout_truncated,
                stderr_truncated,
            )
        except asyncio.TimeoutError as exc:
            if process is not None and process.returncode is None:
                self._kill_process_tree(process.pid)
                await process.wait()
            raise TimeoutError(
                f"Coding command exceeded its {timeout}-second timeout."
            ) from exc
        except asyncio.CancelledError:
            if process is not None and process.returncode is None:
                self._kill_process_tree(process.pid)
                await process.wait()
            raise
        finally:
            for reader in readers:
                if not reader.done():
                    reader.cancel()
            if readers:
                await asyncio.gather(*readers, return_exceptions=True)

    def _build_host_command(
        self, workspace: Path, argv: List[str]
    ) -> Tuple[List[str], Dict[str, str]]:
        executable = argv[0]
        if Path(executable).name != executable or any(
            separator in executable for separator in ("/", "\\", ":")
        ):
            raise ValueError(
                "Host policy requires argv[0] to be a configured executable name, "
                "not a path."
            )
        configured = [
            str(item).strip()
            for item in self.host_os.config.coding_host_allowed_commands
            if str(item).strip()
        ]
        pinned = next(
            (
                Path(item)
                for item in configured
                if Path(item).is_absolute()
                and Path(item).name.casefold() == executable.casefold()
            ),
            None,
        )
        name_approved = executable.casefold() in {
            item.casefold() for item in configured if not Path(item).is_absolute()
        }
        if pinned is None and not name_approved:
            raise PermissionError(
                f"Host command '{executable}' is not pre-approved in "
                "coding_host_allowed_commands."
            )
        env = self._host_environment()
        resolved_text = (
            str(pinned.resolve())
            if pinned is not None
            else shutil.which(executable, path=env.get("PATH"))
        )
        if resolved_text is None:
            raise FileNotFoundError(
                f"Pre-approved host executable was not found ({executable})."
            )
        resolved = Path(resolved_text).resolve()
        if not resolved.is_file():
            raise FileNotFoundError(
                f"Pre-approved host executable was not found ({resolved})."
            )
        if resolved.is_relative_to(
            self.host_os.sandbox_dir.resolve()
        ) or resolved.is_relative_to(workspace.resolve()):
            raise PermissionError(
                "A pre-approved host executable cannot resolve inside the agent sandbox."
            )
        return [str(resolved), *argv[1:]], env

    def _build_container_command(
        self, workspace: Path, cwd: Path, argv: List[str]
    ) -> List[str]:
        config = self.host_os.config
        runtime = config.coding_container_runtime
        resolved_runtime = shutil.which(runtime)
        if resolved_runtime is None:
            raise FileNotFoundError(f"Container runtime was not found ({runtime}).")
        image = str(config.coding_container_image).strip()
        if not self._IMAGE.fullmatch(image) or image.startswith("-"):
            raise ValueError("coding_container_image is not a valid bounded OCI image.")
        workspace_text = str(workspace.resolve())
        if "," in workspace_text:
            raise ValueError(
                "Container bind mount does not support a comma in the workspace path."
            )
        relative_cwd = cwd.resolve().relative_to(workspace.resolve()).as_posix()
        container_cwd = "/workspace"
        if relative_cwd != ".":
            container_cwd += f"/{relative_cwd}"
        mount = f"type=bind,source={workspace_text},target=/workspace"
        return [
            resolved_runtime,
            "run",
            "--rm",
            "--network",
            config.coding_container_network,
            "--memory",
            f"{config.coding_container_memory_mb}m",
            "--cpus",
            f"{config.coding_container_cpus:g}",
            "--pids-limit",
            str(config.coding_container_pids),
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--mount",
            mount,
            "--workdir",
            container_cwd,
            image,
            *argv,
        ]

    @skill(swarm=[Subagents.CODER, Subagents.QA_ENGINEER, Subagents.SYSADMIN])
    @require_access(HostOSAccessLevel.SANDBOX)
    async def run_coding_command(
        self,
        task_id: str,
        argv: List[str],
        expected_workspace_fingerprint: str,
        relative_cwd: str = ".",
        timeout_seconds: Optional[int] = None,
    ) -> SkillResult:
        """Run a shell-free command in a task workspace under configured policy.

        ``host`` permits only human-preapproved executable names. ``container``
        mounts only the managed workspace and applies configured network and
        resource limits. The exact preflight fingerprint is always required.
        """

        try:
            backend = self.host_os.config.coding_execution_backend
            if backend == "disabled":
                return SkillResult.fail(
                    "Task command execution is disabled by host.os policy."
                )
            task_id = self.workspaces._validate_task_id(task_id)
            argv = self._validate_argv(argv)
            expected = self._validate_fingerprint(expected_workspace_fingerprint)
            configured_timeout = int(self.host_os.config.execution_timeout_sec)
            timeout = configured_timeout if timeout_seconds is None else int(timeout_seconds)
            if timeout < 1 or timeout > configured_timeout:
                raise ValueError(
                    f"timeout_seconds must be between 1 and {configured_timeout}."
                )

            async with self.workspaces._lock:
                entry = self.workspaces._get_entry(
                    self.workspaces._load_registry(), task_id
                )
                _, workspace = self.workspaces._entry_paths(entry)
                relative = Path(str(relative_cwd).strip() or ".")
                if relative.is_absolute() or ".." in relative.parts:
                    raise ValueError("relative_cwd must stay inside the task workspace.")
                cwd = (workspace / relative).resolve()
                if not cwd.is_relative_to(workspace.resolve()) or not cwd.is_dir():
                    raise ValueError("relative_cwd is not an existing task directory.")
                before = await self.workspaces.workspace_fingerprint(workspace)
                if before["fingerprint"] != expected:
                    return SkillResult.fail(
                        "Coding command rejected: workspace changed since inspection "
                        f"(expected {expected}, current {before['fingerprint']})."
                    )

                if backend == "host":
                    command, env = self._build_host_command(workspace, argv)
                else:
                    command = self._build_container_command(workspace, cwd, argv)
                    env = None

                exit_code, stdout, stderr, stdout_cut, stderr_cut = (
                    await self._run_bounded(command, cwd, timeout, env)
                )
                after = await self.workspaces.workspace_fingerprint(workspace)

            payload = {
                "task_id": task_id,
                "backend": backend,
                "executable": argv[0],
                "exit_code": exit_code,
                "stdout": truncate_text(
                    redact_sensitive_text(stdout), max_chars=16000
                ),
                "stderr": truncate_text(
                    redact_sensitive_text(stderr), max_chars=16000
                ),
                "stdout_truncated": stdout_cut,
                "stderr_truncated": stderr_cut,
                "workspace_before": before,
                "workspace_after": after,
                "workspace_changed": before != after,
                "trace": current_trace(),
            }
            message = json.dumps(payload, ensure_ascii=False)
            if exit_code == 0:
                return SkillResult.ok(message)
            return SkillResult.fail(message)
        except asyncio.CancelledError:
            raise
        except (
            FileNotFoundError,
            PermissionError,
            TimeoutError,
            ValueError,
        ) as exc:
            return SkillResult.fail(str(exc))
        except Exception as exc:
            return SkillResult.fail(f"Error running coding command: {exc}")
