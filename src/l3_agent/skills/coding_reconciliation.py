"""Inspect-first startup reconciliation for interrupted coding actions."""

from __future__ import annotations

from typing import Any, Dict, List

from src.l2_interfaces.host.os.skills.coding_plans import HostOSCodingPlans
from src.l3_agent.skills.journal import ActionJournal
from src.utils.event.bus import EventBus
from src.utils.event.registry import Events
from src.utils.logger import main_logger


class CodingActionReconciliation:
    """Persist uncertain action state and wake the agent without replaying tools."""

    _MAX_INTERRUPTED = 200
    _MAX_EVENT_ITEMS = 50

    def __init__(
        self,
        journal: ActionJournal,
        coding_plans: HostOSCodingPlans,
        event_bus: EventBus,
    ) -> None:
        self.journal = journal
        self.coding_plans = coding_plans
        self.bus = event_bus

    @staticmethod
    def _relevant_actions(
        actions: List[Dict[str, Any]], task_id: str
    ) -> List[Dict[str, Any]]:
        return [
            {
                "action_id": str(item.get("action_id") or "")[:100],
                "tool_name": str(item.get("tool_name") or "unknown")[:200],
            }
            for item in actions
            if isinstance(item, dict)
            and (
                not item.get("task_id")
                or str(item.get("task_id")) == task_id
            )
        ][:100]

    async def start(self) -> None:
        interrupted = await self.journal.recent_plans(
            limit=self._MAX_INTERRUPTED,
            state="interrupted",
            include_events=False,
        )
        recoveries: List[Dict[str, Any]] = []
        # Journal queries are newest-first. Apply oldest-first so the workspace's
        # ``last_action_recovery`` remains the chronologically latest record.
        for action_plan in reversed(interrupted):
            task_ids = list(dict.fromkeys(action_plan.get("task_ids", [])))
            if not task_ids:
                continue
            plan_results = []
            complete = True
            uncertain = action_plan.get("uncertain_actions", [])
            for task_id in task_ids[:50]:
                try:
                    result = await self.coding_plans.record_startup_action_recovery(
                        task_id=str(task_id),
                        action_plan_id=str(action_plan["plan_id"]),
                        uncertain_actions=self._relevant_actions(
                            uncertain, str(task_id)
                        ),
                    )
                except (
                    OSError,
                    PermissionError,
                    FileNotFoundError,
                    ValueError,
                    KeyError,
                ) as exc:
                    complete = False
                    main_logger.error(
                        "[Coding Recovery] Could not reconcile interrupted action "
                        f"plan {action_plan['plan_id']} for task {task_id}: "
                        f"{type(exc).__name__}."
                    )
                    continue
                plan_results.append(result)
                recoveries.append(
                    {
                        "task_id": str(task_id)[:63],
                        "action_plan_id": str(action_plan["plan_id"])[:200],
                        "state": result["state"],
                        "uncertain_action_count": len(
                            result["uncertain_actions"]
                        ),
                        "workspace_fingerprint": result[
                            "workspace_fingerprint"
                        ],
                    }
                )
            if complete and len(plan_results) == len(task_ids[:50]):
                await self.journal.record(
                    "plan_reconciled",
                    plan_id=str(action_plan["plan_id"]),
                    status=(
                        "inspection_required"
                        if any(
                            item["state"] == "inspection_required"
                            for item in plan_results
                        )
                        else "resume_required"
                    ),
                    task_ids=[str(item)[:63] for item in task_ids[:50]],
                )

        if recoveries:
            await self.bus.publish(
                Events.CODING_ACTION_RECOVERY_REQUIRED,
                recovery_count=len(recoveries),
                recoveries=recoveries[: self._MAX_EVENT_ITEMS],
                recoveries_truncated=(
                    len(recoveries) > self._MAX_EVENT_ITEMS
                ),
                instruction=(
                    "Inspect workspace status, durable coding plan, diff, and "
                    "action journal before resuming; never replay uncertain actions."
                ),
            )

    async def stop(self) -> None:
        return None
