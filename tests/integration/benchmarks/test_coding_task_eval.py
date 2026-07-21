import json
import subprocess
import sys
from pathlib import Path

import pytest

from benchmarks.coding_tasks.drive_jawl import (
    build_task_prompt,
    extract_candidate_patch,
    initialize_fixture,
    run_live_task,
)
from benchmarks.coding_tasks.run import evaluate_task, load_manifest
from src.l2_interfaces.host.os.client import HostOSClient
from src.l2_interfaces.host.os.skills.coding_workspaces import HostOSCodingWorkspaces
from src.l2_interfaces.host.os.state import HostOSState
from src.l3_agent.llm.executor import LLMExecutor
from src.utils.settings import HostOSConfig


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


def test_live_driver_prompt_exposes_contract_but_not_hidden_oracle():
    task = load_manifest()["tasks"][0]

    prompt = build_task_prompt(task, "eval-inclusive-range-parser")

    assert task["prompt"] in prompt
    assert "sandbox/repo" in prompt
    assert "eval-inclusive-range-parser" in prompt
    assert "test_parse_range_accepts_surrounding_whitespace" not in prompt
    assert str(Path(task["fixture"]) / "oracle") not in prompt


@pytest.mark.asyncio
async def test_live_driver_extracts_committed_workspace_patch_for_same_grader(tmp_path):
    task = load_manifest()["tasks"][0]
    runtime_root = tmp_path / "runtime"
    repository = initialize_fixture(task, runtime_root)
    host = HostOSClient(
        runtime_root,
        HostOSConfig(
            enabled=True,
            access_level=0,
            require_deploy_sessions=False,
        ),
        HostOSState(),
        timezone=0,
    )
    workspaces = HostOSCodingWorkspaces(host)
    created = await workspaces.create_coding_workspace(
        "sandbox/repo", "eval-inclusive-range-parser"
    )
    assert created.is_success is True
    entry = workspaces._load_registry()["workspaces"]["eval-inclusive-range-parser"]
    workspace = Path(entry["workspace_path"])
    applied = subprocess.run(
        [
            "git",
            "apply",
            str(SOLUTIONS / "inclusive_range_parser.patch"),
        ],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert applied.returncode == 0, applied.stdout + applied.stderr
    subprocess.run(["git", "add", "--all"], cwd=workspace, check=True, timeout=30)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=JAWL Live Eval",
            "-c",
            "user.email=live-eval@jawl.local",
            "commit",
            "-m",
            "solve task",
        ],
        cwd=workspace,
        check=True,
        capture_output=True,
        timeout=30,
    )
    patch_dir = tmp_path / "patches"
    patch_path = patch_dir / "inclusive_range_parser.patch"

    extraction = await extract_candidate_patch(
        workspaces,
        "eval-inclusive-range-parser",
        repository,
        patch_path,
    )
    evaluation = evaluate_task(task, "solution", patch_dir)

    assert extraction["patch_source"] == "managed_workspace"
    assert extraction["committed"] is True
    assert extraction["worktree_clean"] is True
    assert extraction["lifecycle_gate_passed"] is False
    assert extraction["verification_bypassed"] is None
    assert extraction["patch_bytes"] > 0
    assert evaluation["gate_passed"] is True


@pytest.mark.asyncio
async def test_live_driver_runs_real_react_and_coding_skills_without_network(
    tmp_path, monkeypatch
):
    task = load_manifest()["tasks"][0]
    responses = []

    async def fake_execute(self, **kwargs):
        step = len(responses) + 1
        if step == 1:
            actions = [
                {
                    "tool_name": "HostOSCodingWorkspaces.create_coding_workspace",
                    "action_id": "workspace",
                    "parameters": {
                        "repository_path": "sandbox/repo",
                        "task_id": "eval-inclusive_range_parser",
                    },
                },
                {
                    "tool_name": "HostOSCodingPlans.initialize_coding_task_plan",
                    "action_id": "plan",
                    "depends_on": ["workspace"],
                    "parameters": {
                        "task_id": "eval-inclusive_range_parser",
                        "objective": "Implement and verify inclusive range parsing",
                        "requirements": ["Public range behavior is correct"],
                        "steps": [
                            {
                                "id": "implement",
                                "title": "Implement and verify the change",
                                "depends_on": [],
                            }
                        ],
                    },
                },
                {
                    "tool_name": "HostOSCodingFiles.read_coding_file_range",
                    "action_id": "inspect",
                    "depends_on": ["workspace"],
                    "parameters": {
                        "task_id": "eval-inclusive_range_parser",
                        "relative_path": "ranges.py",
                    },
                },
            ]
        elif step == 2:
            actions = [
                {
                    "tool_name": "HostOSCodingFiles.apply_coding_file_patch",
                    "action_id": "patch",
                    "parameters": {
                        "task_id": "eval-inclusive_range_parser",
                        "relative_path": "ranges.py",
                        "edits": [
                            {
                                "search": "    start = int(start_text)",
                                "replace": "    start = int(start_text.strip())",
                            },
                            {
                                "search": "    end = int(end_text)",
                                "replace": "    end = int(end_text.strip())",
                            },
                            {
                                "search": "    return list(range(start, end))",
                                "replace": "    if start > end:\n"
                                "        raise ValueError(\"descending ranges are not supported\")\n"
                                "    return list(range(start, end + 1))",
                            },
                        ],
                    },
                },
                {
                    "tool_name": "HostOSCodingVerification.run_coding_verification",
                    "action_id": "verify",
                    "depends_on": ["patch"],
                    "parameters": {
                        "task_id": "eval-inclusive_range_parser",
                        "checks": ["pytest"],
                        "timeout_sec": 60,
                    },
                },
                {
                    "tool_name": "HostOSCodingPlans.update_coding_task_step",
                    "action_id": "complete-step",
                    "depends_on": ["verify"],
                    "parameters": {
                        "task_id": "eval-inclusive_range_parser",
                        "step_id": "implement",
                        "status": "completed",
                        "evidence": "Implementation completed and pytest passed",
                    },
                },
                {
                    "tool_name": "HostOSCodingPlans.update_coding_requirement",
                    "action_id": "complete-requirement",
                    "depends_on": ["verify"],
                    "parameters": {
                        "task_id": "eval-inclusive_range_parser",
                        "requirement_id": "req_1",
                        "status": "satisfied",
                        "evidence": "Public pytest verification passed",
                    },
                },
                {
                    "tool_name": "HostOSCodingWorkspaces.commit_coding_workspace",
                    "depends_on": ["complete-step", "complete-requirement"],
                    "parameters": {
                        "task_id": "eval-inclusive_range_parser",
                        "commit_message": "solve inclusive range task",
                    },
                }
            ]
        else:
            actions = []
        response = json.dumps(
            {
                "observation": f"deterministic live-driver step {step}",
                "reasoning": "exercise the real ReAct and coding skill path",
                "reflection": "test-only provider response",
                "actions": actions,
            }
        )
        self.tracker.add_input_record(
            kwargs["messages"], kwargs["log_prefix"], kwargs["logger"]
        )
        self.tracker.add_output_record(
            response, kwargs["log_prefix"], kwargs["logger"]
        )
        self.last_call_metrics = {
            "status": "completed",
            "attempts": 1,
            "tool_call_count": len(actions),
        }
        responses.append(response)
        return response

    monkeypatch.setattr(LLMExecutor, "execute", fake_execute)
    output_dir = tmp_path / "live-output"

    result = await run_live_task(
        task,
        output_dir,
        api_url="http://127.0.0.1:1/v1",
        api_key="dummy-test-key",
        model="deterministic-test-model",
        transport="wrapper",
        max_steps=8,
        timeout_seconds=30,
    )
    evaluation = evaluate_task(task, "solution", output_dir / "patches")

    assert len(responses) == 3
    assert result["error"] == ""
    assert result["timed_out"] is False
    assert result["lifecycle_gate_passed"] is True
    assert result["plan_complete"] is True
    assert result["verification_bypassed"] is False
    assert result["plan_bypassed"] is False
    assert result["input_tokens"] > 0
    assert result["output_tokens"] > 0
    assert result["tick_count"] == 3
    assert result["action_tick_count"] == 2
    assert result["protocol_error_count"] == 0
    assert result["tick_summaries"][-1]["status"] == "completed"
    assert len(result["llm_calls"]) == 3
    assert "completed" in result["terminal_statuses"]
    assert evaluation["gate_passed"] is True
