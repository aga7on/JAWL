"""Persistent task-scoped Git worktrees for coding agents."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.l2_interfaces.host.os.client import HostOSAccessLevel, HostOSClient
from src.l2_interfaces.host.os.decorators import require_access
from src.l2_interfaces.host.os.skills.coding_context import HostOSCodingContext
from src.l3_agent.skills.registry import SkillResult, skill
from src.l3_agent.swarm.roles import Subagents
from src.utils._tools import redact_sensitive_text, truncate_text
from src.utils.logger import main_logger
from src.utils.tracing import current_trace


class HostOSCodingWorkspaces:
    """Creates and manages isolated Git branches and worktrees per task."""

    _TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")
    _FINGERPRINT_IGNORED_PARTS = {
        ".coverage",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
    }
    _SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
    _MAX_REVIEW_SCOPES = 500

    def __init__(
        self,
        host_os_client: HostOSClient,
        coding_context: Optional[HostOSCodingContext] = None,
    ) -> None:
        self.host_os = host_os_client
        self.coding_context = coding_context or HostOSCodingContext(host_os_client)
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
        self,
        cwd: Path,
        *args: str,
        timeout: float = 120,
        strip_output: bool = True,
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
            decoded_stdout = stdout.decode("utf-8", errors="replace")
            decoded_stderr = stderr.decode("utf-8", errors="replace")
            if strip_output:
                decoded_stdout = decoded_stdout.strip()
                decoded_stderr = decoded_stderr.strip()
            return process.returncode, decoded_stdout, decoded_stderr
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

    async def _run_git_bounded(
        self,
        cwd: Path,
        *args: str,
        max_stdout_bytes: int,
        max_stderr_bytes: int = 65536,
        timeout: float = 120,
    ) -> Tuple[int, str, str, bool, bool]:
        """Run Git while draining pipes but retaining bounded byte prefixes."""

        if max_stdout_bytes < 1 or max_stderr_bytes < 1:
            raise ValueError("Git output byte limits must be positive.")
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        process = None
        tasks: List[asyncio.Task] = []

        async def read_bounded(
            stream: asyncio.StreamReader, limit: int
        ) -> Tuple[bytes, bool]:
            retained = bytearray()
            truncated = False
            while True:
                chunk = await stream.read(65536)
                if not chunk:
                    break
                remaining = limit - len(retained)
                if remaining > 0:
                    retained.extend(chunk[:remaining])
                if len(chunk) > max(0, remaining):
                    truncated = True
            return bytes(retained), truncated

        try:
            process = await asyncio.create_subprocess_exec(
                "git",
                *args,
                cwd=str(cwd),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            assert process.stdout is not None and process.stderr is not None
            tasks = [
                asyncio.create_task(read_bounded(process.stdout, max_stdout_bytes)),
                asyncio.create_task(read_bounded(process.stderr, max_stderr_bytes)),
                asyncio.create_task(process.wait()),
            ]
            stdout_result, stderr_result, return_code = await asyncio.wait_for(
                asyncio.gather(*tasks), timeout=timeout
            )
            stdout, stdout_truncated = stdout_result
            stderr, stderr_truncated = stderr_result
            return (
                return_code,
                stdout.decode("utf-8", errors="replace").strip(),
                stderr.decode("utf-8", errors="replace").strip(),
                stdout_truncated,
                stderr_truncated,
            )
        except FileNotFoundError as exc:
            raise FileNotFoundError("'git' utility was not found.") from exc
        except asyncio.TimeoutError as exc:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise TimeoutError(
                f"Git command timed out after {timeout:g} seconds."
            ) from exc
        except asyncio.CancelledError:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
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

    async def _list_changed_files(
        self,
        workspace: Path,
        *,
        pathspec: Optional[str] = None,
        staged: bool = False,
    ) -> Tuple[List[str], List[str]]:
        """Return exact UTF-8 tracked/untracked paths without Git quoting."""

        tracked_args = [
            "-c",
            "core.quotePath=false",
            "diff",
            "--name-only",
            "-z",
            "--no-ext-diff",
        ]
        if staged:
            tracked_args.append("--cached")
        else:
            tracked_args.append("HEAD")
        tracked_args.append("--")
        if pathspec:
            tracked_args.append(pathspec)
        code, tracked_output, err = await self._run_git(
            workspace, *tracked_args, strip_output=False
        )
        if code != 0:
            raise ValueError(
                f"Unable to list tracked changes: {err or tracked_output}"
            )
        tracked_files = [
            path.replace("\\", "/")
            for path in tracked_output.split("\x00")
            if path
        ]

        untracked_files: List[str] = []
        if not staged:
            code, untracked_output, err = await self._run_git(
                workspace,
                "ls-files",
                "--others",
                "--exclude-standard",
                "-z",
                "--",
                *([pathspec] if pathspec else []),
                strip_output=False,
            )
            if code != 0:
                raise ValueError(f"Unable to list untracked files: {err}")
            untracked_files = sorted(
                path.replace("\\", "/")
                for path in untracked_output.split("\x00")
                if path
            )
        return tracked_files, untracked_files

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

    def resolve_workspace_path(
        self, task_id: str, relative_path: str = ".", is_write: bool = False
    ) -> Path:
        """Resolve a task-relative path without exposing worktree internals to models."""

        task_id = self._validate_task_id(task_id)
        relative = Path(relative_path.strip() or ".")
        if relative.is_absolute() or len(str(relative)) > 1000:
            raise ValueError("relative_path must be a bounded relative path.")
        registry = self._load_registry()
        entry = self._get_entry(registry, task_id)
        _, workspace = self._entry_paths(entry)
        resolved = (workspace / relative).resolve()
        if not resolved.is_relative_to(workspace) or ".git" in relative.parts:
            raise PermissionError(
                "Task-relative path must stay inside the managed workspace."
            )
        return self.host_os.validate_path(resolved, is_write=is_write)

    async def workspace_fingerprint(self, workspace: Path) -> Dict[str, str]:
        """Fingerprint HEAD plus every tracked change and untracked file."""

        code, head, err = await self._run_git(workspace, "rev-parse", "HEAD")
        if code != 0:
            raise ValueError(f"Unable to resolve workspace HEAD: {err}")
        code, staged_diff, err = await self._run_git(
            workspace, "diff", "--cached", "--binary", "HEAD", "--"
        )
        if code != 0:
            raise ValueError(f"Unable to fingerprint staged workspace diff: {err}")
        code, unstaged_diff, err = await self._run_git(
            workspace, "diff", "--binary", "--"
        )
        if code != 0:
            raise ValueError(f"Unable to fingerprint unstaged workspace diff: {err}")
        code, untracked_output, err = await self._run_git(
            workspace,
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
            strip_output=False,
        )
        if code != 0:
            raise ValueError(f"Unable to list untracked workspace files: {err}")
        untracked = sorted(
            path
            for path in untracked_output.split("\x00")
            if path
            and not any(
                part in self._FINGERPRINT_IGNORED_PARTS
                or part.endswith((".pyc", ".pyo"))
                for part in Path(path).parts
            )
        )

        def _hash() -> str:
            digest = hashlib.sha256()
            digest.update(b"jawl-workspace-v2\x00")
            digest.update(head.encode("utf-8"))
            digest.update(b"\x00staged-diff\x00")
            digest.update(staged_diff.encode("utf-8"))
            digest.update(b"\x00unstaged-diff\x00")
            digest.update(unstaged_diff.encode("utf-8"))
            for relative_path in untracked:
                candidate = workspace / relative_path
                digest.update(b"\x00untracked\x00")
                digest.update(relative_path.encode("utf-8", errors="surrogatepass"))
                digest.update(b"\x00")
                if candidate.is_symlink():
                    digest.update(os.readlink(candidate).encode("utf-8"))
                    continue
                resolved = candidate.resolve()
                if not resolved.is_relative_to(workspace.resolve()):
                    raise PermissionError(
                        f"Untracked path escaped coding workspace ({relative_path})."
                    )
                if not candidate.is_file():
                    digest.update(b"[missing-or-non-file]")
                    continue
                with open(candidate, "rb") as stream:
                    while chunk := stream.read(1024 * 1024):
                        digest.update(chunk)
            return digest.hexdigest()

        return {
            "head": head,
            "fingerprint": await asyncio.to_thread(_hash),
        }

    def _cleanup_generated_python_caches(self, workspace: Path) -> None:
        """Remove only conventional generated Python cache artifacts."""

        cache_directories = {
            ".mypy_cache",
            ".pytest_cache",
            ".ruff_cache",
            "__pycache__",
        }
        workspace_root = workspace.resolve()
        candidates = sorted(
            workspace.rglob("*"), key=lambda path: len(path.parts), reverse=True
        )
        for candidate in candidates:
            if candidate.name in cache_directories and candidate.is_dir():
                resolved = candidate.resolve()
                if resolved.is_relative_to(workspace_root):
                    shutil.rmtree(resolved, ignore_errors=True)
                continue
            if candidate.is_file() and (
                candidate.name == ".coverage"
                or candidate.suffix.lower() in {".pyc", ".pyo"}
            ):
                resolved = candidate.resolve()
                if resolved.is_relative_to(workspace_root):
                    resolved.unlink(missing_ok=True)

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
        payload.pop("diff_review", None)
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
        fingerprint = await self.workspace_fingerprint(workspace)
        payload["state"] = "dirty" if status.splitlines()[1:] else "clean"
        payload["status"] = truncate_text(status, max_chars=6000)
        payload["diff_stat"] = truncate_text(diff_stat, max_chars=6000)
        payload["head"] = fingerprint["head"]
        payload["workspace_fingerprint"] = fingerprint["fingerprint"]
        tracked, untracked = await self._list_changed_files(workspace)
        payload["diff_review"] = self._diff_review_status(
            entry, fingerprint, sorted(set(tracked + untracked))
        )
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

    @staticmethod
    def _untracked_diff_preview(
        path: Path, relative_path: str, max_bytes: int
    ) -> str:
        try:
            total_bytes = path.stat().st_size
            with path.open("rb") as stream:
                content = stream.read(max_bytes + 1)
        except OSError as exc:
            return f"diff --jawl-untracked {relative_path}\n[read error: {exc}]\n"
        preview_truncated = len(content) > max_bytes
        content = content[:max_bytes]
        header = (
            f"diff --jawl-untracked a/{relative_path} b/{relative_path}\n"
            "new file mode (untracked)\n"
            "--- /dev/null\n"
            f"+++ b/{relative_path}\n"
        )
        if b"\x00" in content[:4096]:
            return header + f"Binary file ({total_bytes} bytes)\n"
        text = content.decode("utf-8", errors="replace")
        lines = text.splitlines()
        body = "\n".join(f"+{line}" for line in lines)
        if text.endswith(("\n", "\r")):
            body += "\n"
        if preview_truncated:
            body += (
                "\n+... [Untracked preview truncated after "
                f"{max_bytes} of {total_bytes} bytes.]\n"
            )
        line_count = max(1, len(lines) + int(preview_truncated))
        return header + f"@@ -0,0 +1,{line_count} @@\n" + body

    def _structured_diff_hunks(
        self,
        workspace: Path,
        diff_text: str,
        diff_truncated: bool,
        max_hunks: int = 200,
    ) -> Dict[str, Any]:
        """Parse reviewed unified text into bounded, symbol-aware hunk metadata."""

        header_pattern = re.compile(
            r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$"
        )
        current_file = ""
        old_file = ""
        current: Optional[Dict[str, Any]] = None
        hunks: List[Dict[str, Any]] = []
        total_hunks = 0

        def finish() -> None:
            nonlocal current, total_hunks
            if current is None:
                return
            total_hunks += 1
            raw = "".join(current.pop("_raw"))
            current["reviewed_hunk_sha256"] = hashlib.sha256(
                raw.encode("utf-8")
            ).hexdigest()
            if len(hunks) < max_hunks:
                hunks.append(current)
            current = None

        for line in diff_text.splitlines(keepends=True):
            if line.startswith("diff --git "):
                finish()
                current_file = ""
                old_file = ""
                continue
            if current is None and line.startswith("--- "):
                candidate = line[4:].strip().strip('"')
                if candidate != "/dev/null":
                    old_file = (
                        candidate[2:] if candidate.startswith("a/") else candidate
                    )
                continue
            if current is None and line.startswith("+++ "):
                candidate = line[4:].strip().strip('"')
                if candidate != "/dev/null":
                    current_file = (
                        candidate[2:] if candidate.startswith("b/") else candidate
                    )
                else:
                    current_file = old_file
                continue
            match = header_pattern.match(line.rstrip("\r\n"))
            if match:
                finish()
                old_count = int(match.group(2) or "1")
                new_count = int(match.group(4) or "1")
                current = {
                    "file": current_file,
                    "old_start": int(match.group(1)),
                    "old_count": old_count,
                    "new_start": int(match.group(3)),
                    "new_count": new_count,
                    "header_context": match.group(5).strip()[:240],
                    "additions": 0,
                    "deletions": 0,
                    "complete": True,
                    "_raw": [line],
                }
                continue
            if current is None:
                continue
            current["_raw"].append(line)
            if line.startswith("+") and not line.startswith("+++"):
                current["additions"] += 1
            elif line.startswith("-") and not line.startswith("---"):
                current["deletions"] += 1
        finish()
        if diff_truncated and hunks:
            hunks[-1]["complete"] = False

        spans_by_file: Dict[str, Tuple[List[Dict[str, Any]], Optional[str]]] = {}
        workspace_root = workspace.resolve()
        for hunk in hunks:
            relative = hunk["file"]
            if relative not in spans_by_file:
                candidate = (workspace / relative).resolve()
                spans: List[Dict[str, Any]] = []
                error: Optional[str] = None
                try:
                    if (
                        candidate.is_relative_to(workspace_root)
                        and candidate.is_file()
                        and candidate.stat().st_size
                        <= self.coding_context._MAX_SOURCE_BYTES
                    ):
                        source = candidate.read_text(encoding="utf-8", errors="strict")
                        spans, error, _ = self.coding_context.definition_spans(
                            candidate, source
                        )
                    else:
                        error = "source unavailable or over structural limit"
                except (OSError, UnicodeError) as exc:
                    error = f"source read failed: {type(exc).__name__}"
                spans_by_file[relative] = spans, error
            spans, error = spans_by_file[relative]
            start = int(hunk["new_start"])
            end = start + max(1, int(hunk["new_count"])) - 1
            matches = [
                span
                for span in spans
                if int(span["start_line"]) <= end
                and int(span["end_line"]) >= start
            ]
            matches.sort(
                key=lambda item: (
                    int(item["end_line"]) - int(item["start_line"]),
                    item["qualified_name"],
                )
            )
            hunk["symbols"] = [
                {
                    "qualified_name": item["qualified_name"],
                    "kind": item["kind"],
                    "start_line": item["start_line"],
                    "end_line": item["end_line"],
                    "backend": item["backend"],
                }
                for item in matches[:3]
            ]
            hunk["symbol_context_truncated"] = len(matches) > 3
            if error:
                hunk["symbol_context_error"] = error[:240]

        review_contract = [
            {
                key: value
                for key, value in hunk.items()
                if key not in {"symbols", "symbol_context_error"}
            }
            for hunk in hunks
        ]
        return {
            "hunk_count": total_hunks,
            "hunks_returned": len(hunks),
            "hunks_truncated": total_hunks > len(hunks),
            "hunks": hunks,
            "hunk_review_sha256": hashlib.sha256(
                json.dumps(
                    review_contract,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
        }

    @skill(swarm=[Subagents.CODER, Subagents.QA_ENGINEER, Subagents.SYSADMIN])
    @require_access(HostOSAccessLevel.SANDBOX)
    async def get_coding_workspace_diff(
        self,
        task_id: str,
        file_path: Optional[str] = None,
        staged: bool = False,
        context_lines: int = 3,
        max_chars: int = 30000,
        file_offset: int = 0,
        file_limit: int = 50,
    ) -> SkillResult:
        """Return a bounded unified diff for a task or one relative file.

        Whole-task output lists all changed and untracked files. If truncated,
        request individual files with ``file_path`` before verification/commit.
        Common credential forms are redacted from the returned diff.
        """

        if context_lines < 0 or context_lines > 20:
            return SkillResult.fail("context_lines must be between 0 and 20.")
        if max_chars < 1000 or max_chars > 100000:
            return SkillResult.fail("max_chars must be between 1000 and 100000.")
        if file_offset < 0:
            return SkillResult.fail("file_offset must be zero or greater.")
        if file_limit < 1 or file_limit > 200:
            return SkillResult.fail("file_limit must be between 1 and 200.")
        try:
            task_id = self._validate_task_id(task_id)
            async with self._lock:
                entry = self._get_entry(self._load_registry(), task_id)
                _, workspace = self._entry_paths(entry)
                if not workspace.is_dir():
                    return SkillResult.fail("Coding workspace directory is missing.")

                pathspec = None
                if file_path is not None:
                    requested = Path(file_path.replace("\\", "/"))
                    if requested.is_absolute() or ".." in requested.parts:
                        return SkillResult.fail(
                            "file_path must be relative to the coding workspace."
                        )
                    candidate = (workspace / requested).resolve()
                    if not candidate.is_relative_to(workspace.resolve()):
                        return SkillResult.fail(
                            "file_path escaped the coding workspace."
                        )
                    pathspec = requested.as_posix()

                tracked_files, untracked_files = await self._list_changed_files(
                    workspace, pathspec=pathspec, staged=staged
                )

                all_changed_files = sorted(set(tracked_files + untracked_files))
                if pathspec:
                    selected_files = all_changed_files
                    effective_offset = 0
                else:
                    selected_files = all_changed_files[
                        file_offset : file_offset + file_limit
                    ]
                    effective_offset = file_offset
                selected_set = set(selected_files)
                selected_tracked = [
                    path for path in tracked_files if path in selected_set
                ]
                selected_untracked = [
                    path for path in untracked_files if path in selected_set
                ]

                diff_args = [
                    "-c",
                    "core.quotePath=false",
                    "diff",
                    "--no-ext-diff",
                    f"--unified={context_lines}",
                ]
                if staged:
                    diff_args.append("--cached")
                else:
                    diff_args.append("HEAD")
                diff_args.append("--")
                diff_args.extend(selected_tracked)
                diff_text = ""
                diff_collection_truncated = False
                if selected_tracked:
                    (
                        code,
                        diff_text,
                        err,
                        diff_collection_truncated,
                        _,
                    ) = await self._run_git_bounded(
                        workspace,
                        *diff_args,
                        max_stdout_bytes=max_chars * 4,
                    )
                    if code != 0:
                        return SkillResult.fail(
                            f"Unable to read workspace diff: {err or diff_text}"
                        )
                if not staged:
                    for untracked in selected_untracked:
                        remaining_chars = max_chars - len(diff_text)
                        if remaining_chars <= 0:
                            break
                        preview_path = (workspace / untracked).resolve()
                        if not preview_path.is_relative_to(workspace.resolve()):
                            continue
                        preview_limit = min(
                            max(1024, remaining_chars),
                            1_000_000,
                        )
                        preview = await asyncio.to_thread(
                            self._untracked_diff_preview,
                            preview_path,
                            untracked,
                            preview_limit,
                        )
                        diff_text += ("\n" if diff_text else "") + preview

                diff_text = redact_sensitive_text(diff_text)
                collected_chars = len(diff_text)
                original_chars = (
                    None if diff_collection_truncated else collected_chars
                )
                truncated = diff_collection_truncated or collected_chars > max_chars
                if truncated:
                    reason = (
                        "Git output byte limit reached; request a smaller file/range."
                        if diff_collection_truncated
                        else "Request changed files individually."
                    )
                    diff_text = (
                        diff_text[:max_chars]
                        + f"\n... [Diff truncated. {reason}]"
                    )
                fingerprint = await self.workspace_fingerprint(workspace)
                payload = {
                    "task_id": task_id,
                    "branch": entry["branch"],
                    "file_path": pathspec,
                    "staged": staged,
                    "context_lines": context_lines,
                    "max_chars": max_chars,
                    "file_offset": effective_offset,
                    "file_limit": file_limit,
                    "changed_file_count": len(all_changed_files),
                    "changed_files": selected_files,
                    "tracked_files": selected_tracked,
                    "untracked_files": selected_untracked,
                    "file_page_has_more": (
                        not pathspec
                        and file_offset + len(selected_files)
                        < len(all_changed_files)
                    ),
                    "fingerprint": fingerprint,
                    "original_diff_chars": original_chars,
                    "collected_diff_chars": collected_chars,
                    "diff_collection_truncated": diff_collection_truncated,
                    "truncated": truncated,
                    "diff": diff_text,
                    "reviewed_diff_sha256": hashlib.sha256(
                        diff_text.encode("utf-8")
                    ).hexdigest(),
                }
                hunk_payload = await asyncio.to_thread(
                    self._structured_diff_hunks,
                    workspace,
                    diff_text,
                    truncated,
                )
                payload.update(hunk_payload)
                payload["hunk_analysis_complete"] = bool(
                    not truncated
                    and not payload["file_page_has_more"]
                    and not hunk_payload["hunks_truncated"]
                    and all(item["complete"] for item in hunk_payload["hunks"])
                )
            return SkillResult.ok(json.dumps(payload, ensure_ascii=False))
        except (PermissionError, FileNotFoundError, TimeoutError, ValueError, KeyError) as exc:
            return SkillResult.fail(str(exc))
        except Exception as exc:
            return SkillResult.fail(f"Error reading coding workspace diff: {exc}")

    @staticmethod
    def _diff_review_status(
        entry: Dict[str, Any],
        fingerprint: Dict[str, str],
        changed_files: List[str],
    ) -> Dict[str, Any]:
        review = entry.get("diff_review")
        plan = entry.get("task_plan")
        is_current = bool(
            isinstance(review, dict)
            and review.get("fingerprint") == fingerprint["fingerprint"]
            and review.get("head") == fingerprint["head"]
        )
        covered = sorted(
            set(review.get("covered_files", [])) & set(changed_files)
            if is_current
            else set()
        )
        missing = sorted(set(changed_files) - set(covered))
        return {
            "required_by_plan": bool(
                isinstance(plan, dict) and plan.get("requires_diff_review", False)
            ),
            "is_current": is_current,
            "ready_for_commit": not changed_files or (is_current and not missing),
            "workspace_fingerprint": fingerprint["fingerprint"],
            "changed_file_count": len(changed_files),
            "covered_file_count": len(covered),
            "covered_files": covered[:500],
            "missing_files": missing[:500],
            "file_list_truncated": len(changed_files) > 500,
            "accepted_scope_count": (
                len(review.get("scopes", {})) if is_current else 0
            ),
            "accepted_at": review.get("accepted_at") if is_current else None,
        }

    @skill(swarm=[Subagents.CODER, Subagents.QA_ENGINEER, Subagents.SYSADMIN])
    @require_access(HostOSAccessLevel.SANDBOX)
    async def accept_coding_workspace_diff_review(
        self,
        task_id: str,
        expected_workspace_fingerprint: str,
        expected_reviewed_diff_sha256: str,
        file_path: Optional[str] = None,
        context_lines: int = 3,
        max_chars: int = 30000,
    ) -> SkillResult:
        """Persist explicit review evidence for one complete exact-state diff."""

        try:
            return await self._accept_coding_workspace_diff_review(
                task_id,
                expected_workspace_fingerprint,
                expected_reviewed_diff_sha256,
                file_path=file_path,
                context_lines=context_lines,
                max_chars=max_chars,
            )
        except (PermissionError, FileNotFoundError, TimeoutError, ValueError, KeyError) as exc:
            return SkillResult.fail(str(exc))
        except Exception as exc:
            return SkillResult.fail(f"Error accepting diff review: {exc}")

    async def _accept_coding_workspace_diff_review(
        self,
        task_id: str,
        expected_workspace_fingerprint: str,
        expected_reviewed_diff_sha256: str,
        file_path: Optional[str] = None,
        context_lines: int = 3,
        max_chars: int = 30000,
    ) -> SkillResult:
        """Persist explicit review evidence for one complete exact-state diff.

        The diff is recomputed before acceptance. Whole-workspace acceptance
        covers every changed file only when its file page and hunks are complete;
        otherwise callers accept complete per-file views under the same workspace
        fingerprint. Raw diff text is never persisted.
        """

        expected_workspace_fingerprint = str(
            expected_workspace_fingerprint or ""
        ).lower()
        expected_reviewed_diff_sha256 = str(
            expected_reviewed_diff_sha256 or ""
        ).lower()
        if not self._SHA256_PATTERN.fullmatch(expected_workspace_fingerprint):
            return SkillResult.fail(
                "expected_workspace_fingerprint must be a SHA-256 value."
            )
        if not self._SHA256_PATTERN.fullmatch(expected_reviewed_diff_sha256):
            return SkillResult.fail(
                "expected_reviewed_diff_sha256 must be a SHA-256 value."
            )

        reviewed = await self.get_coding_workspace_diff(
            task_id,
            file_path=file_path,
            staged=False,
            context_lines=context_lines,
            max_chars=max_chars,
            file_offset=0,
            file_limit=200,
        )
        if not reviewed.is_success:
            return reviewed
        payload = json.loads(reviewed.message)
        if not payload.get("changed_files"):
            return SkillResult.fail("Diff review rejected: workspace has no changes.")
        if not payload.get("hunk_analysis_complete"):
            return SkillResult.fail(
                "Diff review rejected: this response is incomplete. Request "
                "complete per-file diff output before accepting it."
            )
        observed_fingerprint = payload["fingerprint"]["fingerprint"]
        if observed_fingerprint != expected_workspace_fingerprint:
            return SkillResult.fail(
                "Diff review rejected: workspace fingerprint changed since review "
                f"(expected {expected_workspace_fingerprint}, current "
                f"{observed_fingerprint})."
            )
        observed_review_hash = payload["reviewed_diff_sha256"]
        if observed_review_hash != expected_reviewed_diff_sha256:
            return SkillResult.fail(
                "Diff review rejected: reviewed diff hash does not match the "
                "recomputed exact-state diff."
            )

        task_id = self._validate_task_id(task_id)
        async with self._lock:
            registry = self._load_registry()
            entry = self._get_entry(registry, task_id)
            _, workspace = self._entry_paths(entry)
            current_fingerprint = await self.workspace_fingerprint(workspace)
            if current_fingerprint["fingerprint"] != expected_workspace_fingerprint:
                return SkillResult.fail(
                    "Diff review rejected: workspace changed while acceptance was "
                    "being recorded."
                )
            tracked, untracked = await self._list_changed_files(workspace)
            current_files = sorted(set(tracked + untracked))
            scope = payload.get("file_path") or "*"
            existing = entry.get("diff_review")
            if not isinstance(existing, dict) or (
                existing.get("fingerprint") != expected_workspace_fingerprint
                or existing.get("head") != current_fingerprint["head"]
            ):
                existing = {
                    "version": 1,
                    "fingerprint": expected_workspace_fingerprint,
                    "head": current_fingerprint["head"],
                    "scopes": {},
                    "covered_files": [],
                }
            scopes = existing.setdefault("scopes", {})
            scope_key = "workspace" if scope == "*" else f"file:{scope}"
            if scope_key not in scopes and len(scopes) >= self._MAX_REVIEW_SCOPES:
                return SkillResult.fail(
                    "Diff review rejected: accepted scope limit reached; use an "
                    "explicit commit review bypass for this unusually large change."
                )
            now = self._utc_now()
            scopes[scope_key] = {
                "file_path": None if scope == "*" else scope,
                "reviewed_diff_sha256": observed_review_hash,
                "hunk_review_sha256": payload["hunk_review_sha256"],
                "hunk_count": payload["hunk_count"],
                "changed_files": payload["changed_files"][:500],
                "accepted_at": now,
                "trace": current_trace(),
            }
            if scope == "*":
                existing["covered_files"] = current_files[:500]
            else:
                covered = set(existing.get("covered_files", []))
                covered.update(payload["changed_files"])
                existing["covered_files"] = sorted(covered)[:500]
            existing["accepted_at"] = now
            entry["diff_review"] = existing
            status = self._diff_review_status(
                entry, current_fingerprint, current_files
            )
            self._save_registry(registry)
        return SkillResult.ok(json.dumps(status, ensure_ascii=False))

    @skill(swarm=[Subagents.CODER, Subagents.QA_ENGINEER, Subagents.SYSADMIN])
    @require_access(HostOSAccessLevel.SANDBOX)
    async def get_coding_workspace_diff_review_status(
        self, task_id: str
    ) -> SkillResult:
        """Return bounded exact-state diff-review coverage without raw content."""

        try:
            task_id = self._validate_task_id(task_id)
            async with self._lock:
                entry = self._get_entry(self._load_registry(), task_id)
                _, workspace = self._entry_paths(entry)
                fingerprint = await self.workspace_fingerprint(workspace)
                tracked, untracked = await self._list_changed_files(workspace)
                status = self._diff_review_status(
                    entry, fingerprint, sorted(set(tracked + untracked))
                )
            return SkillResult.ok(json.dumps(status, ensure_ascii=False))
        except (PermissionError, FileNotFoundError, TimeoutError, ValueError, KeyError) as exc:
            return SkillResult.fail(str(exc))
        except Exception as exc:
            return SkillResult.fail(f"Error reading diff review status: {exc}")

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
        self,
        task_id: str,
        commit_message: str,
        require_verified: bool = True,
        require_plan_complete: bool = True,
        require_diff_review: Optional[bool] = None,
    ) -> SkillResult:
        """Commit task changes locally without pushing or merging them.

        By default, the exact working state must have a successful verification
        fingerprint. Set ``require_verified=false`` explicitly only for changes
        that cannot reasonably execute (for example, documentation-only work).
        If the task has a durable coding plan, all steps and requirements must
        be complete unless ``require_plan_complete=false`` is explicit.
        New plans also require exact-state diff-review coverage by default.
        Existing plans and ad-hoc workspaces retain their prior behavior unless
        ``require_diff_review=true`` is explicit; ``false`` records a bypass.
        """

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
                await asyncio.to_thread(
                    self._cleanup_generated_python_caches, workspace
                )
                current_fingerprint = await self.workspace_fingerprint(workspace)
                verification = entry.get("last_verification")
                is_verified = bool(
                    verification
                    and verification.get("state") == "passed"
                    and verification.get("fingerprint_after")
                    == current_fingerprint["fingerprint"]
                    and verification.get("head_before") == current_fingerprint["head"]
                )
                if require_verified and not is_verified:
                    return SkillResult.fail(
                        "Commit rejected: this exact workspace state has not passed "
                        "coding verification. Run run_coding_verification first, "
                        "or explicitly set require_verified=false for a justified "
                        "non-executable change."
                    )
                plan = entry.get("task_plan")
                plan_ready = True
                if plan:
                    incomplete_steps = [
                        step["id"]
                        for step in plan.get("steps", [])
                        if step.get("status") != "completed"
                    ]
                    incomplete_requirements = [
                        item["id"]
                        for item in plan.get("requirements", [])
                        if item.get("status") != "satisfied"
                    ]
                    replan_required = bool(
                        plan.get("replanning", {}).get("required")
                    )
                    plan_ready = (
                        not incomplete_steps
                        and not incomplete_requirements
                        and not replan_required
                    )
                    if require_plan_complete and not plan_ready:
                        return SkillResult.fail(
                            "Commit rejected: coding plan is incomplete. Pending "
                            f"steps={incomplete_steps}; requirements="
                            f"{incomplete_requirements}; replan_required="
                            f"{replan_required}. Update plan evidence first, "
                            "or set require_plan_complete=false explicitly for an "
                            "intermediate checkpoint commit."
                        )
                plan_requires_review = bool(
                    plan and plan.get("requires_diff_review", False)
                )
                review_required = (
                    plan_requires_review
                    if require_diff_review is None
                    else bool(require_diff_review)
                )
                tracked, untracked = await self._list_changed_files(workspace)
                changed_files = sorted(set(tracked + untracked))
                review_status = self._diff_review_status(
                    entry, current_fingerprint, changed_files
                )
                review_ready = bool(review_status["ready_for_commit"])
                if review_required and not review_ready:
                    return SkillResult.fail(
                        "Commit rejected: this exact workspace state has not been "
                        "fully reviewed. Accept complete workspace/per-file diffs "
                        "with accept_coding_workspace_diff_review first; missing="
                        f"{review_status['missing_files']}, or explicitly set "
                        "require_diff_review=false for an intermediate bypass."
                    )
                code, status, err = await self._run_git(
                    workspace, "status", "--porcelain=v1", "--untracked-files=all"
                )
                if code != 0:
                    return SkillResult.fail(f"Unable to inspect workspace: {err}")
                if not status:
                    return SkillResult.ok("No changes to commit. Working tree clean.")
                code, out, err = await self._run_git(
                    workspace,
                    "add",
                    "--all",
                    "--",
                    ".",
                    ":(exclude)**/__pycache__/**",
                    ":(exclude)**/.pytest_cache/**",
                    ":(exclude)**/.mypy_cache/**",
                    ":(exclude)**/.ruff_cache/**",
                    ":(exclude)**/*.pyc",
                    ":(exclude)**/*.pyo",
                    ":(exclude)**/.coverage",
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
                entry["last_commit_verification_bypassed"] = not is_verified
                entry["last_commit_plan_bypassed"] = bool(plan and not plan_ready)
                entry["last_commit_diff_review_bypassed"] = bool(
                    plan_requires_review and not review_ready
                )
                entry["last_commit_trace"] = current_trace()
                review_evidence_sha256 = None
                accepted_review = entry.get("diff_review")
                if review_ready and review_status["is_current"] and isinstance(
                    accepted_review, dict
                ):
                    scope_contract = [
                        {
                            "scope": scope,
                            "reviewed_diff_sha256": evidence.get(
                                "reviewed_diff_sha256"
                            ),
                            "hunk_review_sha256": evidence.get(
                                "hunk_review_sha256"
                            ),
                            "changed_files": evidence.get("changed_files", []),
                        }
                        for scope, evidence in sorted(
                            accepted_review.get("scopes", {}).items()
                        )
                    ]
                    review_evidence_sha256 = hashlib.sha256(
                        json.dumps(
                            scope_contract,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest()
                    accepted_review["committed_as"] = commit_hash
                    entry["last_commit_diff_review"] = {
                        "commit": commit_hash,
                        "workspace_fingerprint": current_fingerprint[
                            "fingerprint"
                        ],
                        "review_evidence_sha256": review_evidence_sha256,
                        "covered_file_count": review_status[
                            "covered_file_count"
                        ],
                        "accepted_scope_count": review_status[
                            "accepted_scope_count"
                        ],
                        "accepted_at": review_status["accepted_at"],
                    }
                if is_verified:
                    verification["committed_as"] = commit_hash
                    verification["post_commit_fingerprint"] = (
                        await self.workspace_fingerprint(workspace)
                    )["fingerprint"]
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
                        "verification_bypassed": not is_verified,
                        "plan_completion_bypassed": bool(plan and not plan_ready),
                        "diff_review_bypassed": bool(
                            plan_requires_review and not review_ready
                        ),
                        "diff_review_evidence_sha256": review_evidence_sha256,
                        "trace": current_trace(),
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
