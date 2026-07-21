"""Privacy-preserving rollups over durable coding-agent evidence."""

from __future__ import annotations

import json
import math
from collections import Counter
from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional

from src.l3_agent.skills.journal import ActionJournal
from src.l3_agent.skills.registry import SkillResult, skill
from src.l3_agent.swarm.roles import Subagents
from src.utils._tools import redact_sensitive_text
from src.utils.event.registry import Events


class CodingTelemetrySkills:
    """Aggregate metrics without returning prompts, thoughts, or tool payloads."""

    _MAX_RECORDS = 200
    _MAX_MODELS = 20
    _MAX_TOOLS = 50
    _DASHBOARD_NAME = "Coding telemetry"

    def __init__(self, journal: ActionJournal, sql_ticks: Any, event_bus: Any) -> None:
        self.journal = journal
        self.sql_ticks = sql_ticks
        self.event_bus = event_bus

    @staticmethod
    def _number(value: Any) -> Optional[float]:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        numeric = float(value)
        if not math.isfinite(numeric) or numeric < 0:
            return None
        return numeric

    @staticmethod
    def _trace_id(value: Any) -> str:
        if not isinstance(value, Mapping):
            return ""
        trace_id = value.get("trace_id")
        return str(trace_id)[:128] if trace_id else ""

    @staticmethod
    def _tool_name(value: Any) -> str:
        if not isinstance(value, Mapping):
            return ""
        return redact_sensitive_text(str(value.get("tool_name") or ""))[:200]

    @classmethod
    def _is_coding_tool(cls, value: Any) -> bool:
        return "coding" in cls._tool_name(value).lower()

    @staticmethod
    def _timestamp(value: Any) -> Optional[str]:
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, str) and value:
            return value[:64]
        return None

    @staticmethod
    def _sum_metric(records: Iterable[Mapping[str, Any]], key: str) -> float:
        total = 0.0
        for record in records:
            value = CodingTelemetrySkills._number(record.get(key))
            if value is not None:
                total += value
        return total

    @staticmethod
    def _integer(value: float) -> int | float:
        return int(value) if value.is_integer() else round(value, 3)

    @staticmethod
    def _percentile(values: List[float], percentile: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        index = max(0, math.ceil(percentile * len(ordered)) - 1)
        return round(ordered[index], 1)

    @classmethod
    def _model_rollups(
        cls, requests: List[Mapping[str, Any]]
    ) -> tuple[List[Dict[str, Any]], bool]:
        grouped: Dict[str, List[Mapping[str, Any]]] = {}
        for request in requests:
            model = redact_sensitive_text(str(request.get("model") or "unknown"))[:120]
            grouped.setdefault(model, []).append(request)
        ordered = sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0]))
        truncated = len(ordered) > cls._MAX_MODELS
        rollups = []
        for model, items in ordered[: cls._MAX_MODELS]:
            durations = [
                value
                for item in items
                if (value := cls._number(item.get("duration_ms"))) is not None
            ]
            rollups.append(
                {
                    "model": model,
                    "requests": len(items),
                    "completed": sum(item.get("status") == "completed" for item in items),
                    "failed_or_cancelled": sum(
                        item.get("status") != "completed" for item in items
                    ),
                    "duration_ms": round(sum(durations), 1),
                    "p95_duration_ms": cls._percentile(durations, 0.95),
                    "estimated_input_tokens": cls._integer(
                        cls._sum_metric(items, "estimated_input_tokens")
                    ),
                    "estimated_output_tokens": cls._integer(
                        cls._sum_metric(items, "estimated_output_tokens")
                    ),
                    "provider_total_tokens": cls._integer(
                        cls._sum_metric(items, "provider_total_tokens")
                    ),
                }
            )
        return rollups, truncated

    @classmethod
    def _action_rollups(
        cls, terminal_actions: List[Mapping[str, Any]]
    ) -> tuple[List[Dict[str, Any]], bool]:
        grouped: Dict[str, List[Mapping[str, Any]]] = {}
        for action in terminal_actions:
            tool_name = cls._tool_name(action) or "unknown"
            grouped.setdefault(tool_name, []).append(action)
        ordered = sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0]))
        truncated = len(ordered) > cls._MAX_TOOLS
        rollups = []
        for tool_name, items in ordered[: cls._MAX_TOOLS]:
            durations = [
                value
                for item in items
                if (value := cls._number(item.get("duration_ms"))) is not None
            ]
            rollups.append(
                {
                    "tool_name": tool_name,
                    "calls": len(items),
                    "succeeded": sum(
                        item.get("event") == "action_finished"
                        and item.get("is_success") is True
                        for item in items
                    ),
                    "failed": sum(
                        item.get("event") == "action_finished"
                        and item.get("is_success") is not True
                        for item in items
                    ),
                    "cancelled": sum(
                        item.get("event") == "action_cancelled" for item in items
                    ),
                    "blocked": sum(
                        item.get("event") == "action_blocked" for item in items
                    ),
                    "duration_ms": round(sum(durations), 1),
                    "p95_duration_ms": cls._percentile(durations, 0.95),
                }
            )
        return rollups, truncated

    async def _snapshot(self, tick_limit: int, plan_limit: int) -> Dict[str, Any]:
        if tick_limit < 1 or tick_limit > self._MAX_RECORDS:
            raise ValueError("tick_limit must be between 1 and 200")
        if plan_limit < 1 or plan_limit > self._MAX_RECORDS:
            raise ValueError("plan_limit must be between 1 and 200")
        ticks = await self.sql_ticks.get_ticks(limit=tick_limit)
        plans = await self.journal.recent_plans(
            limit=plan_limit, include_events=True
        )

        coding_trace_ids: set[str] = set()
        coding_plans: List[Mapping[str, Any]] = []
        task_ids: set[str] = set()
        for tick in ticks:
            actions = tick.actions if isinstance(tick.actions, list) else []
            if any(self._is_coding_tool(action) for action in actions):
                trace = self._trace_id(
                    tick.results.get("trace") if isinstance(tick.results, dict) else None
                )
                if trace:
                    coding_trace_ids.add(trace)
        for plan in plans:
            events = plan.get("events") if isinstance(plan.get("events"), list) else []
            plan_task_ids = plan.get("task_ids") if isinstance(plan.get("task_ids"), list) else []
            coding = bool(plan_task_ids) or any(
                self._is_coding_tool(event) for event in events
            )
            if not coding:
                continue
            coding_plans.append(plan)
            task_ids.update(str(task_id)[:63] for task_id in plan_task_ids if task_id)
            for event in events:
                trace = self._trace_id(event.get("trace") if isinstance(event, dict) else None)
                if trace:
                    coding_trace_ids.add(trace)

        coding_ticks = []
        request_metrics: List[Mapping[str, Any]] = []
        request_keys: set[str] = set()
        timestamps: List[str] = []
        for tick in ticks:
            results = tick.results if isinstance(tick.results, dict) else {}
            actions = tick.actions if isinstance(tick.actions, list) else []
            trace = self._trace_id(results.get("trace"))
            if trace not in coding_trace_ids and not any(
                self._is_coding_tool(action) for action in actions
            ):
                continue
            coding_ticks.append(tick)
            timestamp = self._timestamp(getattr(tick, "created_at", None))
            if timestamp:
                timestamps.append(timestamp)
            metrics = results.get("llm_metrics")
            if not isinstance(metrics, Mapping) or not metrics:
                continue
            key = str(metrics.get("request_id") or getattr(tick, "id", id(tick)))
            if key in request_keys:
                continue
            request_keys.add(key)
            request_metrics.append(metrics)

        terminal_actions: List[Mapping[str, Any]] = []
        plan_states: Counter[str] = Counter()
        for plan in coding_plans:
            plan_states[str(plan.get("state") or "unknown")] += 1
            for timestamp_key in ("started_at", "finished_at"):
                timestamp = self._timestamp(plan.get(timestamp_key))
                if timestamp:
                    timestamps.append(timestamp)
            events = plan.get("events") if isinstance(plan.get("events"), list) else []
            terminals: Dict[str, Mapping[str, Any]] = {}
            for event in events:
                if not isinstance(event, Mapping) or event.get("event") not in {
                    "action_finished",
                    "action_cancelled",
                    "action_blocked",
                }:
                    continue
                action_id = str(event.get("action_id") or len(terminals))[:100]
                terminals[action_id] = event
            terminal_actions.extend(terminals.values())

        models, models_truncated = self._model_rollups(request_metrics)
        tools, tools_truncated = self._action_rollups(terminal_actions)
        llm_durations = [
            value
            for metrics in request_metrics
            if (value := self._number(metrics.get("duration_ms"))) is not None
        ]
        action_durations = [
            value
            for action in terminal_actions
            if (value := self._number(action.get("duration_ms"))) is not None
        ]
        statuses = Counter(str(item.get("status") or "unknown") for item in request_metrics)
        provider_covered = sum(
            self._number(item.get("provider_total_tokens")) is not None
            for item in request_metrics
        )
        estimated_covered = sum(
            self._number(item.get("estimated_input_tokens")) is not None
            for item in request_metrics
        )
        verification_actions = [
            action
            for action in terminal_actions
            if "codingverification" in self._tool_name(action).lower()
        ]
        return {
            "schema_version": 1,
            "window": {
                "tick_limit": tick_limit,
                "plan_limit": plan_limit,
                "ticks_examined": len(ticks),
                "plans_examined": len(plans),
                "tick_window_possibly_truncated": len(ticks) == tick_limit,
                "plan_window_possibly_truncated": len(plans) == plan_limit,
                "first_at": min(timestamps) if timestamps else None,
                "last_at": max(timestamps) if timestamps else None,
            },
            "totals": {
                "coding_traces": len(coding_trace_ids),
                "coding_tasks": len(task_ids),
                "coding_ticks": len(coding_ticks),
                "action_plans": len(coding_plans),
                "actions": len(terminal_actions),
                "actions_succeeded": sum(
                    action.get("event") == "action_finished"
                    and action.get("is_success") is True
                    for action in terminal_actions
                ),
                "actions_failed_or_stopped": sum(
                    not (
                        action.get("event") == "action_finished"
                        and action.get("is_success") is True
                    )
                    for action in terminal_actions
                ),
                "action_duration_ms": round(sum(action_durations), 1),
                "llm_requests": len(request_metrics),
                "llm_duration_ms": round(sum(llm_durations), 1),
                "llm_p95_duration_ms": self._percentile(llm_durations, 0.95),
                "estimated_input_tokens": self._integer(
                    self._sum_metric(request_metrics, "estimated_input_tokens")
                ),
                "estimated_output_tokens": self._integer(
                    self._sum_metric(request_metrics, "estimated_output_tokens")
                ),
                "provider_prompt_tokens": self._integer(
                    self._sum_metric(request_metrics, "provider_prompt_tokens")
                ),
                "provider_completion_tokens": self._integer(
                    self._sum_metric(request_metrics, "provider_completion_tokens")
                ),
                "provider_total_tokens": self._integer(
                    self._sum_metric(request_metrics, "provider_total_tokens")
                ),
                "verification_actions": len(verification_actions),
                "verification_failures": sum(
                    action.get("is_success") is not True
                    for action in verification_actions
                ),
            },
            "llm_statuses": dict(sorted(statuses.items())),
            "plan_states": dict(sorted(plan_states.items())),
            "models": models,
            "tools": tools,
            "coverage": {
                "estimated_token_requests": estimated_covered,
                "provider_token_requests": provider_covered,
                "monetary_cost_available": False,
                "monetary_cost_reason": (
                    "No trusted model price catalogue is configured; subscription/web "
                    "traffic cannot be priced from tokens alone."
                ),
                "models_truncated": models_truncated,
                "tools_truncated": tools_truncated,
            },
            "privacy": {
                "contains_prompts": False,
                "contains_thoughts": False,
                "contains_tool_parameters": False,
                "contains_tool_results": False,
                "contains_trace_ids": False,
                "contains_task_ids": False,
            },
        }

    @staticmethod
    def _dashboard(snapshot: Mapping[str, Any]) -> str:
        totals = snapshot["totals"]
        coverage = snapshot["coverage"]
        lines = [
            "## Coding telemetry",
            "",
            f"- Coding tasks/traces: {totals['coding_tasks']} / {totals['coding_traces']}",
            f"- LLM requests: {totals['llm_requests']} ({totals['llm_duration_ms']} ms total, p95 {totals['llm_p95_duration_ms']} ms)",
            f"- Estimated tokens: {totals['estimated_input_tokens']} in / {totals['estimated_output_tokens']} out",
            f"- Provider tokens: {totals['provider_total_tokens']} total ({coverage['provider_token_requests']}/{totals['llm_requests']} requests covered)",
            f"- Tool actions: {totals['actions']} ({totals['actions_succeeded']} succeeded, {totals['actions_failed_or_stopped']} failed/stopped)",
            f"- Verification: {totals['verification_actions']} runs, {totals['verification_failures']} failures",
            "- Monetary cost: unavailable (no trusted price catalogue)",
        ]
        models = snapshot.get("models", [])
        if models:
            lines.extend(["", "### Models"])
            for model in models[:10]:
                lines.append(
                    f"- {model['model']}: {model['requests']} requests, "
                    f"{model['duration_ms']} ms, {model['provider_total_tokens']} provider tokens"
                )
        return "\n".join(lines)[:6000]

    @skill(swarm=[Subagents.CODER, Subagents.QA_ENGINEER, Subagents.SYSADMIN])
    async def inspect_coding_telemetry(
        self, tick_limit: int = 100, plan_limit: int = 100
    ) -> SkillResult:
        """Return bounded token, latency, tool, and verification rollups."""

        try:
            snapshot = await self._snapshot(tick_limit, plan_limit)
            return SkillResult.ok(json.dumps(snapshot, ensure_ascii=False))
        except ValueError as exc:
            return SkillResult.fail(str(exc))
        except Exception as exc:
            return SkillResult.fail(f"Error building coding telemetry: {exc}")

    @skill(swarm=[Subagents.CODER, Subagents.QA_ENGINEER, Subagents.SYSADMIN])
    async def publish_coding_telemetry_dashboard(
        self, tick_limit: int = 100, plan_limit: int = 100
    ) -> SkillResult:
        """Publish a passive redacted coding telemetry dashboard block."""

        try:
            snapshot = await self._snapshot(tick_limit, plan_limit)
            content = self._dashboard(snapshot)
            await self.event_bus.publish(
                Events.SYSTEM_DASHBOARD_UPDATE,
                name=self._DASHBOARD_NAME,
                content=content,
            )
            return SkillResult.ok(
                json.dumps(
                    {
                        "published": True,
                        "dashboard": self._DASHBOARD_NAME,
                        "content_chars": len(content),
                        "schema_version": snapshot["schema_version"],
                    },
                    ensure_ascii=False,
                )
            )
        except ValueError as exc:
            return SkillResult.fail(str(exc))
        except Exception as exc:
            return SkillResult.fail(f"Error publishing coding telemetry: {exc}")
