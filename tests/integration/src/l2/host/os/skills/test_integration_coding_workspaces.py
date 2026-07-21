import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from src.l2_interfaces.host.os.skills.coding_workspaces import (
    HostOSCodingWorkspaces,
)
from src.l2_interfaces.host.os.skills.coding_verification import (
    HostOSCodingVerification,
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


def create_repository(root: Path, name: str = "project") -> Path:
    repository = root / name
    repository.mkdir()
    run_git(repository, "init", "-b", "main")
    run_git(repository, "config", "user.name", "Test User")
    run_git(repository, "config", "user.email", "test@example.com")
    (repository / "app.py").write_text("value = 1\n", encoding="utf-8")
    run_git(repository, "add", "--all")
    run_git(repository, "commit", "-m", "initial")
    return repository


@pytest.mark.asyncio
async def test_coding_workspace_isolates_commits_and_persists_status(os_client):
    repository = create_repository(os_client.sandbox_dir)
    manager = HostOSCodingWorkspaces(os_client)

    created = await manager.create_coding_workspace(
        repository_path="sandbox/project", task_id="feature-123"
    )

    assert created.is_success is True, created.message
    payload = json.loads(created.message)
    workspace = Path(payload["workspace_path"])
    assert workspace.is_relative_to(os_client.sandbox_dir / ".jawl-worktrees")
    assert workspace.is_dir()
    assert run_git(workspace, "branch", "--show-current") == payload["branch"]

    (workspace / "app.py").write_text("value = 2\n", encoding="utf-8")
    assert (repository / "app.py").read_text(encoding="utf-8") == "value = 1\n"

    # A new manager instance simulates a later ReAct cycle/framework restart.
    resumed_manager = HostOSCodingWorkspaces(os_client)
    verifier = HostOSCodingVerification(os_client, resumed_manager)
    status = await resumed_manager.get_coding_workspace_status("feature-123")
    status_payload = json.loads(status.message)
    assert status_payload["state"] == "dirty"
    assert "app.py" in status_payload["diff_stat"]
    diff_result = await resumed_manager.get_coding_workspace_diff(
        "feature-123", file_path="app.py"
    )
    diff_payload = json.loads(diff_result.message)
    assert "-value = 1" in diff_payload["diff"]
    assert "+value = 2" in diff_payload["diff"]
    assert diff_payload["changed_file_count"] == 1

    verified = await verifier.run_coding_verification("feature-123")
    assert verified.is_success is True, verified.message
    assert "python_compile" in json.loads(verified.message)["checks"]
    cache_dir = workspace / "__pycache__"
    cache_dir.mkdir()
    (cache_dir / "app.cpython-test.pyc").write_bytes(b"generated cache")

    committed = await resumed_manager.commit_coding_workspace(
        "feature-123", "implement feature"
    )
    commit_payload = json.loads(committed.message)
    assert committed.is_success is True
    assert run_git(workspace, "rev-parse", "HEAD") == commit_payload["commit"]
    committed_files = run_git(workspace, "show", "--name-only", "--format=", "HEAD")
    assert "__pycache__" not in committed_files
    assert ".pyc" not in committed_files
    committed_status = await verifier.get_coding_verification_status("feature-123")
    assert json.loads(committed_status.message)["is_current"] is True

    clean_status = await resumed_manager.get_coding_workspace_status("feature-123")
    assert json.loads(clean_status.message)["state"] == "clean"

    removed = await resumed_manager.remove_coding_workspace("feature-123")
    removed_payload = json.loads(removed.message)
    assert removed.is_success is True
    assert not workspace.exists()
    assert removed_payload["preserved_branch"] == payload["branch"]
    assert run_git(
        repository, "show-ref", "--verify", f"refs/heads/{payload['branch']}"
    )


@pytest.mark.asyncio
async def test_coding_workspace_rejects_dirty_base_by_default(os_client):
    repository = create_repository(os_client.sandbox_dir)
    (repository / "uncommitted.py").write_text("pending = True\n", encoding="utf-8")
    manager = HostOSCodingWorkspaces(os_client)

    rejected = await manager.create_coding_workspace(
        repository_path="sandbox/project", task_id="dirty-base"
    )

    assert rejected.is_success is False
    assert "uncommitted changes" in rejected.message
    assert json.loads(manager.registry_file.read_text(encoding="utf-8"))[
        "workspaces"
    ] == {}


@pytest.mark.asyncio
async def test_coding_workspace_force_cleanup_is_explicit(os_client):
    create_repository(os_client.sandbox_dir)
    manager = HostOSCodingWorkspaces(os_client)
    created = await manager.create_coding_workspace(
        repository_path="sandbox/project", task_id="cleanup-guard"
    )
    workspace = Path(json.loads(created.message)["workspace_path"])
    (workspace / "dirty.txt").write_text("do not lose silently\n", encoding="utf-8")

    refused = await manager.remove_coding_workspace("cleanup-guard")

    assert refused.is_success is False
    assert "uncommitted changes" in refused.message
    assert workspace.exists()

    forced = await manager.remove_coding_workspace("cleanup-guard", force=True)
    forced_payload = json.loads(forced.message)

    assert forced.is_success is True
    assert not workspace.exists()
    assert forced_payload["recovery_stash"]
    assert "dirty.txt" in run_git(
        os_client.sandbox_dir / "project",
        "stash",
        "show",
        "--name-only",
        "--include-untracked",
        forced_payload["recovery_stash"],
    )


@pytest.mark.asyncio
async def test_coding_workspace_validates_task_identifier(os_client):
    create_repository(os_client.sandbox_dir)
    manager = HostOSCodingWorkspaces(os_client)

    result = await manager.create_coding_workspace(
        repository_path="sandbox/project", task_id="../escape"
    )

    assert result.is_success is False
    assert "task_id must be" in result.message


@pytest.mark.asyncio
async def test_parallel_tasks_receive_independent_worktrees(os_client):
    repository = create_repository(os_client.sandbox_dir)
    manager = HostOSCodingWorkspaces(os_client)

    first, second = await asyncio.gather(
        manager.create_coding_workspace("sandbox/project", "parallel-a"),
        manager.create_coding_workspace("sandbox/project", "parallel-b"),
    )

    assert first.is_success is True, first.message
    assert second.is_success is True, second.message
    first_payload = json.loads(first.message)
    second_payload = json.loads(second.message)
    first_workspace = Path(first_payload["workspace_path"])
    second_workspace = Path(second_payload["workspace_path"])
    assert first_workspace != second_workspace
    assert first_payload["branch"] != second_payload["branch"]

    (first_workspace / "app.py").write_text("value = 'a'\n", encoding="utf-8")
    (second_workspace / "app.py").write_text("value = 'b'\n", encoding="utf-8")

    assert (first_workspace / "app.py").read_text(encoding="utf-8") == "value = 'a'\n"
    assert (second_workspace / "app.py").read_text(encoding="utf-8") == "value = 'b'\n"
    assert (repository / "app.py").read_text(encoding="utf-8") == "value = 1\n"

    assert (await manager.remove_coding_workspace("parallel-a", force=True)).is_success
    assert (await manager.remove_coding_workspace("parallel-b", force=True)).is_success


@pytest.mark.asyncio
async def test_commit_requires_verification_of_exact_workspace_state(os_client):
    create_repository(os_client.sandbox_dir)
    manager = HostOSCodingWorkspaces(os_client)
    verifier = HostOSCodingVerification(os_client, manager)
    created = await manager.create_coding_workspace(
        "sandbox/project", "verified-state"
    )
    workspace = Path(json.loads(created.message)["workspace_path"])
    (workspace / "app.py").write_text("value = 2\n", encoding="utf-8")

    unverified = await manager.commit_coding_workspace(
        "verified-state", "must not commit"
    )
    assert unverified.is_success is False
    assert "exact workspace state" in unverified.message

    verified = await verifier.run_coding_verification(
        "verified-state", checks=["git_diff_check", "python_compile"]
    )
    assert verified.is_success is True, verified.message

    (workspace / "app.py").write_text("value = 3\n", encoding="utf-8")
    stale = await manager.commit_coding_workspace("verified-state", "also rejected")
    status = await verifier.get_coding_verification_status("verified-state")

    assert stale.is_success is False
    assert json.loads(status.message)["is_current"] is False
    assert (await manager.remove_coding_workspace("verified-state", force=True)).is_success


@pytest.mark.asyncio
async def test_verification_detects_workspace_change_during_checks(os_client, monkeypatch):
    create_repository(os_client.sandbox_dir)
    manager = HostOSCodingWorkspaces(os_client)
    verifier = HostOSCodingVerification(os_client, manager)
    created = await manager.create_coding_workspace(
        "sandbox/project", "moving-target"
    )
    workspace = Path(json.loads(created.message)["workspace_path"])
    original_runner = verifier._run_command

    async def changing_runner(check, command, worktree, timeout_sec):
        result = await original_runner(check, command, worktree, timeout_sec)
        (worktree / "changed_during_test.txt").write_text(
            "new state\n", encoding="utf-8"
        )
        return result

    monkeypatch.setattr(verifier, "_run_command", changing_runner)
    verification = await verifier.run_coding_verification(
        "moving-target", checks=["git_diff_check"]
    )

    assert verification.is_success is False
    assert json.loads(verification.message)["state"] == "stale"
    assert (await manager.remove_coding_workspace("moving-target", force=True)).is_success


@pytest.mark.asyncio
async def test_verification_bounds_failure_output(os_client):
    create_repository(os_client.sandbox_dir)
    manager = HostOSCodingWorkspaces(os_client)
    verifier = HostOSCodingVerification(os_client, manager)
    created = await manager.create_coding_workspace(
        "sandbox/project", "bounded-output"
    )
    workspace = Path(json.loads(created.message)["workspace_path"])
    tests_dir = workspace / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_noisy.py").write_text(
        "def test_noisy():\n    assert False, 'x' * 50000\n", encoding="utf-8"
    )

    verification = await verifier.run_coding_verification(
        "bounded-output", checks=["pytest"]
    )
    payload = json.loads(verification.message)
    result = payload["results"][0]

    assert verification.is_success is False
    assert payload["state"] == "failed"
    assert result["stdout_truncated"] is True
    assert len(result["stdout"].encode("utf-8")) <= 16_100
    assert (await manager.remove_coding_workspace("bounded-output", force=True)).is_success


@pytest.mark.asyncio
async def test_verification_timeout_kills_process_tree(os_client):
    create_repository(os_client.sandbox_dir)
    manager = HostOSCodingWorkspaces(os_client)
    verifier = HostOSCodingVerification(os_client, manager)
    created = await manager.create_coding_workspace(
        "sandbox/project", "timeout-check"
    )
    workspace = Path(json.loads(created.message)["workspace_path"])
    tests_dir = workspace / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_slow.py").write_text(
        "import time\n\ndef test_slow():\n    time.sleep(30)\n", encoding="utf-8"
    )

    verification = await verifier.run_coding_verification(
        "timeout-check", checks=["pytest"], timeout_sec=1
    )
    payload = json.loads(verification.message)

    assert verification.is_success is False
    assert payload["results"][0]["timed_out"] is True
    assert payload["results"][0]["duration_sec"] < 10
    assert (await manager.remove_coding_workspace("timeout-check", force=True)).is_success


@pytest.mark.asyncio
async def test_old_running_verification_is_reported_interrupted(os_client):
    create_repository(os_client.sandbox_dir)
    manager = HostOSCodingWorkspaces(os_client)
    created = await manager.create_coding_workspace(
        "sandbox/project", "interrupted-check"
    )
    assert created.is_success
    async with manager._lock:
        registry = manager._load_registry()
        entry = registry["workspaces"]["interrupted-check"]
        entry["verification_runs"] = [
            {
                "run_id": "old-run",
                "session_id": "old-process",
                "state": "running",
                "started_at": "2026-01-01T00:00:00+00:00",
                "results": [],
            }
        ]
        manager._save_registry(registry)

    resumed = HostOSCodingVerification(os_client, HostOSCodingWorkspaces(os_client))
    status = await resumed.get_coding_verification_status("interrupted-check")
    runs = json.loads(status.message)["verification_runs"]

    assert runs[0]["state"] == "interrupted"
    assert (await manager.remove_coding_workspace("interrupted-check")).is_success


@pytest.mark.asyncio
async def test_fingerprint_ignores_only_cache_not_untracked_build_source(os_client):
    create_repository(os_client.sandbox_dir)
    manager = HostOSCodingWorkspaces(os_client)
    created = await manager.create_coding_workspace(
        "sandbox/project", "fingerprint-scope"
    )
    workspace = Path(json.loads(created.message)["workspace_path"])
    initial = await manager.workspace_fingerprint(workspace)

    cache_dir = workspace / "__pycache__"
    cache_dir.mkdir()
    (cache_dir / "app.pyc").write_bytes(b"cache")
    with_cache = await manager.workspace_fingerprint(workspace)

    build_dir = workspace / "build"
    build_dir.mkdir()
    (build_dir / "new_source.py").write_text("important = True\n", encoding="utf-8")
    with_source = await manager.workspace_fingerprint(workspace)

    assert with_cache == initial
    assert with_source["fingerprint"] != initial["fingerprint"]
    assert (await manager.remove_coding_workspace("fingerprint-scope", force=True)).is_success


@pytest.mark.asyncio
async def test_workspace_diff_includes_untracked_redacts_secrets_and_bounds_output(os_client):
    create_repository(os_client.sandbox_dir)
    manager = HostOSCodingWorkspaces(os_client)
    created = await manager.create_coding_workspace(
        "sandbox/project", "diff-review"
    )
    workspace = Path(json.loads(created.message)["workspace_path"])
    secret = "ghp_1234567890abcdefghij"
    (workspace / "new_file.py").write_text(
        f"api_key={secret}\n" + "payload = '" + "x" * 3000 + "'\n",
        encoding="utf-8",
    )

    result = await manager.get_coding_workspace_diff(
        "diff-review", file_path="new_file.py", max_chars=1000
    )
    payload = json.loads(result.message)
    escaped = await manager.get_coding_workspace_diff(
        "diff-review", file_path="../outside.py"
    )

    assert result.is_success is True
    assert payload["untracked_files"] == ["new_file.py"]
    assert "new file mode (untracked)" in payload["diff"]
    assert secret not in payload["diff"]
    assert "[REDACTED]" in payload["diff"]
    assert payload["truncated"] is True
    assert escaped.is_success is False
    assert (await manager.remove_coding_workspace("diff-review", force=True)).is_success


def test_untracked_diff_preview_reads_with_hard_bound(tmp_path):
    artifact = tmp_path / "large.txt"
    artifact.write_bytes(b"a" * 2_000_000)

    preview = HostOSCodingWorkspaces._untracked_diff_preview(
        artifact, "large.txt", max_bytes=256
    )

    assert len(preview) < 1000
    assert "truncated after 256 of 2000000 bytes" in preview


@pytest.mark.asyncio
async def test_workspace_diff_paginates_changed_files(os_client):
    create_repository(os_client.sandbox_dir)
    manager = HostOSCodingWorkspaces(os_client)
    created = await manager.create_coding_workspace(
        "sandbox/project", "diff-pagination"
    )
    workspace = Path(json.loads(created.message)["workspace_path"])
    for index in range(5):
        (workspace / f"new_{index}.py").write_text(
            f"value = {index}\n", encoding="utf-8"
        )

    first = json.loads(
        (
            await manager.get_coding_workspace_diff(
                "diff-pagination", file_offset=0, file_limit=2
            )
        ).message
    )
    last = json.loads(
        (
            await manager.get_coding_workspace_diff(
                "diff-pagination", file_offset=4, file_limit=2
            )
        ).message
    )

    assert first["changed_file_count"] == 5
    assert first["changed_files"] == ["new_0.py", "new_1.py"]
    assert first["file_page_has_more"] is True
    assert "new_2.py" not in first["diff"]
    assert last["changed_files"] == ["new_4.py"]
    assert last["file_page_has_more"] is False
    assert (await manager.remove_coding_workspace("diff-pagination", force=True)).is_success


@pytest.mark.asyncio
async def test_workspace_diff_bounds_tracked_git_output_during_collection(os_client):
    create_repository(os_client.sandbox_dir)
    manager = HostOSCodingWorkspaces(os_client)
    created = await manager.create_coding_workspace(
        "sandbox/project", "large-tracked-diff"
    )
    workspace = Path(json.loads(created.message)["workspace_path"])
    (workspace / "app.py").write_text(
        "value = '" + "x" * 200_000 + "'\n", encoding="utf-8"
    )

    result = await manager.get_coding_workspace_diff(
        "large-tracked-diff", file_path="app.py", max_chars=1000
    )
    payload = json.loads(result.message)

    assert result.is_success is True
    assert payload["diff_collection_truncated"] is True
    assert payload["original_diff_chars"] is None
    assert payload["collected_diff_chars"] <= 4000
    assert "Git output byte limit reached" in payload["diff"]
    assert len(payload["diff"]) < 1200
    assert (
        await manager.remove_coding_workspace("large-tracked-diff", force=True)
    ).is_success


@pytest.mark.asyncio
async def test_repository_verification_policy_selects_only_allowlisted_profiles(os_client):
    create_repository(os_client.sandbox_dir)
    manager = HostOSCodingWorkspaces(os_client)
    verifier = HostOSCodingVerification(os_client, manager)
    created = await manager.create_coding_workspace(
        "sandbox/project", "verification-policy"
    )
    workspace = Path(json.loads(created.message)["workspace_path"])
    policy_dir = workspace / ".jawl"
    policy_dir.mkdir()
    policy_file = policy_dir / "verification.json"
    policy_file.write_text(
        json.dumps(
            {
                "version": 1,
                "checks": ["git_diff_check", "python_compile"],
                "timeout_sec": 45,
                "stop_on_failure": False,
            }
        ),
        encoding="utf-8",
    )

    verified = await verifier.run_coding_verification("verification-policy")
    payload = json.loads(verified.message)

    assert verified.is_success is True, verified.message
    assert payload["checks"] == ["git_diff_check", "python_compile"]
    assert payload["timeout_sec"] == 45
    assert payload["stop_on_failure"] is False
    assert payload["policy"]["path"] == ".jawl/verification.json"
    assert len(payload["policy"]["sha256"]) == 64

    policy_file.write_text(
        json.dumps(
            {
                "version": 1,
                "checks": ["git_diff_check"],
                "commands": [["powershell", "-Command", "echo unsafe"]],
            }
        ),
        encoding="utf-8",
    )
    rejected = await verifier.run_coding_verification("verification-policy")
    assert rejected.is_success is False
    assert "Arbitrary commands" in rejected.message
    assert (
        await manager.remove_coding_workspace("verification-policy", force=True)
    ).is_success
