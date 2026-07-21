"""Read-only observability skill for bounded Heartbeat/ReAct event buffers."""

import json
from typing import TYPE_CHECKING

from src.l3_agent.skills.registry import SkillResult, skill
from src.l3_agent.swarm.roles import Subagents

if TYPE_CHECKING:
    from src.l3_agent.heartbeat import Heartbeat


class EventQueueSkills:
    def __init__(self, heartbeat: "Heartbeat") -> None:
        self.heartbeat = heartbeat

    @skill(swarm=[Subagents.ARCHIVIST, Subagents.SYSADMIN])
    async def get_event_queue_status(self) -> SkillResult:
        """Return payload-free queue size, coalescing, overflow, and wake state."""

        return SkillResult.ok(
            json.dumps(self.heartbeat.get_queue_snapshot(), ensure_ascii=False)
        )
