"""Persistent requirement and execution plans for task-scoped coding work."""

from __future__ import annotations

import json
import re
import uuid
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
        return {
            "completed_steps": completed_steps,
            "total_steps": len(steps),
            "satisfied_requirements": satisfied_requirements,
            "total_requirements": len(requirements),
            "blocked": any(step["status"] == "blocked" for step in steps)
            or any(item["status"] == "blocked" for item in requirements),
            "ready_for_commit": completed_steps == len(steps)
            and satisfied_requirements == len(requirements),
        }

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
            "created_at": plan["created_at"],
            "updated_at": plan["updated_at"],
            "summary": cls._summary(plan),
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
                    "requirements": normalized_requirements,
                    "steps": normalized_steps,
                    "created_at": now,
                    "updated_at": now,
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
