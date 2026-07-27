"""Persistent, compact lifecycle state for one active long-running goal."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field

from src.l0_state.agent.state import AgentState
from src.utils.logger import agent_logger


GoalStatus = Literal["active", "complete", "blocked", "cancelled"]
VerificationPolicy = Literal["auto", "required", "none"]
VerificationStatus = Literal["pending", "passed", "failed", "not_required"]


class GoalRecord(BaseModel):
    """Durable state required to continue a goal without replaying its history."""

    goal_id: str
    objective: str = Field(min_length=1, max_length=8000)
    status: GoalStatus = "active"
    token_budget: Optional[int] = Field(default=None, ge=1)
    accounted_tokens: int = Field(default=0, ge=0)
    estimated_tokens: int = Field(default=0, ge=0)
    provider_tokens: int = Field(default=0, ge=0)
    created_at: float
    updated_at: float
    last_activity_at: float
    revision: int = Field(default=1, ge=1)
    lane_epoch: int = Field(default=1, ge=1)
    pending_work: bool = True
    next_wakeup_at: Optional[float] = None
    continuation_count: int = Field(default=0, ge=0)
    last_cycle_status: str = "created"
    last_summary: str = ""
    last_result: str = ""
    blocked_reason: str = ""
    completion_summary: str = ""
    linked_task_id: str = ""
    verification_policy: VerificationPolicy = "auto"
    verification_status: VerificationStatus = "pending"
    verification_summary: str = ""
    verification_at: Optional[float] = None
    last_action_tools: list[str] = Field(default_factory=list)
    evidence: list[Dict[str, Any]] = Field(default_factory=list)

    @property
    def remaining_tokens(self) -> Optional[int]:
        if self.token_budget is None:
            return None
        return max(0, self.token_budget - self.accounted_tokens)

    @property
    def lane_id(self) -> str:
        return f"goal-{self.goal_id}-e{self.lane_epoch}"


class GoalManager:
    """Own one active durable goal and expose a bounded prompt projection."""

    VERSION = 1
    MAX_RECORDS = 100
    MAX_EVIDENCE = 30

    def __init__(
        self,
        path: Path,
        agent_state: AgentState,
        *,
        enabled: bool = True,
        compact_context: bool = True,
        compact_max_chars: int = 24000,
        suppress_waiting_heartbeats: bool = True,
        recover_on_start: bool = True,
    ) -> None:
        self.path = Path(path)
        self.agent_state = agent_state
        self.enabled = bool(enabled)
        self.compact_context = bool(compact_context)
        self.compact_max_chars = int(compact_max_chars)
        self.suppress_waiting_heartbeats = bool(suppress_waiting_heartbeats)
        self._lock = asyncio.Lock()
        self._records: list[GoalRecord] = []
        self._load_error = ""
        self._load()
        if recover_on_start:
            self._restore_active_goal()
        else:
            self._sync_agent_state(self.active_goal)

    @staticmethod
    def _bounded(value: Any, limit: int) -> str:
        text = str(value or "").strip()
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 18)] + "...[truncated]"

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("version") != self.VERSION:
                raise ValueError("unsupported goal store version")
            records = payload.get("goals", [])
            if not isinstance(records, list):
                raise ValueError("goal store goals must be a list")
            self._records = [GoalRecord.model_validate(item) for item in records][
                -self.MAX_RECORDS :
            ]
        except Exception as exc:
            # Preserve the corrupt file for operator inspection. Goal recovery is
            # fail-closed: no active state is guessed from malformed data.
            agent_logger.error(f"[Goal] Failed to load durable goal state: {exc}")
            self._load_error = str(exc)
            self._records = []

    def _require_writable_store(self) -> None:
        if self._load_error:
            raise ValueError(
                "Goal store is malformed and was preserved for inspection; "
                "repair or move it before changing Goal state."
            )

    def _restore_active_goal(self) -> None:
        active = self.active_goal
        if active is None:
            self._sync_agent_state(None)
            return
        # A framework restart must reconstruct state from JAWL's durable
        # projection instead of silently trusting an old QWB parent chain.
        active.lane_epoch += 1
        active.revision += 1
        active.pending_work = True
        active.last_cycle_status = "restart_recovery"
        active.updated_at = time.time()
        self._sync_agent_state(active)
        try:
            self._save()
        except Exception as exc:
            agent_logger.error(f"[Goal] Failed to persist restart recovery: {exc}")

    def _sync_agent_state(self, goal: Optional[GoalRecord]) -> None:
        self.agent_state.current_goal = goal.objective if goal else ""
        self.agent_state.active_goal_id = goal.goal_id if goal else ""
        self.agent_state.goal_status = goal.status if goal else ""

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": self.VERSION,
            "goals": [record.model_dump(mode="json") for record in self._records],
        }
        data = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        temp = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        try:
            with temp.open("w", encoding="utf-8", newline="\n") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, self.path)
        finally:
            temp.unlink(missing_ok=True)

    def _touch(self, goal: GoalRecord) -> None:
        now = time.time()
        goal.updated_at = now
        goal.last_activity_at = now
        goal.revision += 1
        self._sync_agent_state(goal if goal.status == "active" else None)

    @property
    def active_goal(self) -> Optional[GoalRecord]:
        for goal in reversed(self._records):
            if goal.status == "active":
                return goal
        return None

    @property
    def lane_id(self) -> str:
        goal = self.active_goal
        return goal.lane_id if goal else ""

    def get(self, goal_id: str = "") -> Optional[GoalRecord]:
        if not goal_id:
            return self.active_goal or (self._records[-1] if self._records else None)
        normalized = str(goal_id).strip()
        return next(
            (goal for goal in reversed(self._records) if goal.goal_id == normalized),
            None,
        )

    def view(self, goal_id: str = "") -> Optional[Dict[str, Any]]:
        goal = self.get(goal_id)
        if goal is None:
            return None
        payload = goal.model_dump(mode="json")
        payload["remaining_tokens"] = goal.remaining_tokens
        payload["lane_id"] = goal.lane_id
        payload["evidence"] = payload["evidence"][-10:]
        return payload

    def list_views(self, limit: int = 20) -> list[Dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValueError("limit must be an integer")
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        return [
            self.view(goal.goal_id)
            for goal in reversed(self._records[-limit:])
        ]

    async def create(
        self,
        objective: str,
        *,
        token_budget: Optional[int] = None,
        linked_task_id: str = "",
        verification_policy: VerificationPolicy = "auto",
    ) -> GoalRecord:
        if not self.enabled:
            raise ValueError("Goal Mode is disabled by configuration.")
        clean_objective = self._bounded(objective, 8000)
        if not clean_objective:
            raise ValueError("objective must not be empty")
        if token_budget is not None and (
            isinstance(token_budget, bool) or int(token_budget) < 1
        ):
            raise ValueError("token_budget must be a positive integer")
        clean_task_id = self._bounded(linked_task_id, 200)
        if verification_policy not in {"auto", "required", "none"}:
            raise ValueError(
                "verification_policy must be 'auto', 'required', or 'none'"
            )
        async with self._lock:
            self._require_writable_store()
            existing = self.active_goal
            if existing is not None:
                raise ValueError(
                    f"Active goal '{existing.goal_id}' must be completed, blocked, "
                    "or cancelled before creating another."
                )
            now = time.time()
            goal = GoalRecord(
                goal_id=uuid.uuid4().hex,
                objective=clean_objective,
                token_budget=int(token_budget) if token_budget is not None else None,
                created_at=now,
                updated_at=now,
                last_activity_at=now,
                linked_task_id=clean_task_id,
                verification_policy=verification_policy,
                verification_status=(
                    "not_required"
                    if verification_policy == "none"
                    else "pending"
                ),
            )
            self._records.append(goal)
            self._records = self._records[-self.MAX_RECORDS :]
            self._sync_agent_state(goal)
            self._save()
            return goal.model_copy(deep=True)

    async def update(
        self,
        *,
        status: GoalStatus,
        summary: str,
        goal_id: str = "",
        token_budget: Optional[int] = None,
    ) -> GoalRecord:
        if status not in {"active", "complete", "blocked", "cancelled"}:
            raise ValueError("unsupported goal status")
        clean_summary = self._bounded(summary, 4000)
        if status in {"complete", "blocked", "cancelled"} and not clean_summary:
            raise ValueError(f"{status} requires a concrete summary or reason")
        async with self._lock:
            self._require_writable_store()
            goal = self.get(goal_id)
            if goal is None:
                raise ValueError("goal not found")
            if goal.status == "complete" and status != "complete":
                raise ValueError("a completed goal cannot be reopened")
            if status == "active" and goal.status not in {"active", "blocked"}:
                raise ValueError(f"goal in state '{goal.status}' cannot be resumed")
            verification_required = (
                goal.verification_policy == "required"
                or (
                    goal.verification_policy == "auto"
                    and bool(goal.linked_task_id)
                )
            )
            if (
                status == "complete"
                and verification_required
                and goal.verification_status != "passed"
            ):
                raise ValueError(
                    "linked coding goals require current passing verification "
                    "evidence before completion"
                )
            if token_budget is not None:
                if isinstance(token_budget, bool) or int(token_budget) < 1:
                    raise ValueError("token_budget must be a positive integer")
                if int(token_budget) < goal.accounted_tokens:
                    raise ValueError(
                        "token_budget cannot be below already accounted usage"
                    )
                goal.token_budget = int(token_budget)
            previous_status = goal.status
            goal.status = status
            goal.last_summary = clean_summary
            if status == "complete":
                goal.completion_summary = clean_summary
                goal.pending_work = False
                goal.next_wakeup_at = None
            elif status == "blocked":
                goal.blocked_reason = clean_summary
                goal.pending_work = False
                goal.next_wakeup_at = None
            elif status == "cancelled":
                goal.pending_work = False
                goal.next_wakeup_at = None
            else:
                goal.blocked_reason = ""
                goal.pending_work = True
                if previous_status != "active":
                    goal.lane_epoch += 1
            self._append_evidence(goal, f"status:{status}", clean_summary)
            self._touch(goal)
            self._save()
            return goal.model_copy(deep=True)

    def _append_evidence(self, goal: GoalRecord, kind: str, summary: str) -> None:
        text = self._bounded(summary, 2000)
        if not text:
            return
        digest = hashlib.sha256(f"{kind}\0{text}".encode("utf-8")).hexdigest()[:16]
        if any(item.get("id") == digest for item in goal.evidence):
            return
        goal.evidence.append(
            {"id": digest, "kind": self._bounded(kind, 100), "summary": text, "at": time.time()}
        )
        goal.evidence = goal.evidence[-self.MAX_EVIDENCE :]

    async def begin_cycle(self, event_name: str) -> Optional[GoalRecord]:
        async with self._lock:
            goal = self.active_goal
            if goal is None:
                return None
            self._require_writable_store()
            goal.pending_work = False
            goal.next_wakeup_at = None
            goal.continuation_count += 1
            goal.last_cycle_status = f"running:{self._bounded(event_name, 100)}"
            self._touch(goal)
            self._save()
            return goal.model_copy(deep=True)

    async def finish_cycle(
        self,
        *,
        state: Literal[
            "waiting", "continue", "completed", "blocked", "failed", "exhausted"
        ],
        summary: str = "",
        wake_after_seconds: Optional[int] = None,
    ) -> Optional[GoalRecord]:
        async with self._lock:
            goal = self.active_goal
            if goal is None:
                return None
            self._require_writable_store()
            clean_summary = self._bounded(summary, 4000)
            goal.last_cycle_status = state
            goal.last_summary = clean_summary or goal.last_summary
            if clean_summary:
                self._append_evidence(goal, f"cycle:{state}", clean_summary)
            if state == "completed":
                verification_required = (
                    goal.verification_policy == "required"
                    or (
                        goal.verification_policy == "auto"
                        and bool(goal.linked_task_id)
                    )
                )
                if verification_required and goal.verification_status != "passed":
                    goal.last_cycle_status = "verification_required"
                    goal.last_summary = (
                        "Completion rejected: the linked coding goal has no "
                        "current passing verification evidence."
                    )
                    goal.pending_work = True
                    goal.next_wakeup_at = time.time() + 1
                    self._append_evidence(
                        goal, "verification_required", goal.last_summary
                    )
                else:
                    goal.status = "complete"
                    goal.completion_summary = clean_summary or "Goal completed."
                    goal.pending_work = False
                    goal.next_wakeup_at = None
            elif state == "blocked":
                goal.status = "blocked"
                goal.blocked_reason = clean_summary or "Goal execution blocked."
                goal.pending_work = False
                goal.next_wakeup_at = None
            elif state in {"continue", "exhausted", "failed"}:
                goal.pending_work = True
                if wake_after_seconds is not None:
                    goal.next_wakeup_at = time.time() + max(
                        1, min(int(wake_after_seconds), 86400)
                    )
            else:
                goal.pending_work = False
                if wake_after_seconds is not None:
                    goal.next_wakeup_at = time.time() + max(
                        1, min(int(wake_after_seconds), 86400)
                    )
            self._touch(goal)
            self._save()
            return goal.model_copy(deep=True)

    async def record_action_result(
        self, result: str, actions: Optional[list[Dict[str, Any]]] = None
    ) -> None:
        async with self._lock:
            goal = self.active_goal
            if goal is None:
                return
            self._require_writable_store()
            goal.last_result = self._bounded(result, 6000)
            normalized_actions = [
                item
                for item in (actions or [])
                if isinstance(item, dict)
                and isinstance(item.get("tool_name"), str)
            ]
            goal.last_action_tools = [
                self._bounded(item["tool_name"], 200)
                for item in normalized_actions[-20:]
            ]
            verification_calls = [
                item
                for item in normalized_actions
                if item["tool_name"]
                == "HostOSCodingVerification.run_coding_verification"
            ]
            if verification_calls:
                failed = "status=failed" in result
                goal.verification_status = "failed" if failed else "passed"
                goal.verification_summary = self._bounded(result, 3000)
                goal.verification_at = time.time()
                self._append_evidence(
                    goal,
                    f"verification:{goal.verification_status}",
                    goal.verification_summary,
                )
            self._append_evidence(goal, "tool_result", goal.last_result)
            self._touch(goal)
            self._save()

    async def record_usage(self, metrics: Dict[str, Any]) -> Optional[GoalRecord]:
        async with self._lock:
            goal = self.active_goal
            if goal is None:
                return None
            self._require_writable_store()

            def token_value(name: str) -> int:
                value = metrics.get(name)
                return (
                    int(value)
                    if isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and value >= 0
                    else 0
                )

            provider = token_value("provider_total_tokens")
            estimated = token_value("estimated_input_tokens") + token_value(
                "estimated_output_tokens"
            )
            goal.provider_tokens += provider
            goal.estimated_tokens += estimated
            goal.accounted_tokens += provider or estimated
            if (
                goal.token_budget is not None
                and goal.accounted_tokens >= goal.token_budget
            ):
                goal.status = "blocked"
                goal.blocked_reason = (
                    f"Goal token budget exhausted: {goal.accounted_tokens}/"
                    f"{goal.token_budget}."
                )
                goal.pending_work = False
                goal.next_wakeup_at = None
                goal.last_cycle_status = "budget_exhausted"
                self._append_evidence(
                    goal, "budget_exhausted", goal.blocked_reason
                )
            self._touch(goal)
            self._save()
            return goal.model_copy(deep=True)

    def should_run_heartbeat(self, now: Optional[float] = None) -> bool:
        """Return false only for an active goal that is deterministically waiting."""

        goal = self.active_goal
        if goal is None or not self.suppress_waiting_heartbeats:
            return True
        current = time.time() if now is None else float(now)
        if goal.pending_work:
            return True
        return goal.next_wakeup_at is not None and goal.next_wakeup_at <= current

    def seconds_until_wakeup(self, now: Optional[float] = None) -> Optional[float]:
        goal = self.active_goal
        if goal is None:
            return None
        current = time.time() if now is None else float(now)
        if goal.pending_work:
            return 0.0
        if goal.next_wakeup_at is None:
            return None
        return max(0.0, goal.next_wakeup_at - current)

    async def mark_event_pending(self, event_name: str) -> None:
        if event_name == "HEARTBEAT":
            return
        async with self._lock:
            goal = self.active_goal
            if goal is None:
                return
            self._require_writable_store()
            goal.pending_work = True
            goal.last_cycle_status = f"event:{self._bounded(event_name, 100)}"
            self._touch(goal)
            self._save()

    async def get_context_block(self, **_: Any) -> str:
        goal = self.active_goal
        if goal is None:
            return ""
        remaining = (
            "unbounded"
            if goal.remaining_tokens is None
            else str(goal.remaining_tokens)
        )
        evidence = "\n".join(
            f"- {item['kind']}: {self._bounded(item['summary'], 300)}"
            for item in goal.evidence[-5:]
        ) or "- No durable evidence recorded yet."
        wake = (
            "event only"
            if goal.next_wakeup_at is None
            else time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(goal.next_wakeup_at)
            )
        )
        return f"""
## ACTIVE GOAL
* Goal ID: {goal.goal_id}
* Status: {goal.status}
* Revision: {goal.revision}
* Continuations: {goal.continuation_count}
* Token usage: accounted={goal.accounted_tokens}, remaining={remaining}
* Execution state: {goal.last_cycle_status}
* Next wakeup: {wake}
* Linked coding task: {goal.linked_task_id or "none"}
* Verification: policy={goal.verification_policy}, status={goal.verification_status}
* Verification evidence: {self._bounded(goal.verification_summary, 1000) or "none"}

### Objective
{self._bounded(goal.objective, 3000)}

### Latest durable evidence
{evidence}

### Latest bounded result
{self._bounded(goal.last_result, 2500) or "No tool result recorded yet."}

### Goal protocol
Keep working until the objective is actually achieved. For a compact response,
execute_skill arguments may use Goal Protocol v2:
`{{"v":2,"state":"act","calls":[{{"tool":"Exact.skill","args":{{}}}}],"note":"short"}}`.
Terminal states are `done`, `wait`, and `blocked`; include a concrete summary.
`wait` may include `wake_after_seconds`. Do not report `done` until durable
evidence satisfies the objective. For coding goals, inspect the exact diff,
run the smallest relevant test first, then the repository verification gate.
A passing narrow test is evidence, not proof that the full goal is complete.
""".strip()
