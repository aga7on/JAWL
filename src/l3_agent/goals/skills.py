"""Model-facing skills for explicit durable goal lifecycle control."""

from __future__ import annotations

import json
from typing import Literal, Optional

from src.l3_agent.goals.manager import GoalManager, VerificationPolicy
from src.l3_agent.skills.registry import SkillResult, skill


class GoalSkills:
    def __init__(self, manager: GoalManager) -> None:
        self.manager = manager

    @skill()
    async def create_goal(
        self,
        objective: str,
        token_budget: Optional[int] = None,
        linked_task_id: str = "",
        verification_policy: VerificationPolicy = "auto",
    ) -> SkillResult:
        """Create the only active durable goal; fails if another is unfinished."""

        try:
            goal = await self.manager.create(
                objective,
                token_budget=token_budget,
                linked_task_id=linked_task_id,
                verification_policy=verification_policy,
            )
            return SkillResult.ok(
                json.dumps(self.manager.view(goal.goal_id), ensure_ascii=False)
            )
        except ValueError as exc:
            return SkillResult.fail(str(exc))

    @skill()
    async def get_goal(self, goal_id: str = "") -> SkillResult:
        """Return one bounded durable goal view, defaulting to the active goal."""

        goal = self.manager.view(goal_id)
        if goal is None:
            return SkillResult.fail("Goal not found.")
        return SkillResult.ok(json.dumps(goal, ensure_ascii=False))

    @skill()
    async def update_goal(
        self,
        status: Literal["active", "complete", "blocked", "cancelled"],
        summary: str,
        goal_id: str = "",
        token_budget: Optional[int] = None,
    ) -> SkillResult:
        """Resume or finish a goal with concrete evidence; optionally raise budget."""

        try:
            goal = await self.manager.update(
                status=status,
                summary=summary,
                goal_id=goal_id,
                token_budget=token_budget,
            )
            return SkillResult.ok(
                json.dumps(self.manager.view(goal.goal_id), ensure_ascii=False)
            )
        except ValueError as exc:
            return SkillResult.fail(str(exc))

    @skill()
    async def schedule_goal_wakeup(
        self, wake_after_seconds: int, summary: str
    ) -> SkillResult:
        """Put the active goal into a durable wait with one bounded future wakeup."""

        if wake_after_seconds < 1 or wake_after_seconds > 86400:
            return SkillResult.fail(
                "wake_after_seconds must be between 1 and 86400."
            )
        goal = await self.manager.finish_cycle(
            state="waiting",
            summary=summary,
            wake_after_seconds=wake_after_seconds,
        )
        if goal is None:
            return SkillResult.fail("No active goal.")
        return SkillResult.ok(
            json.dumps(self.manager.view(goal.goal_id), ensure_ascii=False)
        )
