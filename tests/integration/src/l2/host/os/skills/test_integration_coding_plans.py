import json
import subprocess
from pathlib import Path

import pytest

from src.l2_interfaces.host.os.skills.coding_plans import HostOSCodingPlans
from src.l2_interfaces.host.os.skills.coding_verification import (
    HostOSCodingVerification,
)
from src.l2_interfaces.host.os.skills.coding_workspaces import (
    HostOSCodingWorkspaces,
)
from src.utils.tracing import begin_trace, reset_trace


def run_git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def create_repository(root: Path) -> None:
    repository = root / "project"
    repository.mkdir()
    run_git(repository, "init", "-b", "main")
    run_git(repository, "config", "user.name", "Test User")
    run_git(repository, "config", "user.email", "test@example.com")
    (repository / "app.py").write_text("value = 1\n", encoding="utf-8")
    run_git(repository, "add", "--all")
    run_git(repository, "commit", "-m", "initial")


@pytest.mark.asyncio
async def test_coding_plan_persists_dependencies_evidence_and_commit_gate(os_client):
    create_repository(os_client.sandbox_dir)
    workspaces = HostOSCodingWorkspaces(os_client)
    plans = HostOSCodingPlans(os_client, workspaces)
    verifier = HostOSCodingVerification(os_client, workspaces)
    created = await workspaces.create_coding_workspace("sandbox/project", "plan-task")
    workspace = Path(json.loads(created.message)["workspace_path"])

    initialized = await plans.initialize_coding_task_plan(
        task_id="plan-task",
        objective="Change the value safely",
        requirements=["The value is two and verification passes"],
        steps=[
            {"id": "inspect", "title": "Inspect current implementation"},
            {
                "id": "implement",
                "title": "Implement and verify the change",
                "depends_on": ["inspect"],
            },
        ],
    )
    assert initialized.is_success is True, initialized.message
    assert json.loads(initialized.message)["revision"] == 1

    dependency_rejected = await plans.update_coding_task_step(
        "plan-task",
        "implement",
        "completed",
        evidence="app.py changed",
        expected_revision=1,
    )
    assert dependency_rejected.is_success is False
    assert "dependencies are incomplete" in dependency_rejected.message

    (workspace / "app.py").write_text("value = 2\n", encoding="utf-8")
    verify_token, _ = begin_trace("test", trace_id="trace-verification")
    try:
        verified = await verifier.run_coding_verification("plan-task")
    finally:
        reset_trace(verify_token)
    assert verified.is_success is True, verified.message
    assert json.loads(verified.message)["trace"]["trace_id"] == "trace-verification"
    incomplete_commit = await workspaces.commit_coding_workspace(
        "plan-task", "change value"
    )
    assert incomplete_commit.is_success is False
    assert "coding plan is incomplete" in incomplete_commit.message

    inspect_done = await plans.update_coding_task_step(
        "plan-task",
        "inspect",
        "completed",
        evidence="Read app.py line 1 and captured its SHA-256.",
        expected_revision=1,
    )
    assert inspect_done.is_success is True
    assert json.loads(inspect_done.message)["revision"] == 2
    stale = await plans.update_coding_task_step(
        "plan-task",
        "implement",
        "completed",
        evidence="Verification passed.",
        expected_revision=1,
    )
    assert stale.is_success is False
    assert "Stale coding plan revision" in stale.message

    implement_done = await plans.update_coding_task_step(
        "plan-task",
        "implement",
        "completed",
        evidence="Unified diff reviewed; python_compile passed.",
        expected_revision=2,
    )
    assert implement_done.is_success is True
    requirement_done = await plans.update_coding_requirement(
        "plan-task",
        "req_1",
        "satisfied",
        evidence="app.py contains value = 2; verification run passed.",
        expected_revision=3,
    )
    assert requirement_done.is_success is True

    resumed = HostOSCodingPlans(os_client, HostOSCodingWorkspaces(os_client))
    persisted = await resumed.get_coding_task_plan("plan-task")
    persisted_payload = json.loads(persisted.message)
    assert persisted_payload["revision"] == 4
    assert persisted_payload["summary"]["ready_for_commit"] is True
    history = json.loads(
        (await resumed.get_coding_task_plan("plan-task", section="history")).message
    )
    assert len(history["items"]) == 4
    assert history["has_more"] is False

    commit_token, _ = begin_trace("test", trace_id="trace-commit")
    try:
        committed = await workspaces.commit_coding_workspace(
            "plan-task", "change value"
        )
    finally:
        reset_trace(commit_token)
    assert committed.is_success is True, committed.message
    commit_payload = json.loads(committed.message)
    assert commit_payload["plan_completion_bypassed"] is False
    assert commit_payload["trace"]["trace_id"] == "trace-commit"
    assert (await workspaces.remove_coding_workspace("plan-task")).is_success


@pytest.mark.asyncio
async def test_coding_plan_rejects_cycles_and_redacts_evidence(os_client):
    create_repository(os_client.sandbox_dir)
    workspaces = HostOSCodingWorkspaces(os_client)
    plans = HostOSCodingPlans(os_client, workspaces)
    await workspaces.create_coding_workspace("sandbox/project", "invalid-plan")

    cycle = await plans.initialize_coding_task_plan(
        "invalid-plan",
        "Do work",
        ["Work is complete"],
        [
            {"id": "a", "title": "A", "depends_on": ["b"]},
            {"id": "b", "title": "B", "depends_on": ["a"]},
        ],
    )
    assert cycle.is_success is False
    assert "dependency cycle" in cycle.message

    initialized = await plans.initialize_coding_task_plan(
        "invalid-plan",
        "Do work",
        ["Work is complete"],
        [{"id": "only", "title": "Only step"}],
    )
    assert initialized.is_success is True
    secret = "ghp_1234567890abcdefghij"
    updated = await plans.update_coding_task_step(
        "invalid-plan",
        "only",
        "completed",
        evidence=f"Used token {secret} while checking output.",
        expected_revision=1,
    )
    assert updated.is_success is True
    assert secret not in updated.message
    assert "[REDACTED]" in updated.message
    replaced = await plans.initialize_coding_task_plan(
        "invalid-plan",
        "Replacement plan",
        ["Replacement is complete"],
        [{"id": "replacement", "title": "Replacement step"}],
        replace=True,
    )
    assert replaced.is_success is True
    async with workspaces._lock:
        registry = workspaces._load_registry()
        entry = workspaces._get_entry(registry, "invalid-plan")
        assert len(entry["task_plan_archive"]) == 1
        assert entry["task_plan_archive"][0]["archive_reason"] == "explicit_replace"
    assert (await workspaces.remove_coding_workspace("invalid-plan")).is_success
