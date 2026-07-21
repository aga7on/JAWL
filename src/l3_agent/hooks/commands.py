"""Shell-free user-approved command adapters for lifecycle hooks."""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Dict, Optional, Set

import psutil

from src.l3_agent.hooks.lifecycle import (
    HookContext,
    HookDecision,
    HookPhase,
    LifecycleHooks,
)
from src.utils._tools import redact_sensitive_text, truncate_text
from src.utils.logger import agent_logger
from src.utils.settings import LifecycleCommandHookConfig, LifecycleHooksConfig


WorkspaceResolver = Callable[[str, str, bool], Path]
_REPOSITORY_MANIFEST = Path(".jawl/hooks.json")
_MANIFEST_MAX_BYTES = 32 * 1024
_ARG_MAX_CHARS = 4000
_ARGV_MAX_CHARS = 32768


@dataclass(frozen=True)
class _ResolvedHook:
    config: LifecycleCommandHookConfig
    executable: Path
    executable_sha256: str


class DeclarativeCommandHooks:
    """Register exact argv hooks; repositories may only select approved profiles.

    User profiles are declared in ``settings.yaml``. A task repository can opt
    into a profile with ``.jawl/hooks.json`` only when that profile has
    ``scope: repository``. Repository files never supply argv, environment, or
    interpolation values.
    """

    def __init__(
        self,
        config: LifecycleHooksConfig,
        *,
        framework_root: Path,
        workspace_resolver: Optional[WorkspaceResolver] = None,
    ) -> None:
        self.config = config
        self.framework_root = framework_root.resolve()
        self.workspace_resolver = workspace_resolver
        self._hooks = self._resolve_hooks(config.commands)

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    @classmethod
    def _resolve_hooks(
        cls, profiles: list[LifecycleCommandHookConfig]
    ) -> list[_ResolvedHook]:
        names: Set[str] = set()
        resolved: list[_ResolvedHook] = []
        for profile in profiles:
            if profile.name in names:
                raise ValueError(
                    f"Lifecycle command hook names must be unique ({profile.name})."
                )
            names.add(profile.name)
            if (
                profile.scope == "repository"
                and profile.working_directory != "workspace"
            ):
                raise ValueError(
                    f"Repository lifecycle hook '{profile.name}' must run in "
                    "the managed task workspace."
                )
            if any(
                not argument
                or len(argument) > _ARG_MAX_CHARS
                or "\x00" in argument
                for argument in profile.argv
            ):
                raise ValueError(
                    f"Lifecycle command hook '{profile.name}' has an invalid argv."
                )
            if sum(len(argument) for argument in profile.argv) > _ARGV_MAX_CHARS:
                raise ValueError(
                    f"Lifecycle command hook '{profile.name}' argv is too large."
                )
            if any(
                not pattern or len(pattern) > 200 for pattern in profile.tool_patterns
            ):
                raise ValueError(
                    f"Lifecycle command hook '{profile.name}' has invalid tool patterns."
                )
            executable = Path(profile.argv[0])
            if executable.is_absolute():
                executable = executable.resolve()
            else:
                if len(executable.parts) != 1:
                    raise ValueError(
                        f"Lifecycle command hook '{profile.name}' executable must "
                        "be absolute or resolved by PATH."
                    )
                located = shutil.which(profile.argv[0])
                if located is None:
                    raise FileNotFoundError(
                        f"Lifecycle hook executable was not found ({profile.argv[0]})."
                    )
                executable = Path(located).resolve()
            if not executable.is_file():
                raise FileNotFoundError(
                    f"Lifecycle hook executable was not found ({executable.name})."
                )
            resolved.append(
                _ResolvedHook(profile, executable, cls._sha256(executable))
            )
        return resolved

    def register(self, hooks: LifecycleHooks) -> int:
        for profile in self._hooks:
            handler = self._handler(profile)
            hooks.subscribe(
                HookPhase(profile.config.phase),
                handler,
                priority=profile.config.priority,
                name=f"command:{profile.config.name}",
            )
        return len(self._hooks)

    def _handler(
        self, profile: _ResolvedHook
    ) -> Callable[[HookContext], Awaitable[Optional[HookDecision]]]:
        """Bind one immutable profile without exposing it to action input."""

        async def run(context: HookContext):
            return await self._run_profile(profile, context)

        run.__name__ = f"command_{profile.config.name}"
        return run

    def _workspace_for(self, context: HookContext) -> Optional[Path]:
        task_id = context.parameters.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            return None
        if self.workspace_resolver is None:
            return None
        try:
            workspace = self.workspace_resolver(task_id, ".", False).resolve()
        except (OSError, PermissionError, ValueError):
            return None
        return workspace if workspace.is_dir() else None

    @staticmethod
    def _repository_selection(workspace: Path) -> Set[str]:
        root = workspace.resolve()
        candidate = root / _REPOSITORY_MANIFEST
        if not candidate.exists():
            return set()
        manifest = candidate.resolve()
        if not manifest.is_relative_to(root) or not manifest.is_file():
            raise PermissionError("Repository hook manifest escaped the task workspace.")
        if manifest.stat().st_size > _MANIFEST_MAX_BYTES:
            raise ValueError("Repository hook manifest exceeds 32 KiB.")
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or set(payload) != {"version", "hooks"}:
            raise ValueError("Repository hook manifest has unsupported fields.")
        hooks = payload.get("hooks")
        if payload.get("version") != 1 or not isinstance(hooks, list):
            raise ValueError("Repository hook manifest must use schema version 1.")
        if (
            len(hooks) > 100
            or any(
                not isinstance(name, str)
                or not name
                or len(name) > 100
                for name in hooks
            )
            or len(set(hooks)) != len(hooks)
        ):
            raise ValueError("Repository hook selection is invalid or duplicated.")
        return set(hooks)

    async def _run_profile(
        self, profile: _ResolvedHook, context: HookContext
    ) -> Optional[HookDecision]:
        command = profile.config
        if not any(
            fnmatch.fnmatchcase(context.tool_name, pattern)
            for pattern in command.tool_patterns
        ):
            return None

        needs_workspace = (
            command.working_directory == "workspace" or command.scope == "repository"
        )
        workspace = self._workspace_for(context) if needs_workspace else None
        if needs_workspace and workspace is None:
            return None
        if command.scope == "repository":
            assert workspace is not None
            if command.name not in self._repository_selection(workspace):
                return None

        current_hash = await asyncio.to_thread(self._sha256, profile.executable)
        if current_hash != profile.executable_sha256:
            raise RuntimeError(
                f"Lifecycle hook executable changed after preflight ({command.name})."
            )
        cwd = workspace if command.working_directory == "workspace" else self.framework_root
        assert cwd is not None
        return_code, output = await self._execute(profile, context, cwd)
        if (
            context.phase == HookPhase.PRE_TOOL_USE
            and return_code == command.deny_exit_code
        ):
            return HookDecision.deny(
                f"Declarative lifecycle hook '{command.name}' denied the action."
            )
        if return_code != 0:
            suffix = f": {output}" if output else ""
            raise RuntimeError(
                f"Lifecycle command hook '{command.name}' exited with "
                f"code {return_code}{suffix}"
            )
        return None

    @staticmethod
    def _context_payload(context: HookContext) -> bytes:
        outcome = context.outcome or {}
        duration = outcome.get("duration_ms")
        payload = {
            "schema_version": 1,
            "phase": context.phase.value,
            "plan_id": str(context.plan_id)[:500],
            "action_id": str(context.action_id)[:500],
            "tool_name": str(context.tool_name)[:500],
            "parameter_names": sorted(
                str(name)[:100] for name in context.parameters
            )[:64],
            "outcome": {
                **(
                    {"is_success": bool(outcome["is_success"])}
                    if "is_success" in outcome
                    else {}
                ),
                **(
                    {"duration_ms": float(duration)}
                    if isinstance(duration, (int, float))
                    else {}
                ),
            },
            "trace": {
                key: str(value)[:200]
                for key, value in context.trace.items()
                if key in {"trace_id", "cycle_id", "request_id", "task_id"}
            },
        }
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")

    @staticmethod
    def _environment(context: HookContext) -> Dict[str, str]:
        allowed = {
            "HOME",
            "LANG",
            "LOCALAPPDATA",
            "PATH",
            "PATHEXT",
            "SYSTEMDRIVE",
            "SYSTEMROOT",
            "TEMP",
            "TMP",
            "USERPROFILE",
            "WINDIR",
        }
        environment = {key: value for key, value in os.environ.items() if key in allowed}
        environment.update(
            {
                "JAWL_HOOK_PHASE": context.phase.value,
                "JAWL_HOOK_TOOL": context.tool_name[:500],
                "JAWL_HOOK_ACTION_ID": context.action_id[:500],
            }
        )
        return environment

    async def _execute(
        self, profile: _ResolvedHook, context: HookContext, cwd: Path
    ) -> tuple[int, str]:
        command = profile.config
        argv = [str(profile.executable), *command.argv[1:]]
        process: Optional[asyncio.subprocess.Process] = None
        tasks: list[asyncio.Task] = []
        limit = max(256, self.config.max_output_chars * 4)

        async def read_bounded(stream: asyncio.StreamReader) -> tuple[bytes, bool]:
            retained = bytearray()
            truncated = False
            while True:
                chunk = await stream.read(65536)
                if not chunk:
                    break
                remaining = limit - len(retained)
                if remaining > 0:
                    retained.extend(chunk[:remaining])
                if len(chunk) > max(remaining, 0):
                    truncated = True
            return bytes(retained), truncated

        async def feed_stdin(stream: asyncio.StreamWriter, payload: bytes) -> None:
            try:
                stream.write(payload)
                await stream.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                stream.close()

        started = time.perf_counter()
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(cwd),
                env=self._environment(context),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            assert process.stdin is not None
            assert process.stdout is not None
            assert process.stderr is not None
            tasks = [
                asyncio.create_task(feed_stdin(process.stdin, self._context_payload(context))),
                asyncio.create_task(read_bounded(process.stdout)),
                asyncio.create_task(read_bounded(process.stderr)),
                asyncio.create_task(process.wait()),
            ]
            _, stdout_result, stderr_result, return_code = await asyncio.wait_for(
                asyncio.gather(*tasks), timeout=self.config.command_timeout_seconds
            )
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            if process is not None and process.returncode is None:
                await self._terminate_process_tree(process)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise TimeoutError(
                f"Lifecycle command hook '{command.name}' timed out after "
                f"{self.config.command_timeout_seconds:g} seconds."
            ) from exc

        stdout, stdout_truncated = stdout_result
        stderr, stderr_truncated = stderr_result
        output = "\n".join(
            item
            for item in (
                stdout.decode("utf-8", errors="replace").strip(),
                stderr.decode("utf-8", errors="replace").strip(),
            )
            if item
        )
        output = truncate_text(
            redact_sensitive_text(output), self.config.max_output_chars
        )
        if (stdout_truncated or stderr_truncated) and output:
            output = truncate_text(
                output + "\n[hook output truncated]", self.config.max_output_chars
            )
        agent_logger.info(
            f"[Lifecycle Hook] command:{command.name} finished with code "
            f"{return_code} in {(time.perf_counter() - started) * 1000:.1f} ms."
        )
        return int(return_code), output

    @staticmethod
    async def _terminate_process_tree(process: asyncio.subprocess.Process) -> None:
        def terminate() -> None:
            try:
                parent = psutil.Process(process.pid)
            except psutil.NoSuchProcess:
                return
            descendants = parent.children(recursive=True)
            targets = [*descendants, parent]
            for target in reversed(targets):
                try:
                    target.terminate()
                except psutil.NoSuchProcess:
                    pass
            _, alive = psutil.wait_procs(targets, timeout=1)
            for target in alive:
                try:
                    target.kill()
                except psutil.NoSuchProcess:
                    pass
            psutil.wait_procs(alive, timeout=1)

        await asyncio.to_thread(terminate)
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
