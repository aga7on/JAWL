import json
import time

import pytest

from src.l0_state.agent.state import AgentState
from src.l3_agent.goals.manager import GoalManager


@pytest.fixture
def manager(tmp_path):
    return GoalManager(tmp_path / "goals.json", AgentState())


@pytest.mark.asyncio
async def test_goal_persists_and_restart_forces_fresh_lane(tmp_path):
    path = tmp_path / "goals.json"
    first_state = AgentState()
    first = GoalManager(path, first_state)
    created = await first.create(
        "Finish repository verification",
        token_budget=1000,
        linked_task_id="task-1",
    )
    await first.finish_cycle(state="waiting", summary="Waiting for tests")

    second_state = AgentState()
    restored = GoalManager(path, second_state)
    active = restored.active_goal

    assert active is not None
    assert active.goal_id == created.goal_id
    assert active.lane_epoch == created.lane_epoch + 1
    assert active.pending_work is True
    assert active.last_cycle_status == "restart_recovery"
    assert second_state.active_goal_id == created.goal_id
    assert second_state.current_goal == "Finish repository verification"


@pytest.mark.asyncio
async def test_only_one_active_goal_and_terminal_summary_required(manager):
    created = await manager.create("One objective")

    with pytest.raises(ValueError, match="Active goal"):
        await manager.create("Another objective")
    with pytest.raises(ValueError, match="requires"):
        await manager.update(status="complete", summary="")

    completed = await manager.update(
        status="complete",
        summary="All acceptance checks passed.",
        goal_id=created.goal_id,
    )
    assert completed.status == "complete"
    assert manager.active_goal is None
    assert manager.agent_state.current_goal == ""


@pytest.mark.asyncio
async def test_provider_usage_is_authoritative_and_budget_blocks(manager):
    created = await manager.create("Bounded objective", token_budget=100)
    await manager.record_usage(
        {
            "provider_total_tokens": 80,
            "estimated_input_tokens": 500,
            "estimated_output_tokens": 50,
        }
    )
    view = manager.view(created.goal_id)
    assert view["accounted_tokens"] == 80
    assert view["provider_tokens"] == 80
    assert view["estimated_tokens"] == 550
    assert view["remaining_tokens"] == 20

    blocked = await manager.record_usage(
        {"provider_total_tokens": 25, "estimated_input_tokens": 1000}
    )
    assert blocked.status == "blocked"
    assert blocked.accounted_tokens == 105
    assert manager.active_goal is None
    assert "budget exhausted" in blocked.blocked_reason


@pytest.mark.asyncio
async def test_waiting_goal_suppresses_only_until_due(manager):
    await manager.create("Wait deterministically")
    await manager.begin_cycle("HEARTBEAT")
    await manager.finish_cycle(
        state="waiting", summary="Process is running", wake_after_seconds=10
    )
    active = manager.active_goal
    assert active is not None
    assert manager.should_run_heartbeat(now=active.next_wakeup_at - 1) is False
    assert manager.should_run_heartbeat(now=active.next_wakeup_at) is True
    assert manager.seconds_until_wakeup(now=active.next_wakeup_at - 3) == 3


@pytest.mark.asyncio
async def test_context_projection_is_bounded_and_contains_evidence(manager):
    await manager.create("Inspect exact repository state")
    await manager.record_action_result("x" * 10000)
    block = await manager.get_context_block()

    assert "## ACTIVE GOAL" in block
    assert "Inspect exact repository state" in block
    assert "Goal Protocol v2" in block
    assert len(block) < 16000
    assert "[truncated]" in block


def test_corrupt_goal_store_fails_closed_without_overwrite(tmp_path):
    path = tmp_path / "goals.json"
    path.write_text("{broken", encoding="utf-8")

    manager = GoalManager(path, AgentState())

    assert manager.active_goal is None
    assert path.read_text(encoding="utf-8") == "{broken"


@pytest.mark.asyncio
async def test_corrupt_goal_store_rejects_mutation(tmp_path):
    path = tmp_path / "goals.json"
    path.write_text("{broken", encoding="utf-8")
    manager = GoalManager(path, AgentState())

    with pytest.raises(ValueError, match="malformed"):
        await manager.create("Must not overwrite operator evidence")

    assert path.read_text(encoding="utf-8") == "{broken"


@pytest.mark.asyncio
async def test_linked_coding_goal_requires_passing_verification(manager):
    created = await manager.create(
        "Implement and verify the change",
        linked_task_id="coding-task",
    )

    rejected = await manager.finish_cycle(
        state="completed",
        summary="Implementation appears complete.",
    )
    assert rejected.status == "active"
    assert rejected.last_cycle_status == "verification_required"

    await manager.record_action_result(
        "* HostOSCodingVerification.run_coding_verification: all checks passed\n"
        "  [action_id=verify; status=success; duration_ms=12]",
        actions=[
            {
                "tool_name": (
                    "HostOSCodingVerification.run_coding_verification"
                ),
                "parameters": {"task_id": "coding-task"},
            }
        ],
    )
    completed = await manager.finish_cycle(
        state="completed",
        summary="Exact-state verification passed.",
    )
    assert completed.status == "complete"
    assert completed.verification_status == "passed"


@pytest.mark.asyncio
async def test_failed_coding_verification_is_durable(manager):
    created = await manager.create(
        "Fix regression",
        linked_task_id="coding-task",
        verification_policy="required",
    )
    await manager.record_action_result(
        "* HostOSCodingVerification.run_coding_verification: failed\n"
        "  [action_id=verify; status=failed; duration_ms=15]",
        actions=[
            {
                "tool_name": (
                    "HostOSCodingVerification.run_coding_verification"
                ),
                "parameters": {"task_id": "coding-task"},
            }
        ],
    )

    view = manager.view(created.goal_id)
    assert view["verification_status"] == "failed"
    with pytest.raises(ValueError, match="verification"):
        await manager.update(
            status="complete",
            summary="Cannot bypass a failed gate.",
            goal_id=created.goal_id,
        )


@pytest.mark.asyncio
async def test_atomic_store_is_valid_json_after_updates(manager):
    created = await manager.create("Persist every transition")
    for index in range(5):
        await manager.record_action_result(f"result-{index}")
    payload = json.loads(manager.path.read_text(encoding="utf-8"))

    assert payload["version"] == 1
    assert payload["goals"][0]["goal_id"] == created.goal_id
    assert not list(manager.path.parent.glob(".*.tmp"))
