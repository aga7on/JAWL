"""
Module for Dynamic Context Assembly (User Prompt).

Gathers snapshots and caching buffers from all active L0 States and L2 interfaces
and merges them in a strict hierarchical order. Ensures optimal performance of the
LLM attention mechanism by placing critical information closer to attention horizons.
"""

import asyncio
import json
import re
import uuid
from typing import Any, Dict, List, Literal, Optional, TYPE_CHECKING

from src.l0_state.agent.state import AgentState
from src.utils.logger import agent_logger
from src.utils.settings import ContextBudgetConfig, SubconsciousConfig

from src.l3_agent.context.registry import ContextRegistry, ContextSection
from src.l3_agent.hooks.lifecycle import HookContext, HookPhase, LifecycleHooks
from src.l3_agent.skills.registry import get_skills_library

if TYPE_CHECKING:
    from src.l3_agent.goals.manager import GoalManager


class ContextBuilder:
    """
    Context assembler. Retrieves data from the registry and structures
    the blocks in a strict hierarchy for optimal LLM attention allocation.
    """

    def __init__(
        self,
        agent_state: AgentState,
        registry: ContextRegistry,
        subconscious_config: SubconsciousConfig = None,
        tool_transport: Literal["wrapper", "native", "hybrid"] = "wrapper",
        budget_config: ContextBudgetConfig = None,
        hooks: LifecycleHooks = None,
        goal_manager: Optional["GoalManager"] = None,
    ) -> None:
        """
        Initializes the builder and automatically registers mandatory system providers.

        Args:
            agent_state: Agent L0 State instance.
            registry: Global context providers registry.
        """
        self.agent_state = agent_state
        self.registry = registry
        self.subconscious_config = subconscious_config
        self.tool_transport = tool_transport
        self.budget = budget_config or ContextBudgetConfig()
        self.hooks = hooks or LifecycleHooks()
        self.goal_manager = goal_manager
        self.last_build_metrics: Dict[str, Any] = {}

        self.registry.register_provider(
            "skills", self._skills_provider, section=ContextSection.SKILLS
        )
        self.registry.register_provider(
            "heartbeat", self._heartbeat_provider, section=ContextSection.HEARTBEAT
        )
        self.registry.register_provider(
            "tree_of_thoughts", self._tot_provider, section=ContextSection.TREE_OF_THOUGHTS
        )

    async def build(
        self, event_name: str, payload: Dict[str, Any], missed_events: List[Dict[str, Any]]
    ) -> str:
        """
        Compiles the final context (User Message) for the agent in a strict order.

        Args:
            event_name: Name of the primary trigger event that woke up the agent.
            payload: Parameters and metadata of the primary trigger.
            missed_events: List of background events missed while sleeping.

        Returns:
            str: Compiled and formatted Markdown context block for the LLM.
        """

        blocks = await self.registry.gather_all(
            event_name=event_name,
            payload=payload,
            missed_events=missed_events,
            agent_state=self.agent_state,
        )
        original_chars = len(self._join_blocks(blocks))
        trimmed = {}
        hook_failures: List[str] = []
        if self.budget.enabled:
            compacted_blocks, trimmed = self._apply_context_budget(blocks)
            if trimmed:
                operation_id = f"context-{uuid.uuid4().hex}"
                parameters = {
                    "event_name": event_name[:200],
                    "original_chars": original_chars,
                    "candidate_chars": len(self._join_blocks(compacted_blocks)),
                    "trimmed_provider_names": sorted(trimmed)[:100],
                }
                hook_failures.extend(
                    await self._run_observational_hook(
                        HookContext(
                            phase=HookPhase.PRE_CONTEXT_COMPACTION,
                            plan_id=operation_id,
                            action_id="compact",
                            tool_name="Context.compaction",
                            parameters=parameters,
                        )
                    )
                )
                blocks = compacted_blocks
                final_chars = len(self._join_blocks(blocks))
                hook_failures.extend(
                    await self._run_observational_hook(
                        HookContext(
                            phase=HookPhase.POST_CONTEXT_COMPACTION,
                            plan_id=operation_id,
                            action_id="compact",
                            tool_name="Context.compaction",
                            parameters=parameters,
                            outcome={
                                "is_success": True,
                                "original_chars": original_chars,
                                "final_chars": final_chars,
                                "trimmed_provider_count": len(trimmed),
                            },
                        )
                    )
                )
            else:
                blocks = compacted_blocks
        goal_compacted = {}
        goal_profile_active = bool(
            self.goal_manager is not None
            and self.goal_manager.active_goal is not None
            and self.goal_manager.compact_context
        )
        if (
            goal_profile_active
        ):
            blocks, goal_compacted = self._apply_goal_context_profile(blocks)
            for name, values in goal_compacted.items():
                existing = trimmed.get(name)
                if existing:
                    values = {
                        "before": max(existing["before"], values["before"]),
                        "after": values["after"],
                    }
                trimmed[name] = values
        context = self._join_blocks(blocks)
        self.last_build_metrics = {
            "policy": (
                "goal_compact"
                if goal_profile_active
                else self.budget.skill_policy if self.budget.enabled else "full"
            ),
            "original_chars": original_chars,
            "final_chars": len(context),
            "trimmed_providers": trimmed,
            "hook_failures": hook_failures,
        }
        if trimmed:
            agent_logger.info(
                "[Context] Budgeted dynamic context: "
                f"{original_chars} -> {len(context)} chars; "
                f"trimmed={','.join(trimmed)}"
            )
        return context

    def _apply_goal_context_profile(
        self, blocks: Dict[str, str]
    ) -> tuple[Dict[str, str], Dict[str, Dict[str, int]]]:
        """Project durable goal state instead of replaying unrelated cognition."""

        bounded = dict(blocks)
        trimmed: Dict[str, Dict[str, int]] = {}
        # SOUL and system safety rules live in the static system message. These
        # volatile providers are useful for autonomous reflection but are not
        # authoritative execution state for an explicit active goal.
        omitted = {
            "sql_drives",
            "sql_traits",
            "sql_hypotheses",
            "sql_mental_states",
            "custom_dashboard",
        }
        for name in list(bounded):
            if name in omitted:
                before = len(bounded.pop(name))
                trimmed[name] = {"before": before, "after": 0}

        limits = {
            "skills": 9000,
            "active_goal": 9000,
            "heartbeat": 7000,
            "sql_ticks": 5000,
            "rag memories": 4000,
            "sql_tasks": 4000,
            "sql_notes": 3000,
            "agent_state": 3000,
            "tree_of_thoughts": 3000,
        }
        for name, block in list(bounded.items()):
            limit = limits.get(name, 3000)
            reduced = self._trim_block(
                block,
                limit,
                preserve_tail=name in {"heartbeat", "sql_ticks"},
            )
            if len(reduced) != len(block):
                trimmed[name] = {"before": len(block), "after": len(reduced)}
                bounded[name] = reduced

        target = self.goal_manager.compact_max_chars
        total = len(self._join_blocks(bounded))
        minimums = {
            "skills": 1000,
            "active_goal": 5000,
            "heartbeat": 1000,
            "agent_state": 500,
        }
        while total > target:
            candidates = [
                (len(block) - minimums.get(name, 0), name)
                for name, block in bounded.items()
                if len(block) > minimums.get(name, 0)
            ]
            if not candidates:
                break
            available, name = max(candidates)
            before = len(bounded[name])
            reduction = min(available, total - target)
            bounded[name] = self._trim_block(
                bounded[name],
                before - reduction,
                preserve_tail=name in {"heartbeat", "sql_ticks"},
            )
            entry = trimmed.setdefault(name, {"before": before, "after": before})
            entry["before"] = max(entry["before"], before)
            entry["after"] = len(bounded[name])
            total = len(self._join_blocks(bounded))
        return bounded, trimmed

    async def _run_observational_hook(self, context: HookContext) -> List[str]:
        """Run a non-blocking lifecycle observer without risking context assembly."""

        try:
            result = await self.hooks.run(context)
            return list(result.failures)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure = f"lifecycle infrastructure: {type(exc).__name__}: {exc}"
            agent_logger.warning(f"[Context] Compaction hook failed: {failure}")
            return [failure]

    # -------------------------------------------------------------------------
    # Service Providers
    # -------------------------------------------------------------------------

    async def _skills_provider(
        self,
        event_name: str,
        payload: Dict[str, Any],
        missed_events: List[Dict[str, Any]],
        **kwargs: Any,
    ) -> str:
        """
        Returns a formatted block describing currently available skills.
        """
        if self.tool_transport == "native":
            return (
                "## SKILLS\nAvailable actions are exposed through native function "
                "schemas. Use those exact schemas; no textual skill catalogue is "
                "injected in native-only mode."
            )
        adaptive = self.budget.enabled and self.budget.skill_policy == "adaptive"
        prefixes = (
            self._adaptive_skill_prefixes(event_name, payload, missed_events)
            if adaptive
            else None
        )
        library = get_skills_library(
            self.subconscious_config,
            prefixes=prefixes,
            max_chars=self.budget.skills_max_chars if self.budget.enabled else None,
            include_omitted_index=adaptive,
        )
        return f"## SKILLS\n{library}"

    def _adaptive_skill_prefixes(
        self,
        event_name: str,
        payload: Dict[str, Any],
        missed_events: List[Dict[str, Any]],
    ) -> List[str]:
        """Select likely namespaces while retaining on-demand catalogue discovery."""
        prefixes = ["SkillCatalog", "MemoryRecallSkill", "SQLTasks", "SQLNotes"]
        upper_event = event_name.upper()
        event_routes = {
            "TELETHON": ["Telethon"],
            "AIOGRAM": ["Aiogram"],
            "TELEGRAM": ["Telethon", "Aiogram"],
            "GITHUB": ["GitHub"],
            "EMAIL": ["Email"],
            "CALENDAR": ["Calendar"],
            "WEB": ["Web"],
            "TERMINAL": ["HostTerminal"],
            "VOICE": ["Voice", "ElevenLabs", "Whisper"],
        }
        for marker, routed in event_routes.items():
            if marker in upper_event:
                prefixes.extend(routed)

        recent_payloads = [event.get("payload", {}) for event in missed_events[-3:]]
        signal = " ".join(
            [
                event_name,
                self.agent_state.current_goal,
                json.dumps(payload, ensure_ascii=False, default=str),
                json.dumps(recent_payloads, ensure_ascii=False, default=str),
                *self.agent_state.last_action_args,
            ]
        ).lower()
        coding_pattern = re.compile(
            r"(?:\bcode\b|\bcoding\b|\brepo(?:sitory)?\b|\bgit\b|\bbug\b|"
            r"\btests?\b|\bpytest\b|\bcompile\b|\brefactor\b|"
            r"(?:^|[\\/])[\w.-]+\.(?:py|js|ts|tsx|jsx|rs|go|c|cpp|h|java)\b|"
            r"код|репозитор|программ|рефактор|тест|ошибк)"
        )
        if coding_pattern.search(signal) or any(
            tool.startswith("HostOSCoding")
            for tool in self.agent_state.last_action_tools
        ):
            prefixes.extend(
                [
                    "HostOSCoding",
                    "HostOSReader",
                    "HostOSSearch",
                    "HostOSEditor",
                    "HostOSWorkspace",
                    "HostOSExecution",
                    "HostOSWriter",
                    "CodeGraph",
                    "GitHubLocalGit",
                ]
            )

        prefixes.extend(
            tool.split(".", 1)[0]
            for tool in self.agent_state.last_action_tools
            if "." in tool
        )
        return list(dict.fromkeys(prefixes))

    @staticmethod
    def _trim_block(block: str, max_chars: int, preserve_tail: bool = False) -> str:
        if max_chars <= 0:
            return ""
        if len(block) <= max_chars:
            return block
        marker = "\n...[context budget truncation]...\n"
        if max_chars <= len(marker):
            return block[-max_chars:] if preserve_tail else block[:max_chars]
        if preserve_tail:
            heading = block.splitlines()[0] if block else ""
            if len(heading) + len(marker) >= max_chars:
                return block[-max_chars:]
            remaining = max_chars - len(heading) - len(marker)
            tail = block[-remaining:] if remaining else ""
            return heading + marker + tail
        return block[: max_chars - len(marker)] + marker

    @staticmethod
    def _join_blocks(blocks: Dict[str, str]) -> str:
        return "\n\n\n".join(block for block in blocks.values() if block).strip()

    def _apply_context_budget(
        self, blocks: Dict[str, str]
    ) -> tuple[Dict[str, str], Dict[str, Dict[str, int]]]:
        bounded = dict(blocks)
        trimmed: Dict[str, Dict[str, int]] = {}
        for name, block in list(bounded.items()):
            if name == "skills":
                limit = self.budget.skills_max_chars + len("## SKILLS\n")
            elif name == "sql_ticks":
                limit = self.budget.recent_ticks_max_chars
            elif name == "sql_hypotheses":
                limit = self.budget.hypotheses_max_chars
            else:
                limit = self.budget.provider_max_chars
            preserve_tail = name in {"sql_ticks", "heartbeat"}
            reduced = self._trim_block(block, limit, preserve_tail=preserve_tail)
            if len(reduced) != len(block):
                trimmed[name] = {"before": len(block), "after": len(reduced)}
                bounded[name] = reduced

        total = len(self._join_blocks(bounded))
        while total > self.budget.max_dynamic_chars:
            minimums = {
                # Keep the searchable entry point even under a very small budget.
                "skills": min(512, len(bounded.get("skills", ""))),
                # The current trigger is the tail of the heartbeat provider.
                "heartbeat": min(1024, len(bounded.get("heartbeat", ""))),
            }
            candidates = [
                (len(block) - minimums.get(name, 0), len(block), name)
                for name, block in bounded.items()
                if len(block) > minimums.get(name, 0)
            ]
            if not candidates:
                break
            _, _, name = max(candidates)
            before = len(bounded[name])
            reduction = min(
                before - minimums.get(name, 0),
                total - self.budget.max_dynamic_chars,
            )
            bounded[name] = self._trim_block(
                bounded[name],
                before - reduction,
                preserve_tail=name in {"sql_ticks", "heartbeat"},
            )
            entry = trimmed.setdefault(name, {"before": before, "after": before})
            entry["before"] = max(entry["before"], before)
            entry["after"] = len(bounded[name])
            total = len(self._join_blocks(bounded))
        return bounded, trimmed

    async def _heartbeat_provider(
        self,
        event_name: str,
        payload: Dict[str, Any],
        missed_events: List[Dict[str, Any]],
        **kwargs: Any,
    ) -> str:
        """
        Returns the primary trigger (Heartbeat/Wakeup) block.
        Injects proactive guidelines if corresponding settings are enabled.
        """

        local_event_name = event_name
        local_payload = payload.copy()
        local_missed_events = missed_events.copy()

        # On steps > 1 hide the original trigger in history and set CURRENT to HEARTBEAT
        if self.agent_state.current_step > 1 and local_event_name != "HEARTBEAT":
            processed_event = {
                "name": f"{local_event_name} [Already received on step 1]",
                "payload": local_payload.copy(),
                "time": "Step 1",
                "level": "PROCESSED",
            }
            local_missed_events.append(processed_event)

            local_event_name = "HEARTBEAT"
            local_payload = {}

        seen_chat_histories = set()

        if local_payload.get("chat_id") and "recent_history" in local_payload:
            seen_chat_histories.add(local_payload["chat_id"])

        current_trigger = self._format_single_event(local_event_name, local_payload)

        # Format missed background events (Event Log)
        log_blocks = []

        # Walk backwards (from newest to oldest) to preserve history on the freshest event only
        for evt in reversed(local_missed_events):
            evt_payload = evt["payload"].copy()
            chat_id = evt_payload.get("chat_id")

            if chat_id and "recent_history" in evt_payload:
                if chat_id in seen_chat_histories:
                    del evt_payload["recent_history"]
                else:
                    seen_chat_histories.add(chat_id)

            formatted = self._format_single_event(
                event_name=evt["name"],
                payload=evt_payload,
                event_time=evt.get("time"),
                level=evt.get("level"),
            )
            log_blocks.insert(0, formatted)

        event_log = "\n\n---\n\n".join(log_blocks) if log_blocks else "No other events in log"

        return f"""
## EVENT LOG (missed while sleeping/thinking)
{event_log}

---

## CURRENT TRIGGER
{current_trigger}
""".strip()

    def _build_answer_to_event_reason(
        self, event_name: str, payload: Dict[str, Any], missed_events: List[Dict[str, Any]]
    ) -> str:
        """
        Utility method that returns a formatted text summary of background events.
        """

        payload_lines = [f"{k}: {v}" for k, v in payload.items()]
        payload_str = "\n".join(payload_lines) if payload_lines else "No data"

        main_trigger = f"{event_name}\n{payload_str}"

        if missed_events:
            events_log = "\n".join(str(e) for e in missed_events)
            return f"{main_trigger}\n\nEvent Log:\n{events_log}"

        return main_trigger

    def _format_single_event(
        self,
        event_name: str,
        payload: Dict[str, Any],
        event_time: str = None,
        level: str = None,
    ) -> str:
        """
        Helper method for clean Markdown formatting of a single event.

        Args:
            event_name: Event name.
            payload: Event payload dict.
            event_time: Optional time string.
            level: Event level name (CRITICAL, HIGH, etc.).

        Returns:
            str: Formatted Markdown block.
        """

        proactive_prompt = """
[SYSTEM]
Proactive action execution is recommended.

Activity vectors may include:

- Executing steps for long-term tasks.
- Gathering data in external networks on relevant topics.
- Revising, consolidating, or deleting unnecessary data in memory subsystems.
- Reflecting on recent actions.
- Clearing working directories of irrelevant files.
- Drafting/creating new tasks for execution.

In the absence of current tasks, the system is advised to proactively generate them.
"""

        header = f"**{event_name}**"
        if event_time and level:
            header = f"[{event_time}] [{level}] {header}"

        if event_name == "HEARTBEAT":
            if self.agent_state.proactive_guidance:
                return f"{header}\n[Status: Heartbeat tick] \n{proactive_prompt}"
            else:
                return f"{header}\n[Heartbeat tick]"

        if event_name == "SYSTEM_CORE_START":
            return f"{header}\n[Initializing JAWL kernel. Subsystem startup complete]"

        if event_name == "SYSTEM_CALENDAR_ALARM":
            alarm_title = payload.get("title", "Unknown")
            return f"{header}\n[System timer triggered]\n\nTask: {alarm_title}."

        lines = [header]

        if "sender_name" in payload:
            lines.append(f"Sender: {payload['sender_name']}")

        if "message" in payload:
            lines.append(f"Message: {payload['message']}")

        for k, v in payload.items():
            if k not in ["message", "sender_name", "recent_history"]:
                lines.append(f"* {k}: {v}")

        if "recent_history" in payload and payload["recent_history"]:
            lines.append(f"\n#### Recent Chat History:\n{payload['recent_history']}")

        return "\n".join(lines)

    async def _tot_provider(self, **kwargs: Any) -> str:
        """
        Injects the generated thoughts tree (if any).
        """

        if self.agent_state.current_thoughts_tree:
            return self.agent_state.current_thoughts_tree
        return ""
