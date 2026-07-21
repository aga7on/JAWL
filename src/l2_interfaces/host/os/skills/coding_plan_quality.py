"""Deterministic, payload-free quality grading for durable coding plans."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from typing import Any, Dict, List, Literal, Mapping, Sequence


PlanQualityPolicy = Literal["advisory", "enforce"]

_PREFIX = re.compile(r"^\s*(?:r(?:eq)?|s(?:tep)?)?\s*\d+\s*[:.)-]\s*", re.I)
_WORDS = re.compile(r"[\w-]+", re.UNICODE)
_PROCESS_REQUIREMENT_PATTERNS = (
    re.compile(r"\b(?:plan steps?|requirements?|diff review|commit(?:ted)?)\b", re.I),
    re.compile(
        r"\b(?:repository\s+)?(?:verification|tests?|pytest|lint|type[- ]?check|build)\b"
        r".{0,80}\b(?:pass(?:es|ed|ing)?|green|succeed(?:s|ed)?)\b",
        re.I,
    ),
    re.compile(r"\b(?:шаг(?:и|ов)?\s+плана|требован\w*|коммит\w*|ревью\s+диффа)\b", re.I),
    re.compile(
        r"\b(?:проверк\w*|тест\w*|линт\w*|сборк\w*)\b"
        r".{0,80}\b(?:проход\w*|успеш\w*|зел[её]н\w*)\b",
        re.I,
    ),
)
_BOOKKEEPING_STARTS = {
    "inspect",
    "search",
    "read",
    "review",
    "verify",
    "test",
    "run",
    "commit",
    "locate",
    "explore",
    "check",
    "update",
    "исследовать",
    "найти",
    "прочитать",
    "проверить",
    "протестировать",
    "закоммитить",
    "обновить",
}
_SUBSTANTIVE_WORDS = {
    "implement",
    "fix",
    "add",
    "remove",
    "change",
    "refactor",
    "migrate",
    "secure",
    "optimize",
    "support",
    "prevent",
    "resolve",
    "реализовать",
    "исправить",
    "добавить",
    "удалить",
    "изменить",
    "отрефакторить",
    "мигрировать",
    "защитить",
    "оптимизировать",
}


def _normalized_text(value: Any) -> str:
    text = _PREFIX.sub("", str(value or "").strip().lower())
    return " ".join(_WORDS.findall(text))


def _finding(
    code: str,
    severity: Literal["warning", "error"],
    message: str,
    *,
    item_ids: Sequence[str] = (),
) -> Dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "message": message,
        "item_ids": sorted(dict.fromkeys(item_ids)),
    }


def _duplicate_ids(items: Sequence[Mapping[str, Any]], text_field: str) -> List[str]:
    normalized = [_normalized_text(item.get(text_field)) for item in items]
    duplicate_values = {
        value for value, count in Counter(normalized).items() if value and count > 1
    }
    return [
        str(item.get("id"))
        for item, value in zip(items, normalized)
        if value in duplicate_values
    ]


def _dependency_depths(steps: Sequence[Mapping[str, Any]]) -> Dict[str, int]:
    by_id = {str(step["id"]): step for step in steps}
    cache: Dict[str, int] = {}

    def depth(step_id: str) -> int:
        if step_id in cache:
            return cache[step_id]
        dependencies = by_id[step_id].get("depends_on", [])
        value = 1 + max((depth(str(item)) for item in dependencies), default=0)
        cache[step_id] = value
        return value

    for identifier in by_id:
        depth(identifier)
    return cache


def _is_process_requirement(text: Any) -> bool:
    normalized = _PREFIX.sub("", str(text or "").strip())
    return any(pattern.search(normalized) for pattern in _PROCESS_REQUIREMENT_PATTERNS)


def _is_bookkeeping_step(text: Any) -> bool:
    words = _normalized_text(text).split()
    return bool(
        words
        and words[0] in _BOOKKEEPING_STARTS
        and not any(word in _SUBSTANTIVE_WORDS for word in words)
    )


def grade_coding_plan(
    requirements: Sequence[Mapping[str, Any]],
    steps: Sequence[Mapping[str, Any]],
    policy: PlanQualityPolicy,
) -> Dict[str, Any]:
    """Grade one normalized plan without persisting user-authored text."""

    if policy not in {"advisory", "enforce"}:
        raise ValueError("quality_policy must be 'advisory' or 'enforce'.")
    requirement_ids = {str(item["id"]) for item in requirements}
    process_requirement_ids = [
        str(item["id"])
        for item in requirements
        if _is_process_requirement(item.get("text"))
    ]
    outcome_requirement_count = max(
        1, len(requirements) - len(process_requirement_ids)
    )
    covered = {
        str(requirement_id)
        for step in steps
        for requirement_id in step.get("requirement_ids", [])
    }
    uncovered = sorted(requirement_ids - covered)
    orphan_steps = [
        str(step["id"]) for step in steps if not step.get("requirement_ids")
    ]
    bookkeeping_steps = [
        str(step["id"])
        for step in steps
        if _is_bookkeeping_step(step.get("title"))
    ]
    duplicate_requirements = _duplicate_ids(requirements, "text")
    duplicate_steps = _duplicate_ids(steps, "title")
    depths = _dependency_depths(steps)
    max_depth = max(depths.values(), default=0)
    root_count = sum(not step.get("depends_on") for step in steps)
    recommended_max_steps = min(12, outcome_requirement_count)
    findings: List[Dict[str, Any]] = []

    if duplicate_requirements:
        findings.append(
            _finding(
                "duplicate_requirements",
                "error",
                "Requirements must describe distinct outcomes.",
                item_ids=duplicate_requirements,
            )
        )
    if duplicate_steps:
        findings.append(
            _finding(
                "duplicate_steps",
                "error",
                "Steps must describe distinct implementation outcomes.",
                item_ids=duplicate_steps,
            )
        )
    if process_requirement_ids:
        findings.append(
            _finding(
                "process_requirements",
                "error" if policy == "enforce" else "warning",
                "Verification, review, plan maintenance, and commit are evidence/actions, not outcome requirements.",
                item_ids=process_requirement_ids,
            )
        )
    if uncovered:
        findings.append(
            _finding(
                "uncovered_requirements",
                "error" if policy == "enforce" else "warning",
                "Every requirement should be explicitly covered by at least one step.",
                item_ids=uncovered,
            )
        )
    if orphan_steps:
        findings.append(
            _finding(
                "orphan_steps",
                "warning",
                "Steps without requirement_ids add work without an explicit outcome binding.",
                item_ids=orphan_steps,
            )
        )
    if bookkeeping_steps:
        findings.append(
            _finding(
                "bookkeeping_steps",
                "warning",
                "Search, inspection, verification, review, and commit should normally remain actions inside an outcome step.",
                item_ids=bookkeeping_steps,
            )
        )
    if len(steps) > recommended_max_steps:
        findings.append(
            _finding(
                "excessive_step_count",
                "error" if policy == "enforce" else "warning",
                "The step graph is larger than the deterministic outcome-based budget.",
                item_ids=[str(step["id"]) for step in steps],
            )
        )
    if len(steps) >= 4 and max_depth == len(steps):
        findings.append(
            _finding(
                "fully_serialized_plan",
                "warning",
                "A fully serialized graph of four or more steps may create avoidable reasoning cycles.",
                item_ids=[str(step["id"]) for step in steps],
            )
        )

    errors = sum(item["severity"] == "error" for item in findings)
    warnings = len(findings) - errors
    status = "reject" if errors else ("advisory" if warnings else "pass")
    graph_contract = {
        "requirements": [
            {"id": str(item["id"]), "text": str(item.get("text", ""))}
            for item in requirements
        ],
        "steps": [
            {
                "id": str(step["id"]),
                "title": str(step.get("title", "")),
                "depends_on": list(step.get("depends_on", [])),
                "requirement_ids": list(step.get("requirement_ids", [])),
            }
            for step in steps
        ],
    }
    graph_sha256 = hashlib.sha256(
        json.dumps(
            graph_contract, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    report: Dict[str, Any] = {
        "version": 1,
        "policy": policy,
        "status": status,
        "score": max(0, 100 - errors * 25 - warnings * 8),
        "graph_sha256": graph_sha256,
        "metrics": {
            "requirement_count": len(requirements),
            "outcome_requirement_count": outcome_requirement_count,
            "process_requirement_count": len(process_requirement_ids),
            "step_count": len(steps),
            "recommended_max_steps": recommended_max_steps,
            "covered_requirement_count": len(requirement_ids & covered),
            "max_dependency_depth": max_depth,
            "root_step_count": root_count,
            "finding_count": len(findings),
        },
        "findings": findings,
    }
    report["report_sha256"] = hashlib.sha256(
        json.dumps(report, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return report
