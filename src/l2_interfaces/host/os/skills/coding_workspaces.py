"""Persistent task-scoped Git worktrees for coding agents."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

from src.l2_interfaces.host.os.client import HostOSAccessLevel, HostOSClient
from src.l2_interfaces.host.os.decorators import require_access
from src.l3_agent.skills.registry import SkillResult, skill
from src.l3_agent.swarm.roles import Subagents
from src.utils._tools import truncate_text
from src.utils.logger import main_logger


class HostOSCodingWorkspaces:
    """Creates and manages isolated Git branches and worktrees per task."""

    _TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")

    def __init__(self, host_os_client: HostOSClient) -> None:
        self.host_os = host_os_client
        # Hidden from the global file watcher/context tree to prevent every task
        # checkout from multiplying heartbeat noise and prompt size.
        self.worktrees_dir = self.host_os.sandbox_dir / ".jawl-worktrees"
        self.registry_file = self.host_os.system_dir / "coding_workspaces.json"
        self.worktrees_dir.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        if not self.registry_file.exists():
            self._save_registry({"version": 1, "workspaces": {}})

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, path)
        finally:
            temp_path.unlink(missing_ok=True)

    def _load_registry(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.registry_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Coding workspace registry is unreadable: {exc}") from exc
        if data.get("version") != 1 or not isinstance(data.get("workspaces"), dict):
            raise ValueError("Coding workspace registry has an unsupported format.")
        return data

    def _save_registry(self, data: Dict[str, Any]) -> None:
        self._atomic_write(
            self.registry_file, json.dumps(data, ensure_ascii=False, indent=2)
        )

    @classmethod
    def _validate_task_id(cls, task_id: str) -> str:
        normalized = task_id.strip()
        if not cls._TASK_ID_PATTERN.fullmatch(normalized):
            raise ValueError(
                "task_id must be 1-63 characters and contain only letters, "
                "digits, '_' or '-' (starting with a letter or digit)."
            )
        return normalized

    async def _run_git(
        self, cwd: Path, *args: str, timeout: float = 120
    ) -> Tuple[int, str, str]:
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                "git",
                *args,
                cwd=str(cwd),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout
            )
            return (
                process.returncode,
                stdout.decode("utf-8", errors="replace").strip(),
                stderr.decode("utf-8", errors="replace").strip(),
            )
        except FileNotFoundError as exc:
            raise FileNotFoundError("'git' utility was not found.") from exc
        except asyncio.TimeoutError as exc:
            if process is not None:
                process.kill()
                await process.wait()
            raise TimeoutError(
                f"Git command timed out after {timeout:g} seconds."
            ) from exc
        except asyncio.CancelledError:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            raise

    async def _resolve_repository(self, repository_path: str) -> Path:
        requested = self.host_os.validate_path(repository_path, is_write=True)
        if not requested.is_dir():
            raise ValueError(f"Repository directory not found ({repository_path}).")
        code, out, err = await self._run_git(
            requested, "rev-parse", "--show-toplevel"
        )
        if code != 0 or not out:
            raise ValueError(f"Not a Git repository: {err or out or repository_path}")
        repository = self.host_os.validate_path(Path(out).resolve(), is_write=True)
        if repository.is_relative_to(self.worktrees_dir.resolve()):
            raise ValueError("A managed worktree cannot be used as the base repository.")
        return repository

    def _entry_paths(self, entry: Dict[str, Any]) -> Tuple[Path, Path]:
        repository = self.host_os.validate_path(
            entry["repository_path"], is_write=True
        )
        workspace = self.host_os.validate_path(entry["workspace_path"], is_write=True)
        if not workspace.is_relative_to(self.worktrees_dir.resolve()):
            raise PermissionError(
                "Managed coding workspace escaped the sandbox worktrees directory."
            )
        return repository, workspace

    def _get_entry(self, registry: Dict[str, Any], task_id: str) -> Dict[str, Any]:
        entry = registry["workspaces"].get(task_id)
        if entry is None:
            raise ValueError(f"Coding workspace not found for task '{task_id}'.")
        return entry

    @skill(swarm=[Subagents.CODER, Subagents.QA_ENGINEER, Subagents.SYSADMIN])
    @require_access(HostOSAccessLevel.SANDBOX)
    async def create_coding_workspace(
        self,
        repository_path: str,
        task_id: str,
        base_ref: str = "HEAD",
        allow_dirty_repository: bool = False,
    ) -> SkillResult:
        """Create an isolated Git branch and worktree for one coding task.

        The base repository must be clean unless ``allow_dirty_repository`` is
        explicitly true; uncommitted base changes are never copied. The returned
        workspace path is compatible with normal Host OS skills.
        """

        try:
            task_id = self._validate_task_id(task_id)
            if not base_ref.strip() or base_ref.startswith("-") or "\x00" in base_ref:
                return SkillResult.fail("Invalid base_ref.")
            async with self._lock:
                registry = self._load_registry()
                if task_id in registry["workspaces"]:
                    return SkillResult.fail(
                        f"Coding workspace already exists for task '{task_id}'."
                    )
                repository = await self._resolve_repository(repository_path)
                code, status, err = await self._run_git(
                    repository, "status", "--porcelain=v1", "--untracked-files=all"
                )
                if code != 0:
                    return SkillResult.fail(f"Unable to inspect repository: {err}")
                if status and not allow_dirty_repository:
                    return SkillResult.fail(
                        "Base repository has uncommitted changes. Commit/stash them, "
                        "or explicitly set allow_dirty_repository=true knowing that "
                        "those changes will not be included."
                    )

                code, base_commit, err = await self._run_git(
                    repository,
                    "rev-parse",
                    "--verify",
                    f"{base_ref}^{{commit}}",
                )
                if code != 0 or not base_commit:
                    return SkillResult.fail(
                        f"Unable to resolve base_ref '{base_ref}': {err or base_commit}"
                    )

                repository_key = hashlib.sha256(
                    str(repository).encode("utf-8")
                ).hexdigest()[:8]
                repo_name = re.sub(
                    r"[^A-Za-z0-9_-]+", "-", repository.name
                ).strip("-") or "repository"
                branch = f"jawl/{task_id}-{repository_key}"
                workspace = (
                    self.worktrees_dir / f"{repo_name}-{task_id}-{repository_key}"
                ).resolve()
                self.host_os.validate_path(workspace, is_write=True)
                if workspace.is_relative_to(repository):
                    return SkillResult.fail(
                        "The base repository cannot be the sandbox root because its "
                        "managed worktree would be nested inside that repository."
                    )
                if workspace.exists():
                    return SkillResult.fail(
                        f"Workspace destination already exists ({workspace})."
                    )

                code, _, _ = await self._run_git(
                    repository,
                    "show-ref",
                    "--verify",
                    "--quiet",
                    f"refs/heads/{branch}",
                )
                if code == 0:
                    return SkillResult.fail(
                        f"Task branch already exists ({branch}); choose a new task_id."
                    )
                if code != 1:
                    return SkillResult.fail("Unable to inspect existing task branches.")

                code, out, err = await self._run_git(
                    repository,
                    "worktree",
                    "add",
                    "-b",
                    branch,
                    "--",
                    str(workspace),
                    base_commit,
                )
                if code != 0:
                    return SkillResult.fail(
                        f"Unable to create coding workspace: {err or out}"
                    )

                entry = {
                    "task_id": task_id,
                    "repository_path": str(repository),
                    "workspace_path": str(workspace),
                    "branch": branch,
                    "base_ref": base_ref,
                    "base_commit": base_commit,
                    "base_was_dirty": bool(status),
                    "created_at": self._utc_now(),
                }
                try:
                    registry["workspaces"][task_id] = entry
                    self._save_registry(registry)
                except Exception:
                    await self._run_git(
                        repository,
                        "worktree",
                        "remove",
                        "--force",
                        "--",
                        str(workspace),
                    )
                    await self._run_git(repository, "branch", "-D", "--", branch)
                    raise

            main_logger.info(
                f"[Host OS] Created coding workspace '{task_id}' on {branch}."
            )
            return SkillResult.ok(json.dumps(entry, ensure_ascii=False))
        except (PermissionError, FileNotFoundError, TimeoutError, ValueError) as exc:
            return SkillResult.fail(str(exc))
        except Exception as exc:
            return SkillResult.fail(f"Error creating coding workspace: {exc}")

    async def _status_payload(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        repository, workspace = self._entry_paths(entry)
        payload = dict(entry)
        payload["repository_exists"] = repository.is_dir()
        payload["workspace_exists"] = workspace.is_dir()
        if not workspace.is_dir():
            payload["state"] = "missing"
            return payload
        code, status, err = await self._run_git(
            workspace, "status", "--porcelain=v1", "--branch"
        )
        if code != 0:
            payload.update(state="error", error=err)
            return payload
        _, diff_stat, _ = await self._run_git(
            workspace, "diff", "--stat", "HEAD", "--"
        )
        payload["state"] = "dirty" if status.splitlines()[1:] else "clean"
        payload["status"] = truncate_text(status, max_chars=6000)
        payload["diff_stat"] = truncate_text(diff_stat, max_chars=6000)
        return payload

    @skill(swarm=[Subagents.CODER, Subagents.QA_ENGINEER, Subagents.SYSADMIN])
    @require_access(HostOSAccessLevel.SANDBOX)
    async def get_coding_workspace_status(self, task_id: str) -> SkillResult:
        """Return metadata, Git status, and diff summary for one coding task."""

        try:
            task_id = self._validate_task_id(task_id)
            async with self._lock:
                entry = self._get_entry(self._load_registry(), task_id)
                payload = await self._status_payload(entry)
            return SkillResult.ok(json.dumps(payload, ensure_ascii=False))
        except (PermissionError, FileNotFoundError, TimeoutError, ValueError, KeyError) as exc:
            return SkillResult.fail(str(exc))
        except Exception as exc:
            return SkillResult.fail(f"Error inspecting coding workspace: {exc}")

    @skill(swarm=[Subagents.CODER, Subagents.QA_ENGINEER, Subagents.SYSADMIN])
    @require_access(HostOSAccessLevel.SANDBOX)
    async def list_coding_workspaces(self) -> SkillResult:
        """List registered coding workspaces and their current states."""

        try:
            async with self._lock:
                entries = self._load_registry()["workspaces"]
                payloads: List[Dict[str, Any]] = []
                for task_id in sorted(entries):
                    payloads.append(await self._status_payload(entries[task_id]))
            return SkillResult.ok(json.dumps(payloads, ensure_ascii=False))
        except (PermissionError, FileNotFoundError, TimeoutError, ValueError, KeyError) as exc:
            return SkillResult.fail(str(exc))
        except Exception as exc:
            return SkillResult.fail(f"Error listing coding workspaces: {exc}")

    @skill(swarm=[Subagents.CODER, Subagents.QA_ENGINEER, Subagents.SYSADMIN])
    @require_access(HostOSAccessLevel.SANDBOX)
    async def commit_coding_workspace(
        self, task_id: str, commit_message: str
    ) -> SkillResult:
        """Commit all task changes locally without pushing or merging them."""

        if not commit_message.strip():
            return SkillResult.fail("commit_message cannot be empty.")
        try:
            task_id = self._validate_task_id(task_id)
            async with self._lock:
                registry = self._load_registry()
                entry = self._get_entry(registry, task_id)
                _, workspace = self._entry_paths(entry)
                if not workspace.is_dir():
                    return SkillResult.fail("Coding workspace directory is missing.")
                code, status, err = await self._run_git(
                    workspace, "status", "--porcelain=v1", "--untracked-files=all"
                )
                if code != 0:
                    return SkillResult.fail(f"Unable to inspect workspace: {err}")
                if not status:
                    return SkillResult.ok("No changes to commit. Working tree clean.")
                code, out, err = await self._run_git(
                    workspace, "add", "--all", "--"
                )
                if code != 0:
                    return SkillResult.fail(f"Unable to stage workspace: {err or out}")
                code, out, err = await self._run_git(
                    workspace,
                    "-c",
                    "user.name=JAWL Agent",
                    "-c",
                    "user.email=agent@jawl.local",
                    "commit",
                    "-m",
                    commit_message.strip(),
                )
                if code != 0:
                    return SkillResult.fail(f"Unable to commit workspace: {err or out}")
                code, commit_hash, err = await self._run_git(
                    workspace, "rev-parse", "HEAD"
                )
                if code != 0:
                    return SkillResult.fail(
                        f"Commit created but identity lookup failed: {err}"
                    )
                entry["last_commit"] = commit_hash
                entry["last_commit_at"] = self._utc_now()
                self._save_registry(registry)
            main_logger.info(
                f"[Host OS] Committed coding workspace '{task_id}' at "
                f"{commit_hash[:12]}."
            )
            return SkillResult.ok(
                json.dumps(
                    {
                        "task_id": task_id,
                        "branch": entry["branch"],
                        "commit": commit_hash,
                        "summary": truncate_text(out, max_chars=3000),
                    },
                    ensure_ascii=False,
                )
            )
        except (PermissionError, FileNotFoundError, TimeoutError, ValueError, KeyError) as exc:
            return SkillResult.fail(str(exc))
        except Exception as exc:
            return SkillResult.fail(f"Error committing coding workspace: {exc}")

    @skill(swarm=[Subagents.CODER, Subagents.QA_ENGINEER, Subagents.SYSADMIN])
    @require_access(HostOSAccessLevel.SANDBOX)
    async def remove_coding_workspace(
        self, task_id: str, force: bool = False
    ) -> SkillResult:
        """Remove a worktree while preserving its branch and commits.

        Dirty worktrees are refused unless ``force`` is explicitly true. Forced
        cleanup first stores tracked and untracked changes in a recovery Git stash.
        """

        try:
            task_id = self._validate_task_id(task_id)
            async with self._lock:
                registry = self._load_registry()
                entry = self._get_entry(registry, task_id)
                repository, workspace = self._entry_paths(entry)
                recovery_stash = None
                if workspace.is_dir():
                    code, status, err = await self._run_git(
                        workspace,
                        "status",
                        "--porcelain=v1",
                        "--untracked-files=all",
                    )
                    if code != 0:
                        return SkillResult.fail(
                            f"Unable to inspect workspace before removal: {err}"
                        )
                    if status and not force:
                        return SkillResult.fail(
                            "Workspace has uncommitted changes. Commit them first, "
                            "or explicitly set force=true to move them into a "
                            "recovery stash before cleanup."
                        )
                    if status:
                        code, out, err = await self._run_git(
                            workspace,
                            "stash",
                            "push",
                            "--include-untracked",
                            "-m",
                            f"JAWL recovery before removing task {task_id}",
                        )
                        if code != 0:
                            return SkillResult.fail(
                                "Unable to preserve dirty workspace in a recovery "
                                f"stash: {err or out}"
                            )
                        code, recovery_stash, err = await self._run_git(
                            workspace, "rev-parse", "refs/stash"
                        )
                        if code != 0 or not recovery_stash:
                            return SkillResult.fail(
                                "Workspace was stashed but the recovery reference "
                                f"could not be resolved: {err}"
                            )
                    args = ["worktree", "remove"]
                    if force:
                        args.append("--force")
                    args.extend(["--", str(workspace)])
                    code, out, err = await self._run_git(repository, *args)
                    if code != 0:
                        return SkillResult.fail(
                            f"Unable to remove coding workspace: {err or out}"
                        )
                else:
                    await self._run_git(repository, "worktree", "prune")
                registry["workspaces"].pop(task_id)
                self._save_registry(registry)
            main_logger.info(
                f"[Host OS] Removed workspace '{task_id}'; branch preserved."
            )
            return SkillResult.ok(
                json.dumps(
                    {
                        "task_id": task_id,
                        "removed_workspace": entry["workspace_path"],
                        "preserved_branch": entry["branch"],
                        "recovery_stash": recovery_stash,
                    },
                    ensure_ascii=False,
                )
            )
        except (PermissionError, FileNotFoundError, TimeoutError, ValueError, KeyError) as exc:
            return SkillResult.fail(str(exc))
        except Exception as exc:
            return SkillResult.fail(f"Error removing coding workspace: {exc}")
