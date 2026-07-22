"""
Swarm Orchestrator (Swarm Manager).

Initializes background workers under strict asyncio Semaphore limits,
resolves active roles, and dynamically maps authorized skills directories.
"""

import asyncio
import json
import uuid
import traceback
import time
from pathlib import Path
from typing import Any, Optional

from src.utils.logger import swarm_logger
from src.utils.settings import SwarmConfig
from src.utils.event.bus import EventBus
from src.utils.event.registry import Events

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
        coding_plans: Optional[Any] = None,
        event_bus: Optional[EventBus] = None,
    ) -> None:
        self.executor = executor
        self.config = swarm_config
        self.hooks = hooks or LifecycleHooks()
        self.root_dir = Path(root_dir).resolve()
        self.coding_plans = coding_plans
        self.event_bus = event_bus

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
        self.control_messages: dict[str, list[str]] = {}

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
    async def spawn_subagent(
        self,
        role: str,
        task_description: str,
        parent_task_id: str = "",
        parent_step_id: str = "",
        expected_plan_revision: Optional[int] = None,
    ) -> SkillResult:
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

        parent_requested = bool(
            parent_task_id or parent_step_id or expected_plan_revision is not None
        )
        parent: Optional[dict[str, Any]] = None
        if parent_requested:
            if (
                not parent_task_id
                or not parent_step_id
                or expected_plan_revision is None
            ):
                return SkillResult.fail(
                    "Parent binding requires parent_task_id, parent_step_id, and "
                    "expected_plan_revision together."
                )
            if self.coding_plans is None:
                return SkillResult.fail(
                    "Parent coding-plan binding is unavailable because the Host OS "
                    "coding plan service is disabled."
                )
            parent = {
                "task_id": parent_task_id,
                "step_id": parent_step_id,
                "expected_revision": expected_plan_revision,
            }

        subagent_id = uuid.uuid4().hex[:8]

        hook_parameters = {
            "role": target_role.id,
            "subagent_id": subagent_id,
            "task_description": task_description,
            **({"parent": parent} if parent else {}),
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
                parent,
            )
        except (OSError, ValueError) as exc:
            return SkillResult.fail(f"Could not persist delegation: {exc}")

        if parent is not None:
            try:
                binding = await self.coding_plans.bind_delegation(
                    task_id=parent["task_id"],
                    step_id=parent["step_id"],
                    delegation_id=subagent_id,
                    role=target_role.id,
                    expected_revision=parent["expected_revision"],
                )
                parent = {**parent, **binding}
            except (OSError, PermissionError, FileNotFoundError, ValueError, KeyError) as exc:
                await self._transition(
                    subagent_id,
                    "failed",
                    detail=f"Parent plan binding failed: {exc}",
                )
                return SkillResult.fail(f"Could not bind parent coding plan: {exc}")

        effective_task = task_description
        if parent is not None:
            effective_task = (
                "[BOUND CODING PLAN]\n"
                f"task_id: {parent['task_id']}\n"
                f"step_id: {parent['step_id']}\n"
                "Work only through task-scoped coding skills for this task.\n\n"
                + task_description
            )

        try:
            task = asyncio.create_task(
                self._run_subagent_task(
                    subagent_id,
                    target_role,
                    effective_task,
                    parent=parent,
                )
            )
        except Exception as exc:
            await self._transition(
                subagent_id, "failed", detail=f"Task creation failed: {type(exc).__name__}"
            )
            await self._record_parent_result(
                parent,
                subagent_id,
                "failed",
                detail="Worker task creation failed.",
            )
            return SkillResult.fail(f"Could not start delegated worker: {exc}")
        self.active_tasks.add(task)
        self.tasks_by_id[subagent_id] = task
        self.control_messages[subagent_id] = []

        def forget(completed: asyncio.Task) -> None:
            self.active_tasks.discard(completed)
            if self.tasks_by_id.get(subagent_id) is completed:
                self.tasks_by_id.pop(subagent_id, None)
            self.control_messages.pop(subagent_id, None)

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
    async def wait_for_delegation(
        self, delegation_id: str, timeout_seconds: int = 30
    ) -> SkillResult:
        """Waits at most 60 seconds for one exact delegation to become terminal."""

        if self.registry is None:
            return SkillResult.fail("Durable delegation registry is unavailable.")
        if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool):
            return SkillResult.fail("timeout_seconds must be an integer between 0 and 60.")
        if timeout_seconds < 0 or timeout_seconds > 60:
            return SkillResult.fail("timeout_seconds must be between 0 and 60.")

        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                record = await asyncio.to_thread(self.registry.get, delegation_id)
            except (OSError, ValueError) as exc:
                return SkillResult.fail(f"Could not resolve delegation: {exc}")
            if record.get("status") in {"completed", "failed", "cancelled", "interrupted"}:
                return SkillResult.ok(json.dumps(record, ensure_ascii=False, indent=2))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return SkillResult.ok(
                    json.dumps(
                        {**record, "wait_timed_out": True},
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            await asyncio.sleep(min(0.25, remaining))

    @skill()
    async def send_delegation_message(
        self, delegation_id: str, message: str
    ) -> SkillResult:
        """Queues bounded steering for an active worker's next safe step boundary."""

        if self.registry is None:
            return SkillResult.fail("Durable delegation registry is unavailable.")
        normalized = str(message).strip()
        if not normalized:
            return SkillResult.fail("Control message cannot be empty.")
        if len(normalized) > 4000:
            return SkillResult.fail("Control message exceeds 4000 characters.")
        try:
            record = await asyncio.to_thread(self.registry.get, delegation_id)
        except (OSError, ValueError) as exc:
            return SkillResult.fail(f"Could not resolve delegation: {exc}")
        if record.get("status") not in {"queued", "running"}:
            return SkillResult.fail(
                f"Delegation {delegation_id} is already {record.get('status')}."
            )
        task = self.tasks_by_id.get(record["id"])
        inbox = self.control_messages.get(record["id"])
        if task is None or task.done() or inbox is None:
            return SkillResult.fail(
                f"Delegation {delegation_id} has no active local worker handle."
            )
        if len(inbox) >= 20:
            return SkillResult.fail("Worker control inbox is full (20 messages).")
        inbox.append(normalized)
        return SkillResult.ok(
            f"Control message queued for delegation {record['id']} at its next step boundary."
        )

    @skill()
    async def get_delegation_report(
        self, delegation_id: str, max_chars: int = 20000
    ) -> SkillResult:
        """Reads a completed worker report through its exact durable registry record."""

        if self.registry is None:
            return SkillResult.fail("Durable delegation registry is unavailable.")
        if not isinstance(max_chars, int) or isinstance(max_chars, bool):
            return SkillResult.fail("max_chars must be an integer between 1 and 50000.")
        if max_chars < 1 or max_chars > 50000:
            return SkillResult.fail("max_chars must be between 1 and 50000.")
        try:
            record = await asyncio.to_thread(self.registry.get, delegation_id)
        except (OSError, ValueError) as exc:
            return SkillResult.fail(f"Could not resolve delegation: {exc}")
        relative = str(record.get("report_path") or "").strip()
        if record.get("status") != "completed" or not relative:
            return SkillResult.fail(
                f"Delegation {delegation_id} has no completed report (status={record.get('status')})."
            )
        reports_root = (
            self.root_dir / "sandbox" / "_system" / "subagents"
        ).resolve()
        report_path = (self.root_dir / relative).resolve()
        if not report_path.is_relative_to(reports_root) or not report_path.is_file():
            return SkillResult.fail("Delegation report path is missing or outside protected storage.")
        content = await asyncio.to_thread(report_path.read_text, encoding="utf-8", errors="replace")
        truncated = len(content) > max_chars
        payload = {
            "delegation_id": record["id"],
            "status": record["status"],
            "report_path": relative,
            "report": content[:max_chars],
            "truncated": truncated,
        }
        return SkillResult.ok(json.dumps(payload, ensure_ascii=False, indent=2))

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
        """Reconcile interrupted prior-session workers into their parent plans."""

        if self.registry is None or self.coding_plans is None:
            return
        while True:
            try:
                records = await asyncio.to_thread(
                    self.registry.unreconciled_interruptions, 100
                )
            except (OSError, ValueError) as exc:
                swarm_logger.error(
                    f"[Swarm] Could not inspect interrupted delegations: {exc}"
                )
                return
            if not records:
                return
            for record in records:
                parent = record.get("parent")
                detail = "Interrupted delegated worker recovered after process restart."
                try:
                    await self.coding_plans.record_delegation_result(
                        task_id=parent["task_id"],
                        step_id=parent["step_id"],
                        delegation_id=record["id"],
                        status="interrupted",
                        detail=detail,
                    )
                    reconciliation = "Parent plan marked interrupted."
                except (
                    OSError,
                    PermissionError,
                    FileNotFoundError,
                    ValueError,
                    KeyError,
                ) as exc:
                    reconciliation = f"Parent reconciliation failed: {exc}"
                    swarm_logger.error(
                        f"[Swarm] {reconciliation} ({record['id']})"
                    )
                try:
                    await asyncio.to_thread(
                        self.registry.mark_parent_reconciled,
                        record["id"],
                        reconciliation,
                    )
                except (OSError, ValueError) as exc:
                    swarm_logger.error(
                        f"[Swarm] Could not persist parent reconciliation marker: {exc}"
                    )
                    return

    async def stop(self) -> None:
        """Cancel and await all workers before LLM clients and EventBus close."""

        tasks = list(self.active_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_subagent_task(
        self,
        subagent_id: str,
        role: SubagentRole,
        task_description: str,
        *,
        parent: Optional[dict[str, Any]] = None,
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
                    control_message_provider=lambda: self._drain_control_messages(
                        subagent_id
                    ),
                )

                result = await loop.run()
                await running_record_task
            if result == "failed":
                await self._transition(
                    subagent_id, "failed", detail="Worker returned an incomplete report."
                )
                parent_reconciled = await self._record_parent_result(
                    parent,
                    subagent_id,
                    "failed",
                    detail="Worker returned an incomplete report.",
                )
                await self._publish_terminal(
                    subagent_id,
                    role,
                    "failed",
                    parent,
                    parent_reconciled,
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
                parent_reconciled = await self._record_parent_result(
                    parent,
                    subagent_id,
                    "completed",
                    report_path=persisted_report,
                )
                await self._publish_terminal(
                    subagent_id,
                    role,
                    "completed",
                    parent,
                    parent_reconciled,
                    report_path=persisted_report,
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
            parent_reconciled = await asyncio.shield(
                self._record_parent_result(
                    parent,
                    subagent_id,
                    "cancelled",
                    detail="Delegated worker was cancelled.",
                )
            )
            await asyncio.shield(
                self._publish_terminal(
                    subagent_id,
                    role,
                    "cancelled",
                    parent,
                    parent_reconciled,
                )
            )
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
            parent_reconciled = await self._record_parent_result(
                parent,
                subagent_id,
                "failed",
                detail="Worker raised an internal exception.",
            )
            await self._publish_terminal(
                subagent_id,
                role,
                "failed",
                parent,
                parent_reconciled,
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

    async def _record_parent_result(
        self,
        parent: Optional[dict[str, Any]],
        subagent_id: str,
        status: str,
        *,
        detail: str = "",
        report_path: str = "",
    ) -> bool:
        if parent is None or self.coding_plans is None:
            return parent is None
        try:
            await self.coding_plans.record_delegation_result(
                task_id=parent["task_id"],
                step_id=parent["step_id"],
                delegation_id=subagent_id,
                status=status,
                detail=detail,
                report_path=report_path,
            )
            return True
        except (OSError, PermissionError, FileNotFoundError, ValueError, KeyError) as exc:
            swarm_logger.error(
                f"[Swarm] Could not reconcile delegation {subagent_id} into "
                f"parent plan: {exc}"
            )
            return False

    async def _publish_terminal(
        self,
        subagent_id: str,
        role: SubagentRole,
        status: str,
        parent: Optional[dict[str, Any]],
        parent_reconciled: bool,
        *,
        report_path: str = "",
    ) -> None:
        if self.event_bus is None:
            return
        event = (
            Events.SUBAGENT_TASK_COMPLETED
            if status == "completed"
            else Events.SUBAGENT_TASK_FAILED
        )
        payload = {
            "subagent_id": subagent_id,
            "role": role.id,
            "status": status,
            "parent_reconciled": parent_reconciled,
            **({"report_path": report_path} if report_path else {}),
            **(
                {
                    "parent_task_id": parent["task_id"],
                    "parent_step_id": parent["step_id"],
                }
                if parent
                else {}
            ),
            "message": (
                f"Subagent [{role.id}_{subagent_id}] finished with status "
                f"'{status}'."
            ),
        }
        try:
            await self.event_bus.publish(event, **payload)
        except Exception as exc:
            swarm_logger.error(
                f"[Swarm] Could not publish terminal worker event: {exc}"
            )

    def _drain_control_messages(self, subagent_id: str) -> list[str]:
        """Return and clear steering queued for one current-process worker."""

        inbox = self.control_messages.get(subagent_id)
        if not inbox:
            return []
        messages = list(inbox)
        inbox.clear()
        return messages
