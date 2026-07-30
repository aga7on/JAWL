"""Bounded localhost operator controls used by the CLI without an LLM round trip."""

from __future__ import annotations

import re
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
                "instance_id": self.container.instance_id,
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
                "debug_broker": (
                    self.container.interfaces_config.debug_broker.model_dump()
                ),
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
        if action.startswith("debug."):
            broker = self.container.l2_clients.get("debug_broker")
            if broker is None:
                raise ValueError("Debug Broker is not initialized.")
            if action == "debug.get":
                session_id = str(params.get("session_id", "")).strip() or None
                if session_id is not None and not re.fullmatch(
                    r"[A-Za-z0-9_.-]{1,128}", session_id
                ):
                    raise ValueError("Invalid debug session ID.")
                return broker.session_snapshot(session_id)
            if action == "debug.search":
                query = str(params.get("query", ""))
                provider = str(params.get("provider", "")).strip() or None
                limit = int(params.get("limit", 12))
                return broker.search_operations(query, provider, limit)
            if action == "debug.start":
                options = params.get("options", {})
                if not isinstance(options, dict):
                    raise ValueError("Debug session options must be an object.")
                target = str(params.get("target", "")).strip() or None
                return await broker.start_session(
                    str(params.get("provider", "")),
                    target,
                    options,
                )
            if action == "debug.stop":
                session_id = str(params.get("session_id", "")).strip()
                if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", session_id):
                    raise ValueError("Invalid debug session ID.")
                return await broker.stop_session(session_id)
            if action == "debug.skill":
                skill_name = str(params.get("skill", ""))
                allowed = {
                    "DebugBroker.list_providers",
                    "DebugBroker.search_operations",
                    "DebugBroker.start_session",
                    "DebugBroker.call_operation",
                    "DebugBroker.wait_session",
                    "DebugBroker.session_snapshot",
                    "DebugBroker.stop_session",
                }
                if skill_name not in allowed:
                    raise ValueError("Only DebugBroker skills are allowed here.")
                arguments = params.get("arguments", {})
                if not isinstance(arguments, dict):
                    raise ValueError("Debug skill arguments must be an object.")
                from src.l3_agent.skills.registry import call_skill

                result = await call_skill(skill_name, arguments)
                return {
                    "skill": skill_name,
                    "is_success": result.is_success,
                    "message": result.message,
                }
            raise ValueError(f"Unsupported control action '{action}'.")
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
