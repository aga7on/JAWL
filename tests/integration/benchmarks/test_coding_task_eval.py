import json
import subprocess
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
RUNNER = REPOSITORY_ROOT / "benchmarks" / "coding_tasks" / "run.py"
SOLUTIONS = REPOSITORY_ROOT / "benchmarks" / "coding_tasks" / "solutions"


def _run(*arguments: str) -> tuple[subprocess.CompletedProcess[str], dict]:
    result = subprocess.run(
        [sys.executable, str(RUNNER), "--no-output", *arguments],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    report = json.loads(result.stdout.splitlines()[-1])
    return result, report


def test_reference_patches_pass_public_hidden_scope_and_economy_gates():
    result, report = _run()

    assert result.returncode == 0, result.stdout + result.stderr
    assert report["gate_passed"] is True
    assert report["passed_task_count"] == report["task_count"] == 2
    assert report["mean_quality_score"] == 1.0
    assert all(task["public_tests"]["passed"] for task in report["tasks"])
    assert all(task["hidden_tests"]["passed"] for task in report["tasks"])


def test_candidate_cannot_hide_an_untracked_file_from_scope_gate(tmp_path):
    patch_dir = tmp_path / "patches"
    patch_dir.mkdir()
    reference = (SOLUTIONS / "inclusive_range_parser.patch").read_bytes()
    forbidden_file = """\
diff --git a/notes.txt b/notes.txt
new file mode 100644
--- /dev/null
+++ b/notes.txt
@@ -0,0 +1 @@
+out-of-scope artifact
"""
    (patch_dir / "inclusive_range_parser.patch").write_bytes(
        reference + forbidden_file.encode("utf-8")
    )

    result, report = _run(
        "--task", "inclusive_range_parser", "--patch-dir", str(patch_dir)
    )

    task = report["tasks"][0]
    assert result.returncode == 1
    assert task["patch_applied"] is True
    assert task["public_tests"]["passed"] is True
    assert task["hidden_tests"]["passed"] is True
    assert task["changed_files"] == ["notes.txt", "ranges.py"]
    assert task["scope_passed"] is False
    assert task["gate_passed"] is False
