"""
Collector and formatter of agent actions history (Ticks).

Logs every step of the ReAct loop and handles smart compression of older steps
in the system prompt (leaving the N latest steps detailed, while compressing the rest
by character count to save context space).
"""

import json
import uuid
from typing import TYPE_CHECKING, Any, List, Optional
from sqlalchemy import literal_column, select, text
from datetime import datetime, timezone

from src.utils.dtime import format_datetime, get_timezone

from src.l1_databases.sql.tables import (
    AgentRuntimeStateTable,
    TickTable,
    TickTimelineTable,
)
from src.l3_agent.skills.registry import skill, SkillResult
from src.l3_agent.swarm.roles import Subagents

if TYPE_CHECKING:
    from src.l1_databases.sql.db import SQLDB


class SQLTicks:
    """
    CRUD functions for interacting with the agent's tick logging table.
    Handles saving, retrieving, and dynamic formatting of history
    depending on the target subsystem (Main Agent or Background Processes).
    """

    def __init__(
        self,
        db: "SQLDB",
        high_ticks: int = 3,
        medium_ticks: int = 7,
        low_ticks: int = 20,
        action_max_chars: int = 2000,
        result_max_chars: int = 5000,
        thoughts_short_max_chars: int = 1000,
        action_short_max_chars: int = 100,
        result_short_max_chars: int = 200,
        tz_offset: int = 0,
    ) -> None:
        """
        Initializes the tick controller and sets strict limits on the context size.

        Args:
            db: Connection to SQLite.
            limit: Maximum number of ticks (steps) displayed in the prompt.
            detailed_ticks: How many of the freshest ticks to output in detailed form (no truncation).
            action_max_chars: Character limit for fresh actions.
            result_max_chars: Character limit for fresh results.
            thoughts_short_max_chars: Character limit for compressed thoughts.
            action_short_max_chars: Character limit for compressed actions.
            result_short_max_chars: Character limit for compressed results.
            tz_offset: Timezone offset.
        """
        
        self.db = db
        self.high_ticks = high_ticks
        self.medium_ticks = medium_ticks
        self.low_ticks = low_ticks

        self.action_max_chars = action_max_chars
        self.result_max_chars = result_max_chars

        self.thoughts_short_max_chars = thoughts_short_max_chars
        self.action_short_max_chars = action_short_max_chars
        self.result_short_max_chars = result_short_max_chars

        self.tz_offset = tz_offset

    async def bootstrap_migrations(self) -> None:
        """Add timeline support without rewriting existing append-only ticks."""

        async with self.db.engine.begin() as conn:
            columns = await conn.execute(text("PRAGMA table_info(ticks)"))
            names = {str(row[1]) for row in columns.fetchall()}
            if "timeline_id" not in names:
                await conn.execute(
                    text(
                        "ALTER TABLE ticks ADD COLUMN timeline_id TEXT "
                        "NOT NULL DEFAULT 'main'"
                    )
                )
            await conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_ticks_timeline_id "
                    "ON ticks (timeline_id)"
                )
            )

        async with self.db.session_factory() as session:
            if await session.get(TickTimelineTable, "main") is None:
                session.add(
                    TickTimelineTable(
                        id="main",
                        parent_id=None,
                        anchor_tick_id=None,
                        reason="initial timeline",
                    )
                )
            state = await session.get(AgentRuntimeStateTable, "active_tick_timeline")
            if state is None:
                session.add(
                    AgentRuntimeStateTable(
                        key="active_tick_timeline", value="main"
                    )
                )
            await session.commit()

    @staticmethod
    async def _tick_rowid(session: Any, tick_id: Optional[str]) -> Optional[int]:
        if tick_id is None:
            return None
        result = await session.execute(
            text("SELECT rowid FROM ticks WHERE id = :tick_id"),
            {"tick_id": tick_id},
        )
        value = result.scalar_one_or_none()
        if value is None:
            raise ValueError(f"Tick cursor was not found ({tick_id}).")
        return int(value)

    @staticmethod
    async def _active_timeline_id(session: Any) -> str:
        if await session.get(TickTimelineTable, "main") is None:
            session.add(
                TickTimelineTable(
                    id="main",
                    parent_id=None,
                    anchor_tick_id=None,
                    reason="initial timeline",
                )
            )
            await session.flush()
        state = await session.get(AgentRuntimeStateTable, "active_tick_timeline")
        if state is None:
            state = AgentRuntimeStateTable(
                key="active_tick_timeline", value="main"
            )
            session.add(state)
            await session.flush()
        return state.value

    async def _is_tick_visible(
        self, session: Any, timeline_id: str, tick_id: str
    ) -> bool:
        result = await session.execute(
            text("SELECT rowid, timeline_id FROM ticks WHERE id = :tick_id"),
            {"tick_id": tick_id},
        )
        row = result.first()
        if row is None:
            raise ValueError(f"Tick cursor was not found ({tick_id}).")
        target_rowid, target_timeline = int(row[0]), str(row[1])
        current_id: Optional[str] = timeline_id
        maximum_rowid: Optional[int] = None
        visited = set()
        while current_id:
            if current_id in visited:
                raise ValueError("Tick timeline ancestry contains a cycle.")
            visited.add(current_id)
            if current_id == target_timeline and (
                maximum_rowid is None or target_rowid <= maximum_rowid
            ):
                return True
            timeline = await session.get(TickTimelineTable, current_id)
            if timeline is None:
                raise ValueError(f"Tick timeline was not found ({current_id}).")
            if timeline.parent_id is None or timeline.anchor_tick_id is None:
                return False
            anchor_rowid = await self._tick_rowid(session, timeline.anchor_tick_id)
            assert anchor_rowid is not None
            maximum_rowid = (
                anchor_rowid
                if maximum_rowid is None
                else min(maximum_rowid, anchor_rowid)
            )
            current_id = timeline.parent_id
        return False

    async def _visible_ticks(
        self,
        session: Any,
        timeline_id: str,
        limit: int,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
    ) -> List[TickTable]:
        """Traverse one immutable timeline ancestry from newest to oldest."""

        if limit < 1:
            return []
        newest_first: List[TickTable] = []
        current_id: Optional[str] = timeline_id
        maximum_rowid: Optional[int] = None
        visited = set()
        while current_id and len(newest_first) < limit:
            if current_id in visited:
                raise ValueError("Tick timeline ancestry contains a cycle.")
            visited.add(current_id)
            timeline = await session.get(TickTimelineTable, current_id)
            if timeline is None:
                raise ValueError(f"Tick timeline was not found ({current_id}).")

            stmt = select(TickTable).where(TickTable.timeline_id == current_id)
            if maximum_rowid is not None:
                stmt = stmt.where(literal_column("rowid") <= maximum_rowid)
            if start_time is not None:
                stmt = stmt.where(TickTable.created_at >= start_time)
            if end_time is not None:
                stmt = stmt.where(TickTable.created_at <= end_time)
            stmt = stmt.order_by(literal_column("rowid").desc()).limit(
                limit - len(newest_first)
            )
            result = await session.execute(stmt)
            newest_first.extend(result.scalars().all())

            if timeline.parent_id is None:
                break
            anchor_rowid = await self._tick_rowid(session, timeline.anchor_tick_id)
            if anchor_rowid is None:
                break
            maximum_rowid = (
                anchor_rowid
                if maximum_rowid is None
                else min(maximum_rowid, anchor_rowid)
            )
            current_id = timeline.parent_id
        return list(reversed(newest_first))

    async def get_active_cursor(self) -> dict[str, Optional[str]]:
        """Return the durable timeline and last visible tick for a checkpoint."""

        async with self.db.session_factory() as session:
            timeline_id = await self._active_timeline_id(session)
            ticks = await self._visible_ticks(session, timeline_id, 1)
            return {
                "timeline_id": timeline_id,
                "tick_id": ticks[-1].id if ticks else None,
            }

    async def branch_from_cursor(
        self,
        timeline_id: str,
        tick_id: Optional[str],
        reason: str,
    ) -> str:
        """Create and activate a child timeline from a previously saved cursor."""

        if not timeline_id or len(timeline_id) > 64:
            raise ValueError("Invalid timeline_id.")
        clean_reason = str(reason).strip()[:1000]
        new_timeline_id = uuid.uuid4().hex
        async with self.db.session_factory() as session:
            if await session.get(TickTimelineTable, timeline_id) is None:
                raise ValueError(f"Tick timeline was not found ({timeline_id}).")
            if tick_id is not None:
                if not await self._is_tick_visible(session, timeline_id, tick_id):
                    raise ValueError(
                        "Tick cursor is not reachable from the requested timeline."
                    )
            elif await self._visible_ticks(session, timeline_id, 1):
                raise ValueError("A non-empty timeline requires a tick cursor.")

            session.add(
                TickTimelineTable(
                    id=new_timeline_id,
                    parent_id=timeline_id,
                    anchor_tick_id=tick_id,
                    reason=clean_reason,
                )
            )
            state = await session.get(AgentRuntimeStateTable, "active_tick_timeline")
            if state is None:
                state = AgentRuntimeStateTable(
                    key="active_tick_timeline", value=new_timeline_id
                )
                session.add(state)
            else:
                state.value = new_timeline_id
            await session.commit()
        return new_timeline_id

    async def save_tick(
        self, thoughts: str, actions: list[dict[str, Any]], results: dict[str, Any]
    ) -> str:
        """
        Saves a single tick of the agent's work to the database.

        Args:
            thoughts: Internal monologue and logic of the agent.
            actions: Array of invoked tools and their parameters.
            results: Responses from tools or Traceback/error text.

        Returns:
            Generated UUID of the saved tick.
        """

        tick_id = str(uuid.uuid4())

        async with self.db.session_factory() as session:
            timeline_id = await self._active_timeline_id(session)
            new_tick = TickTable(
                id=tick_id,
                timeline_id=timeline_id,
                thoughts=thoughts,
                actions=actions,
                results=results,
            )
            session.add(new_tick)
            await session.commit()

        return tick_id

    async def get_ticks(self, limit: int = 5) -> List[TickTable]:
        """
        Returns the last N ticks from the database in chronological order.

        Args:
            limit: How many records to retrieve.

        Returns:
            List of TickTable objects.
        """

        async with self.db.session_factory() as session:
            timeline_id = await self._active_timeline_id(session)
            return await self._visible_ticks(session, timeline_id, limit)

    def _format_tick_entry(self, t: TickTable, tier: str) -> str:
        """
        Internal helper method to format a single tick.

        Returns:
            Formatted Markdown string containing thoughts, actions, and results.
        """
        time_str = format_datetime(t.created_at, self.tz_offset, "%m-%d %H:%M:%S")
        step_str = (
            f"\n[Step {t.results['step']}/{t.results['max_steps']}]"
            if t.results and "step" in t.results
            else ""
        )

        header = f"\n\n## TICK {time_str}{step_str}\n"

        thoughts_str = t.thoughts
        if tier in ("MEDIUM", "LOW") and len(thoughts_str) > self.thoughts_short_max_chars:
            thoughts_str = thoughts_str[: self.thoughts_short_max_chars] + "...[Truncated]"

        # For LOW ticks we return ONLY thoughts
        if tier == "LOW":
            return f"{header}\n### Thoughts:\n{thoughts_str}"

        # MEDIUM and HIGH
        action_limit = self.action_max_chars if tier == "HIGH" else self.action_short_max_chars
        actions_list = []
        actions_raw = t.actions if isinstance(t.actions, list) else [t.actions]

        for a in actions_raw:
            if isinstance(a, dict):
                t_name = a.get("tool_name", "unknown")
                params = a.get("parameters", {})
                act_str = f"* {t_name}({json.dumps(params, ensure_ascii=False)})"
            else:
                act_str = f"* {a}"
            if len(act_str) > action_limit:
                act_str = act_str[:action_limit] + "...[Truncated]"
            actions_list.append(act_str)

        actions_str = "\n".join(actions_list) if actions_list else "None"

        res_limit = self.result_max_chars if tier == "HIGH" else self.result_short_max_chars
        res_str = "None"
        if t.results:
            res_str = str(t.results.get("execution_report", t.results))
            if len(res_str) > res_limit:
                res_str = res_str[:res_limit] + f"...[Truncated limit {res_limit}]"

        return f"{header}\n### Thoughts: \n{thoughts_str} \n\n### Actions:\n{actions_str} \n\n### Result:\n{res_str}"

    async def get_context_block(self, **kwargs: Any) -> str:
        """
        Extracts the last N ticks from the database and dynamically compresses their size.
        The last 'detailed_ticks' are returned almost entirely, the rest are strictly truncated
        to 'short_max_chars' to prevent overflowing the LLM context window.

        Intended for use by the Main Agent (Orchestrator).

        Returns:
            Finished Markdown block 'RECENT TICKS' for injection into the prompt.
        """

        total_limit = self.high_ticks + self.medium_ticks + self.low_ticks
        ticks = await self.get_ticks(limit=total_limit)

        if not ticks:
            return "## RECENT TICKS\nEmpty."

        blocks = []
        total = len(ticks)
        for i, t in enumerate(ticks):
            distance_from_newest = total - 1 - i

            if distance_from_newest < self.high_ticks:
                tier = "HIGH"

            elif distance_from_newest < self.high_ticks + self.medium_ticks:
                tier = "MEDIUM"

            else:
                tier = "LOW"

            blocks.append(self._format_tick_entry(t, tier))

        return "## RECENT TICKS\n" + "\n\n".join(blocks)

    async def get_full_context_block(self, limit: int = 10) -> str:
        """
        Extracts the last N ticks from the database WITHOUT applying strict historical compression.
        All requested ticks are treated as 'detailed', allowing models to see
        real execution results (results) rather than just thoughts.

        Intended for background cognitive processes (Subconscious, Tree of Thoughts),
        which critically need to see full cause-and-effect relationships.

        Args:
            limit: Maximum number of extracted ticks.

        Returns:
            Formatted Markdown actions log.
        """

        ticks = await self.get_ticks(limit=limit)
        if not ticks:
            return "RECENT ACTIONS LOG\nEmpty."

        blocks = [self._format_tick_entry(t, "HIGH") for t in ticks]

        return "RECENT ACTIONS LOG\n" + "\n\n".join(blocks)

    @skill(swarm=[Subagents.ARCHIVIST])
    async def get_ticks_by_time(
        self, start_time: str, end_time: str, detail: bool = False
    ) -> SkillResult:
        """
        Retrieves ticks for a specific time period.
        Format 'YYYY-MM-DD HH:MM:SS'.

        detail: If True, returns full logs.
        """
        try:
            tz = get_timezone(self.tz_offset)

            dt_start = datetime.strptime(start_time, "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)
            dt_end = datetime.strptime(end_time, "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)

            # SQLite works with strings (naive datetimes) for comparisons in filters
            # Therefore we convert time to UTC and strip tzinfo (make it naive)
            utc_start = dt_start.astimezone(timezone.utc).replace(tzinfo=None)
            utc_end = dt_end.astimezone(timezone.utc).replace(tzinfo=None)

            if utc_start > utc_end:
                return SkillResult.fail("Error: start_time cannot be later than end_time.")

            limit = 200  # Hard limit on returned ticks count to avoid bloating the prompt

            async with self.db.session_factory() as session:
                timeline_id = await self._active_timeline_id(session)
                ticks = await self._visible_ticks(
                    session,
                    timeline_id,
                    limit,
                    start_time=utc_start,
                    end_time=utc_end,
                )

            if not ticks:
                return SkillResult.ok(
                    f"No ticks found for the specified period ({start_time} - {end_time})."
                )

            tier = "HIGH" if detail else "MEDIUM"
            blocks = [self._format_tick_entry(t, tier) for t in ticks]

            res_str = "\n\n".join(blocks)
            if len(ticks) == limit:
                res_str += f"\n\n... [Output limit of {limit} ticks reached. It is recommended to narrow the time range for a more focused search]"

            return SkillResult.ok(f"Tick history ({start_time} - {end_time}):\n\n{res_str}")

        except ValueError:
            return SkillResult.fail("Error: Invalid time format. Use 'YYYY-MM-DD HH:MM:SS'.")
        except Exception as e:
            return SkillResult.fail(f"Internal error searching ticks: {e}")
