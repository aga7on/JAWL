"""Deterministic execution engine for agent action plans.

The engine keeps the legacy ``ActionCall`` contract usable while making action
ordering explicit and safe. Actions execute sequentially by default. The model
may opt independent actions into a named parallel group and may declare
dependencies between actions.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional

from src.l3_agent.skills.schema import ActionCall
from src.l3_agent.skills.journal import ActionJournal, NullActionJournal
from src.l3_agent.hooks.lifecycle import HookContext, HookPhase, LifecycleHooks
from src.utils.logger import agent_logger
from src.utils.tracing import current_trace


ActionRunner = Callable[[ActionCall], Awaitable[Any]]


@dataclass(frozen=True)
class PlannedAction:
    """Normalized action with a stable identifier and source order."""

    index: int
    action_id: str
    call: ActionCall


@dataclass(frozen=True)
class ActionOutcome:
    """Execution result independent from the concrete skill result class."""

    index: int
    action_id: str
    tool_name: str
    is_success: bool
    message: str
    duration_ms: float = 0.0


class ActionExecutionEngine:
    """Executes dependency-aware action plans with bounded parallelism.

    Safety rules:
    - actions without ``parallel_group`` run one at a time in source order;
    - only ready actions from the same explicit group may run concurrently;
    - failed dependencies skip their dependants;
    - actions touching the same inferred or explicit resource are serialized;
    - cancellation is propagated to every running child task.
    """

    _PATH_PARAMETER_NAMES = {
        "cwd",
        "destination",
        "destination_path",
        "directory",
        "directory_path",
        "file",
        "filepath",
        "folder",
        "path",
        "project_dir",
        "project_path",
        "repo_path",
        "repository_path",
        "root_dir",
        "source",
        "source_path",
        "target_path",
        "workdir",
        "workspace",
    }

    def __init__(
        self,
        max_parallel_actions: int = 4,
        journal: Optional[ActionJournal] = None,
        hooks: Optional[LifecycleHooks] = None,
    ) -> None:
        if max_parallel_actions < 1:
            raise ValueError("max_parallel_actions must be at least 1")
        self.max_parallel_actions = max_parallel_actions
        self.journal: ActionJournal | NullActionJournal = (
            journal or NullActionJournal()
        )
        self.hooks = hooks or LifecycleHooks()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._resource_locks: Dict[str, asyncio.Lock] = {}

    async def execute(
        self, actions: List[ActionCall], runner: ActionRunner
    ) -> List[ActionOutcome]:
        """Execute an action plan and return outcomes in original order."""

        if not actions:
            return []

        self._ensure_loop_state()
        plans = self._normalize(actions)
        plan_id = uuid.uuid4().hex
        await self._safe_record(
            "plan_started",
            plan_id=plan_id,
            task_ids=sorted(
                {
                    str(plan.call.parameters["task_id"])
                    for plan in plans
                    if plan.call.parameters.get("task_id") is not None
                }
            ),
            actions=[
                {
                    "index": plan.index,
                    "action_id": plan.action_id,
                    "tool_name": plan.call.tool_name,
                    "parameters": plan.call.parameters,
                    "depends_on": plan.call.depends_on,
                    "parallel_group": plan.call.parallel_group,
                    "resources": plan.call.resources,
                }
                for plan in plans
            ],
        )
        try:
            outcomes = await self._execute_plans(plan_id, plans, runner)
        except asyncio.CancelledError:
            await asyncio.shield(
                self._safe_record(
                    "plan_finished",
                    plan_id=plan_id,
                    state="cancelled",
                    outcomes=[],
                )
            )
            raise
        except Exception as exc:
            await asyncio.shield(
                self._safe_record(
                    "plan_finished",
                    plan_id=plan_id,
                    state="error",
                    error=str(exc),
                    outcomes=[],
                )
            )
            raise

        state = "completed" if all(item.is_success for item in outcomes) else "failed"
        await self._safe_record(
            "plan_finished",
            plan_id=plan_id,
            state=state,
            outcomes=[self._outcome_payload(item) for item in outcomes],
        )
        return outcomes

    async def _execute_plans(
        self, plan_id: str, plans: List[PlannedAction], runner: ActionRunner
    ) -> List[ActionOutcome]:
        outcomes: Dict[int, ActionOutcome] = {}
        pending: Dict[int, PlannedAction] = {plan.index: plan for plan in plans}

        explicit_ids: Dict[str, List[PlannedAction]] = {}
        for plan in plans:
            if plan.call.action_id:
                explicit_ids.setdefault(plan.call.action_id, []).append(plan)

        duplicate_ids = {key for key, values in explicit_ids.items() if len(values) > 1}
        for plan in plans:
            if plan.call.action_id in duplicate_ids:
                outcomes[plan.index] = self._failure(
                    plan, f"Invalid action plan: duplicate action_id '{plan.call.action_id}'."
                )
                pending.pop(plan.index, None)

        while pending:
            completed_by_id = {
                plans[index].action_id: outcome for index, outcome in outcomes.items()
            }
            declared_ids = {plan.action_id for plan in plans}
            made_progress = False

            for index, plan in list(pending.items()):
                missing = [dep for dep in plan.call.depends_on if dep not in declared_ids]
                if missing:
                    outcomes[index] = self._failure(
                        plan,
                        "Invalid action plan: missing dependencies " + ", ".join(missing) + ".",
                    )
                    pending.pop(index)
                    made_progress = True
                    continue

                failed = [
                    dep
                    for dep in plan.call.depends_on
                    if dep in completed_by_id and not completed_by_id[dep].is_success
                ]
                if failed:
                    outcomes[index] = self._failure(
                        plan, "Skipped: failed dependencies " + ", ".join(failed) + "."
                    )
                    pending.pop(index)
                    made_progress = True

            if not pending:
                break

            completed_by_id = {
                plans[index].action_id: outcome for index, outcome in outcomes.items()
            }
            ready = [
                plan
                for plan in pending.values()
                if all(dep in completed_by_id for dep in plan.call.depends_on)
            ]
            ready.sort(key=lambda plan: plan.index)

            if not ready:
                for plan in pending.values():
                    outcomes[plan.index] = self._failure(
                        plan, "Invalid action plan: cyclic or unresolved dependencies."
                    )
                pending.clear()
                break

            first = ready[0]
            if first.call.parallel_group:
                batch = [
                    plan
                    for plan in ready
                    if plan.call.parallel_group == first.call.parallel_group
                ]
            else:
                batch = [first]

            batch_outcomes = await self._run_batch(plan_id, batch, runner)
            for outcome in batch_outcomes:
                outcomes[outcome.index] = outcome
                pending.pop(outcome.index, None)
            made_progress = True

            if not made_progress:
                raise RuntimeError("Action execution engine made no progress")

        return [outcomes[index] for index in sorted(outcomes)]

    def set_journal(self, journal: ActionJournal) -> None:
        """Attach persistent journaling after the system paths are configured."""

        self.journal = journal

    def set_hooks(self, hooks: LifecycleHooks) -> None:
        """Attach the runtime lifecycle policy/observation layer."""

        self.hooks = hooks

    async def _safe_record(self, event: str, **payload: Any) -> None:
        try:
            payload.setdefault("trace", current_trace())
            await self.journal.record(event, **payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Observability must never prevent physical actions from completing.
            agent_logger.warning(f"[Action Journal] Unable to persist event: {exc}")
            return

    @staticmethod
    def _outcome_payload(outcome: ActionOutcome) -> Dict[str, Any]:
        return {
            "index": outcome.index,
            "action_id": outcome.action_id,
            "tool_name": outcome.tool_name,
            "is_success": outcome.is_success,
            "message": outcome.message,
            "duration_ms": outcome.duration_ms,
        }

    def _ensure_loop_state(self) -> None:
        """Keep asyncio primitives scoped to their running event loop."""

        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            self._loop = loop
            self._semaphore = asyncio.Semaphore(self.max_parallel_actions)
            self._resource_locks = {}

    @staticmethod
    def _normalize(actions: Iterable[ActionCall]) -> List[PlannedAction]:
        plans = []
        for index, action in enumerate(actions):
            action_id = action.action_id or f"action_{index + 1}"
            plans.append(PlannedAction(index=index, action_id=action_id, call=action))
        return plans

    async def _run_batch(
        self, plan_id: str, batch: List[PlannedAction], runner: ActionRunner
    ) -> List[ActionOutcome]:
        tasks = [
            asyncio.create_task(self._run_one(plan_id, plan, runner)) for plan in batch
        ]
        try:
            return await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def _run_one(
        self, plan_id: str, plan: PlannedAction, runner: ActionRunner
    ) -> ActionOutcome:
        assert self._semaphore is not None
        resource_keys = self._resource_keys(plan.call)
        locks = [self._resource_locks.setdefault(key, asyncio.Lock()) for key in resource_keys]

        async with self._semaphore:
            for lock in locks:
                await lock.acquire()
            try:
                hook_context = HookContext(
                    phase=HookPhase.PRE_TOOL_USE,
                    plan_id=plan_id,
                    action_id=plan.action_id,
                    tool_name=plan.call.tool_name,
                    parameters=dict(plan.call.parameters),
                )
                pre_hooks = await self.hooks.run(hook_context)
                if pre_hooks.failures:
                    await self._safe_record(
                        "action_hook_failed",
                        plan_id=plan_id,
                        action_id=plan.action_id,
                        tool_name=plan.call.tool_name,
                        phase=HookPhase.PRE_TOOL_USE.value,
                        failures=list(pre_hooks.failures),
                    )
                if not pre_hooks.decision.allowed:
                    outcome = self._failure(
                        plan,
                        "Blocked by lifecycle hook: " + pre_hooks.decision.reason,
                    )
                    await self._safe_record(
                        "action_blocked",
                        plan_id=plan_id,
                        **self._outcome_payload(outcome),
                    )
                    return outcome
                await self._safe_record(
                    "action_started",
                    plan_id=plan_id,
                    action_id=plan.action_id,
                    index=plan.index,
                    tool_name=plan.call.tool_name,
                    parameters=plan.call.parameters,
                    resources=resource_keys,
                    task_id=plan.call.parameters.get("task_id"),
                )
                started = time.perf_counter()
                try:
                    result = await runner(plan.call)
                    outcome = ActionOutcome(
                        index=plan.index,
                        action_id=plan.action_id,
                        tool_name=plan.call.tool_name,
                        is_success=bool(getattr(result, "is_success", False)),
                        message=str(getattr(result, "message", result)),
                        duration_ms=round(
                            (time.perf_counter() - started) * 1000, 1
                        ),
                    )
                    result_phase = (
                        HookPhase.POST_TOOL_USE
                        if outcome.is_success
                        else HookPhase.TOOL_ERROR
                    )
                    post_hooks = await self.hooks.run(
                        HookContext(
                            phase=result_phase,
                            plan_id=plan_id,
                            action_id=plan.action_id,
                            tool_name=plan.call.tool_name,
                            parameters=dict(plan.call.parameters),
                            outcome=self._outcome_payload(outcome),
                        )
                    )
                    if post_hooks.failures:
                        await self._safe_record(
                            "action_hook_failed",
                            plan_id=plan_id,
                            action_id=plan.action_id,
                            tool_name=plan.call.tool_name,
                            phase=result_phase.value,
                            failures=list(post_hooks.failures),
                        )
                    await self._safe_record(
                        "action_finished",
                        plan_id=plan_id,
                        **self._outcome_payload(outcome),
                    )
                    return outcome
                except asyncio.CancelledError:
                    await asyncio.shield(
                        self.hooks.run(
                            HookContext(
                                phase=HookPhase.TOOL_CANCELLED,
                                plan_id=plan_id,
                                action_id=plan.action_id,
                                tool_name=plan.call.tool_name,
                                parameters=dict(plan.call.parameters),
                            )
                        )
                    )
                    await asyncio.shield(
                        self._safe_record(
                            "action_cancelled",
                            plan_id=plan_id,
                            action_id=plan.action_id,
                            index=plan.index,
                            tool_name=plan.call.tool_name,
                            duration_ms=round(
                                (time.perf_counter() - started) * 1000, 1
                            ),
                        )
                    )
                    raise
                except Exception as exc:
                    outcome = self._failure(
                        plan,
                        f"Internal action execution error: {exc}",
                        duration_ms=round(
                            (time.perf_counter() - started) * 1000, 1
                        ),
                    )
                    error_hooks = await self.hooks.run(
                        HookContext(
                            phase=HookPhase.TOOL_ERROR,
                            plan_id=plan_id,
                            action_id=plan.action_id,
                            tool_name=plan.call.tool_name,
                            parameters=dict(plan.call.parameters),
                            outcome=self._outcome_payload(outcome),
                        )
                    )
                    if error_hooks.failures:
                        await self._safe_record(
                            "action_hook_failed",
                            plan_id=plan_id,
                            action_id=plan.action_id,
                            tool_name=plan.call.tool_name,
                            phase=HookPhase.TOOL_ERROR.value,
                            failures=list(error_hooks.failures),
                        )
                    await self._safe_record(
                        "action_finished",
                        plan_id=plan_id,
                        **self._outcome_payload(outcome),
                    )
                    return outcome
            finally:
                for lock in reversed(locks):
                    lock.release()

    def _resource_keys(self, action: ActionCall) -> List[str]:
        keys = {f"explicit:{resource}" for resource in action.resources if resource}

        for name, value in action.parameters.items():
            if name.lower() not in self._PATH_PARAMETER_NAMES:
                continue
            values = value if isinstance(value, list) else [value]
            for item in values:
                if not isinstance(item, (str, Path)) or not str(item).strip():
                    continue
                normalized = os.path.normcase(
                    os.path.abspath(os.path.expanduser(str(item).strip()))
                )
                keys.add(f"path:{normalized}")

        return sorted(keys)

    @staticmethod
    def _failure(
        plan: PlannedAction, message: str, duration_ms: float = 0.0
    ) -> ActionOutcome:
        return ActionOutcome(
            index=plan.index,
            action_id=plan.action_id,
            tool_name=plan.call.tool_name,
            is_success=False,
            message=message,
            duration_ms=duration_ms,
        )
