import json
import subprocess
from pathlib import Path

import pytest

from src.l2_interfaces.host.os.skills.coding_verification import (
    HostOSCodingVerification,
)
from src.l2_interfaces.host.os.skills.coding_workspaces import (
    HostOSCodingWorkspaces,
)


def run_git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def create_repository(root: Path, name: str) -> Path:
    repository = root / name
    repository.mkdir()
    run_git(repository, "init", "-b", "main")
    run_git(repository, "config", "user.name", "Test User")
    run_git(repository, "config", "user.email", "test@example.com")
    (repository / "app.py").write_text("value = 1\n", encoding="utf-8")
    run_git(repository, "add", "--all")
    run_git(repository, "commit", "-m", "initial")
    return repository


async def create_task_commit(
    manager: HostOSCodingWorkspaces,
    os_client,
    repository_name: str,
    task_id: str,
    content: str = "value = 2\n",
) -> tuple[Path, dict]:
    created = await manager.create_coding_workspace(
        f"sandbox/{repository_name}", task_id
    )
    assert created.is_success, created.message
    workspace = Path(json.loads(created.message)["workspace_path"])
    (workspace / "app.py").write_text(content, encoding="utf-8")
    verifier = HostOSCodingVerification(os_client, manager)
    verified = await verifier.run_coding_verification(task_id)
    assert verified.is_success, verified.message
    committed = await manager.commit_coding_workspace(task_id, "task change")
    assert committed.is_success, committed.message
    return workspace, json.loads(committed.message)


@pytest.mark.asyncio
async def test_delivery_preflight_is_exact_local_and_becomes_stale(os_client):
    repository = create_repository(os_client.sandbox_dir, "delivery_fast_forward")
    manager = HostOSCodingWorkspaces(os_client)
    workspace, committed = await create_task_commit(
        manager, os_client, repository.name, "delivery-fast-forward"
    )

    before_head = run_git(workspace, "rev-parse", "HEAD")
    before_status = run_git(workspace, "status", "--porcelain=v1")
    prepared = await manager.prepare_coding_workspace_delivery(
        "delivery-fast-forward", "main"
    )

    assert prepared.is_success, prepared.message
    payload = json.loads(prepared.message)
    assert payload["head"] == committed["commit"] == before_head
    assert payload["relationship"] == "target_ancestor"
    assert payload["integration_outcome"] == "fast_forward_target"
    assert payload["ahead_count"] == 1
    assert payload["behind_count"] == 0
    assert payload["ready_for_delivery"] is True
    assert payload["network_accessed"] is False
    assert payload["target_snapshot_source"] == "local_git_refs"
    assert len(payload["delivery_contract_sha256"]) == 64
    assert run_git(workspace, "rev-parse", "HEAD") == before_head
    assert run_git(workspace, "status", "--porcelain=v1") == before_status

    current = await manager.get_coding_workspace_delivery_status(
        "delivery-fast-forward"
    )
    current_payload = json.loads(current.message)
    assert current_payload["state"] == "current"
    assert current_payload["ready_for_delivery"] is True
    workspace_status = await manager.get_coding_workspace_status(
        "delivery-fast-forward"
    )
    compact_preflight = json.loads(workspace_status.message)["delivery_preflight"]
    assert "conflict_paths" not in compact_preflight
    assert compact_preflight["delivery_contract_sha256"] == payload[
        "delivery_contract_sha256"
    ]

    (repository / "target.py").write_text("target = True\n", encoding="utf-8")
    run_git(repository, "add", "--all")
    run_git(repository, "commit", "-m", "move target")
    stale = await manager.get_coding_workspace_delivery_status(
        "delivery-fast-forward"
    )
    stale_payload = json.loads(stale.message)
    assert stale_payload["state"] == "stale"
    assert stale_payload["ready_for_delivery"] is False
    assert "target_ref_moved" in stale_payload["stale_reasons"]


@pytest.mark.asyncio
async def test_delivery_preflight_predicts_clean_divergent_merge(os_client):
    repository = create_repository(os_client.sandbox_dir, "delivery_clean_merge")
    manager = HostOSCodingWorkspaces(os_client)
    created = await manager.create_coding_workspace(
        f"sandbox/{repository.name}", "delivery-clean-merge"
    )
    workspace = Path(json.loads(created.message)["workspace_path"])

    (repository / "target.py").write_text("target = True\n", encoding="utf-8")
    run_git(repository, "add", "--all")
    run_git(repository, "commit", "-m", "target change")
    (workspace / "app.py").write_text("value = 2\n", encoding="utf-8")
    verifier = HostOSCodingVerification(os_client, manager)
    assert (await verifier.run_coding_verification("delivery-clean-merge")).is_success
    committed = await manager.commit_coding_workspace(
        "delivery-clean-merge", "task change"
    )
    assert committed.is_success, committed.message

    prepared = await manager.prepare_coding_workspace_delivery(
        "delivery-clean-merge", "main"
    )

    assert prepared.is_success, prepared.message
    payload = json.loads(prepared.message)
    assert payload["relationship"] == "diverged"
    assert payload["integration_outcome"] == "clean_merge"
    assert payload["ahead_count"] == 1
    assert payload["behind_count"] == 1
    assert payload["has_conflicts"] is False
    assert payload["needs_target_sync"] is True
    assert payload["ready_for_delivery"] is True
    assert len(payload["merge_tree_oid"]) in (40, 64)


@pytest.mark.asyncio
async def test_delivery_preflight_reports_conflicts_without_mutation(os_client):
    repository = create_repository(os_client.sandbox_dir, "delivery_conflict")
    manager = HostOSCodingWorkspaces(os_client)
    created = await manager.create_coding_workspace(
        f"sandbox/{repository.name}", "delivery-conflict"
    )
    workspace = Path(json.loads(created.message)["workspace_path"])

    (repository / "app.py").write_text("value = 'target'\n", encoding="utf-8")
    run_git(repository, "add", "--all")
    run_git(repository, "commit", "-m", "conflicting target")
    (workspace / "app.py").write_text("value = 'task'\n", encoding="utf-8")
    verifier = HostOSCodingVerification(os_client, manager)
    assert (await verifier.run_coding_verification("delivery-conflict")).is_success
    committed = await manager.commit_coding_workspace(
        "delivery-conflict", "conflicting task"
    )
    assert committed.is_success, committed.message
    before_head = run_git(workspace, "rev-parse", "HEAD")
    before_target = run_git(repository, "rev-parse", "main")

    prepared = await manager.prepare_coding_workspace_delivery(
        "delivery-conflict", "main"
    )

    assert prepared.is_success, prepared.message
    payload = json.loads(prepared.message)
    assert payload["integration_outcome"] == "conflicted_merge"
    assert payload["has_conflicts"] is True
    assert payload["needs_conflict_resolution"] is True
    assert payload["ready_for_delivery"] is False
    assert payload["conflict_paths"] == ["app.py"]
    assert payload["conflict_paths_truncated"] is False
    assert run_git(workspace, "rev-parse", "HEAD") == before_head
    assert run_git(repository, "rev-parse", "main") == before_target
    assert run_git(workspace, "status", "--porcelain=v1") == ""
    merge_head_path = Path(run_git(workspace, "rev-parse", "--git-path", "MERGE_HEAD"))
    assert not merge_head_path.exists()


@pytest.mark.asyncio
async def test_delivery_requires_managed_unbypassed_clean_commit(os_client):
    repository = create_repository(os_client.sandbox_dir, "delivery_gates")
    manager = HostOSCodingWorkspaces(os_client)
    created = await manager.create_coding_workspace(
        f"sandbox/{repository.name}", "delivery-gates"
    )
    workspace = Path(json.loads(created.message)["workspace_path"])

    (workspace / "app.py").write_text("value = 2\n", encoding="utf-8")
    dirty = await manager.prepare_coding_workspace_delivery(
        "delivery-gates", "main"
    )
    assert dirty.is_success is False
    assert "uncommitted changes" in dirty.message

    bypassed_commit = await manager.commit_coding_workspace(
        "delivery-gates", "unchecked task", require_verified=False
    )
    assert bypassed_commit.is_success, bypassed_commit.message
    rejected = await manager.prepare_coding_workspace_delivery(
        "delivery-gates", "main"
    )
    assert rejected.is_success is False
    assert "gate bypasses" in rejected.message

    explicit = await manager.prepare_coding_workspace_delivery(
        "delivery-gates", "main", allow_bypassed_commit=True
    )
    assert explicit.is_success, explicit.message
    payload = json.loads(explicit.message)
    assert payload["allow_bypassed_commit"] is True
    assert payload["commit_gate_bypasses"]["verification"] is True

    invalid_ref = await manager.prepare_coding_workspace_delivery(
        "delivery-gates", "--upload-pack=bad", allow_bypassed_commit=True
    )
    assert invalid_ref.is_success is False
    assert "Invalid target_ref" in invalid_ref.message
