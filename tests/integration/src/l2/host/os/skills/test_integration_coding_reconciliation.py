import json
import subprocess
from pathlib import Path

import pytest

from src.l2_interfaces.host.os.skills.coding_plans import HostOSCodingPlans
from src.l2_interfaces.host.os.skills.coding_workspaces import (
    HostOSCodingWorkspaces,
)
from src.l3_agent.skills.coding_reconciliation import (
    CodingActionReconciliation,
)
from src.l3_agent.skills.journal import ActionJournal
from src.utils.event.bus import EventBus
from src.utils.event.registry import Events


def git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


async def recovery_stack(os_client, tmp_path, task_id):
    repository = os_client.sandbox_dir / f"project-{task_id}"
    repository.mkdir()
    git(repository, "init", "-b", "main")
    git(repository, "config", "user.name", "Test User")
    git(repository, "config", "user.email", "test@example.com")
    (repository / "app.py").write_text("value = 1\n", encoding="utf-8")
    git(repository, "add", "--all")
    git(repository, "commit", "-m", "initial")
    workspaces = HostOSCodingWorkspaces(os_client)
    created = await workspaces.create_coding_workspace(
        f"sandbox/project-{task_id}", task_id
    )
    assert created.is_success is True, created.message
    plans = HostOSCodingPlans(os_client, workspaces)
    initialized = await plans.initialize_coding_task_plan(
        task_id,
        "Resume safely after restart",
        ["No uncertain action is replayed blindly"],
        [{"id": "implement", "title": "Implement the change"}],
    )
    assert initialized.is_success is True, initialized.message
    journal_path = tmp_path / f"{task_id}.jsonl"
    old_journal = ActionJournal(journal_path)
    return workspaces, plans, old_journal, journal_path


@pytest.mark.asyncio
async def test_startup_reconciliation_marks_uncertain_action_and_is_idempotent(
    os_client, tmp_path
):
    workspaces, plans, old_journal, journal_path = await recovery_stack(
        os_client, tmp_path, "startup-uncertain"
    )
    await old_journal.record(
        "plan_started",
        plan_id="prior-actions",
        task_ids=["startup-uncertain"],
        actions=[{"action_id": "edit", "tool_name": "apply_file_patch"}],
    )
    await old_journal.record(
        "action_started",
        plan_id="prior-actions",
        action_id="edit",
        tool_name="apply_file_patch",
        task_id="startup-uncertain",
    )
    current_journal = ActionJournal(journal_path)
    bus = EventBus()
    observed = []

    async def capture(**payload):
        observed.append(payload)

    bus.subscribe(Events.CODING_ACTION_RECOVERY_REQUIRED, capture)
    reconciler = CodingActionReconciliation(current_journal, plans, bus)
    await reconciler.start()
    await bus.stop()

    plan = json.loads((await plans.get_coding_task_plan("startup-uncertain")).message)
    assert plan["summary"]["replan_required"] is True
    assert plan["replanning"]["latest_reasons"][-1]["trigger"] == (
        "action_interruption"
    )
    status = json.loads(
        (await workspaces.get_coding_workspace_status("startup-uncertain")).message
    )
    recovery = status["last_action_recovery"]
    assert recovery["state"] == "inspection_required"
    assert recovery["uncertain_actions"] == [
        {"action_id": "edit", "tool_name": "apply_file_patch"}
    ]
    assert len(observed) == 1
    assert observed[0]["recoveries"][0]["uncertain_action_count"] == 1
    assert "parameters" not in json.dumps(observed[0])
    assert (await current_journal.recent_plans(state="interrupted")) == []
    revision = plan["revision"]

    await reconciler.start()
    unchanged = json.loads(
        (await plans.get_coding_task_plan("startup-uncertain")).message
    )
    assert unchanged["revision"] == revision


@pytest.mark.asyncio
async def test_startup_reconciliation_safe_boundary_requests_resume_not_replan(
    os_client, tmp_path
):
    workspaces, plans, old_journal, journal_path = await recovery_stack(
        os_client, tmp_path, "startup-resume"
    )
    await old_journal.record(
        "plan_started",
        plan_id="prior-safe-boundary",
        task_ids=["startup-resume"],
        actions=[{"action_id": "read", "tool_name": "read_file"}],
    )
    current_journal = ActionJournal(journal_path)
    bus = EventBus()
    reconciler = CodingActionReconciliation(current_journal, plans, bus)
    await reconciler.start()
    await bus.stop()

    plan = json.loads((await plans.get_coding_task_plan("startup-resume")).message)
    assert plan["summary"]["replan_required"] is False
    status = json.loads(
        (await workspaces.get_coding_workspace_status("startup-resume")).message
    )
    assert status["last_action_recovery"]["state"] == "resume_required"
    reconciled = await current_journal.recent_plans(state="reconciled")
    assert reconciled[0]["reconciliation"]["status"] == "resume_required"
