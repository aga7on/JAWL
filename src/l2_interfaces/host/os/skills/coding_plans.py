"""Persistent requirement and execution plans for task-scoped coding work."""

from __future__ import annotations

import json
import hashlib
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from src.l2_interfaces.host.os.client import HostOSAccessLevel, HostOSClient
from src.l2_interfaces.host.os.decorators import require_access
from src.l2_interfaces.host.os.skills.coding_workspaces import (
    HostOSCodingWorkspaces,
)
from src.l3_agent.skills.registry import SkillResult, skill
from src.l3_agent.swarm.roles import Subagents
from src.utils._tools import redact_sensitive_text
from src.utils.tracing import current_trace


class HostOSCodingPlans:
    """Owns the durable plan embedded in each coding workspace record."""

    _ITEM_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")
    _FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")
    _STEP_STATES = {"pending", "in_progress", "completed", "blocked"}
    _REQUIREMENT_STATES = {"pending", "satisfied", "blocked"}

    def __init__(
        self,
        host_os_client: HostOSClient,
        workspaces: HostOSCodingWorkspaces,
    ) -> None:
        self.host_os = host_os_client
        self.workspaces = workspaces

    @staticmethod
    def _bounded_text(value: str, field: str, max_chars: int) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} must be a non-empty string.")
        clean = redact_sensitive_text(value.strip())
        if len(clean) > max_chars:
            raise ValueError(f"{field} cannot exceed {max_chars} characters.")
        return clean

    @classmethod
    def _item_id(cls, value: Any, field: str) -> str:
        if not isinstance(value, str) or not cls._ITEM_ID.fullmatch(value):
            raise ValueError(
                f"{field} must be 1-63 characters using letters, digits, '_' or '-'."
            )
        return value

    @staticmethod
    def _assert_acyclic(steps: List[Dict[str, Any]]) -> None:
        dependencies = {step["id"]: set(step["depends_on"]) for step in steps}
        visiting: Set[str] = set()
        visited: Set[str] = set()

        def visit(step_id: str) -> None:
            if step_id in visiting:
                raise ValueError("Coding plan contains a dependency cycle.")
            if step_id in visited:
                return
            visiting.add(step_id)
            for dependency in dependencies[step_id]:
                visit(dependency)
            visiting.remove(step_id)
            visited.add(step_id)

        for identifier in dependencies:
            visit(identifier)

    @classmethod
    def _normalize_steps(cls, raw_steps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not isinstance(raw_steps, list) or not 1 <= len(raw_steps) <= 50:
            raise ValueError("steps must contain between 1 and 50 items.")
        normalized = []
        identifiers: Set[str] = set()
        allowed = {"id", "title", "depends_on"}
        for index, raw in enumerate(raw_steps):
            if not isinstance(raw, dict):
                raise ValueError(f"steps[{index}] must be an object.")
            unknown = set(raw) - allowed
            if unknown:
                raise ValueError(
                    f"steps[{index}] has unsupported fields: {', '.join(sorted(unknown))}."
                )
            identifier = cls._item_id(raw.get("id"), f"steps[{index}].id")
            if identifier in identifiers:
                raise ValueError(f"Duplicate coding step id: {identifier}")
            identifiers.add(identifier)
            dependencies = raw.get("depends_on", [])
            if not isinstance(dependencies, list) or not all(
                isinstance(item, str) for item in dependencies
            ):
                raise ValueError(f"steps[{index}].depends_on must be a string list.")
            normalized.append(
                {
                    "id": identifier,
                    "title": cls._bounded_text(
                        raw.get("title", ""), f"steps[{index}].title", 1000
                    ),
                    "depends_on": list(dict.fromkeys(dependencies)),
                    "status": "pending",
                    "evidence": "",
                    "updated_at": None,
                    "completed_at": None,
                }
            )
        for step in normalized:
            missing = set(step["depends_on"]) - identifiers
            if missing:
                raise ValueError(
                    f"Step '{step['id']}' has unknown dependencies: "
                    + ", ".join(sorted(missing))
                    + "."
                )
            if step["id"] in step["depends_on"]:
                raise ValueError(f"Step '{step['id']}' cannot depend on itself.")
        cls._assert_acyclic(normalized)
        return normalized

    @classmethod
    def _normalize_requirements(cls, raw: List[str]) -> List[Dict[str, Any]]:
        if not isinstance(raw, list) or not 1 <= len(raw) <= 50:
            raise ValueError("requirements must contain between 1 and 50 items.")
        return [
            {
                "id": f"req_{index + 1}",
                "text": cls._bounded_text(value, f"requirements[{index}]", 1000),
                "status": "pending",
                "evidence": "",
                "updated_at": None,
            }
            for index, value in enumerate(raw)
        ]

    @staticmethod
    def _summary(plan: Dict[str, Any]) -> Dict[str, Any]:
        steps = plan["steps"]
        requirements = plan["requirements"]
        completed_steps = sum(step["status"] == "completed" for step in steps)
        satisfied_requirements = sum(
            item["status"] == "satisfied" for item in requirements
        )
        replanning = plan.get("replanning", {})
        return {
            "completed_steps": completed_steps,
            "total_steps": len(steps),
            "satisfied_requirements": satisfied_requirements,
            "total_requirements": len(requirements),
            "blocked": any(step["status"] == "blocked" for step in steps)
            or any(item["status"] == "blocked" for item in requirements),
            "replan_required": bool(replanning.get("required")),
            "replan_reason_count": len(replanning.get("reasons", [])),
            "ready_for_commit": completed_steps == len(steps)
            and satisfied_requirements == len(requirements)
            and not bool(replanning.get("required")),
        }

    @staticmethod
    def _replanning_state(plan: Dict[str, Any]) -> Dict[str, Any]:
        return plan.setdefault(
            "replanning",
            {
                "required": False,
                "generation": 0,
                "reasons": [],
                "last_applied_at": None,
            },
        )

    def _mark_replan_required(
        self,
        plan: Dict[str, Any],
        *,
        trigger: str,
        evidence: str,
        source_id: str,
        workspace_fingerprint: str = "",
        append_history: bool = False,
    ) -> bool:
        """Record one idempotent, bounded reason to revisit the remaining graph."""

        clean_trigger = self._item_id(trigger, "trigger")
        clean_source = self._bounded_text(source_id, "source_id", 200)
        clean_evidence = self._bounded_text(evidence, "evidence", 1000)
        if workspace_fingerprint and not self._FINGERPRINT.fullmatch(
            workspace_fingerprint
        ):
            raise ValueError("workspace_fingerprint must be a SHA-256 value.")
        state = self._replanning_state(plan)
        reason_id = hashlib.sha256(
            f"{clean_trigger}\0{clean_source}".encode("utf-8")
        ).hexdigest()[:16]
        if any(item.get("reason_id") == reason_id for item in state["reasons"]):
            return False
        if not state["required"]:
            state["generation"] = int(state.get("generation", 0)) + 1
        now = self.workspaces._utc_now()
        state["required"] = True
        state["reasons"].append(
            {
                "reason_id": reason_id,
                "trigger": clean_trigger,
                "source_id": clean_source,
                "evidence": clean_evidence,
                "workspace_fingerprint": workspace_fingerprint,
                "created_at": now,
                "trace": current_trace(),
            }
        )
        state["reasons"] = state["reasons"][-20:]
        if append_history:
            self._append_history(
                plan,
                "replan_required",
                {
                    "reason_id": reason_id,
                    "trigger": clean_trigger,
                    "source_id": clean_source,
                },
            )
        return True

    def mark_verification_replan_required(
        self, entry: Dict[str, Any], run: Dict[str, Any]
    ) -> bool:
        """Attach a failed final verification to an existing plan under its lock."""

        plan = entry.get("task_plan")
        state = str(run.get("state", "error"))
        if not plan or state == "passed":
            return False
        return self._mark_replan_required(
            plan,
            trigger="verification_failure",
            source_id=f"verification:{run.get('run_id', 'unknown')}",
            evidence=f"Verification finished in state '{state}'.",
            workspace_fingerprint=str(run.get("fingerprint_after", "")),
            append_history=True,
        )

    @classmethod
    def _view_payload(
        cls,
        plan: Dict[str, Any],
        section: str = "summary",
        offset: int = 0,
        limit: int = 20,
    ) -> Dict[str, Any]:
        if section not in {"summary", "steps", "requirements", "history"}:
            raise ValueError(
                "section must be summary, steps, requirements, or history."
            )
        if offset < 0:
            raise ValueError("offset must be zero or greater.")
        if limit < 1 or limit > 50:
            raise ValueError("limit must be between 1 and 50.")
        payload: Dict[str, Any] = {
            "plan_id": plan["plan_id"],
            "revision": plan["revision"],
            "objective": plan["objective"],
            "requires_diff_review": bool(
                plan.get("requires_diff_review", False)
            ),
            "created_at": plan["created_at"],
            "updated_at": plan["updated_at"],
            "summary": cls._summary(plan),
            "replanning": {
                "required": bool(plan.get("replanning", {}).get("required")),
                "generation": int(
                    plan.get("replanning", {}).get("generation", 0)
                ),
                "latest_reasons": [
                    {
                        "reason_id": item.get("reason_id"),
                        "trigger": item.get("trigger"),
                        "source_id": item.get("source_id"),
                        "evidence": str(item.get("evidence", ""))[:200],
                        "created_at": item.get("created_at"),
                    }
                    for item in plan.get("replanning", {}).get("reasons", [])[-5:]
                ],
                "last_applied_at": plan.get("replanning", {}).get(
                    "last_applied_at"
                ),
            },
        }
        if section == "summary":
            payload["requirements"] = [
                {
                    "id": item["id"],
                    "status": item["status"],
                    "text": item["text"][:200],
                }
                for item in plan["requirements"]
            ]
            payload["steps"] = [
                {
                    "id": step["id"],
                    "status": step["status"],
                    "title": step["title"][:200],
                    "depends_on": step["depends_on"],
                    **(
                        {"delegation_status": step["delegation_status"]}
                        if step.get("delegation_status")
                        else {}
                    ),
                    **(
                        {
                            "latest_delegation_id": step["delegations"][-1][
                                "delegation_id"
                            ]
                        }
                        if step.get("delegations")
                        else {}
                    ),
                }
                for step in plan["steps"]
            ]
            return payload

        items = plan[section]
        page = items[offset : offset + limit]
        payload.update(
            {
                "section": section,
                "offset": offset,
                "limit": limit,
                "total_items": len(items),
                "has_more": offset + len(page) < len(items),
                "items": page,
            }
        )
        return payload

    @staticmethod
    def _check_revision(plan: Dict[str, Any], expected_revision: Optional[int]) -> None:
        if expected_revision is not None and expected_revision != plan["revision"]:
            raise ValueError(
                "Stale coding plan revision: expected "
                f"{expected_revision}, current revision is {plan['revision']}. "
                "Read the plan again before updating it."
            )

    def _append_history(
        self, plan: Dict[str, Any], event: str, details: Dict[str, Any]
    ) -> None:
        plan["updated_at"] = self.workspaces._utc_now()
        plan["revision"] += 1
        plan["history"].append(
            {
                "event": event,
                "revision": plan["revision"],
                "time": plan["updated_at"],
                "details": details,
                "trace": current_trace(),
            }
        )
        plan["history"] = plan["history"][-200:]

    async def bind_delegation(
        self,
        *,
        task_id: str,
        step_id: str,
        delegation_id: str,
        role: str,
        expected_revision: int,
    ) -> Dict[str, Any]:
        """Atomically bind a persisted worker to one exact plan revision/step."""

        task_id = self.workspaces._validate_task_id(task_id)
        step_id = self._item_id(step_id, "step_id")
        delegation_id = self._item_id(delegation_id, "delegation_id")
        role = self._item_id(role, "role")
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool):
            raise ValueError("expected_revision must be an integer.")
        async with self.workspaces._lock:
            registry = self.workspaces._load_registry()
            entry = self.workspaces._get_entry(registry, task_id)
            plan = entry.get("task_plan")
            if not plan:
                raise ValueError("Coding task has no initialized plan.")
            self._check_revision(plan, expected_revision)
            by_id = {step["id"]: step for step in plan["steps"]}
            target = by_id.get(step_id)
            if target is None:
                raise ValueError(f"Coding step '{step_id}' was not found.")
            if target["status"] not in {"pending", "in_progress"}:
                raise ValueError(
                    f"Coding step '{step_id}' cannot be delegated while "
                    f"{target['status']}."
                )
            incomplete = [
                dependency
                for dependency in target["depends_on"]
                if by_id[dependency]["status"] != "completed"
            ]
            if incomplete:
                raise ValueError(
                    "Step dependencies are incomplete: " + ", ".join(incomplete)
                )
            delegations = target.setdefault("delegations", [])
            if any(
                item.get("status") in {"queued", "running", "reported"}
                for item in delegations
            ):
                raise ValueError("Coding step already has active delegated work.")
            if len(delegations) >= 20:
                raise ValueError("Coding step delegation history reached its limit.")
            _, workspace = self.workspaces._entry_paths(entry)
            if not workspace.is_dir():
                raise FileNotFoundError("Coding workspace directory is missing.")
            fingerprint = await self.workspaces.workspace_fingerprint(workspace)
            now = self.workspaces._utc_now()
            target["status"] = "in_progress"
            target["delegation_status"] = "queued"
            target["updated_at"] = now
            target["completed_at"] = None
            delegations.append(
                {
                    "delegation_id": delegation_id,
                    "role": role,
                    "status": "queued",
                    "base_workspace_fingerprint": fingerprint["fingerprint"],
                    "base_head": fingerprint["head"],
                    "created_at": now,
                    "updated_at": now,
                }
            )
            self._append_history(
                plan,
                "delegation_bound",
                {
                    "step_id": step_id,
                    "delegation_id": delegation_id,
                    "role": role,
                },
            )
            self.workspaces._save_registry(registry)
            return {
                "bound_revision": plan["revision"],
                "base_workspace_fingerprint": fingerprint["fingerprint"],
            }

    def _validated_report(self, report_path: str) -> tuple[Path, str]:
        if not report_path:
            raise ValueError("Completed delegation has no persisted report.")
        candidate = (self.host_os.framework_dir / report_path).resolve()
        reports_root = (self.host_os.system_dir / "subagents").resolve()
        if not candidate.is_relative_to(reports_root) or not candidate.is_file():
            raise ValueError("Delegation report path is missing or outside report storage.")
        with candidate.open("rb") as stream:
            contents = stream.read(1024 * 1024 + 1)
        if len(contents) > 1024 * 1024:
            raise ValueError("Delegation report exceeds the 1 MiB reconciliation limit.")
        digest = hashlib.sha256(contents).hexdigest()
        return candidate, digest

    async def record_delegation_result(
        self,
        *,
        task_id: str,
        step_id: str,
        delegation_id: str,
        status: str,
        detail: str = "",
        report_path: str = "",
    ) -> Dict[str, Any]:
        """Attach a terminal worker result without declaring plan work complete."""

        if status not in {"completed", "failed", "cancelled", "interrupted"}:
            raise ValueError("Delegation result status is unsupported.")
        task_id = self.workspaces._validate_task_id(task_id)
        step_id = self._item_id(step_id, "step_id")
        delegation_id = self._item_id(delegation_id, "delegation_id")
        clean_detail = (
            self._bounded_text(detail, "detail", 1000) if detail.strip() else ""
        )
        normalized_report = ""
        report_sha256 = ""
        if status == "completed":
            report, report_sha256 = self._validated_report(report_path)
            normalized_report = report.relative_to(self.host_os.framework_dir).as_posix()
        async with self.workspaces._lock:
            registry = self.workspaces._load_registry()
            entry = self.workspaces._get_entry(registry, task_id)
            plan = entry.get("task_plan")
            if not plan:
                raise ValueError("Coding task has no initialized plan.")
            target = next(
                (step for step in plan["steps"] if step["id"] == step_id), None
            )
            if target is None:
                raise ValueError(f"Coding step '{step_id}' was not found.")
            delegation = next(
                (
                    item
                    for item in target.get("delegations", [])
                    if item.get("delegation_id") == delegation_id
                ),
                None,
            )
            if delegation is None:
                raise ValueError("Delegation is not bound to this coding step.")
            current_status = delegation.get("status")
            reconciled_status = "reported" if status == "completed" else status
            if current_status == reconciled_status:
                return {**delegation, "revision": plan["revision"]}
            if current_status not in {"queued", "running"}:
                raise ValueError("Delegation already has a terminal plan result.")
            _, workspace = self.workspaces._entry_paths(entry)
            fingerprint = await self.workspaces.workspace_fingerprint(workspace)
            now = self.workspaces._utc_now()
            delegation.update(
                {
                    "status": reconciled_status,
                    "updated_at": now,
                    "finished_at": now,
                    "result_workspace_fingerprint": fingerprint["fingerprint"],
                    "result_head": fingerprint["head"],
                    **({"detail": clean_detail} if clean_detail else {}),
                    **({"report_path": normalized_report} if normalized_report else {}),
                    **({"report_sha256": report_sha256} if report_sha256 else {}),
                }
            )
            target["delegation_status"] = reconciled_status
            target["updated_at"] = now
            if reconciled_status in {"failed", "cancelled", "interrupted"}:
                target["status"] = "blocked"
                target["completed_at"] = None
                self._mark_replan_required(
                    plan,
                    trigger="delegation_failure",
                    source_id=f"delegation:{delegation_id}:{reconciled_status}",
                    evidence=clean_detail
                    or f"Delegation finished in state '{reconciled_status}'.",
                    workspace_fingerprint=fingerprint["fingerprint"],
                )
            self._append_history(
                plan,
                "delegation_result",
                {
                    "step_id": step_id,
                    "delegation_id": delegation_id,
                    "status": reconciled_status,
                },
            )
            self.workspaces._save_registry(registry)
            return {**delegation, "revision": plan["revision"]}

    @skill()
    @require_access(HostOSAccessLevel.SANDBOX)
    async def reconcile_coding_delegation(
        self,
        task_id: str,
        step_id: str,
        delegation_id: str,
        decision: str,
        evidence: str,
        expected_revision: int,
        require_verified: bool = True,
    ) -> SkillResult:
        """Accept or reject a reported worker result under exact-state guards."""

        try:
            if decision not in {"accept", "reject"}:
                raise ValueError("decision must be accept or reject.")
            task_id = self.workspaces._validate_task_id(task_id)
            step_id = self._item_id(step_id, "step_id")
            delegation_id = self._item_id(delegation_id, "delegation_id")
            clean_evidence = self._bounded_text(evidence, "evidence", 4000)
            async with self.workspaces._lock:
                registry = self.workspaces._load_registry()
                entry = self.workspaces._get_entry(registry, task_id)
                plan = entry.get("task_plan")
                if not plan:
                    return SkillResult.fail("Coding task has no initialized plan.")
                self._check_revision(plan, expected_revision)
                by_id = {step["id"]: step for step in plan["steps"]}
                target = by_id.get(step_id)
                if target is None:
                    return SkillResult.fail(f"Coding step '{step_id}' was not found.")
                delegation = next(
                    (
                        item
                        for item in target.get("delegations", [])
                        if item.get("delegation_id") == delegation_id
                    ),
                    None,
                )
                if delegation is None:
                    return SkillResult.fail("Delegation is not bound to this step.")
                _, workspace = self.workspaces._entry_paths(entry)
                current = await self.workspaces.workspace_fingerprint(workspace)
                if decision == "accept":
                    if delegation.get("status") != "reported":
                        return SkillResult.fail(
                            "Only a reported delegation can be accepted."
                        )
                    report, report_sha256 = self._validated_report(
                        delegation.get("report_path", "")
                    )
                    if report_sha256 != delegation.get("report_sha256"):
                        return SkillResult.fail(
                            "Delegation report changed after result recording."
                        )
                    if current["fingerprint"] != delegation.get(
                        "result_workspace_fingerprint"
                    ):
                        return SkillResult.fail(
                            "Workspace changed after the delegation report; inspect "
                            "and record a new result before acceptance."
                        )
                    verification = entry.get("last_verification")
                    verified = bool(
                        verification
                        and verification.get("state") == "passed"
                        and verification.get("fingerprint_after")
                        == current["fingerprint"]
                        and verification.get("head_before") == current["head"]
                    )
                    if require_verified and not verified:
                        return SkillResult.fail(
                            "Delegation acceptance requires successful verification "
                            "of the exact current workspace state."
                        )
                    incomplete = [
                        dependency
                        for dependency in target["depends_on"]
                        if by_id[dependency]["status"] != "completed"
                    ]
                    if incomplete:
                        return SkillResult.fail(
                            "Step dependencies are incomplete: " + ", ".join(incomplete)
                        )
                    target["status"] = "completed"
                    target["completed_at"] = self.workspaces._utc_now()
                    delegation["status"] = "accepted"
                    delegation["accepted_report"] = report.relative_to(
                        self.host_os.framework_dir
                    ).as_posix()
                else:
                    if delegation.get("status") not in {
                        "reported",
                        "failed",
                        "cancelled",
                        "interrupted",
                    }:
                        return SkillResult.fail(
                            "Only terminal delegated work can be rejected."
                        )
                    target["status"] = "blocked"
                    target["completed_at"] = None
                    delegation["status"] = "rejected"
                    self._mark_replan_required(
                        plan,
                        trigger="delegation_rejected",
                        source_id=f"delegation:{delegation_id}:rejected",
                        evidence=clean_evidence,
                        workspace_fingerprint=current["fingerprint"],
                    )
                now = self.workspaces._utc_now()
                target["evidence"] = clean_evidence
                target["delegation_status"] = delegation["status"]
                target["updated_at"] = now
                delegation["updated_at"] = now
                delegation["review_evidence"] = clean_evidence
                self._append_history(
                    plan,
                    "delegation_reconciled",
                    {
                        "step_id": step_id,
                        "delegation_id": delegation_id,
                        "decision": decision,
                        "require_verified": require_verified,
                    },
                )
                self.workspaces._save_registry(registry)
                payload = {**target, "revision": plan["revision"]}
            return SkillResult.ok(json.dumps(payload, ensure_ascii=False))
        except (PermissionError, FileNotFoundError, ValueError, KeyError) as exc:
            return SkillResult.fail(str(exc))
        except Exception as exc:
            return SkillResult.fail(f"Error reconciling coding delegation: {exc}")

    @skill(swarm=[Subagents.CODER, Subagents.QA_ENGINEER, Subagents.SYSADMIN])
    @require_access(HostOSAccessLevel.SANDBOX)
    async def initialize_coding_task_plan(
        self,
        task_id: str,
        objective: str,
        requirements: List[str],
        steps: List[Dict[str, Any]],
        replace: bool = False,
    ) -> SkillResult:
        """Create a durable requirement/step plan for an existing coding task.

        Step objects accept only ``id``, ``title``, and ``depends_on``. Replacing
        an existing plan is refused unless ``replace=true`` is explicit.
        """

        try:
            task_id = self.workspaces._validate_task_id(task_id)
            objective = self._bounded_text(objective, "objective", 4000)
            normalized_requirements = self._normalize_requirements(requirements)
            normalized_steps = self._normalize_steps(steps)
            async with self.workspaces._lock:
                registry = self.workspaces._load_registry()
                entry = self.workspaces._get_entry(registry, task_id)
                existing_plan = entry.get("task_plan")
                if existing_plan and not replace:
                    return SkillResult.fail(
                        "Coding task already has a plan. Read and update it, or "
                        "set replace=true explicitly."
                    )
                now = self.workspaces._utc_now()
                plan = {
                    "plan_id": uuid.uuid4().hex,
                    "revision": 1,
                    "objective": objective,
                    "requires_diff_review": True,
                    "requirements": normalized_requirements,
                    "steps": normalized_steps,
                    "created_at": now,
                    "updated_at": now,
                    "replanning": {
                        "required": False,
                        "generation": 0,
                        "reasons": [],
                        "last_applied_at": None,
                    },
                    "history": [
                        {
                            "event": "initialized",
                            "revision": 1,
                            "time": now,
                            "details": {"replaced_existing": bool(existing_plan)},
                            "trace": current_trace(),
                        }
                    ],
                }
                if existing_plan:
                    archive = entry.setdefault("task_plan_archive", [])
                    archive.append(
                        {
                            **existing_plan,
                            "archived_at": now,
                            "archive_reason": "explicit_replace",
                        }
                    )
                    entry["task_plan_archive"] = archive[-20:]
                entry["task_plan"] = plan
                self.workspaces._save_registry(registry)
            return SkillResult.ok(
                json.dumps(self._view_payload(plan), ensure_ascii=False)
            )
        except (PermissionError, FileNotFoundError, ValueError, KeyError) as exc:
            return SkillResult.fail(str(exc))
        except Exception as exc:
            return SkillResult.fail(f"Error initializing coding task plan: {exc}")

    @skill(swarm=[Subagents.CODER, Subagents.QA_ENGINEER, Subagents.SYSADMIN])
    @require_access(HostOSAccessLevel.SANDBOX)
    async def get_coding_task_plan(
        self,
        task_id: str,
        section: str = "summary",
        offset: int = 0,
        limit: int = 20,
    ) -> SkillResult:
        """Return a bounded plan summary or one page of steps/evidence/history."""

        try:
            task_id = self.workspaces._validate_task_id(task_id)
            async with self.workspaces._lock:
                entry = self.workspaces._get_entry(
                    self.workspaces._load_registry(), task_id
                )
                plan = entry.get("task_plan")
                if not plan:
                    return SkillResult.fail("Coding task has no initialized plan.")
                payload = self._view_payload(plan, section, offset, limit)
            return SkillResult.ok(json.dumps(payload, ensure_ascii=False))
        except (PermissionError, FileNotFoundError, ValueError, KeyError) as exc:
            return SkillResult.fail(str(exc))
        except Exception as exc:
            return SkillResult.fail(f"Error reading coding task plan: {exc}")

    @skill(swarm=[Subagents.CODER, Subagents.QA_ENGINEER, Subagents.SYSADMIN])
    @require_access(HostOSAccessLevel.SANDBOX)
    async def revise_coding_task_plan(
        self,
        task_id: str,
        reason: str,
        steps: List[Dict[str, Any]],
        expected_revision: int,
        expected_workspace_fingerprint: str,
        reopen_step_ids: Optional[List[str]] = None,
    ) -> SkillResult:
        """Atomically replace only the unfinished plan graph under exact state.

        The objective and requirements are immutable here. Completed steps and
        steps with active work are retained byte-for-byte at the structural
        level. Blocked steps must be explicitly reopened or removed.
        """

        try:
            task_id = self.workspaces._validate_task_id(task_id)
            clean_reason = self._bounded_text(reason, "reason", 2000)
            if not isinstance(expected_revision, int) or isinstance(
                expected_revision, bool
            ):
                raise ValueError("expected_revision must be an integer.")
            if not isinstance(
                expected_workspace_fingerprint, str
            ) or not self._FINGERPRINT.fullmatch(expected_workspace_fingerprint):
                raise ValueError(
                    "expected_workspace_fingerprint must be a SHA-256 value."
                )
            proposed = self._normalize_steps(steps)
            raw_reopen = reopen_step_ids or []
            if not isinstance(raw_reopen, list) or not all(
                isinstance(item, str) for item in raw_reopen
            ):
                raise ValueError("reopen_step_ids must be a string list.")
            reopen = {
                self._item_id(item, "reopen_step_ids") for item in raw_reopen
            }
            if len(reopen) != len(raw_reopen):
                raise ValueError("reopen_step_ids cannot contain duplicates.")

            async with self.workspaces._lock:
                registry = self.workspaces._load_registry()
                entry = self.workspaces._get_entry(registry, task_id)
                plan = entry.get("task_plan")
                if not plan:
                    return SkillResult.fail("Coding task has no initialized plan.")
                self._check_revision(plan, expected_revision)
                _, workspace = self.workspaces._entry_paths(entry)
                if not workspace.is_dir():
                    return SkillResult.fail("Coding workspace directory is missing.")
                current_fingerprint = await self.workspaces.workspace_fingerprint(
                    workspace
                )
                if (
                    current_fingerprint["fingerprint"]
                    != expected_workspace_fingerprint
                ):
                    return SkillResult.fail(
                        "Workspace changed after plan inspection: expected "
                        f"{expected_workspace_fingerprint}, current fingerprint is "
                        f"{current_fingerprint['fingerprint']}. Inspect it before "
                        "replanning."
                    )

                current_by_id = {step["id"]: step for step in plan["steps"]}
                proposed_by_id = {step["id"]: step for step in proposed}
                unknown_reopens = reopen - set(current_by_id)
                if unknown_reopens:
                    raise ValueError(
                        "Cannot reopen unknown steps: "
                        + ", ".join(sorted(unknown_reopens))
                        + "."
                    )
                invalid_reopens = [
                    step_id
                    for step_id in sorted(reopen)
                    if current_by_id[step_id]["status"] != "blocked"
                ]
                if invalid_reopens:
                    raise ValueError(
                        "Only blocked steps can be reopened: "
                        + ", ".join(invalid_reopens)
                        + "."
                    )

                def active_delegation(step: Dict[str, Any]) -> bool:
                    return any(
                        item.get("status") in {"queued", "running", "reported"}
                        for item in step.get("delegations", [])
                    )

                protected = {
                    step_id
                    for step_id, step in current_by_id.items()
                    if step.get("status") in {"completed", "in_progress"}
                    or active_delegation(step)
                }
                missing_protected = protected - set(proposed_by_id)
                if missing_protected:
                    raise ValueError(
                        "A revision cannot remove completed, in-progress, or active "
                        "delegated steps: "
                        + ", ".join(sorted(missing_protected))
                        + "."
                    )
                for step_id in sorted(protected):
                    old = current_by_id[step_id]
                    new = proposed_by_id[step_id]
                    if old["title"] != new["title"] or old["depends_on"] != new[
                        "depends_on"
                    ]:
                        raise ValueError(
                            f"Protected step '{step_id}' must retain its title and "
                            "dependencies."
                        )

                retained_blocked = [
                    step_id
                    for step_id, step in current_by_id.items()
                    if step.get("status") == "blocked"
                    and step_id in proposed_by_id
                    and step_id not in reopen
                ]
                if retained_blocked:
                    raise ValueError(
                        "Blocked steps retained by a revision must be explicitly "
                        "reopened: "
                        + ", ".join(sorted(retained_blocked))
                        + "."
                    )
                blocked_requirements = [
                    item["id"]
                    for item in plan["requirements"]
                    if item.get("status") == "blocked"
                ]
                if blocked_requirements:
                    raise ValueError(
                        "Blocked requirements must be reviewed before replanning: "
                        + ", ".join(blocked_requirements)
                        + "."
                    )

                rebuilt: List[Dict[str, Any]] = []
                for proposed_step in proposed:
                    step_id = proposed_step["id"]
                    old = current_by_id.get(step_id)
                    if old is None:
                        rebuilt.append(proposed_step)
                        continue
                    retained = {
                        **old,
                        "title": proposed_step["title"],
                        "depends_on": proposed_step["depends_on"],
                    }
                    if step_id in reopen:
                        prior = retained.setdefault("replan_evidence_history", [])
                        prior.append(
                            {
                                "status": old["status"],
                                "evidence": old.get("evidence", ""),
                                "updated_at": old.get("updated_at"),
                            }
                        )
                        retained["replan_evidence_history"] = prior[-10:]
                        retained["status"] = "pending"
                        retained["evidence"] = ""
                        retained.pop("delegation_status", None)
                        retained["updated_at"] = self.workspaces._utc_now()
                        retained["completed_at"] = None
                    rebuilt.append(retained)

                before_shape = [
                    {
                        "id": step["id"],
                        "title": step["title"],
                        "depends_on": step["depends_on"],
                        "status": step["status"],
                    }
                    for step in plan["steps"]
                ]
                after_shape = [
                    {
                        "id": step["id"],
                        "title": step["title"],
                        "depends_on": step["depends_on"],
                        "status": step["status"],
                    }
                    for step in rebuilt
                ]
                if before_shape == after_shape:
                    return SkillResult.fail(
                        "The proposed revision does not change the coding plan."
                    )

                before_ids = set(current_by_id)
                after_ids = set(proposed_by_id)
                changed = sorted(
                    step_id
                    for step_id in before_ids & after_ids
                    if next(
                        item for item in before_shape if item["id"] == step_id
                    )
                    != next(item for item in after_shape if item["id"] == step_id)
                )
                added = sorted(after_ids - before_ids)
                removed = sorted(before_ids - after_ids)
                if not added and not removed and not changed and not reopen:
                    return SkillResult.fail(
                        "The proposed revision only reorders steps and does not "
                        "change remaining work."
                    )
                latest_fingerprint = await self.workspaces.workspace_fingerprint(
                    workspace
                )
                if (
                    latest_fingerprint["fingerprint"]
                    != expected_workspace_fingerprint
                ):
                    return SkillResult.fail(
                        "Workspace changed while the plan revision was being "
                        "validated. Inspect it and retry with its new fingerprint."
                    )
                state = self._replanning_state(plan)
                resolved_reason_ids = [
                    item.get("reason_id") for item in state.get("reasons", [])
                ]
                previous_revision = plan["revision"]
                now = self.workspaces._utc_now()
                plan["steps"] = rebuilt
                state["required"] = False
                state["reasons"] = []
                state["last_applied_at"] = now
                state["last_reason"] = clean_reason
                state["last_workspace_fingerprint"] = expected_workspace_fingerprint
                changes = {
                    "added": added,
                    "removed": removed,
                    "changed": changed,
                    "reopened": sorted(reopen),
                }
                archive = plan.setdefault("replan_history", [])
                archive.append(
                    {
                        "from_revision": previous_revision,
                        "reason": clean_reason,
                        "workspace_fingerprint": expected_workspace_fingerprint,
                        "resolved_reason_ids": resolved_reason_ids,
                        "changes": changes,
                        "applied_at": now,
                        "trace": current_trace(),
                    }
                )
                plan["replan_history"] = archive[-20:]
                self._append_history(
                    plan,
                    "plan_revised",
                    {
                        "reason": clean_reason,
                        "workspace_fingerprint": expected_workspace_fingerprint,
                        **changes,
                    },
                )
                self.workspaces._save_registry(registry)
                payload = self._view_payload(plan)
                payload["changes"] = changes
            return SkillResult.ok(json.dumps(payload, ensure_ascii=False))
        except (PermissionError, FileNotFoundError, ValueError, KeyError) as exc:
            return SkillResult.fail(str(exc))
        except Exception as exc:
            return SkillResult.fail(f"Error revising coding task plan: {exc}")

    @skill(swarm=[Subagents.CODER, Subagents.QA_ENGINEER, Subagents.SYSADMIN])
    @require_access(HostOSAccessLevel.SANDBOX)
    async def update_coding_task_step(
        self,
        task_id: str,
        step_id: str,
        status: str,
        evidence: str = "",
        expected_revision: Optional[int] = None,
        allow_reopen: bool = False,
    ) -> SkillResult:
        """Move one plan step while enforcing dependencies and revision safety.

        Completed/blocked states require bounded evidence. A completed step can
        only move backward with explicit ``allow_reopen=true``.
        """

        try:
            task_id = self.workspaces._validate_task_id(task_id)
            step_id = self._item_id(step_id, "step_id")
            if status not in self._STEP_STATES:
                raise ValueError(
                    "status must be pending, in_progress, completed, or blocked."
                )
            clean_evidence = ""
            if evidence.strip():
                clean_evidence = self._bounded_text(evidence, "evidence", 4000)
            if status in {"completed", "blocked"} and not clean_evidence:
                raise ValueError(f"{status} steps require evidence.")
            async with self.workspaces._lock:
                registry = self.workspaces._load_registry()
                entry = self.workspaces._get_entry(registry, task_id)
                plan = entry.get("task_plan")
                if not plan:
                    return SkillResult.fail("Coding task has no initialized plan.")
                self._check_revision(plan, expected_revision)
                by_id = {step["id"]: step for step in plan["steps"]}
                if step_id not in by_id:
                    return SkillResult.fail(f"Coding step '{step_id}' was not found.")
                target = by_id[step_id]
                active_delegations = [
                    item.get("delegation_id", "unknown")
                    for item in target.get("delegations", [])
                    if item.get("status") in {"queued", "running", "reported"}
                ]
                if status == "completed" and active_delegations:
                    return SkillResult.fail(
                        "Step has delegated work awaiting terminal reconciliation: "
                        + ", ".join(active_delegations)
                        + ". Use reconcile_coding_delegation instead."
                    )
                if (
                    target["status"] == "completed"
                    and status != "completed"
                    and not allow_reopen
                ):
                    return SkillResult.fail(
                        "Completed step cannot move backward without allow_reopen=true."
                    )
                incomplete = [
                    dependency
                    for dependency in target["depends_on"]
                    if by_id[dependency]["status"] != "completed"
                ]
                if status in {"in_progress", "completed"} and incomplete:
                    return SkillResult.fail(
                        "Step dependencies are incomplete: " + ", ".join(incomplete)
                    )
                previous = target["status"]
                now = self.workspaces._utc_now()
                target["status"] = status
                target["evidence"] = clean_evidence
                target["updated_at"] = now
                target["completed_at"] = now if status == "completed" else None
                if status == "blocked":
                    self._mark_replan_required(
                        plan,
                        trigger="step_blocked",
                        source_id=f"step:{step_id}:revision:{plan['revision']}",
                        evidence=clean_evidence,
                    )
                self._append_history(
                    plan,
                    "step_updated",
                    {"step_id": step_id, "from": previous, "to": status},
                )
                self.workspaces._save_registry(registry)
                payload = {**target, "revision": plan["revision"]}
            return SkillResult.ok(json.dumps(payload, ensure_ascii=False))
        except (PermissionError, FileNotFoundError, ValueError, KeyError) as exc:
            return SkillResult.fail(str(exc))
        except Exception as exc:
            return SkillResult.fail(f"Error updating coding task step: {exc}")

    @skill(swarm=[Subagents.CODER, Subagents.QA_ENGINEER, Subagents.SYSADMIN])
    @require_access(HostOSAccessLevel.SANDBOX)
    async def update_coding_requirement(
        self,
        task_id: str,
        requirement_id: str,
        status: str,
        evidence: str = "",
        expected_revision: Optional[int] = None,
    ) -> SkillResult:
        """Record bounded evidence for one requirement using revision safety."""

        try:
            task_id = self.workspaces._validate_task_id(task_id)
            requirement_id = self._item_id(requirement_id, "requirement_id")
            if status not in self._REQUIREMENT_STATES:
                raise ValueError("status must be pending, satisfied, or blocked.")
            clean_evidence = ""
            if evidence.strip():
                clean_evidence = self._bounded_text(evidence, "evidence", 4000)
            if status in {"satisfied", "blocked"} and not clean_evidence:
                raise ValueError(f"{status} requirements require evidence.")
            async with self.workspaces._lock:
                registry = self.workspaces._load_registry()
                entry = self.workspaces._get_entry(registry, task_id)
                plan = entry.get("task_plan")
                if not plan:
                    return SkillResult.fail("Coding task has no initialized plan.")
                self._check_revision(plan, expected_revision)
                by_id = {item["id"]: item for item in plan["requirements"]}
                if requirement_id not in by_id:
                    return SkillResult.fail(
                        f"Coding requirement '{requirement_id}' was not found."
                    )
                target = by_id[requirement_id]
                previous = target["status"]
                target["status"] = status
                target["evidence"] = clean_evidence
                target["updated_at"] = self.workspaces._utc_now()
                if status == "blocked":
                    self._mark_replan_required(
                        plan,
                        trigger="requirement_blocked",
                        source_id=(
                            f"requirement:{requirement_id}:revision:{plan['revision']}"
                        ),
                        evidence=clean_evidence,
                    )
                self._append_history(
                    plan,
                    "requirement_updated",
                    {
                        "requirement_id": requirement_id,
                        "from": previous,
                        "to": status,
                    },
                )
                self.workspaces._save_registry(registry)
                payload = {**target, "revision": plan["revision"]}
            return SkillResult.ok(json.dumps(payload, ensure_ascii=False))
        except (PermissionError, FileNotFoundError, ValueError, KeyError) as exc:
            return SkillResult.fail(str(exc))
        except Exception as exc:
            return SkillResult.fail(f"Error updating coding requirement: {exc}")
