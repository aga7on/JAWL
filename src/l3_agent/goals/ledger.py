"""Compact, durable execution checkpoint for a long-running Goal."""

from __future__ import annotations

import hashlib
import time
from typing import Any, Optional

from pydantic import BaseModel, Field, model_validator


_PATCH_LIST_LIMITS = {
    "acceptance_criteria": 20,
    "completed_steps": 20,
    "completed_add": 20,
    "completed_remove": 20,
    "pending_steps": 20,
    "confirmed_facts": 24,
    "facts_add": 20,
    "facts_remove": 24,
    "hypotheses": 12,
    "failures": 16,
    "failures_remove": 16,
    "failures_add": 12,
    "artifacts": 20,
    "artifacts_add": 20,
    "artifacts_remove": 20,
    "tool_state": 24,
    "tool_state_add": 20,
    "tool_state_remove": 24,
    "blockers": 12,
}
_STRING_COLLECTIONS = set(_PATCH_LIST_LIMITS) - {
    "failures",
    "failures_add",
}
_TOOL_STATE_COLLECTIONS = {
    "tool_state",
    "tool_state_add",
    "tool_state_remove",
}


class LedgerFailurePatch(BaseModel):
    """One failed approach and the condition under which retry is useful."""

    action: str = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=1, max_length=1000)
    retry_when: str = Field(default="", max_length=500)


class LedgerFailure(LedgerFailurePatch):
    failure_id: str
    at: float


class LedgerActionOutcome(BaseModel):
    """Bounded pointer from the ledger to the full durable action evidence."""

    action_id: str = Field(max_length=200)
    tool: str = Field(max_length=300)
    status: str = Field(max_length=40)
    evidence_id: str = Field(max_length=64)


class TaskLedgerPatch(BaseModel):
    """Sparse model-authored update; omitted fields keep their current value."""

    phase: Optional[str] = Field(default=None, max_length=300)
    acceptance_criteria: Optional[list[str]] = Field(
        default=None, max_length=20
    )
    completed_steps: Optional[list[str]] = Field(default=None, max_length=20)
    completed_add: list[str] = Field(default_factory=list, max_length=20)
    completed_remove: list[str] = Field(default_factory=list, max_length=20)
    pending_steps: Optional[list[str]] = Field(default=None, max_length=20)
    confirmed_facts: Optional[list[str]] = Field(default=None, max_length=24)
    facts_add: list[str] = Field(default_factory=list, max_length=20)
    facts_remove: list[str] = Field(default_factory=list, max_length=24)
    hypotheses: Optional[list[str]] = Field(default=None, max_length=12)
    failures: Optional[list[LedgerFailurePatch]] = Field(
        default=None, max_length=16
    )
    failures_remove: list[str] = Field(default_factory=list, max_length=16)
    failures_add: list[LedgerFailurePatch] = Field(
        default_factory=list, max_length=12
    )
    artifacts: Optional[list[str]] = Field(default=None, max_length=20)
    artifacts_add: list[str] = Field(default_factory=list, max_length=20)
    artifacts_remove: list[str] = Field(default_factory=list, max_length=20)
    tool_state: Optional[list[str]] = Field(default=None, max_length=24)
    tool_state_add: list[str] = Field(default_factory=list, max_length=20)
    tool_state_remove: list[str] = Field(default_factory=list, max_length=24)
    blockers: Optional[list[str]] = Field(default=None, max_length=12)
    next_action: Optional[str] = Field(default=None, max_length=1000)
    checkpoint_summary: str = Field(default="", max_length=1200)

    @model_validator(mode="before")
    @classmethod
    def _repair_bounded_collections(cls, value: Any) -> Any:
        """Repair common Qwen shorthands without letting a patch kill a cycle.

        The JSON schema advertises finite collection sizes, but web-model output
        and automatic MCP catalog checkpointing can still exceed them. Ledger
        state is deliberately bounded, so retain the highest-ranked/first items
        instead of raising after useful actions have already completed.
        """

        if not isinstance(value, dict):
            return value
        payload = dict(value)
        for field, limit in _PATCH_LIST_LIMITS.items():
            if field not in payload:
                continue
            collection = payload[field]
            if isinstance(collection, str) and field in _STRING_COLLECTIONS:
                collection = [collection] if collection.strip() else []
            elif (
                isinstance(collection, dict)
                and field in _TOOL_STATE_COLLECTIONS
            ):
                collection = [
                    f"{key} | schema={item}"
                    for key, item in collection.items()
                    if str(key).strip() and str(item).strip()
                ]
            elif (
                isinstance(collection, dict)
                and field in {"failures", "failures_add"}
            ):
                collection = [collection]
            if isinstance(collection, (list, tuple)):
                payload[field] = list(collection)[:limit]
        return payload


class TaskLedger(BaseModel):
    """Authoritative operational state that survives provider chat loss."""

    version: int = 1
    revision: int = Field(default=0, ge=0)
    current_phase: str = Field(default="initial", max_length=300)
    acceptance_criteria: list[str] = Field(default_factory=list)
    completed_steps: list[str] = Field(default_factory=list)
    pending_steps: list[str] = Field(default_factory=list)
    confirmed_facts: list[str] = Field(default_factory=list)
    hypotheses: list[str] = Field(default_factory=list)
    failed_attempts: list[LedgerFailure] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    tool_state: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    next_action: str = Field(default="", max_length=1000)
    checkpoint_summary: str = Field(default="", max_length=1200)
    last_action_batch: list[LedgerActionOutcome] = Field(default_factory=list)
    updated_at: Optional[float] = None


_LIMITS = {
    "acceptance_criteria": (12, 400),
    "completed_steps": (20, 400),
    "pending_steps": (16, 400),
    "confirmed_facts": (24, 500),
    "hypotheses": (10, 500),
    "artifacts": (20, 500),
    "tool_state": (24, 500),
    "blockers": (10, 500),
}


def _bounded(value: object, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 18)] + "...[truncated]"


def _dedupe(values: list[str], *, count: int, chars: int) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _bounded(value, chars)
        key = " ".join(text.casefold().split())
        if not text or key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result[-count:]


def _merge(current: list[str], additions: list[str], field: str) -> list[str]:
    count, chars = _LIMITS[field]
    if field == "tool_state":
        # Automatically produced tool-state entries use a stable key before
        # the first separator. Replace that key instead of accumulating stale
        # schemas/session observations forever.
        keyed: dict[str, str] = {}
        unkeyed: list[str] = []
        for value in [*current, *additions]:
            text = _bounded(value, chars)
            if not text:
                continue
            key, separator, _ = text.partition(" | ")
            if separator and key.strip():
                keyed[" ".join(key.casefold().split())] = text
            else:
                unkeyed.append(text)
        return _dedupe([*unkeyed, *keyed.values()], count=count, chars=chars)
    return _dedupe([*current, *additions], count=count, chars=chars)


def _replace(values: list[str], field: str) -> list[str]:
    count, chars = _LIMITS[field]
    return _dedupe(values, count=count, chars=chars)


def _remove(current: list[str], removals: list[str], field: str) -> list[str]:
    """Remove exact normalized items from one bounded ledger collection."""

    if not removals:
        return current
    removal_keys = {
        " ".join(_bounded(item, _LIMITS[field][1]).casefold().split())
        for item in removals
        if str(item or "").strip()
    }
    return [
        item
        for item in current
        if " ".join(item.casefold().split()) not in removal_keys
    ]


def apply_ledger_patch(ledger: TaskLedger, patch: TaskLedgerPatch) -> bool:
    """Apply a sparse bounded patch and return whether durable state changed."""

    before = ledger.model_dump(mode="json", exclude={"updated_at", "revision"})
    fields_set = patch.model_fields_set

    if "phase" in fields_set:
        ledger.current_phase = _bounded(patch.phase, 300) or "unspecified"
    if "acceptance_criteria" in fields_set:
        ledger.acceptance_criteria = _replace(
            patch.acceptance_criteria or [], "acceptance_criteria"
        )
    if "pending_steps" in fields_set:
        ledger.pending_steps = _replace(
            patch.pending_steps or [], "pending_steps"
        )
    if "completed_steps" in fields_set:
        ledger.completed_steps = _replace(
            patch.completed_steps or [], "completed_steps"
        )
    if patch.completed_remove:
        ledger.completed_steps = _remove(
            ledger.completed_steps, patch.completed_remove, "completed_steps"
        )
    if patch.completed_add:
        ledger.completed_steps = _merge(
            ledger.completed_steps, patch.completed_add, "completed_steps"
        )
        completed_keys = {
            " ".join(item.casefold().split()) for item in ledger.completed_steps
        }
        ledger.pending_steps = [
            item
            for item in ledger.pending_steps
            if " ".join(item.casefold().split()) not in completed_keys
        ]
    if "confirmed_facts" in fields_set:
        ledger.confirmed_facts = _replace(
            patch.confirmed_facts or [], "confirmed_facts"
        )
    if patch.facts_remove:
        ledger.confirmed_facts = _remove(
            ledger.confirmed_facts, patch.facts_remove, "confirmed_facts"
        )
    if patch.facts_add:
        ledger.confirmed_facts = _merge(
            ledger.confirmed_facts, patch.facts_add, "confirmed_facts"
        )
    if "hypotheses" in fields_set:
        ledger.hypotheses = _replace(patch.hypotheses or [], "hypotheses")
    if "failures" in fields_set:
        ledger.failed_attempts = []
        for failure in patch.failures or []:
            action = _bounded(failure.action, 500)
            reason = _bounded(failure.reason, 1000)
            retry_when = _bounded(failure.retry_when, 500)
            if not action:
                continue
            ledger.failed_attempts.append(
                LedgerFailure(
                    failure_id=hashlib.sha256(
                        f"{action}\0{reason}\0{retry_when}".encode("utf-8")
                    ).hexdigest()[:16],
                    action=action,
                    reason=reason,
                    retry_when=retry_when,
                    at=time.time(),
                )
            )
        ledger.failed_attempts = ledger.failed_attempts[-16:]
    if patch.failures_remove:
        removal_keys = {
            " ".join(str(item).casefold().split())
            for item in patch.failures_remove
            if str(item).strip()
        }
        ledger.failed_attempts = [
            item
            for item in ledger.failed_attempts
            if " ".join(item.action.casefold().split()) not in removal_keys
        ]
    if patch.failures_add:
        known = {item.failure_id for item in ledger.failed_attempts}
        for failure in patch.failures_add:
            action = _bounded(failure.action, 500)
            reason = _bounded(failure.reason, 1000)
            retry_when = _bounded(failure.retry_when, 500)
            failure_id = hashlib.sha256(
                f"{action}\0{reason}\0{retry_when}".encode("utf-8")
            ).hexdigest()[:16]
            if failure_id in known:
                continue
            known.add(failure_id)
            ledger.failed_attempts.append(
                LedgerFailure(
                    failure_id=failure_id,
                    action=action,
                    reason=reason,
                    retry_when=retry_when,
                    at=time.time(),
                )
            )
        ledger.failed_attempts = ledger.failed_attempts[-16:]
    if "artifacts" in fields_set:
        ledger.artifacts = _replace(patch.artifacts or [], "artifacts")
    if patch.artifacts_remove:
        ledger.artifacts = _remove(
            ledger.artifacts, patch.artifacts_remove, "artifacts"
        )
    if patch.artifacts_add:
        ledger.artifacts = _merge(
            ledger.artifacts, patch.artifacts_add, "artifacts"
        )
    if "tool_state" in fields_set:
        ledger.tool_state = _replace(patch.tool_state or [], "tool_state")
    if patch.tool_state_remove:
        ledger.tool_state = _remove(
            ledger.tool_state, patch.tool_state_remove, "tool_state"
        )
    if patch.tool_state_add:
        ledger.tool_state = _merge(
            ledger.tool_state, patch.tool_state_add, "tool_state"
        )
    if "blockers" in fields_set:
        ledger.blockers = _replace(patch.blockers or [], "blockers")
    if "next_action" in fields_set:
        ledger.next_action = _bounded(patch.next_action, 1000)
    if "checkpoint_summary" in fields_set:
        ledger.checkpoint_summary = _bounded(patch.checkpoint_summary, 1200)

    after = ledger.model_dump(mode="json", exclude={"updated_at", "revision"})
    if before == after:
        return False
    ledger.revision += 1
    ledger.updated_at = time.time()
    return True
