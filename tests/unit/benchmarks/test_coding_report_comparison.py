import json
from pathlib import Path

import pytest

from benchmarks.coding_tasks.compare_reports import compare


def write_report(path: Path, benchmark: str, fingerprint: str, score: float) -> Path:
    common = {
        "schema_version": 1,
        "benchmark": benchmark,
        "contract": {
            "fingerprint": fingerprint,
            "task_ids": ["task-a"],
        },
        "quality_gate_passed": score == 1,
        "mean_quality_score": score,
        "evaluations": [
            {"id": "task-a", "quality_score": score, "gate_passed": score == 1}
        ],
    }
    if benchmark == "jawl-live-coding-task-eval":
        common.update(
            model="qwen-test",
            live_lifecycle_passed=True,
            totals={"duration_ms": 100, "input_tokens": 20, "output_tokens": 5},
        )
    else:
        common.update(
            candidate="codex-test",
            execution_passed=True,
            runs=[{"execution": {"duration_ms": 80}}],
        )
    path.write_text(json.dumps(common), encoding="utf-8")
    return path


def test_comparison_ranks_only_identical_quality_contracts(tmp_path):
    jawl = write_report(
        tmp_path / "jawl.json", "jawl-live-coding-task-eval", "same", 1.0
    )
    external = write_report(
        tmp_path / "external.json", "jawl-external-cli-coding-eval", "same", 0.7
    )

    result = compare([jawl, external])

    assert result["quality_ranking"][0]["label"].startswith("JAWL:qwen-test:")
    external_candidate = next(
        item
        for item in result["candidates"]
        if item["label"].startswith("external:codex-test:")
    )
    assert external_candidate["input_tokens"] is None
    assert "Not directly rankable" in result["comparison_scope"]["lifecycle"]


def test_comparison_rejects_mismatched_hidden_grader_contract(tmp_path):
    first = write_report(
        tmp_path / "first.json", "jawl-live-coding-task-eval", "one", 1.0
    )
    second = write_report(
        tmp_path / "second.json", "jawl-external-cli-coding-eval", "two", 1.0
    )

    with pytest.raises(ValueError, match="not comparable"):
        compare([first, second])
