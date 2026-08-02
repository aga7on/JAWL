"""Persistent, compact lifecycle state for one active long-running goal."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field

from src.l0_state.agent.state import AgentState
from src.l3_agent.goals.ledger import (
    LedgerActionOutcome,
    LedgerFailurePatch,
    TaskLedger,
    TaskLedgerPatch,
    apply_ledger_patch,
)
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
    recent_action_fingerprints: list[Dict[str, Any]] = Field(default_factory=list)
    evidence: list[Dict[str, Any]] = Field(default_factory=list)
    task_ledger: TaskLedger = Field(default_factory=TaskLedger)
    context_rebase_count: int = Field(default=0, ge=0)
    last_provider_prompt_tokens: int = Field(default=0, ge=0)
    last_rebased_lane_epoch: int = Field(default=0, ge=0)

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

    VERSION = 2
    MAX_RECORDS = 100
    MAX_EVIDENCE = 30
    MAX_ACTION_FINGERPRINTS = 32
    _DISCOVERY_TOOLS = {
        "MCPTools.search_tools",
        "SkillCatalog.search_skills",
    }
    _SEARCH_STOP_WORDS = {
        "a",
        "an",
        "and",
        "for",
        "in",
        "of",
        "or",
        "the",
        "to",
        "tool",
        "tools",
        "with",
    }

    def __init__(
        self,
        path: Path,
        agent_state: AgentState,
        *,
        enabled: bool = True,
        compact_context: bool = True,
        compact_max_chars: int = 36000,
        suppress_waiting_heartbeats: bool = True,
        task_ledger_enabled: bool = True,
        task_ledger_max_chars: int = 10000,
        provider_rebase_prompt_tokens: int = 65000,
        server_side_conversation: bool = True,
        recover_on_start: bool = True,
    ) -> None:
        self.path = Path(path)
        self.agent_state = agent_state
        self.enabled = bool(enabled)
        self.compact_context = bool(compact_context)
        self.compact_max_chars = int(compact_max_chars)
        self.suppress_waiting_heartbeats = bool(suppress_waiting_heartbeats)
        self.task_ledger_enabled = bool(task_ledger_enabled)
        self.task_ledger_max_chars = int(task_ledger_max_chars)
        self.provider_rebase_prompt_tokens = int(
            provider_rebase_prompt_tokens
        )
        self.server_side_conversation = bool(server_side_conversation)
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

    @classmethod
    def _search_tokens(cls, value: Any) -> list[str]:
        return sorted(
            {
                token
                for token in re.findall(r"[a-z0-9_.:-]{2,}", str(value or "").casefold())
                if token not in cls._SEARCH_STOP_WORDS
            }
        )[:40]

    @classmethod
    def _action_fingerprint(
        cls, action: Dict[str, Any], status: str = "unknown"
    ) -> Optional[Dict[str, Any]]:
        tool = str(action.get("tool_name") or "")
        if tool not in cls._DISCOVERY_TOOLS:
            return None
        parameters = action.get("parameters")
        parameters = parameters if isinstance(parameters, dict) else {}
        query_tokens = cls._search_tokens(parameters.get("query"))
        if not query_tokens:
            return None
        return {
            "tool": tool,
            "server": cls._bounded(parameters.get("server"), 100),
            "tokens": query_tokens,
            "status": status,
            "at": time.time(),
        }

    def repeated_action_warning(
        self, actions: list[Dict[str, Any]]
    ) -> str:
        """Reject a third semantically equivalent discovery search.

        Discovery is intentionally cheap, but repeated catalog searches without
        advancing to a concrete tool call are a strong signal that provider
        context or tool-state was lost.
        """

        goal = self.active_goal
        if goal is None:
            return ""
        history = goal.recent_action_fingerprints[-20:]
        for action in actions:
            current = self._action_fingerprint(action)
            if current is None:
                continue
            current_tokens = set(current["tokens"])
            similar = 0
            for previous in history:
                if (
                    previous.get("tool") != current["tool"]
                    or previous.get("server", "") != current["server"]
                    or previous.get("status") not in {"success", "guarded"}
                ):
                    continue
                previous_tokens = set(previous.get("tokens") or [])
                if not previous_tokens:
                    continue
                overlap = len(current_tokens & previous_tokens)
                union = len(current_tokens | previous_tokens)
                similarity = overlap / union if union else 0.0
                if current_tokens == previous_tokens or (
                    overlap >= 3 and similarity >= 0.25
                ):
                    similar += 1
            if similar >= 2:
                query = str((action.get("parameters") or {}).get("query") or "")
                return (
                    "Goal repetition guard rejected another semantically "
                    f"equivalent {current['tool']} query ({query[:240]}). "
                    "Use an already discovered exact tool, persist its schema in "
                    "ledger.tool_state, reconnect the MCP server if its catalog "
                    "changed, or record a concrete blocker instead of searching "
                    "the same capability again."
                )
        return ""

    @classmethod
    def _automatic_tool_state(
        cls,
        action: Dict[str, Any],
        result_block: str,
        status: str,
    ) -> list[str]:
        """Project durable, non-secret tool/session facts from action outcomes."""

        tool = str(action.get("tool_name") or "")
        parameters = action.get("parameters")
        parameters = parameters if isinstance(parameters, dict) else {}
        entries: list[str] = []
        if tool == "MCPTools.search_tools" and status == "success":
            for match in re.finditer(
                r'"name"\s*:\s*"([^"]+)".{0,2200}?'
                r'"schema_sha256"\s*:\s*"([a-f0-9]{64})".{0,300}?'
                r'"allowed"\s*:\s*(true|false)',
                result_block,
                flags=re.IGNORECASE | re.DOTALL,
            ):
                name, schema_hash, allowed = match.groups()
                server = cls._bounded(parameters.get("server") or "unknown", 100)
                entries.append(
                    f"mcp:{server}:{name} | allowed={allowed.casefold()} "
                    f"schema={schema_hash.casefold()}"
                )
        elif tool == "MCPTools.call_tool":
            server = cls._bounded(parameters.get("server") or "unknown", 100)
            called_tool = cls._bounded(parameters.get("tool") or "unknown", 160)
            schema_hash = cls._bounded(
                parameters.get("expected_schema_sha256") or "unknown", 80
            )
            entries.append(
                f"mcp:{server}:{called_tool} | last_status={status} "
                f"schema={schema_hash}"
            )
        elif tool == "MCPTools.reconnect_server":
            server = cls._bounded(parameters.get("server") or "unknown", 100)
            entries.append(
                f"mcp-session:{server} | reconnect_status={status}"
            )
        elif tool.startswith("HostOSProcessSessions."):
            session_match = re.search(
                r'"id"\s*:\s*"([a-f0-9]{12})"', result_block, re.IGNORECASE
            )
            if session_match:
                session_status_match = re.search(
                    r'"status"\s*:\s*"([^"]+)"',
                    result_block,
                    re.IGNORECASE,
                )
                session_status = (
                    session_status_match.group(1).casefold()
                    if session_status_match
                    else status
                )
                entries.append(
                    f"process-session:{session_match.group(1).casefold()} | "
                    f"tool={tool} status={session_status}"
                )
        return entries

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("version") not in {
                1,
                self.VERSION,
            }:
                raise ValueError("unsupported goal store version")
            records = payload.get("goals", [])
            if not isinstance(records, list):
                raise ValueError("goal store goals must be a list")
            loaded: list[GoalRecord] = []
            for item in records:
                record = GoalRecord.model_validate(item)
                if (
                    payload.get("version") == 1
                    and isinstance(item, dict)
                    and "task_ledger" not in item
                    and record.status == "active"
                ):
                    # Legacy goals already contain useful evidence/result
                    # fields, but no explicit execution checkpoint. Seed a
                    # conservative recovery stage without inventing facts.
                    record.task_ledger = TaskLedger(
                        revision=1,
                        current_phase="legacy_recovery",
                        pending_steps=[
                            self._bounded(
                                f"Continue objective: {record.objective}", 400
                            )
                        ],
                        next_action=(
                            "Reconcile the latest durable evidence and action "
                            "result, then record a precise Task Ledger checkpoint."
                        ),
                        checkpoint_summary=(
                            self._bounded(record.last_summary, 1200)
                            or "Migrated from Goal store v1."
                        ),
                        updated_at=time.time(),
                    )
                loaded.append(record)
            self._records = loaded[-self.MAX_RECORDS :]
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
        # A framework restart reconstructs state from JAWL's durable
        # projection instead of trusting an optional provider conversation.
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

    def set_provider_capabilities(
        self, *, server_side_conversation: bool
    ) -> None:
        """Configure optional provider-session optimization without persistence."""

        self.server_side_conversation = bool(server_side_conversation)

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

    def _append_evidence(
        self, goal: GoalRecord, kind: str, summary: str
    ) -> str:
        text = self._bounded(summary, 2000)
        if not text:
            return ""
        digest = hashlib.sha256(f"{kind}\0{text}".encode("utf-8")).hexdigest()[:16]
        if any(item.get("id") == digest for item in goal.evidence):
            return digest
        goal.evidence.append(
            {
                "id": digest,
                "kind": self._bounded(kind, 100),
                "summary": text,
                "at": time.time(),
            }
        )
        goal.evidence = goal.evidence[-self.MAX_EVIDENCE :]
        return digest

    async def record_ledger_patch(
        self, patch: TaskLedgerPatch
    ) -> Optional[GoalRecord]:
        """Atomically checkpoint sparse operational state before the next call."""

        if not self.task_ledger_enabled:
            goal = self.active_goal
            return goal.model_copy(deep=True) if goal is not None else None
        async with self._lock:
            goal = self.active_goal
            if goal is None:
                return None
            self._require_writable_store()
            if not apply_ledger_patch(goal.task_ledger, patch):
                return goal.model_copy(deep=True)
            summary = (
                patch.checkpoint_summary
                or patch.next_action
                or f"Task ledger advanced to {goal.task_ledger.current_phase}."
            )
            self._append_evidence(goal, "ledger_checkpoint", summary)
            self._touch(goal)
            self._save()
            return goal.model_copy(deep=True)

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
            evidence_id = self._append_evidence(
                goal, "tool_result", goal.last_result
            )
            if self.task_ledger_enabled:
                result_by_action_id: dict[str, str] = {}
                for block in re.split(r"(?m)(?=^\* )", result):
                    match = re.search(
                        r"\[action_id=([^;\]]+);", block, flags=re.IGNORECASE
                    )
                    if match:
                        result_by_action_id[match.group(1).strip()] = block.strip()
                statuses = re.findall(
                    r"\[action_id=([^;\]]+);\s*status=(success|failed);",
                    result,
                    flags=re.IGNORECASE,
                )
                status_by_id = {
                    action_id.strip(): status.casefold()
                    for action_id, status in statuses
                }
                batch: list[LedgerActionOutcome] = []
                automatic_failures: list[LedgerFailurePatch] = []
                automatic_tool_state: list[str] = []
                for index, action in enumerate(normalized_actions):
                    action_id = self._bounded(
                        action.get("action_id") or f"action_{index + 1}", 200
                    )
                    tool = self._bounded(action["tool_name"], 300)
                    status = status_by_id.get(action_id, "unknown")
                    result_block = result_by_action_id.get(action_id, result)
                    batch.append(
                        LedgerActionOutcome(
                            action_id=action_id,
                            tool=tool,
                            status=status,
                            evidence_id=evidence_id,
                        )
                    )
                    fingerprint = self._action_fingerprint(action, status)
                    if fingerprint is not None:
                        goal.recent_action_fingerprints.append(fingerprint)
                    automatic_tool_state.extend(
                        self._automatic_tool_state(
                            action,
                            result_block,
                            status,
                        )
                    )
                    if status == "failed":
                        automatic_failures.append(
                            LedgerFailurePatch(
                                action=f"{tool} ({action_id})",
                                reason=self._bounded(
                                    result_block,
                                    700,
                                ),
                                retry_when=(
                                    "Only after new evidence, changed inputs, "
                                    "or an explicit recovery condition."
                                ),
                            )
                        )
                goal.recent_action_fingerprints = goal.recent_action_fingerprints[
                    -self.MAX_ACTION_FINGERPRINTS :
                ]
                ledger_before = goal.task_ledger.model_dump(
                    mode="json", exclude={"updated_at", "revision"}
                )
                goal.task_ledger.last_action_batch = batch[-20:]
                patch_changed = False
                if automatic_failures or automatic_tool_state:
                    patch_changed = apply_ledger_patch(
                        goal.task_ledger,
                        TaskLedgerPatch(
                            failures_add=automatic_failures,
                            tool_state_add=automatic_tool_state,
                        ),
                    )
                ledger_after = goal.task_ledger.model_dump(
                    mode="json", exclude={"updated_at", "revision"}
                )
                if ledger_before != ledger_after and not patch_changed:
                    goal.task_ledger.revision += 1
                    goal.task_ledger.updated_at = time.time()
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
            provider_prompt = token_value("provider_prompt_tokens")
            estimated = token_value("estimated_input_tokens") + token_value(
                "estimated_output_tokens"
            )
            goal.provider_tokens += provider
            goal.estimated_tokens += estimated
            goal.accounted_tokens += provider or estimated
            goal.last_provider_prompt_tokens = provider_prompt
            if (
                self.server_side_conversation
                and
                self.provider_rebase_prompt_tokens > 0
                and provider_prompt >= self.provider_rebase_prompt_tokens
                and goal.last_rebased_lane_epoch != goal.lane_epoch
            ):
                previous_epoch = goal.lane_epoch
                goal.lane_epoch += 1
                goal.last_rebased_lane_epoch = previous_epoch
                goal.context_rebase_count += 1
                self._append_evidence(
                    goal,
                    "context_rebase",
                    (
                        "Provider context reached "
                        f"{provider_prompt} prompt tokens; continuing from the "
                        "local Task Ledger in a fresh provider session."
                    ),
                )
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
        stable_evidence = [
            item for item in goal.evidence if item.get("kind") != "tool_result"
        ]
        evidence = "\n".join(
            f"- [{item['id']}] {item['kind']}: "
            f"{self._bounded(item['summary'], 300)}"
            for item in stable_evidence[-5:]
        ) or "- No durable evidence recorded yet."
        wake = (
            "event only"
            if goal.next_wakeup_at is None
            else time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(goal.next_wakeup_at)
            )
        )
        ledger = self._task_ledger_context(goal)
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
* Provider context: last_prompt={goal.last_provider_prompt_tokens}, rebases={goal.context_rebase_count}

### Objective
{self._bounded(goal.objective, 3000)}

{ledger}

### Latest durable evidence
{evidence}

### Latest bounded result
{self._bounded(goal.last_result, 2500) or "No tool result recorded yet."}

### Goal protocol
Keep working until the objective is actually achieved. For a compact response,
execute_skill arguments may use Goal Protocol v2:
`{{"v":2,"state":"act","calls":[{{"tool":"Exact.skill","args":{{}}}}],"note":"short"}}`.
Terminal states are `done`, `wait`, and `blocked`; include a concrete summary.
For every Goal response, include a sparse `ledger` checkpoint. Record only
evidence-backed facts, current phase, unfinished steps, failed approaches and
the exact next action. Omitted ledger fields remain unchanged; do not replay
the full ledger. Example:
`"ledger":{{"phase":"verify","completed_add":["patch applied"],"pending_steps":["run tests"],"next_action":"run the focused test"}}`.
When new evidence disproves durable state, correct it immediately with
`facts_remove`, `completed_remove`, or an authoritative replacement using
`confirmed_facts` / `completed_steps`. Persist exact discovered MCP schemas and
owned process-session IDs in `tool_state_add`; do not repeat catalog discovery
that is already represented there.
`wait` may include `wake_after_seconds`. Do not report `done` until durable
evidence satisfies the objective. For coding goals, inspect the exact diff,
run the smallest relevant test first, then the repository verification gate.
A passing narrow test is evidence, not proof that the full goal is complete.
""".strip()

    def _task_ledger_context(self, goal: GoalRecord) -> str:
        if not self.task_ledger_enabled:
            return "### Task Ledger\nDisabled by configuration."
        ledger = goal.task_ledger

        def items(values: list[str], limit: int) -> str:
            selected = values[-limit:]
            return (
                "\n".join(f"- {self._bounded(item, 400)}" for item in selected)
                or "- none"
            )

        failures = "\n".join(
            (
                f"- {self._bounded(item.action, 220)}: "
                f"{self._bounded(item.reason, 300)}"
                + (
                    f" | retry when: {self._bounded(item.retry_when, 220)}"
                    if item.retry_when
                    else ""
                )
            )
            for item in ledger.failed_attempts[-5:]
        ) or "- none"
        last_batch = "\n".join(
            f"- {item.tool} [{item.action_id}]: {item.status}; "
            f"evidence={item.evidence_id}"
            for item in ledger.last_action_batch[-8:]
        ) or "- none"
        # Put recovery-critical state in an independently bounded prefix.
        # Optional sections are admitted in priority order, so truncation never
        # removes both the exact next action and the newest tool outcomes.
        projection = f"""
### Local Task Ledger (authoritative after provider session reset)
* Ledger revision: {ledger.revision}
* Current phase: {self._bounded(ledger.current_phase, 160)}
* Checkpoint: {self._bounded(ledger.checkpoint_summary, 350) or "none"}
* Exact next action: {self._bounded(ledger.next_action, 500) or "not recorded"}

#### Last action batch — reconcile before repeating work
{self._bounded(last_batch, 600)}
""".strip()
        optional_sections = [
            ("Pending stages", items(ledger.pending_steps, 7)),
            ("Current blockers", items(ledger.blockers, 5)),
            (
                "Failed approaches — do not repeat without the retry condition",
                failures,
            ),
            ("Acceptance criteria", items(ledger.acceptance_criteria, 6)),
            ("Confirmed facts", items(ledger.confirmed_facts, 8)),
            ("Known tool/session state", items(ledger.tool_state, 7)),
            ("Completed stages", items(ledger.completed_steps, 7)),
            ("Artifacts", items(ledger.artifacts, 6)),
            ("Active hypotheses", items(ledger.hypotheses, 5)),
        ]
        for title, content in optional_sections:
            section = f"\n\n#### {title}\n{content}"
            remaining = self.task_ledger_max_chars - len(projection)
            if remaining <= 0:
                break
            if len(section) <= remaining:
                projection += section
                continue
            if remaining >= 120:
                projection += self._bounded(section, remaining)
            break
        return projection
