import json
import subprocess
from pathlib import Path

import pytest

from src.l1_databases.sql.db import SQLDB
from src.l1_databases.sql.management.ticks import SQLTicks
from src.l2_interfaces.host.os.skills.coding_plans import HostOSCodingPlans
from src.l2_interfaces.host.os.skills.coding_recovery import HostOSCodingRecovery
from src.l2_interfaces.host.os.skills.coding_workspaces import HostOSCodingWorkspaces


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


def create_repository(root: Path) -> None:
    repository = root / "project"
    repository.mkdir()
    run_git(repository, "init", "-b", "main")
    run_git(repository, "config", "user.name", "Test User")
    run_git(repository, "config", "user.email", "test@example.com")
    (repository / "app.py").write_text("value = 1\n", encoding="utf-8")
    (repository / "delete_me.txt").write_text("tracked\n", encoding="utf-8")
    run_git(repository, "add", "--all")
    run_git(repository, "commit", "-m", "initial")


async def create_recovery_stack(os_client, tmp_path: Path, task_id: str):
    create_repository(os_client.sandbox_dir)
    workspaces = HostOSCodingWorkspaces(os_client)
    created = await workspaces.create_coding_workspace("sandbox/project", task_id)
    assert created.is_success is True, created.message
    workspace = Path(json.loads(created.message)["workspace_path"])
    plans = HostOSCodingPlans(os_client, workspaces)
    initialized = await plans.initialize_coding_task_plan(
        task_id,
        "Exercise transactional recovery",
        ["Workspace and context can move backward and forward exactly"],
        [{"id": "work", "title": "Change the workspace"}],
    )
    assert initialized.is_success is True, initialized.message

    db = SQLDB(str(tmp_path / f"{task_id}.db"))
    await db.connect()
    ticks = SQLTicks(db)
    await ticks.bootstrap_migrations()
    recovery = HostOSCodingRecovery(os_client, workspaces, ticks)
    return workspaces, plans, recovery, ticks, db, workspace


@pytest.mark.asyncio
async def test_recovery_round_trip_preserves_index_plan_and_context(os_client, tmp_path):
    workspaces, plans, recovery, ticks, db, workspace = await create_recovery_stack(
        os_client, tmp_path, "round-trip"
    )
    try:
        (workspace / "app.py").write_text("value = 2\n", encoding="utf-8")
        run_git(workspace, "add", "app.py")
        (workspace / "delete_me.txt").unlink()
        (workspace / "checkpoint-note.txt").write_text(
            "checkpoint\n", encoding="utf-8"
        )
        checkpoint_state = await workspaces.workspace_fingerprint(workspace)
        before_tick = await ticks.save_tick("before", [], {"phase": "checkpoint"})

        created = await recovery.create_coding_recovery_checkpoint(
            "round-trip",
            "Known-good state before speculative work",
            checkpoint_state["fingerprint"],
        )
        assert created.is_success is True, created.message
        checkpoint_id = json.loads(created.message)["checkpoint_id"]

        # Produce a distinct future with both staged and unstaged changes to the
        # same file. The checkpoint must be able to recover this topology too.
        (workspace / "app.py").write_text("value = 3\n", encoding="utf-8")
        (workspace / "delete_me.txt").write_text("tracked\n", encoding="utf-8")
        (workspace / "checkpoint-note.txt").unlink()
        (workspace / "future.txt").write_text("future\n", encoding="utf-8")
        updated = await plans.update_coding_task_step(
            "round-trip",
            "work",
            "completed",
            evidence="Speculative implementation complete.",
            expected_revision=1,
        )
        assert updated.is_success is True, updated.message
        later_tick = await ticks.save_tick("later", [], {"phase": "future"})
        future_state = await workspaces.workspace_fingerprint(workspace)

        rewound = await recovery.rewind_coding_recovery_checkpoint(
            "round-trip", checkpoint_id, future_state["fingerprint"]
        )
        assert rewound.is_success is True, rewound.message
        rewind_payload = json.loads(rewound.message)
        assert await workspaces.workspace_fingerprint(workspace) == checkpoint_state
        assert (workspace / "app.py").read_text(encoding="utf-8") == "value = 2\n"
        assert not (workspace / "delete_me.txt").exists()
        assert (workspace / "checkpoint-note.txt").exists()
        assert run_git(workspace, "diff", "--cached", "--name-only") == "app.py"
        assert run_git(workspace, "diff", "--name-only") == "delete_me.txt"
        assert run_git(workspace, "ls-files", "--others", "--exclude-standard") == (
            "checkpoint-note.txt"
        )
        plan = json.loads((await plans.get_coding_task_plan("round-trip")).message)
        assert plan["revision"] == 1
        visible_ids = [tick.id for tick in await ticks.get_ticks(limit=20)]
        assert before_tick in visible_ids
        assert later_tick not in visible_ids

        forward_id = rewind_payload["forward_checkpoint_id"]
        current = await workspaces.workspace_fingerprint(workspace)
        returned = await recovery.rewind_coding_recovery_checkpoint(
            "round-trip", forward_id, current["fingerprint"]
        )
        assert returned.is_success is True, returned.message
        assert await workspaces.workspace_fingerprint(workspace) == future_state
        assert (workspace / "app.py").read_text(encoding="utf-8") == "value = 3\n"
        assert (workspace / "delete_me.txt").exists()
        assert not (workspace / "checkpoint-note.txt").exists()
        assert (workspace / "future.txt").exists()
        assert run_git(workspace, "diff", "--cached", "--name-only") == "app.py"
        assert run_git(workspace, "diff", "--name-only") == "app.py"
        plan = json.loads((await plans.get_coding_task_plan("round-trip")).message)
        assert plan["revision"] == 2
        assert later_tick in [tick.id for tick in await ticks.get_ticks(limit=20)]
    finally:
        await db.disconnect()


@pytest.mark.asyncio
async def test_recovery_rejects_stale_state_and_compensates_failed_context_branch(
    os_client, tmp_path, monkeypatch
):
    workspaces, plans, recovery, ticks, db, workspace = await create_recovery_stack(
        os_client, tmp_path, "compensate"
    )
    try:
        checkpoint_state = await workspaces.workspace_fingerprint(workspace)
        created = await recovery.create_coding_recovery_checkpoint(
            "compensate", "baseline", checkpoint_state["fingerprint"]
        )
        checkpoint_id = json.loads(created.message)["checkpoint_id"]
        (workspace / "app.py").write_text("value = 9\n", encoding="utf-8")

        stale = await recovery.rewind_coding_recovery_checkpoint(
            "compensate", checkpoint_id, checkpoint_state["fingerprint"]
        )
        assert stale.is_success is False
        listed = json.loads(
            (await recovery.list_coding_recovery_checkpoints("compensate")).message
        )
        assert [item["checkpoint_id"] for item in listed] == [checkpoint_id]

        future_state = await workspaces.workspace_fingerprint(workspace)
        original_plan = json.loads(
            (await plans.get_coding_task_plan("compensate")).message
        )

        async def fail_branch(*args, **kwargs):
            raise RuntimeError("injected timeline failure")

        monkeypatch.setattr(ticks, "branch_from_cursor", fail_branch)
        failed = await recovery.rewind_coding_recovery_checkpoint(
            "compensate", checkpoint_id, future_state["fingerprint"]
        )
        assert failed.is_success is False
        assert "was compensated" in failed.message
        assert await workspaces.workspace_fingerprint(workspace) == future_state
        assert (workspace / "app.py").read_text(encoding="utf-8") == "value = 9\n"
        restored_plan = json.loads(
            (await plans.get_coding_task_plan("compensate")).message
        )
        assert restored_plan == original_plan
        listed = json.loads(
            (await recovery.list_coding_recovery_checkpoints("compensate")).message
        )
        assert listed[0]["kind"] == "automatic_forward"
    finally:
        await db.disconnect()


@pytest.mark.asyncio
async def test_recovery_rechecks_workspace_immediately_before_destructive_restore(
    os_client, tmp_path, monkeypatch
):
    workspaces, _, recovery, _, db, workspace = await create_recovery_stack(
        os_client, tmp_path, "last-moment-change"
    )
    try:
        checkpoint_state = await workspaces.workspace_fingerprint(workspace)
        created = await recovery.create_coding_recovery_checkpoint(
            "last-moment-change", "baseline", checkpoint_state["fingerprint"]
        )
        checkpoint_id = json.loads(created.message)["checkpoint_id"]
        (workspace / "app.py").write_text("value = 4\n", encoding="utf-8")
        inspected = await workspaces.workspace_fingerprint(workspace)

        real_save = workspaces._save_registry
        injected = False

        def save_then_inject_change(registry):
            nonlocal injected
            real_save(registry)
            entry = registry["workspaces"]["last-moment-change"]
            checkpoints = entry.get("recovery_checkpoints", {}).values()
            if not injected and any(
                item.get("kind") == "automatic_forward" for item in checkpoints
            ):
                injected = True
                (workspace / "arrived-late.txt").write_text(
                    "do not overwrite\n", encoding="utf-8"
                )

        monkeypatch.setattr(workspaces, "_save_registry", save_then_inject_change)
        rejected = await recovery.rewind_coding_recovery_checkpoint(
            "last-moment-change", checkpoint_id, inspected["fingerprint"]
        )
        assert rejected.is_success is False
        assert "aborted without mutation" in rejected.message
        assert (workspace / "app.py").read_text(encoding="utf-8") == "value = 4\n"
        assert (workspace / "arrived-late.txt").read_text(encoding="utf-8") == (
            "do not overwrite\n"
        )
        listed = json.loads(
            (
                await recovery.list_coding_recovery_checkpoints(
                    "last-moment-change"
                )
            ).message
        )
        assert listed[0]["kind"] == "automatic_forward"
    finally:
        await db.disconnect()
