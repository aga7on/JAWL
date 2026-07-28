"""Bounded localhost operator controls used by the CLI without an LLM round trip."""

from __future__ import annotations

from typing import Any, Dict

from src.l3_agent.goals.ledger import TaskLedgerPatch
from src.system.container import SystemContainer
from src.utils.event.registry import EventLevel


class OperatorControl:
    """Expose a small allowlisted runtime/Goal control surface."""

    def __init__(self, container: SystemContainer) -> None:
        self.container = container

    def _manager(self):
        manager = self.container.goal_manager
        if manager is None:
            raise ValueError("Goal Mode is not initialized.")
        return manager

    def _wake_goal(self, goal_id: str) -> None:
        heartbeat = self.container.heartbeat
        if heartbeat is not None:
            heartbeat.answer_to_event(
                EventLevel.CRITICAL,
                "GOAL_OPERATOR_CONTROL",
                {"goal_id": str(goal_id)[:64]},
            )

    def _status(self) -> Dict[str, Any]:
        state = self.container.agent_state
        manager = self.container.goal_manager
        heartbeat = self.container.heartbeat
        return {
            "agent": {
                "state": (
                    getattr(getattr(state, "state", None), "value", None)
                    if state is not None
                    else "starting"
                ),
                "model": getattr(state, "llm_model", "unknown"),
                "step": getattr(state, "current_step", 0),
                "max_steps": getattr(state, "max_react_steps", 0),
                "uptime": state.get_uptime() if state is not None else "",
                "last_input_tokens": getattr(state, "last_input_tokens", 0),
                "last_action_error": str(
                    getattr(state, "last_action_error", "")
                )[:1000],
            },
            "modes": {
                "thinking_policy": self.container.settings.llm.thinking_policy,
                "tool_transport": self.container.settings.llm.tool_transport,
                "continuous_cycle": (
                    self.container.settings.system.continuous_cycle
                ),
                "heartbeat_interval": (
                    self.container.settings.system.heartbeat_interval
                ),
                "goal_mode": self.container.settings.system.goal_mode.model_dump(),
                "idle_heartbeat_backoff": (
                    self.container.settings.system.idle_heartbeat_backoff.model_dump()
                ),
                "event_policy": (
                    self.container.settings.system.event_acceleration.active_cycle_policy
                ),
                "mcp_enabled": self.container.interfaces_config.mcp.enabled,
                "media": (
                    self.container.interfaces_config.multimodality.model_dump()
                ),
            },
            "goal": manager.view() if manager is not None else None,
            "heartbeat": (
                heartbeat.get_queue_snapshot() if heartbeat is not None else None
            ),
        }

    async def handle(
        self, action: str, params: Dict[str, Any]
    ) -> Dict[str, Any]:
        if not isinstance(action, str) or len(action) > 100:
            raise ValueError("Invalid control action.")
        if not isinstance(params, dict):
            raise ValueError("Control params must be an object.")

        if action == "status.get":
            return self._status()
        manager = self._manager()
        if action == "goal.get":
            goal = manager.view(str(params.get("goal_id", "")))
            if goal is None:
                raise ValueError("Goal not found.")
            return goal
        if action == "goal.list":
            return {"goals": manager.list_views(int(params.get("limit", 20)))}
        if action == "goal.create":
            goal = await manager.create(
                str(params.get("objective", "")),
                token_budget=params.get("token_budget"),
                linked_task_id=str(params.get("linked_task_id", "")),
                verification_policy=str(
                    params.get("verification_policy", "auto")
                ),
            )
            self._wake_goal(goal.goal_id)
            return manager.view(goal.goal_id)
        if action == "goal.update":
            status = str(params.get("status", ""))
            goal = await manager.update(
                status=status,
                summary=str(params.get("summary", "")),
                goal_id=str(params.get("goal_id", "")),
                token_budget=params.get("token_budget"),
            )
            if status == "active":
                self._wake_goal(goal.goal_id)
            return manager.view(goal.goal_id)
        if action == "goal.wakeup":
            seconds = params.get("wake_after_seconds")
            if (
                isinstance(seconds, bool)
                or not isinstance(seconds, int)
                or seconds < 1
                or seconds > 86400
            ):
                raise ValueError(
                    "wake_after_seconds must be between 1 and 86400."
                )
            goal = await manager.finish_cycle(
                state="waiting",
                summary=str(params.get("summary", "")),
                wake_after_seconds=seconds,
            )
            if goal is None:
                raise ValueError("No active goal.")
            return manager.view(goal.goal_id)
        if action == "goal.ledger.update":
            patch = TaskLedgerPatch.model_validate(params.get("patch", {}))
            goal = await manager.record_ledger_patch(patch)
            if goal is None:
                raise ValueError("No active goal.")
            return manager.view(goal.goal_id)
        raise ValueError(f"Unsupported control action '{action}'.")
