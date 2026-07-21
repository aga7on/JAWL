"""
Swarm Orchestrator (Swarm Manager).

Initializes background workers under strict asyncio Semaphore limits,
resolves active roles, and dynamically maps authorized skills directories.
"""

import asyncio
import json
import uuid
import traceback
from pathlib import Path
from typing import Optional

from src.utils.logger import swarm_logger
from src.utils.settings import SwarmConfig

from src.l3_agent.llm.executor import LLMExecutor
from src.l3_agent.skills.registry import skill, SkillResult, _REGISTRY
from src.l3_agent.hooks.lifecycle import HookContext, HookPhase, LifecycleHooks

from src.l3_agent.swarm.roles import Subagents, SubagentRole
from src.l3_agent.swarm.prompt.builder import SwarmPromptBuilder
from src.l3_agent.swarm.context.builder import SwarmContextBuilder
from src.l3_agent.swarm.loop import SubagentLoop
from src.l3_agent.swarm.registry import DelegationRegistry


class SwarmManager:
    """Manager of the swarm subsystem. Spawns and manages stateless background subagents."""

    def __init__(
        self,
        executor: LLMExecutor,
        swarm_config: SwarmConfig,
        root_dir: Path,
        hooks: LifecycleHooks = None,
        registry: Optional[DelegationRegistry] = None,
    ) -> None:
        self.executor = executor
        self.config = swarm_config
        self.hooks = hooks or LifecycleHooks()
        self.root_dir = Path(root_dir).resolve()

        self.registry_error = ""
        try:
            self.registry = registry or DelegationRegistry(
                self.root_dir
                / "sandbox"
                / "_system"
                / "subagents"
                / "delegations.json"
            )
        except (OSError, ValueError) as exc:
            self.registry = None
            self.registry_error = f"{type(exc).__name__}: {exc}"
            swarm_logger.error(
                f"[Swarm] Durable delegation registry unavailable: {self.registry_error}"
            )

        self.prompt_builder = SwarmPromptBuilder(self.root_dir)
        self.semaphore = asyncio.Semaphore(self.config.max_concurrent_workers)
        self.active_tasks: set[asyncio.Task] = set()
        self.tasks_by_id: dict[str, asyncio.Task] = {}

        self.role_skills: dict[str, list[str]] = {}
        self.active_roles: dict[str, SubagentRole] = {}

        for skill_name, data in _REGISTRY.items():
            for role_obj in data.get("swarm", []):
                if role_obj.id not in self.role_skills:
                    self.role_skills[role_obj.id] = []
                self.role_skills[role_obj.id].append(skill_name)
                self.active_roles[role_obj.id] = role_obj

        base_doc = "Delegates heavy tasks to background autonomous subagent, returns report. "

        if self.active_roles:
            roles_desc = ["Currently available subagent roles:"]
            for r_id, r_obj in self.active_roles.items():
                roles_desc.append(f"- '{r_id}' ({r_obj.name}): {r_obj.description}")
            roles_str = "\n".join(roles_desc)
        else:
            roles_str = "Warning: no subagent roles are active. Ensure required interfaces are enabled."

        if hasattr(self.spawn_subagent, "__func__"):
            self.spawn_subagent.__func__.__doc__ = f"{base_doc}\n\n{roles_str}"
        else:
            self.spawn_subagent.__doc__ = f"{base_doc}\n\n{roles_str}"

    @skill()
    async def spawn_subagent(self, role: str, task_description: str) -> SkillResult:
        """
        Spawns a background subagent worker for the delegated task.
        """

        if not self.config.enabled:
            return SkillResult.fail("Error: Swarm subsystem is disabled in the configuration.")

        if self.config.subagent_model == "unknown":
            return SkillResult.fail("Error: No subagent model specified in the configuration.")

        if self.registry is None:
            return SkillResult.fail(
                "Error: Durable delegation registry is unavailable; refusing "
                "untracked background work. "
                + self.registry_error
            )

        target_role = Subagents.get_by_id(role)
        if not target_role or target_role.id not in self.active_roles:
            active_ids = list(self.active_roles.keys())
            return SkillResult.fail(
                f"Role '{role}' is currently unavailable. Active roles: {active_ids}"
            )

        subagent_id = uuid.uuid4().hex[:8]

        hook_parameters = {
            "role": target_role.id,
            "subagent_id": subagent_id,
            "task_description": task_description,
        }
        try:
            pre_hooks = await self.hooks.run(
                HookContext(
                    phase=HookPhase.PRE_DELEGATION,
                    plan_id=f"delegation-{subagent_id}",
                    action_id=subagent_id,
                    tool_name="Swarm.delegate",
                    parameters=hook_parameters,
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            swarm_logger.error(f"[Swarm] Delegation lifecycle preflight failed: {exc}")
            if self.hooks.fail_closed:
                return SkillResult.fail(
                    "Delegation blocked because lifecycle preflight failed closed."
                )
        else:
            if not pre_hooks.decision.allowed:
                return SkillResult.fail(
                    "Delegation blocked by lifecycle hook: "
                    + pre_hooks.decision.reason
                )

        try:
            await asyncio.to_thread(
                self.registry.create,
                subagent_id,
                target_role.id,
                task_description,
            )
        except (OSError, ValueError) as exc:
            return SkillResult.fail(f"Could not persist delegation: {exc}")

        try:
            task = asyncio.create_task(
                self._run_subagent_task(subagent_id, target_role, task_description)
            )
        except Exception as exc:
            await self._transition(
                subagent_id, "failed", detail=f"Task creation failed: {type(exc).__name__}"
            )
            return SkillResult.fail(f"Could not start delegated worker: {exc}")
        self.active_tasks.add(task)
        self.tasks_by_id[subagent_id] = task

        def forget(completed: asyncio.Task) -> None:
            self.active_tasks.discard(completed)
            if self.tasks_by_id.get(subagent_id) is completed:
                self.tasks_by_id.pop(subagent_id, None)

        task.add_done_callback(forget)

        return SkillResult.ok(
            f"Subagent {role}_{subagent_id} successfully spawned in the background. You will receive a notification upon completion."
        )

    @skill()
    async def list_delegations(self, status: str = "", limit: int = 20) -> SkillResult:
        """Lists durable delegated-work state without exposing raw task prompts."""

        if self.registry is None:
            return SkillResult.fail("Durable delegation registry is unavailable.")
        try:
            records = await asyncio.to_thread(self.registry.list, limit, status)
        except (OSError, ValueError) as exc:
            return SkillResult.fail(f"Could not inspect delegations: {exc}")
        return SkillResult.ok(json.dumps(records, ensure_ascii=False, indent=2))

    @skill()
    async def cancel_delegation(self, delegation_id: str) -> SkillResult:
        """Requests cancellation of an active local delegated worker by exact ID."""

        if self.registry is None:
            return SkillResult.fail("Durable delegation registry is unavailable.")
        try:
            record = await asyncio.to_thread(self.registry.get, delegation_id)
        except (OSError, ValueError) as exc:
            return SkillResult.fail(f"Could not resolve delegation: {exc}")
        if record["status"] not in {"queued", "running"}:
            return SkillResult.fail(
                f"Delegation {delegation_id} is already {record['status']}."
            )
        task = self.tasks_by_id.get(record["id"])
        if task is None or task.done():
            await self._transition(
                record["id"],
                "failed",
                detail="Active task handle was unavailable in the current session.",
            )
            return SkillResult.fail(
                f"Delegation {delegation_id} has no active task handle."
            )
        task.cancel()
        return SkillResult.ok(f"Cancellation requested for delegation {record['id']}.")

    async def start(self) -> None:
        """Lifecycle component compatibility; recovery happens in the constructor."""

    async def stop(self) -> None:
        """Cancel and await all workers before LLM clients and EventBus close."""

        tasks = list(self.active_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_subagent_task(
        self, subagent_id: str, role: SubagentRole, task_description: str
    ) -> None:
        """
        Background task thread. Runs the subagent ReAct loop under semaphore limits.
        """

        running_record_task: Optional[asyncio.Task] = None
        try:
            actual_skills = self.role_skills.get(role.id, [])

            async with self.semaphore:
                running_record_task = asyncio.create_task(
                    self._transition(subagent_id, "running")
                )
                context_builder = SwarmContextBuilder(
                    role=role, allowed_skills=actual_skills, config=self.config.context_depth
                )

                loop = SubagentLoop(
                    subagent_id=subagent_id,
                    role=role,
                    task_description=task_description,
                    executor=self.executor,
                    model_name=self.config.subagent_model,
                    prompt_builder=self.prompt_builder,
                    context_builder=context_builder,
                    allowed_skills=actual_skills,
                    max_steps=self.config.context_depth.max_steps,
                )

                result = await loop.run()
                await running_record_task
            if result == "failed":
                await self._transition(
                    subagent_id, "failed", detail="Worker returned an incomplete report."
                )
                await self._observe_delegation(
                    HookPhase.DELEGATION_ERROR,
                    subagent_id,
                    role,
                    task_description,
                    outcome={"is_success": False},
                )
            else:
                report_path = (
                    f"sandbox/_system/subagents/{role.id}_{subagent_id}.md"
                )
                persisted_report = (
                    report_path if (self.root_dir / report_path).is_file() else ""
                )
                await self._transition(
                    subagent_id, "completed", report_path=persisted_report
                )
                await self._observe_delegation(
                    HookPhase.POST_DELEGATION,
                    subagent_id,
                    role,
                    task_description,
                    outcome={"is_success": True},
                )
        except asyncio.CancelledError:
            if running_record_task is not None:
                await asyncio.shield(running_record_task)
            await asyncio.shield(self._transition(subagent_id, "cancelled"))
            await asyncio.shield(
                self._observe_delegation(
                    HookPhase.DELEGATION_CANCELLED,
                    subagent_id,
                    role,
                    task_description,
                    outcome={"is_success": False},
                )
            )
            raise
        except Exception:
            log = f"[Swarm] Critical exception in background subagent task {role.id}_{subagent_id}:\n{traceback.format_exc()}"
            swarm_logger.error(log)
            if running_record_task is not None:
                await running_record_task
            await self._transition(
                subagent_id,
                "failed",
                detail="Worker raised an internal exception.",
            )
            await self._observe_delegation(
                HookPhase.DELEGATION_ERROR,
                subagent_id,
                role,
                task_description,
                outcome={"is_success": False},
            )

    async def _observe_delegation(
        self,
        phase: HookPhase,
        subagent_id: str,
        role: SubagentRole,
        task_description: str,
        *,
        outcome: dict,
    ) -> None:
        """Emit terminal delegation evidence without altering worker outcomes."""

        try:
            await self.hooks.run(
                HookContext(
                    phase=phase,
                    plan_id=f"delegation-{subagent_id}",
                    action_id=subagent_id,
                    tool_name="Swarm.delegate",
                    parameters={
                        "role": role.id,
                        "subagent_id": subagent_id,
                        "task_description": task_description,
                    },
                    outcome=outcome,
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            swarm_logger.error(f"[Swarm] Delegation lifecycle observer failed: {exc}")

    async def _transition(
        self,
        subagent_id: str,
        status: str,
        *,
        detail: str = "",
        report_path: str = "",
    ) -> None:
        if self.registry is None:
            return
        try:
            await asyncio.to_thread(
                self.registry.transition,
                subagent_id,
                status,
                detail=detail,
                report_path=report_path,
            )
        except (OSError, ValueError) as exc:
            swarm_logger.error(
                f"[Swarm] Could not persist delegation {subagent_id} -> {status}: {exc}"
            )
