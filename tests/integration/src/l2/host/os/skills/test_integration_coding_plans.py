import json
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

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
async def test_enforced_plan_quality_rejects_process_overhead_before_persistence(
    os_client,
):
    create_repository(os_client.sandbox_dir)
    workspaces = HostOSCodingWorkspaces(os_client)
    plans = HostOSCodingPlans(os_client, workspaces)
    await workspaces.create_coding_workspace("sandbox/project", "quality-rejected")

    rejected = await plans.initialize_coding_task_plan(
        "quality-rejected",
        "Change one value safely",
        [
            "The value is two",
            "Repository verification passes",
            "Plan evidence is completed and committed",
        ],
        [
            {"id": "inspect", "title": "Inspect the current implementation"},
            {
                "id": "implement",
                "title": "Implement the value change",
                "depends_on": ["inspect"],
                "requirement_ids": ["req_1"],
            },
            {
                "id": "verify",
                "title": "Run repository tests",
                "depends_on": ["implement"],
                "requirement_ids": ["req_2"],
            },
            {
                "id": "commit",
                "title": "Commit verified workspace",
                "depends_on": ["verify"],
                "requirement_ids": ["req_3"],
            },
        ],
        quality_policy="enforce",
    )

    assert rejected.is_success is False
    assert "Coding plan quality rejected" in rejected.message
    report = json.loads(rejected.message.split(": ", 1)[1])
    assert report["status"] == "reject"
    assert {item["code"] for item in report["findings"]} >= {
        "process_requirements",
        "bookkeeping_steps",
        "excessive_step_count",
    }
    assert (await plans.get_coding_task_plan("quality-rejected")).is_success is False
    unknown_link = await plans.initialize_coding_task_plan(
        "quality-rejected",
        "Change one value safely",
        ["The value is two"],
        [
            {
                "id": "implement",
                "title": "Implement the value change",
                "requirement_ids": ["req_99"],
            }
        ],
        quality_policy="enforce",
    )
    assert unknown_link.is_success is False
    assert "unknown requirements: req_99" in unknown_link.message
    assert (
        await workspaces.remove_coding_workspace("quality-rejected", force=True)
    ).is_success


@pytest.mark.asyncio
async def test_enforced_plan_quality_auto_satisfies_explicitly_covered_requirements(
    os_client,
):
    create_repository(os_client.sandbox_dir)
    workspaces = HostOSCodingWorkspaces(os_client)
    plans = HostOSCodingPlans(os_client, workspaces)
    await workspaces.create_coding_workspace("sandbox/project", "quality-compact")

    initialized = await plans.initialize_coding_task_plan(
        "quality-compact",
        "Change one value safely",
        ["The value is two", "Existing behavior remains compatible"],
        [
            {
                "id": "implement",
                "title": "Implement and verify the compatible value change",
                "requirement_ids": ["req_1", "req_2"],
            }
        ],
        quality_policy="enforce",
    )
    payload = json.loads(initialized.message)

    assert initialized.is_success is True, initialized.message
    assert payload["quality_policy"] == "enforce"
    assert payload["quality_report"]["status"] == "pass"
    assert payload["summary"]["plan_quality_status"] == "pass"
    workspace = Path(
        json.loads(
            (await workspaces.get_coding_workspace_status("quality-compact")).message
        )["workspace_path"]
    )
    fingerprint = (await workspaces.workspace_fingerprint(workspace))["fingerprint"]
    rejected_revision = await plans.revise_coding_task_plan(
        "quality-compact",
        "Attempt a revision that drops explicit coverage.",
        [
            {
                "id": "implement",
                "title": "Implement the value change",
                "requirement_ids": ["req_1"],
            },
            {
                "id": "verify",
                "title": "Run compatibility checks",
                "depends_on": ["implement"],
            },
        ],
        expected_revision=1,
        expected_workspace_fingerprint=fingerprint,
    )
    assert rejected_revision.is_success is False
    assert "Coding plan quality rejected" in rejected_revision.message
    unchanged = json.loads(
        (await plans.get_coding_task_plan("quality-compact")).message
    )
    assert unchanged["revision"] == 1
    assert unchanged["quality_report"]["status"] == "pass"
    completed = await plans.update_coding_task_step(
        "quality-compact",
        "implement",
        "completed",
        "The exact diff was reviewed and verification passed.",
        expected_revision=1,
    )
    completed_payload = json.loads(completed.message)
    assert completed.is_success is True, completed.message
    assert completed_payload["auto_satisfied_requirement_ids"] == [
        "req_1",
        "req_2",
    ]

    resumed = HostOSCodingPlans(os_client, HostOSCodingWorkspaces(os_client))
    persisted = json.loads((await resumed.get_coding_task_plan("quality-compact")).message)
    assert persisted["summary"]["satisfied_requirements"] == 2
    assert persisted["summary"]["ready_for_commit"] is True
    assert all(item["status"] == "satisfied" for item in persisted["requirements"])
    assert (
        await workspaces.remove_coding_workspace("quality-compact", force=True)
    ).is_success


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
    assert json.loads(initialized.message)["requires_diff_review"] is True

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
    assert persisted_payload["requires_diff_review"] is True
    history = json.loads(
        (await resumed.get_coding_task_plan("plan-task", section="history")).message
    )
    assert len(history["items"]) == 4
    assert history["has_more"] is False

    unreviewed = await workspaces.commit_coding_workspace(
        "plan-task", "change value"
    )
    assert unreviewed.is_success is False
    assert "has not been fully reviewed" in unreviewed.message
    diff = await workspaces.get_coding_workspace_diff(
        "plan-task", file_path="app.py"
    )
    diff_payload = json.loads(diff.message)
    wrong_review = await workspaces.accept_coding_workspace_diff_review(
        "plan-task",
        diff_payload["fingerprint"]["fingerprint"],
        "0" * 64,
        file_path="app.py",
    )
    assert wrong_review.is_success is False
    assert "reviewed diff hash" in wrong_review.message
    accepted = await workspaces.accept_coding_workspace_diff_review(
        "plan-task",
        diff_payload["fingerprint"]["fingerprint"],
        diff_payload["reviewed_diff_sha256"],
        file_path="app.py",
    )
    assert accepted.is_success is True, accepted.message
    review_status = json.loads(accepted.message)
    assert review_status["required_by_plan"] is True
    assert review_status["ready_for_commit"] is True
    assert review_status["missing_files"] == []

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
    assert commit_payload["diff_review_bypassed"] is False
    assert len(commit_payload["diff_review_evidence_sha256"]) == 64
    committed_entry = workspaces._load_registry()["workspaces"]["plan-task"]
    assert committed_entry["last_commit_diff_review"]["commit"] == (
        commit_payload["commit"]
    )
    assert committed_entry["last_commit_diff_review"][
        "review_evidence_sha256"
    ] == commit_payload["diff_review_evidence_sha256"]
    assert commit_payload["trace"]["trace_id"] == "trace-commit"
    assert (await workspaces.remove_coding_workspace("plan-task")).is_success


@pytest.mark.asyncio
async def test_diff_review_gate_preserves_legacy_plans_and_records_explicit_bypass(
    os_client,
):
    create_repository(os_client.sandbox_dir)
    workspaces = HostOSCodingWorkspaces(os_client)
    plans = HostOSCodingPlans(os_client, workspaces)
    verifier = HostOSCodingVerification(os_client, workspaces)

    async def prepare(task_id: str, value: int) -> None:
        created = await workspaces.create_coding_workspace(
            "sandbox/project", task_id
        )
        workspace = Path(json.loads(created.message)["workspace_path"])
        initialized = await plans.initialize_coding_task_plan(
            task_id,
            "Change one value",
            ["The new value is committed"],
            [{"id": "change", "title": "Change and verify the value"}],
        )
        assert initialized.is_success is True, initialized.message
        (workspace / "app.py").write_text(
            f"value = {value}\n", encoding="utf-8"
        )
        verified = await verifier.run_coding_verification(
            task_id, checks=["git_diff_check", "python_compile"]
        )
        assert verified.is_success is True, verified.message
        step = await plans.update_coding_task_step(
            task_id,
            "change",
            "completed",
            evidence="Exact diff and verification inspected.",
            expected_revision=1,
        )
        assert step.is_success is True, step.message
        requirement = await plans.update_coding_requirement(
            task_id,
            "req_1",
            "satisfied",
            evidence="Verification passed for the exact workspace state.",
            expected_revision=2,
        )
        assert requirement.is_success is True, requirement.message

    await prepare("legacy-review-plan", 2)
    async with workspaces._lock:
        registry = workspaces._load_registry()
        legacy_entry = registry["workspaces"]["legacy-review-plan"]
        legacy_entry["task_plan"].pop("requires_diff_review")
        workspaces._save_registry(registry)
    legacy_commit = await workspaces.commit_coding_workspace(
        "legacy-review-plan", "legacy compatible commit"
    )
    assert legacy_commit.is_success is True, legacy_commit.message
    assert json.loads(legacy_commit.message)["diff_review_bypassed"] is False

    await prepare("review-bypass-plan", 3)
    bypass_commit = await workspaces.commit_coding_workspace(
        "review-bypass-plan",
        "explicit review bypass",
        require_diff_review=False,
    )
    assert bypass_commit.is_success is True, bypass_commit.message
    assert json.loads(bypass_commit.message)["diff_review_bypassed"] is True
    stored = workspaces._load_registry()["workspaces"]["review-bypass-plan"]
    assert stored["last_commit_diff_review_bypassed"] is True

    assert (
        await workspaces.remove_coding_workspace("legacy-review-plan")
    ).is_success
    assert (
        await workspaces.remove_coding_workspace("review-bypass-plan")
    ).is_success


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


@pytest.mark.asyncio
async def test_delegated_step_requires_exact_report_workspace_and_verification(os_client):
    create_repository(os_client.sandbox_dir)
    workspaces = HostOSCodingWorkspaces(os_client)
    plans = HostOSCodingPlans(os_client, workspaces)
    verifier = HostOSCodingVerification(os_client, workspaces)
    created = await workspaces.create_coding_workspace(
        "sandbox/project", "delegated-task"
    )
    workspace = Path(json.loads(created.message)["workspace_path"])
    initialized = await plans.initialize_coding_task_plan(
        task_id="delegated-task",
        objective="Delegate one exact change",
        requirements=["The delegated change is independently reviewed"],
        steps=[{"id": "implement", "title": "Implement the change"}],
    )
    assert initialized.is_success is True

    with pytest.raises(ValueError, match="Stale coding plan revision"):
        await plans.bind_delegation(
            task_id="delegated-task",
            step_id="implement",
            delegation_id="staleone",
            role="coder",
            expected_revision=99,
        )

    binding = await plans.bind_delegation(
        task_id="delegated-task",
        step_id="implement",
        delegation_id="deadbeef",
        role="coder",
        expected_revision=1,
    )
    assert binding["bound_revision"] == 2
    bypass = await plans.update_coding_task_step(
        "delegated-task",
        "implement",
        "completed",
        evidence="Trust the worker without reconciliation.",
        expected_revision=2,
    )
    assert bypass.is_success is False
    assert "reconcile_coding_delegation" in bypass.message
    (workspace / "app.py").write_text("value = 2\n", encoding="utf-8")
    report_dir = os_client.system_dir / "subagents"
    report_dir.mkdir(parents=True, exist_ok=True)
    report = report_dir / "coder_deadbeef.md"
    report.write_text("Changed app.py to value 2.", encoding="utf-8")

    result = await plans.record_delegation_result(
        task_id="delegated-task",
        step_id="implement",
        delegation_id="deadbeef",
        status="completed",
        report_path="sandbox/_system/subagents/coder_deadbeef.md",
    )
    assert result["status"] == "reported"
    assert result["revision"] == 3

    unverified = await plans.reconcile_coding_delegation(
        "delegated-task",
        "implement",
        "deadbeef",
        "accept",
        "Reviewed report and diff.",
        expected_revision=3,
    )
    assert unverified.is_success is False
    assert "successful verification" in unverified.message

    (workspace / "app.py").write_text("value = 3\n", encoding="utf-8")
    drifted = await plans.reconcile_coding_delegation(
        "delegated-task",
        "implement",
        "deadbeef",
        "accept",
        "Reviewed report and diff.",
        expected_revision=3,
    )
    assert drifted.is_success is False
    assert "Workspace changed" in drifted.message
    (workspace / "app.py").write_text("value = 2\n", encoding="utf-8")

    verified = await verifier.run_coding_verification("delegated-task")
    assert verified.is_success is True, verified.message
    report.write_text("tampered", encoding="utf-8")
    tampered = await plans.reconcile_coding_delegation(
        "delegated-task",
        "implement",
        "deadbeef",
        "accept",
        "Reviewed report and diff.",
        expected_revision=3,
    )
    assert tampered.is_success is False
    assert "report changed" in tampered.message

    report.write_text("Changed app.py to value 2.", encoding="utf-8")
    accepted = await plans.reconcile_coding_delegation(
        "delegated-task",
        "implement",
        "deadbeef",
        "accept",
        "Reviewed exact report, diff, and passing verification.",
        expected_revision=3,
    )
    assert accepted.is_success is True, accepted.message
    accepted_payload = json.loads(accepted.message)
    assert accepted_payload["status"] == "completed"
    assert accepted_payload["delegation_status"] == "accepted"
    assert accepted_payload["revision"] == 4


@pytest.mark.asyncio
async def test_plan_revision_rebuilds_only_unfinished_graph_under_exact_state(os_client):
    create_repository(os_client.sandbox_dir)
    workspaces = HostOSCodingWorkspaces(os_client)
    plans = HostOSCodingPlans(os_client, workspaces)
    created = await workspaces.create_coding_workspace(
        "sandbox/project", "replan-task"
    )
    workspace = Path(json.loads(created.message)["workspace_path"])
    initialized = await plans.initialize_coding_task_plan(
        "replan-task",
        "Preserve the objective while adapting execution",
        ["The implementation remains correct"],
        [
            {"id": "inspect", "title": "Inspect the implementation"},
            {
                "id": "implement",
                "title": "Implement the change",
                "depends_on": ["inspect"],
            },
        ],
    )
    assert initialized.is_success is True
    inspected = await plans.update_coding_task_step(
        "replan-task",
        "inspect",
        "completed",
        "Inspected app.py and recorded its current behavior.",
        expected_revision=1,
    )
    assert inspected.is_success is True
    blocked = await plans.update_coding_task_step(
        "replan-task",
        "implement",
        "blocked",
        "The first approach cannot satisfy the observed constraint.",
        expected_revision=2,
    )
    assert blocked.is_success is True
    blocked_payload = json.loads(
        (await plans.get_coding_task_plan("replan-task")).message
    )
    assert blocked_payload["revision"] == 3
    assert blocked_payload["replanning"]["required"] is True
    assert blocked_payload["replanning"]["latest_reasons"][-1]["trigger"] == (
        "step_blocked"
    )

    proposed = [
        {"id": "inspect", "title": "Inspect the implementation"},
        {
            "id": "diagnose",
            "title": "Diagnose the observed constraint",
            "depends_on": ["inspect"],
        },
        {
            "id": "implement",
            "title": "Implement the change",
            "depends_on": ["diagnose"],
        },
    ]
    stale_workspace = await plans.revise_coding_task_plan(
        "replan-task",
        "Add a diagnosis step before retrying the implementation.",
        proposed,
        expected_revision=3,
        expected_workspace_fingerprint="0" * 64,
        reopen_step_ids=["implement"],
    )
    assert stale_workspace.is_success is False
    assert "Workspace changed" in stale_workspace.message

    fingerprint = (await workspaces.workspace_fingerprint(workspace))["fingerprint"]
    changed_completed = [
        {"id": "inspect", "title": "Rewrite completed inspection"},
        *proposed[1:],
    ]
    protected = await plans.revise_coding_task_plan(
        "replan-task",
        "Try to rewrite completed evidence.",
        changed_completed,
        expected_revision=3,
        expected_workspace_fingerprint=fingerprint,
        reopen_step_ids=["implement"],
    )
    assert protected.is_success is False
    assert "Protected step 'inspect'" in protected.message

    current = await workspaces.workspace_fingerprint(workspace)
    drifted = {**current, "fingerprint": "f" * 64}
    with patch.object(
        workspaces,
        "workspace_fingerprint",
        AsyncMock(side_effect=[current, drifted]),
    ):
        raced = await plans.revise_coding_task_plan(
            "replan-task",
            "Detect a concurrent workspace mutation.",
            proposed,
            expected_revision=3,
            expected_workspace_fingerprint=fingerprint,
            reopen_step_ids=["implement"],
        )
    assert raced.is_success is False
    assert "while the plan revision was being validated" in raced.message

    revised = await plans.revise_coding_task_plan(
        "replan-task",
        "Add a diagnosis step before retrying the implementation.",
        proposed,
        expected_revision=3,
        expected_workspace_fingerprint=fingerprint,
        reopen_step_ids=["implement"],
    )
    assert revised.is_success is True, revised.message
    revised_payload = json.loads(revised.message)
    assert revised_payload["revision"] == 4
    assert revised_payload["objective"] == (
        "Preserve the objective while adapting execution"
    )
    assert revised_payload["changes"] == {
        "added": ["diagnose"],
        "removed": [],
        "changed": ["implement"],
        "reopened": ["implement"],
    }
    assert revised_payload["replanning"]["required"] is False
    details = json.loads(
        (await plans.get_coding_task_plan("replan-task", section="steps")).message
    )
    assert [item["status"] for item in details["items"]] == [
        "completed",
        "pending",
        "pending",
    ]
    assert details["items"][2]["replan_evidence_history"][0]["status"] == (
        "blocked"
    )
    assert (await workspaces.remove_coding_workspace("replan-task")).is_success


@pytest.mark.asyncio
async def test_failed_verification_requires_replan_and_blocks_unverified_commit(os_client):
    create_repository(os_client.sandbox_dir)
    workspaces = HostOSCodingWorkspaces(os_client)
    plans = HostOSCodingPlans(os_client, workspaces)
    verifier = HostOSCodingVerification(os_client, workspaces, plans)
    created = await workspaces.create_coding_workspace(
        "sandbox/project", "verification-replan"
    )
    workspace = Path(json.loads(created.message)["workspace_path"])
    initialized = await plans.initialize_coding_task_plan(
        "verification-replan",
        "Keep Python syntax valid",
        ["Python compilation passes"],
        [{"id": "implement", "title": "Implement the requested change"}],
    )
    assert initialized.is_success is True
    completed = await plans.update_coding_task_step(
        "verification-replan",
        "implement",
        "completed",
        "Implementation finished and diff reviewed.",
        expected_revision=1,
    )
    assert completed.is_success is True
    requirement = await plans.update_coding_requirement(
        "verification-replan",
        "req_1",
        "satisfied",
        "The intended behavior is present pending final verification.",
        expected_revision=2,
    )
    assert requirement.is_success is True
    (workspace / "app.py").write_text("value =\n", encoding="utf-8")

    failed = await verifier.run_coding_verification(
        "verification-replan", checks=["python_compile"]
    )
    assert failed.is_success is False
    plan = json.loads(
        (await plans.get_coding_task_plan("verification-replan")).message
    )
    assert plan["revision"] == 4
    assert plan["summary"]["replan_required"] is True
    assert plan["summary"]["ready_for_commit"] is False
    assert plan["replanning"]["latest_reasons"][-1]["trigger"] == (
        "verification_failure"
    )

    rejected = await workspaces.commit_coding_workspace(
        "verification-replan",
        "do not commit a failed plan",
        require_verified=False,
    )
    assert rejected.is_success is False
    assert "replan_required=True" in rejected.message

    fingerprint = (await workspaces.workspace_fingerprint(workspace))["fingerprint"]
    revised = await plans.revise_coding_task_plan(
        "verification-replan",
        "Add an explicit repair step after failed compilation.",
        [
            {"id": "implement", "title": "Implement the requested change"},
            {
                "id": "repair_syntax",
                "title": "Repair syntax and rerun verification",
                "depends_on": ["implement"],
            },
        ],
        expected_revision=4,
        expected_workspace_fingerprint=fingerprint,
    )
    assert revised.is_success is True, revised.message
    revised_payload = json.loads(revised.message)
    assert revised_payload["revision"] == 5
    assert revised_payload["replanning"]["required"] is False
    assert revised_payload["changes"]["added"] == ["repair_syntax"]
    assert (
        await workspaces.remove_coding_workspace("verification-replan", force=True)
    ).is_success


@pytest.mark.asyncio
async def test_failed_delegation_blocks_parent_step_and_requests_replan(os_client):
    create_repository(os_client.sandbox_dir)
    workspaces = HostOSCodingWorkspaces(os_client)
    plans = HostOSCodingPlans(os_client, workspaces)
    created = await workspaces.create_coding_workspace(
        "sandbox/project", "delegation-replan"
    )
    workspace = Path(json.loads(created.message)["workspace_path"])
    initialized = await plans.initialize_coding_task_plan(
        "delegation-replan",
        "Recover from failed delegated work",
        ["The delegated task is eventually completed"],
        [{"id": "delegate", "title": "Delegate implementation"}],
    )
    assert initialized.is_success is True
    binding = await plans.bind_delegation(
        task_id="delegation-replan",
        step_id="delegate",
        delegation_id="failedworker",
        role="coder",
        expected_revision=1,
    )
    assert binding["bound_revision"] == 2
    result = await plans.record_delegation_result(
        task_id="delegation-replan",
        step_id="delegate",
        delegation_id="failedworker",
        status="failed",
        detail="Worker stopped before producing a reviewable report.",
    )
    assert result["revision"] == 3
    plan = json.loads(
        (await plans.get_coding_task_plan("delegation-replan")).message
    )
    assert plan["steps"][0]["status"] == "blocked"
    assert plan["steps"][0]["delegation_status"] == "failed"
    assert plan["replanning"]["latest_reasons"][-1]["trigger"] == (
        "delegation_failure"
    )

    fingerprint = (await workspaces.workspace_fingerprint(workspace))["fingerprint"]
    revised = await plans.revise_coding_task_plan(
        "delegation-replan",
        "Retry the unchanged step after inspecting the failed worker evidence.",
        [{"id": "delegate", "title": "Delegate implementation"}],
        expected_revision=3,
        expected_workspace_fingerprint=fingerprint,
        reopen_step_ids=["delegate"],
    )
    assert revised.is_success is True, revised.message
    payload = json.loads(revised.message)
    assert payload["steps"][0]["status"] == "pending"
    assert "delegation_status" not in payload["steps"][0]
    assert (await workspaces.remove_coding_workspace("delegation-replan")).is_success
