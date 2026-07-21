"""Agent-facing inspection tools for the durable action journal."""

import json
from typing import Optional

from src.l3_agent.skills.journal import ActionJournal
from src.l3_agent.skills.registry import SkillResult, skill
from src.l3_agent.swarm.roles import Subagents


class ActionJournalSkills:
    def __init__(self, journal: ActionJournal) -> None:
        self.journal = journal

    @skill(swarm=[Subagents.CODER, Subagents.QA_ENGINEER, Subagents.SYSADMIN])
    async def inspect_action_journal(
        self,
        limit: int = 20,
        state: Optional[str] = None,
        include_events: bool = False,
    ) -> SkillResult:
        """Inspect recent action plans, failures, cancellations, and interruptions.

        Use ``state='interrupted'`` after a framework restart. Journal entries are
        evidence for inspection; physical side effects are never replayed blindly.
        """

        try:
            plans = await self.journal.recent_plans(
                limit=limit, state=state, include_events=include_events
            )
            return SkillResult.ok(json.dumps(plans, ensure_ascii=False))
        except ValueError as exc:
            return SkillResult.fail(str(exc))
        except Exception as exc:
            return SkillResult.fail(f"Error reading action journal: {exc}")
