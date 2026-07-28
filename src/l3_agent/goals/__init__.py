"""Durable goal-mode control plane for long-running JAWL work.

The package namespace stays lazy because the generic response schema imports
``goals.ledger`` while the skill registry itself imports that schema. Eagerly
importing ``GoalSkills`` here would therefore make import success depend on
module order.
"""

from __future__ import annotations

from typing import Any

__all__ = ["GoalManager", "GoalRecord", "GoalSkills"]


def __getattr__(name: str) -> Any:
    if name in {"GoalManager", "GoalRecord"}:
        from src.l3_agent.goals.manager import GoalManager, GoalRecord

        return {"GoalManager": GoalManager, "GoalRecord": GoalRecord}[name]
    if name == "GoalSkills":
        from src.l3_agent.goals.skills import GoalSkills

        return GoalSkills
    raise AttributeError(name)
