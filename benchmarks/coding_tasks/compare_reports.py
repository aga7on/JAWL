"""Compare JAWL and external coding-agent reports on an identical contract."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def _load(path: Path) -> Dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1 or not isinstance(
        payload.get("contract"), dict
    ):
        raise ValueError(f"Report lacks a versioned benchmark contract ({path}).")
    return payload


def _normalize(report: Dict[str, Any], source: Path) -> Dict[str, Any]:
    benchmark = report.get("benchmark")
    evaluations = report.get("evaluations")
    if benchmark == "jawl-live-coding-task-eval":
        label = ":".join(
            [
                "JAWL",
                str(report.get("model", "unknown")),
                str(report.get("transport", "unknown")),
                str(report.get("thinking_policy", "unknown")),
                str(report.get("context_policy", "unknown")),
            ]
        )
        lifecycle_gate = bool(report.get("live_lifecycle_passed"))
        lifecycle_definition = "JAWL verified workspace/plan/commit lifecycle"
        duration_ms = report.get("totals", {}).get("duration_ms")
        input_tokens = report.get("totals", {}).get("input_tokens")
        output_tokens = report.get("totals", {}).get("output_tokens")
    elif benchmark == "jawl-external-cli-coding-eval":
        label = (
            f"external:{report.get('candidate', 'unknown')}:"
            f"{report.get('candidate_version', 'unknown')}"
        )
        lifecycle_gate = bool(report.get("execution_passed"))
        lifecycle_definition = "external CLI exit/timeout/patch-extraction lifecycle"
        duration_ms = round(
            sum(
                float(item.get("execution", {}).get("duration_ms", 0))
                for item in report.get("runs", [])
            ),
            1,
        )
        input_tokens = None
        output_tokens = None
    else:
        raise ValueError(f"Unsupported live report benchmark ({benchmark}).")
    if not isinstance(evaluations, list) or not evaluations:
        raise ValueError(f"Report has no task evaluations ({source}).")
    tasks = {
        item["id"]: {
            "quality_score": item["quality_score"],
            "gate_passed": bool(item["gate_passed"]),
        }
        for item in evaluations
    }
    contract_task_ids = report["contract"].get("task_ids", [])
    if set(tasks) != set(contract_task_ids) or len(tasks) != len(contract_task_ids):
        raise ValueError(
            f"Evaluation task IDs do not match the report contract ({source})."
        )
    return {
        "label": label,
        "source": str(source),
        "benchmark": benchmark,
        "contract": report["contract"],
        "quality_gate_passed": bool(report.get("quality_gate_passed")),
        "mean_quality_score": float(report.get("mean_quality_score", 0)),
        "lifecycle_gate_passed": lifecycle_gate,
        "lifecycle_definition": lifecycle_definition,
        "execution_duration_ms": duration_ms,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "tasks": tasks,
    }


def compare(report_paths: List[Path]) -> Dict[str, Any]:
    if len(report_paths) < 2:
        raise ValueError("At least two live reports are required for comparison.")
    candidates = [_normalize(_load(path), path) for path in report_paths]
    fingerprints = {item["contract"].get("fingerprint") for item in candidates}
    task_sets = {tuple(item["contract"].get("task_ids", [])) for item in candidates}
    if None in fingerprints or len(fingerprints) != 1 or len(task_sets) != 1:
        raise ValueError(
            "Reports are not comparable: benchmark contract fingerprints/task order differ."
        )
    labels = [item["label"] for item in candidates]
    if len(set(labels)) != len(labels):
        raise ValueError("Candidate labels must be unique in one comparison.")
    task_ids = list(next(iter(task_sets)))
    quality_ranking = sorted(
        (
            {
                "rank": 0,
                "label": item["label"],
                "mean_quality_score": item["mean_quality_score"],
                "quality_gate_passed": item["quality_gate_passed"],
            }
            for item in candidates
        ),
        key=lambda item: (
            -item["mean_quality_score"],
            -int(item["quality_gate_passed"]),
            item["label"],
        ),
    )
    for index, item in enumerate(quality_ranking, start=1):
        item["rank"] = index
    return {
        "schema_version": 1,
        "benchmark": "jawl-cross-agent-coding-comparison",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "contract_fingerprint": next(iter(fingerprints)),
        "task_ids": task_ids,
        "candidate_count": len(candidates),
        "comparison_scope": {
            "quality": (
                "Comparable: identical candidate-visible tasks and hidden grader contract."
            ),
            "lifecycle": (
                "Not directly rankable: JAWL commit/verification lifecycle and external "
                "CLI process/extraction lifecycle have different definitions."
            ),
            "wall_time": (
                "Indicative only: execution environments/provider latency may differ."
            ),
            "tokens": (
                "Reported when available; absent external metering is not treated as zero."
            ),
        },
        "quality_ranking": quality_ranking,
        "tasks": [
            {
                "id": task_id,
                "candidates": {
                    item["label"]: item["tasks"][task_id] for item in candidates
                },
            }
            for task_id in task_ids
        ],
        "candidates": candidates,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", action="append", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        result = compare([path.resolve() for path in args.report])
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        parser.error(str(exc))
    if args.output:
        output = args.output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(output.suffix + ".tmp")
        temporary.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(output)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
