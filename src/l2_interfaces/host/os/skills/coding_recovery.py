"""Transactional workspace, plan, and episodic-context recovery for coding tasks."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.l0_state.agent.state import AgentState
from src.l1_databases.sql.management.ticks import SQLTicks
from src.l2_interfaces.host.os.client import HostOSAccessLevel, HostOSClient
from src.l2_interfaces.host.os.decorators import require_access
from src.l2_interfaces.host.os.skills.coding_workspaces import (
    HostOSCodingWorkspaces,
)
from src.l3_agent.skills.registry import SkillResult, skill
from src.l3_agent.swarm.roles import Subagents
from src.utils._tools import redact_sensitive_text
from src.utils.tracing import current_trace


class HostOSCodingRecovery:
    """Creates immutable recovery points and guarded, compensating rewinds."""

    _CHECKPOINT_ID = re.compile(r"^[0-9a-f]{32}$")
    _OBJECT_ID = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
    _MAX_CHECKPOINTS = 100

    def __init__(
        self,
        host_os_client: HostOSClient,
        workspaces: HostOSCodingWorkspaces,
        ticks: SQLTicks,
        agent_state: Optional[AgentState] = None,
    ) -> None:
        self.host_os = host_os_client
        self.workspaces = workspaces
        self.ticks = ticks
        self.agent_state = agent_state

    @staticmethod
    def _bounded_reason(reason: str) -> str:
        clean = redact_sensitive_text(str(reason).strip())
        if not clean:
            raise ValueError("reason must be a non-empty string.")
        if len(clean) > 1000:
            raise ValueError("reason cannot exceed 1000 characters.")
        return clean

    @classmethod
    def _validate_checkpoint_id(cls, checkpoint_id: str) -> str:
        normalized = str(checkpoint_id).strip().lower()
        if not cls._CHECKPOINT_ID.fullmatch(normalized):
            raise ValueError("Invalid recovery checkpoint_id.")
        return normalized

    @classmethod
    def _validate_object_id(cls, object_id: str, field: str) -> str:
        normalized = str(object_id).strip().lower()
        if not cls._OBJECT_ID.fullmatch(normalized):
            raise ValueError(f"Invalid {field} in recovery checkpoint.")
        return normalized

    @staticmethod
    def _validate_fingerprint(value: str, field: str) -> str:
        normalized = str(value).strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", normalized):
            raise ValueError(f"{field} must be a 64-character SHA-256 value.")
        return normalized

    async def _run_git_env(
        self,
        workspace: Path,
        args: List[str],
        extra_env: Optional[Dict[str, str]] = None,
        stdin: Optional[bytes] = None,
        timeout: float = 120,
    ) -> Tuple[int, str, str]:
        env = os.environ.copy()
        env.update(
            {
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_AUTHOR_NAME": "JAWL Recovery",
                "GIT_AUTHOR_EMAIL": "recovery@localhost",
                "GIT_COMMITTER_NAME": "JAWL Recovery",
                "GIT_COMMITTER_EMAIL": "recovery@localhost",
            }
        )
        if extra_env:
            env.update(extra_env)
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                "git",
                *args,
                cwd=str(workspace),
                env=env,
                stdin=asyncio.subprocess.PIPE if stdin is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(stdin), timeout=timeout
            )
            return (
                process.returncode,
                stdout.decode("utf-8", errors="replace").strip(),
                stderr.decode("utf-8", errors="replace").strip(),
            )
        except asyncio.TimeoutError as exc:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            raise TimeoutError(
                f"Recovery Git command timed out after {timeout:g} seconds."
            ) from exc
        except asyncio.CancelledError:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            raise

    async def _snapshot_commit(
        self, workspace: Path, task_id: str, checkpoint_id: str, head: str
    ) -> Tuple[str, str, str]:
        code, index_tree, err = await self._run_git_env(workspace, ["write-tree"])
        if code != 0 or not index_tree:
            raise ValueError(f"Unable to snapshot recovery index: {err or index_tree}")
        index_tree = self._validate_object_id(index_tree, "index tree")
        descriptor, index_name = tempfile.mkstemp(
            prefix="jawl-recovery-index-", dir=self.host_os.system_dir
        )
        os.close(descriptor)
        index_path = Path(index_name)
        index_path.unlink(missing_ok=True)
        env = {"GIT_INDEX_FILE": str(index_path)}
        ref = f"refs/jawl/checkpoints/{task_id}/{checkpoint_id}"
        try:
            for args in (["read-tree", head], ["add", "-A", "--", "."]):
                code, out, err = await self._run_git_env(
                    workspace, list(args), extra_env=env
                )
                if code != 0:
                    raise ValueError(
                        f"Unable to stage recovery snapshot: {err or out}"
                    )
            code, tree, err = await self._run_git_env(
                workspace, ["write-tree"], extra_env=env
            )
            if code != 0 or not tree:
                raise ValueError(f"Unable to write recovery tree: {err or tree}")
            code, commit, err = await self._run_git_env(
                workspace,
                [
                    "commit-tree",
                    tree,
                    "-p",
                    head,
                    "-m",
                    f"JAWL recovery checkpoint {checkpoint_id}",
                ],
                extra_env=env,
            )
            if code != 0 or not commit:
                raise ValueError(
                    f"Unable to create recovery commit: {err or commit}"
                )
            commit = self._validate_object_id(commit, "snapshot commit")
            code, out, err = await self._run_git_env(
                workspace, ["update-ref", ref, commit]
            )
            if code != 0:
                raise ValueError(f"Unable to persist recovery ref: {err or out}")
            return commit, ref, index_tree
        finally:
            index_path.unlink(missing_ok=True)

    async def _delete_ref(self, workspace: Path, ref: str) -> None:
        await self._run_git_env(workspace, ["update-ref", "-d", ref])

    async def _create_checkpoint_locked(
        self,
        registry: Dict[str, Any],
        entry: Dict[str, Any],
        task_id: str,
        reason: str,
        expected_workspace_fingerprint: Optional[str] = None,
        kind: str = "manual",
    ) -> Dict[str, Any]:
        _, workspace = self.workspaces._entry_paths(entry)
        before = await self.workspaces.workspace_fingerprint(workspace)
        if expected_workspace_fingerprint is not None:
            expected = self._validate_fingerprint(
                expected_workspace_fingerprint, "expected_workspace_fingerprint"
            )
            if before["fingerprint"] != expected:
                raise ValueError(
                    "Recovery checkpoint rejected: workspace changed since it was "
                    f"inspected (expected {expected}, current "
                    f"{before['fingerprint']})."
                )
        checkpoint_id = uuid.uuid4().hex
        commit, ref, index_tree = await self._snapshot_commit(
            workspace, task_id, checkpoint_id, before["head"]
        )
        try:
            after = await self.workspaces.workspace_fingerprint(workspace)
            if after != before:
                raise ValueError(
                    "Workspace changed while the recovery checkpoint was being "
                    "created; checkpoint rejected."
                )
            cursor = await self.ticks.get_active_cursor()
            checkpoint = {
                "version": 1,
                "checkpoint_id": checkpoint_id,
                "task_id": task_id,
                "kind": kind,
                "reason": reason,
                "created_at": self.workspaces._utc_now(),
                "workspace": {
                    "head": before["head"],
                    "fingerprint": before["fingerprint"],
                    "snapshot_commit": commit,
                    "snapshot_ref": ref,
                    "index_tree": index_tree,
                },
                "task_plan": copy.deepcopy(entry.get("task_plan")),
                "tick_cursor": cursor,
                "trace": current_trace(),
            }
            checkpoints = entry.setdefault("recovery_checkpoints", {})
            checkpoints[checkpoint_id] = checkpoint
            order = entry.setdefault("recovery_checkpoint_order", [])
            order.append(checkpoint_id)
            entry["recovery_checkpoint_order"] = order[-self._MAX_CHECKPOINTS :]
            # Keep manifests referenced by retained order. Git refs intentionally
            # remain durable even if an old manifest ages out.
            retained = set(entry["recovery_checkpoint_order"])
            entry["recovery_checkpoints"] = {
                key: value for key, value in checkpoints.items() if key in retained
            }
            return checkpoint
        except Exception:
            await self._delete_ref(workspace, ref)
            raise

    @staticmethod
    def _checkpoint_payload(checkpoint: Dict[str, Any]) -> Dict[str, Any]:
        workspace = checkpoint["workspace"]
        plan = checkpoint.get("task_plan")
        return {
            "checkpoint_id": checkpoint["checkpoint_id"],
            "task_id": checkpoint["task_id"],
            "kind": checkpoint["kind"],
            "reason": checkpoint["reason"],
            "created_at": checkpoint["created_at"],
            "workspace_head": workspace["head"],
            "workspace_fingerprint": workspace["fingerprint"],
            "plan_id": plan.get("plan_id") if isinstance(plan, dict) else None,
            "plan_revision": plan.get("revision") if isinstance(plan, dict) else None,
            "tick_cursor": checkpoint["tick_cursor"],
        }

    async def _restore_workspace(
        self, workspace: Path, checkpoint: Dict[str, Any]
    ) -> Dict[str, str]:
        snapshot = checkpoint["workspace"]
        head = self._validate_object_id(snapshot["head"], "workspace head")
        commit = self._validate_object_id(
            snapshot["snapshot_commit"], "snapshot commit"
        )
        index_tree = self._validate_object_id(snapshot["index_tree"], "index tree")
        expected = self._validate_fingerprint(
            snapshot["fingerprint"], "checkpoint workspace fingerprint"
        )
        commands = [
            ["reset", "--hard", head],
            ["clean", "-fd", "--", "."],
            ["restore", "--source", commit, "--worktree", "--", "."],
            ["read-tree", index_tree],
        ]
        for args in commands:
            code, out, err = await self._run_git_env(workspace, args)
            if code != 0:
                raise ValueError(
                    f"Recovery restore failed during {' '.join(args[:2])}: "
                    f"{err or out}"
                )
        actual = await self.workspaces.workspace_fingerprint(workspace)
        if actual["fingerprint"] != expected or actual["head"] != head:
            raise ValueError(
                "Recovery verification failed: restored workspace fingerprint "
                "does not match the checkpoint."
            )
        return actual

    @skill(swarm=[Subagents.CODER, Subagents.QA_ENGINEER, Subagents.SYSADMIN])
    @require_access(HostOSAccessLevel.SANDBOX)
    async def create_coding_recovery_checkpoint(
        self,
        task_id: str,
        reason: str,
        expected_workspace_fingerprint: Optional[str] = None,
    ) -> SkillResult:
        """Snapshot workspace, plan, and active tick cursor without changing them."""

        try:
            task_id = self.workspaces._validate_task_id(task_id)
            reason = self._bounded_reason(reason)
            async with self.workspaces._lock:
                registry = self.workspaces._load_registry()
                entry = self.workspaces._get_entry(registry, task_id)
                checkpoint = await self._create_checkpoint_locked(
                    registry,
                    entry,
                    task_id,
                    reason,
                    expected_workspace_fingerprint,
                )
                try:
                    self.workspaces._save_registry(registry)
                except Exception:
                    _, workspace = self.workspaces._entry_paths(entry)
                    await self._delete_ref(
                        workspace, checkpoint["workspace"]["snapshot_ref"]
                    )
                    raise
            return SkillResult.ok(
                json.dumps(self._checkpoint_payload(checkpoint), ensure_ascii=False)
            )
        except (PermissionError, FileNotFoundError, ValueError, KeyError) as exc:
            return SkillResult.fail(str(exc))
        except Exception as exc:
            return SkillResult.fail(f"Error creating coding recovery checkpoint: {exc}")

    @skill(swarm=[Subagents.CODER, Subagents.QA_ENGINEER, Subagents.SYSADMIN])
    @require_access(HostOSAccessLevel.SANDBOX)
    async def list_coding_recovery_checkpoints(
        self, task_id: str, limit: int = 20
    ) -> SkillResult:
        """List newest recovery checkpoints with bounded metadata only."""

        try:
            task_id = self.workspaces._validate_task_id(task_id)
            if limit < 1 or limit > 100:
                raise ValueError("limit must be between 1 and 100.")
            async with self.workspaces._lock:
                entry = self.workspaces._get_entry(
                    self.workspaces._load_registry(), task_id
                )
                checkpoints = entry.get("recovery_checkpoints", {})
                identifiers = entry.get("recovery_checkpoint_order", [])[-limit:]
                payload = [
                    self._checkpoint_payload(checkpoints[identifier])
                    for identifier in reversed(identifiers)
                    if identifier in checkpoints
                ]
            return SkillResult.ok(json.dumps(payload, ensure_ascii=False))
        except (PermissionError, FileNotFoundError, ValueError, KeyError) as exc:
            return SkillResult.fail(str(exc))
        except Exception as exc:
            return SkillResult.fail(f"Error listing recovery checkpoints: {exc}")

    @skill(swarm=[Subagents.CODER, Subagents.QA_ENGINEER, Subagents.SYSADMIN])
    @require_access(HostOSAccessLevel.SANDBOX)
    async def rewind_coding_recovery_checkpoint(
        self,
        task_id: str,
        checkpoint_id: str,
        expected_current_fingerprint: str,
        rewind_context: bool = True,
    ) -> SkillResult:
        """Guardedly rewind workspace and plan, optionally branching tick context.

        A forward-recovery checkpoint is persisted before mutation. The required
        current fingerprint prevents overwriting work created after inspection.
        """

        try:
            task_id = self.workspaces._validate_task_id(task_id)
            checkpoint_id = self._validate_checkpoint_id(checkpoint_id)
            expected = self._validate_fingerprint(
                expected_current_fingerprint, "expected_current_fingerprint"
            )
            async with self.workspaces._lock:
                registry = self.workspaces._load_registry()
                entry = self.workspaces._get_entry(registry, task_id)
                checkpoints = entry.get("recovery_checkpoints", {})
                target = copy.deepcopy(checkpoints.get(checkpoint_id))
                if target is None:
                    return SkillResult.fail(
                        f"Recovery checkpoint not found ({checkpoint_id})."
                    )
                if target.get("task_id") != task_id:
                    return SkillResult.fail("Recovery checkpoint task identity mismatch.")
                _, workspace = self.workspaces._entry_paths(entry)
                current = await self.workspaces.workspace_fingerprint(workspace)
                if current["fingerprint"] != expected:
                    return SkillResult.fail(
                        "Rewind rejected: workspace changed since it was inspected "
                        f"(expected {expected}, current {current['fingerprint']})."
                    )

                forward = await self._create_checkpoint_locked(
                    registry,
                    entry,
                    task_id,
                    f"Automatic forward recovery before rewind to {checkpoint_id}",
                    expected,
                    kind="automatic_forward",
                )
                try:
                    self.workspaces._save_registry(registry)
                except Exception:
                    await self._delete_ref(
                        workspace, forward["workspace"]["snapshot_ref"]
                    )
                    raise
                registry_with_forward = copy.deepcopy(registry)
                latest = await self.workspaces.workspace_fingerprint(workspace)
                if latest != {
                    "head": forward["workspace"]["head"],
                    "fingerprint": forward["workspace"]["fingerprint"],
                }:
                    return SkillResult.fail(
                        "Rewind aborted without mutation: workspace changed after "
                        "the automatic forward checkpoint was created. Forward "
                        f"checkpoint {forward['checkpoint_id']} was retained."
                    )
                try:
                    restored = await self._restore_workspace(workspace, target)
                    previous_plan = copy.deepcopy(entry.get("task_plan"))
                    target_plan = copy.deepcopy(target.get("task_plan"))
                    if target_plan is None:
                        entry.pop("task_plan", None)
                    else:
                        entry["task_plan"] = target_plan
                    history = entry.setdefault("recovery_history", [])
                    history.append(
                        {
                            "event": "rewound",
                            "checkpoint_id": checkpoint_id,
                            "forward_checkpoint_id": forward["checkpoint_id"],
                            "time": self.workspaces._utc_now(),
                            "previous_plan_id": previous_plan.get("plan_id")
                            if isinstance(previous_plan, dict)
                            else None,
                            "previous_plan_revision": previous_plan.get("revision")
                            if isinstance(previous_plan, dict)
                            else None,
                            "trace": current_trace(),
                        }
                    )
                    entry["recovery_history"] = history[-200:]
                    self.workspaces._save_registry(registry)

                    new_timeline_id = None
                    if rewind_context:
                        cursor = target["tick_cursor"]
                        new_timeline_id = await self.ticks.branch_from_cursor(
                            cursor["timeline_id"],
                            cursor.get("tick_id"),
                            f"coding rewind {task_id}:{checkpoint_id}",
                        )
                except Exception as rewind_error:
                    try:
                        await self._restore_workspace(workspace, forward)
                        self.workspaces._save_registry(registry_with_forward)
                    except Exception as compensation_error:
                        return SkillResult.fail(
                            "Rewind failed and automatic compensation also failed. "
                            f"Forward checkpoint {forward['checkpoint_id']} remains "
                            f"available. Rewind error: {rewind_error}; compensation "
                            f"error: {compensation_error}"
                        )
                    return SkillResult.fail(
                        f"Rewind failed and was compensated from forward checkpoint "
                        f"{forward['checkpoint_id']}: {rewind_error}"
                    )

                marker_id = None
                marker_warning = None
                try:
                    marker_id = await self.ticks.save_tick(
                        thoughts="[Coding task restored from recovery checkpoint]",
                        actions=[],
                        results={
                            "status": "coding_rewind",
                            "task_id": task_id,
                            "checkpoint_id": checkpoint_id,
                            "forward_checkpoint_id": forward["checkpoint_id"],
                            "rewind_context": rewind_context,
                            "timeline_id": new_timeline_id,
                            "trace": current_trace(),
                        },
                    )
                except Exception as marker_error:
                    # Workspace, plan, and timeline are already committed. A
                    # missing explanatory marker must not trigger a false rollback
                    # after the durable timeline switch succeeded.
                    marker_warning = f"Unable to append rewind marker: {marker_error}"

            message = (
                f"Rewound coding task '{task_id}' to checkpoint {checkpoint_id}; "
                f"forward recovery is {forward['checkpoint_id']}."
            )
            if self.agent_state is not None:
                self.agent_state.last_action_error = ""
                self.agent_state.last_actions_result = message
            return SkillResult.ok(
                json.dumps(
                    {
                        "task_id": task_id,
                        "checkpoint_id": checkpoint_id,
                        "forward_checkpoint_id": forward["checkpoint_id"],
                        "workspace": restored,
                        "rewind_context": rewind_context,
                        "timeline_id": new_timeline_id,
                        "marker_tick_id": marker_id,
                        "warning": marker_warning,
                    },
                    ensure_ascii=False,
                )
            )
        except (PermissionError, FileNotFoundError, ValueError, KeyError) as exc:
            return SkillResult.fail(str(exc))
        except Exception as exc:
            return SkillResult.fail(f"Error rewinding coding recovery checkpoint: {exc}")
