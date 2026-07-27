import json

import pytest

from src.l0_state.agent.state import AgentState
from src.l3_agent.goals.manager import GoalManager
from src.l3_agent.goals.skills import GoalSkills


@pytest.fixture
def goal_skills(tmp_path):
    manager = GoalManager(tmp_path / "goals.json", AgentState())
    return manager, GoalSkills(manager)


@pytest.mark.asyncio
async def test_goal_skills_cover_lifecycle_and_bounded_wakeup(goal_skills):
    manager, skills = goal_skills
    created = await skills.create_goal(
        "Inspect, change, and verify the repository",
        token_budget=500,
        verification_policy="none",
    )
    assert created.is_success
    goal_id = json.loads(created.message)["goal_id"]

    inspected = await skills.get_goal(goal_id)
    assert inspected.is_success
    assert json.loads(inspected.message)["remaining_tokens"] == 500

    invalid_wait = await skills.schedule_goal_wakeup(0, "invalid")
    assert not invalid_wait.is_success

    waiting = await skills.schedule_goal_wakeup(10, "Waiting for process")
    assert waiting.is_success
    assert manager.active_goal.pending_work is False

    completed = await skills.update_goal(
        "complete", "All requested checks passed.", goal_id
    )
    assert completed.is_success
    assert manager.get(goal_id).status == "complete"


@pytest.mark.asyncio
async def test_goal_skills_do_not_replace_an_active_goal(goal_skills):
    _, skills = goal_skills
    assert (await skills.create_goal("First")).is_success

    second = await skills.create_goal("Second")

    assert not second.is_success
    assert "Active goal" in second.message
