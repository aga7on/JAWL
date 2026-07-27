"""
Agent Reasoning and Acting (ReAct) Core.

Implements the Stateless loop: gathers context, sends prompt to LLM,
parses JSON outputs (Chain-of-Thought + Tool Calls), executes skills, and
commits results (Ticks) to the database.
"""

import asyncio
from typing import Callable, Dict, Any, List, Literal, Optional, Tuple, Union, TYPE_CHECKING

import base64
import re
import copy
from pathlib import Path

from src.utils.logger import main_logger, agent_logger
from src.utils.settings import TreeOfThoughtsConfig
from src.utils._tools import dump_prompt_to_file, redact_sensitive_text, truncate_text

from src.utils.event.bus import EventBus
from src.utils.event.registry import Events
from src.utils.tracing import begin_trace, current_trace, reset_trace

from src.l0_state.agent.state import AgentState, AgentStatus

from src.l1_databases.sql.management.ticks import SQLTicks
from src.l1_databases.vector.manager import VectorManager

from src.l3_agent.llm.executor import LLMExecutor
from src.l3_agent.prompt.builder import PromptBuilder
from src.l3_agent.context.builder import ContextBuilder

from src.l3_agent.tot.generator import ToTGenerator

from src.l3_agent.skills.registry import execute_skill, resolve_native_tool_name
from src.l3_agent.skills.schema import AgentResponse, ActionCall, parse_llm_json
from src.l3_agent.event_buffer import BoundedEventBuffer

if TYPE_CHECKING:
    from src.l3_agent.goals.manager import GoalManager


def _normalize_tool_output(text: str) -> str:
    """Make tool output safe for JSON/UTF-8 without destroying valid Unicode."""
    if not text:
        return text
    # Replace only invalid, unpaired UTF-16 surrogates. Emoji, CJK, Arabic,
    # mathematical symbols and other valid scripts must remain intact.
    normalized = text.encode("utf-8", errors="replace").decode("utf-8")
    # Tool output is textual. Drop C0 controls that commonly break upstream
    # JSON/web transports while retaining normal whitespace.
    return "".join(
        ch for ch in normalized if ord(ch) >= 0x20 or ch in "\t\n\r"
    )

class ReactLoop:
    """
    Autonomous agent core.
    Implements the ReAct (Reasoning and Acting) loop in Stateless mode.
    """

    def __init__(
        self,
        executor: LLMExecutor,
        prompt_builder: PromptBuilder,
        context_builder: ContextBuilder,
        agent_state: AgentState,
        sql_ticks: SQLTicks,
        vector_manager: VectorManager,
        tools: Union[list, Callable[[], list]],
        event_bus: EventBus,
        tool_transport: Literal["wrapper", "native", "hybrid"] = "wrapper",
        thinking_policy: Literal[
            "provider_default", "always", "never", "first_step"
        ] = "provider_default",
        cooldown_sec: int = 30,
        llm_max_retries: int = 3,
        llm_max_timeout_retries: int = 2,
        event_queue_max: int = 100,
        event_coalesce_window_sec: float = 2.0,
        event_coalesce_names: Optional[List[str]] = None,
        event_payload_sample_limit: int = 3,
        tot_config: Optional[TreeOfThoughtsConfig] = None,
        tot_generator: Optional[ToTGenerator] = None,
        goal_manager: Optional["GoalManager"] = None,
    ) -> None:
        """
        Initializes the ReAct loop.

        Args:
            executor: LLM executor instance.
            prompt_builder: Static system prompt compiler.
            context_builder: Context builder (manages L0 States and episodic memory).
            agent_state: Agent State L0 instance.
            sql_ticks: SQLite ticks controller.
            vector_manager: Vector DB manager.
            tools: List of available JSON schema tools.
            event_bus: Global event bus.
            cooldown_sec: Interval in seconds to wait when hitting Rate Limits (429).
            tot_config: Optional Tree of Thoughts configuration.
            tot_generator: Optional Tree of Thoughts generator.
        """

        self.executor = executor

        self.prompt_builder = prompt_builder
        self.context_builder = context_builder

        self.agent_state = agent_state

        self.sql_ticks = sql_ticks
        self.vector_manager = vector_manager

        self.tools = tools
        self.tool_transport = tool_transport
        self.thinking_policy = thinking_policy
        self.cooldown_sec = cooldown_sec
        self.llm_max_retries = max(1, llm_max_retries)
        self.llm_max_timeout_retries = max(1, llm_max_timeout_retries)

        self.event_bus = event_bus

        self.tot_config = tot_config
        self.tot_generator = tot_generator
        self.goal_manager = goal_manager

        self._realtime_events = BoundedEventBuffer(
            capacity=event_queue_max,
            coalesce_window_sec=event_coalesce_window_sec,
            coalesce_names=event_coalesce_names,
            payload_sample_limit=event_payload_sample_limit,
        )
        self._steer_event_buffer = BoundedEventBuffer(
            capacity=event_queue_max,
            coalesce_window_sec=event_coalesce_window_sec,
            coalesce_names=event_coalesce_names,
            payload_sample_limit=event_payload_sample_limit,
        )
        self.current_events: List[Dict[str, Any]] = self._realtime_events.items
        self._steer_requested: bool = False
        self.last_cycle_outcome: Dict[str, Any] = {
            "status": "never_run",
            "event_name": "",
            "had_actions": False,
        }

    def _thinking_enabled_for_step(self) -> Optional[bool]:
        """Resolve the optional provider thinking flag for this ReAct step."""
        if self.thinking_policy == "provider_default":
            return None
        if self.thinking_policy == "always":
            return True
        if self.thinking_policy == "never":
            return False
        return self.agent_state.current_step == 1

    async def run(
        self, event_name: str, payload: Dict[str, Any], missed_events: List[Dict[str, Any]]
    ) -> None:
        """
        Launches the ReAct loop call to the LLM (Orchestrator).

        Args:
            event_name: Primary trigger event name.
            payload: Primary trigger event payload parameters dict.
            missed_events: List of missed background events.
        """

        self._realtime_events.clear()
        self._realtime_events.extend(missed_events)
        self.current_events = self._realtime_events.items
        self.last_cycle_outcome = {
            "status": "running",
            "event_name": event_name,
            "had_actions": False,
        }
        trace_token, trace = begin_trace(
            "react_cycle", event_name=event_name, model=self.agent_state.llm_model
        )
        self.agent_state.current_trace_id = trace["trace_id"]
        cycle_goal_id = ""

        try:
            self.agent_state.reset_step()
            if self.goal_manager is not None:
                goal = await self.goal_manager.begin_cycle(event_name)
                if goal is not None:
                    cycle_goal_id = goal.goal_id

            log = f"[ReAct] Reasoning cycle initialized. Reason: {event_name} (LLM Model: {self.agent_state.llm_model})."
            agent_logger.info(log)

            prompt = self.prompt_builder.build()
            cycle_concluded = False

            # ==================================================================
            # MAIN LOOP
            # ==================================================================

            while self.agent_state.current_step <= self.agent_state.max_react_steps:
                if self._steer_requested:
                    await self._handle_cycle_steered()
                    cycle_concluded = True
                    break
                self.agent_state.update_state(AgentStatus.THINKING)

                # --------------------------------------------------------------
                # Tree of Thoughts generation
                # --------------------------------------------------------------

                if (
                    self.tot_config
                    and self.tot_config.enabled
                    and self.tot_config.mode in ("auto", "hybrid")
                ):
                    if (self.agent_state.current_step == 1) or (
                        (self.agent_state.current_step - 1)
                        % self.tot_config.auto_interval_steps
                        == 0
                    ):

                        tree_md = await self.tot_generator.generate(
                            event_name,
                            payload,
                            missed_events,
                            task_description="Automated thoughts tree generation to evaluate current vector.",
                        )
                        if tree_md:
                            self.agent_state.current_thoughts_tree = tree_md

                if self._steer_requested:
                    await self._handle_cycle_steered()
                    cycle_concluded = True
                    break

                # --------------------------------------------------------------
                # Context and Prompt compilation
                # --------------------------------------------------------------

                messages = await self._prepare_messages(prompt, event_name, payload)

                if self._steer_requested:
                    await self._handle_cycle_steered()
                    cycle_concluded = True
                    break

                # --------------------------------------------------------------
                # LLM execution call
                # --------------------------------------------------------------

                raw_answer = await self.executor.execute(
                    model_name=self.agent_state.llm_model,
                    messages=messages,
                    temperature=self.agent_state.temperature,
                    logger=agent_logger,
                    log_prefix="[LLM]",
                    tools=self.tools() if callable(self.tools) else self.tools,
                    tool_transport=self.tool_transport,
                    enable_thinking=self._thinking_enabled_for_step(),
                    max_retries=self.llm_max_retries,
                    max_timeout_retries=self.llm_max_timeout_retries,
                    session_id=(
                        self.goal_manager.lane_id
                        if self.goal_manager is not None
                        else ""
                    ),
                )
                budget_blocked = False
                if self.goal_manager is not None:
                    usage_goal = await self.goal_manager.record_usage(
                        self._llm_metrics_snapshot()
                    )
                    budget_blocked = bool(
                        usage_goal is not None and usage_goal.status == "blocked"
                    )
                if self._steer_requested:
                    await self._handle_cycle_steered()
                    cycle_concluded = True
                    break
                if raw_answer is None:
                    self.agent_state.update_state(AgentStatus.ERROR)
                    failure_status = str(
                        self._llm_metrics_snapshot().get("status") or "failed"
                    )
                    self.last_cycle_outcome["status"] = failure_status
                    if self.goal_manager is not None:
                        if failure_status == "invalid_request":
                            await self.goal_manager.finish_cycle(
                                state="blocked",
                                summary=(
                                    "Provider rejected the request as invalid; "
                                    "automatic replay was stopped."
                                ),
                            )
                        else:
                            await self.goal_manager.finish_cycle(
                                state="failed",
                                summary="LLM request failed before a valid response.",
                                wake_after_seconds=30,
                            )
                    break

                # --------------------------------------------------------------
                # Response parsing
                # --------------------------------------------------------------

                parsed_response, error_msg = self._parse_response(raw_answer)
                if error_msg:
                    await self._handle_protocol_error(raw_answer, error_msg)
                    if budget_blocked:
                        cycle_concluded = True
                        break
                    self.agent_state.next_step()
                    continue

                thoughts = parsed_response.thoughts.strip()
                actions = parsed_response.actions

                if thoughts:
                    log = f"[Thoughts]:\n{thoughts}\n"
                    agent_logger.info(log)

                # --------------------------------------------------------------
                # Completion checks
                # --------------------------------------------------------------

                if not actions:
                    completion_text = thoughts or parsed_response.goal_summary
                    completion_status = "completed"
                    if self.goal_manager is not None:
                        goal_state = parsed_response.goal_state
                        if goal_state == "done":
                            transitioned_goal = await self.goal_manager.finish_cycle(
                                state="completed",
                                summary=parsed_response.goal_summary,
                            )
                            if (
                                transitioned_goal is not None
                                and transitioned_goal.status == "active"
                            ):
                                completion_status = "goal_continues"
                                completion_text = transitioned_goal.last_summary
                        elif goal_state == "blocked":
                            await self.goal_manager.finish_cycle(
                                state="blocked",
                                summary=parsed_response.goal_summary,
                            )
                        elif goal_state == "continue":
                            await self.goal_manager.finish_cycle(
                                state="continue",
                                summary=parsed_response.goal_summary
                                or "Goal requested another bounded continuation.",
                                wake_after_seconds=(
                                    parsed_response.wake_after_seconds or 1
                                ),
                            )
                        elif goal_state == "wait":
                            await self.goal_manager.finish_cycle(
                                state="waiting",
                                summary=parsed_response.goal_summary,
                                wake_after_seconds=parsed_response.wake_after_seconds,
                            )
                        elif self.goal_manager.active_goal is not None:
                            await self.goal_manager.finish_cycle(
                                state="waiting",
                                summary=(
                                    completion_text
                                    or "Legacy empty-actions response; waiting for "
                                    "new evidence or an explicit wakeup."
                                ),
                            )
                    await self._handle_completion(
                        completion_text, status=completion_status
                    )
                    cycle_concluded = True
                    break

                # --------------------------------------------------------------
                # Actions execution
                # --------------------------------------------------------------

                await self._execute_actions(thoughts, actions)

                if cycle_goal_id and self.goal_manager is not None:
                    cycle_goal = self.goal_manager.get(cycle_goal_id)
                    if cycle_goal is not None and cycle_goal.status != "active":
                        await self._handle_completion(
                            cycle_goal.completion_summary
                            or cycle_goal.blocked_reason
                            or cycle_goal.last_summary
                        )
                        cycle_concluded = True
                        break
                if budget_blocked:
                    await self._handle_completion(
                        "Goal token budget reached after the last valid action batch."
                    )
                    cycle_concluded = True
                    break

                if self._steer_requested:
                    await self._handle_cycle_steered()
                    cycle_concluded = True
                    break

                self.agent_state.next_step()

            if (
                not cycle_concluded
                and self.agent_state.current_step > self.agent_state.max_react_steps
            ):
                await self._handle_step_limit()

        except asyncio.CancelledError:
            self.last_cycle_outcome["status"] = "cancelled"
            await asyncio.shield(self._handle_cycle_cancelled())
            raise
        finally:
            self.agent_state.update_state(AgentStatus.IDLE)
            self.agent_state.current_trace_id = ""
            self._steer_requested = False
            self._steer_event_buffer.clear()
            reset_trace(trace_token)

    # -------------------------------------------------------------------------
    # Private Helpers
    # -------------------------------------------------------------------------

    async def _prepare_messages(
        self, prompt: str, event_name: str, payload: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """
        Assembles context, formats messages for the LLM, and injects multimodality.

        Args:
            prompt: Static system prompt.
            event_name: Primary wakeup event.
            payload: Primary wakeup payload.

        Returns:
            List[Dict[str, Any]]: Messages list in OpenAI format.
        """

        context = await self.context_builder.build(
            event_name, payload, self._realtime_events.view()
        )

        if self.agent_state.current_step >= 5:
            self._realtime_events.clear()

        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": context},
        ]

        messages = copy.deepcopy(messages)

        pending_media = self._collect_pending_media(
            payload, self._realtime_events.view()
        )
        messages = await self._inject_images_to_payload(
            messages, pending_media=pending_media
        )

        self.agent_state.last_input_tokens = self.executor.tracker.count_messages_tokens(
            messages
        )

        await asyncio.to_thread(self._dump_context_to_file, messages)

        log = (
            f"[ReAct] Step {self.agent_state.current_step}/{self.agent_state.max_react_steps}."
        )
        main_logger.info(log)
        agent_logger.info(log)

        return messages

    async def _execute_actions(self, thoughts: str, actions: List[ActionCall]) -> None:
        """
        Executes requested tools, updates state, and commits results to the DB.

        Args:
            thoughts: Internal monologue (CoT).
            actions: List of actions to execute.
        """

        self.agent_state.update_state(AgentStatus.ACTING)
        self.last_cycle_outcome["had_actions"] = True
        self.last_cycle_outcome["status"] = "acted"
        results_str = await execute_skill(actions=actions)
        results_str = _normalize_tool_output(results_str)

        self.agent_state.last_thoughts = thoughts
        self.agent_state.last_actions_result = results_str
        self.agent_state.last_action_tools = [action.tool_name for action in actions]
        if self.goal_manager is not None:
            await self.goal_manager.record_action_result(
                results_str,
                actions=[action.model_dump() for action in actions],
            )

        args_to_rag = []
        for act in actions:
            for val in act.parameters.values():
                if isinstance(val, str) and len(val) > 3:
                    args_to_rag.append(val)
        self.agent_state.last_action_args = args_to_rag

        await self.sql_ticks.save_tick(
            thoughts=thoughts,
            actions=[a.model_dump() for a in actions],
            results={
                "execution_report": results_str,
                "step": self.agent_state.current_step,
                "max_steps": self.agent_state.max_react_steps,
                "llm_metrics": self._llm_metrics_snapshot(),
                "trace": current_trace(),
            },
        )
        await self.event_bus.publish(Events.REACT_TICK_SAVED)

    async def _handle_completion(
        self, thoughts: str, status: str = "completed"
    ) -> None:
        """
        Logic for graceful completion (absence of actions).

        Args:
            thoughts: Final thoughts of the agent prior to sleep.
        """

        self.last_cycle_outcome["status"] = status
        log = (
            "[ReAct] Empty actions list received. Concluding cycle."
            if status == "completed"
            else "[ReAct] Terminal response rejected by Goal gate; scheduling continuation."
        )
        agent_logger.info(log)

        await self.sql_ticks.save_tick(
            thoughts=thoughts,
            actions=[],
            results={
                "status": status,
                "step": self.agent_state.current_step,
                "max_steps": self.agent_state.max_react_steps,
                "llm_metrics": self._llm_metrics_snapshot(),
                "trace": current_trace(),
            },
        )
        await self.event_bus.publish(Events.REACT_TICK_SAVED)

    async def _handle_protocol_error(self, raw_answer: str, error_msg: str) -> None:
        """Persist invalid provider output so the next step can self-correct."""

        safe_error = truncate_text(
            redact_sensitive_text(error_msg), max_chars=2000
        )
        safe_excerpt = truncate_text(
            redact_sensitive_text(raw_answer), max_chars=4000
        )
        safe_tail = redact_sensitive_text(raw_answer[-2000:])
        self.agent_state.last_thoughts = ""
        self.agent_state.last_action_error = safe_error
        self.agent_state.last_actions_result = (
            "LLM tool protocol error. Correct the response format on the next step: "
            + safe_error
        )
        agent_logger.warning(f"[ReAct] Tool protocol error: {safe_error}")
        await self.sql_ticks.save_tick(
            thoughts="[Protocol error: provider response rejected]",
            actions=[],
            results={
                "status": "protocol_error",
                "error": safe_error,
                "response_excerpt": safe_excerpt,
                "response_tail": safe_tail,
                "step": self.agent_state.current_step,
                "max_steps": self.agent_state.max_react_steps,
                "llm_metrics": self._llm_metrics_snapshot(),
                "trace": current_trace(),
            },
        )
        await self.event_bus.publish(Events.REACT_TICK_SAVED)

    async def _handle_step_limit(self) -> None:
        """Write a terminal record when a cycle exhausts its reasoning budget."""

        message = (
            f"ReAct cycle exhausted its {self.agent_state.max_react_steps}-step "
            "budget before producing a terminal response. Resume from durable "
            "ticks and the action journal on the next wakeup."
        )
        self.agent_state.last_action_error = message
        self.agent_state.last_actions_result = message
        if self.goal_manager is not None:
            await self.goal_manager.finish_cycle(
                state="exhausted",
                summary=message,
                wake_after_seconds=5,
            )
        agent_logger.warning(f"[ReAct] {message}")
        await self.sql_ticks.save_tick(
            thoughts="[Cycle stopped: step budget exhausted]",
            actions=[],
            results={
                "status": "max_steps_exhausted",
                "error": message,
                "step": self.agent_state.max_react_steps,
                "max_steps": self.agent_state.max_react_steps,
                "llm_metrics": self._llm_metrics_snapshot(),
                "trace": current_trace(),
            },
        )
        await self.event_bus.publish(Events.REACT_TICK_SAVED)

    async def _handle_cycle_cancelled(self) -> None:
        """Persist a terminal record before Heartbeat replaces this cycle."""

        message = "ReAct cycle cancelled by a higher-priority event."
        self.agent_state.last_action_error = message
        self.agent_state.last_actions_result = message
        if self.goal_manager is not None:
            await self.goal_manager.finish_cycle(
                state="continue",
                summary=message,
                wake_after_seconds=1,
            )
        await self.sql_ticks.save_tick(
            thoughts="[Cycle interrupted by higher-priority event]",
            actions=[],
            results={
                "status": "cycle_cancelled",
                "error": message,
                "step": self.agent_state.current_step,
                "max_steps": self.agent_state.max_react_steps,
                "llm_metrics": self._llm_metrics_snapshot(),
                "trace": current_trace(),
            },
        )
        await self.event_bus.publish(Events.REACT_TICK_SAVED)

    async def _handle_cycle_steered(self) -> None:
        """Persist a safe-boundary yield before Heartbeat starts queued work."""

        event_summaries = [
            {
                "name": event.get("name", "UNKNOWN"),
                "level": event.get("level", "UNKNOWN"),
                "time": event.get("time"),
                "count": event.get(
                    "coalesced_count",
                    event.get("payload", {}).get("dropped_total", 1),
                ),
            }
            for event in self._steer_event_buffer.view()[-20:]
        ]
        message = (
            "ReAct cycle yielded at a safe boundary for queued higher-priority "
            "event(s). The in-flight LLM request was not cancelled."
        )
        self.agent_state.last_action_error = ""
        self.agent_state.last_actions_result = message
        if self.goal_manager is not None:
            await self.goal_manager.finish_cycle(
                state="continue",
                summary=message,
                wake_after_seconds=1,
            )
        await self.sql_ticks.save_tick(
            thoughts="[Cycle steered to queued higher-priority event]",
            actions=[],
            results={
                "status": "cycle_steered",
                "message": message,
                "queued_events": event_summaries,
                "step": self.agent_state.current_step,
                "max_steps": self.agent_state.max_react_steps,
                "llm_metrics": self._llm_metrics_snapshot(),
                "trace": current_trace(),
            },
        )
        await self.event_bus.publish(Events.REACT_TICK_SAVED)

    def _llm_metrics_snapshot(self) -> Dict[str, Any]:
        metrics = getattr(self.executor, "last_call_metrics", {})
        return copy.deepcopy(metrics) if isinstance(metrics, dict) else {}

    def _parse_response(
        self, raw_answer: str
    ) -> Tuple[Optional[AgentResponse], Optional[str]]:
        """
        Parses the agent's JSON response.
        """
        parsed, error = parse_llm_json(raw_answer)
        if parsed is None or error:
            return parsed, error
        for action in parsed.actions:
            resolved = resolve_native_tool_name(action.tool_name)
            if action.tool_name.startswith("jawl_") and resolved == action.tool_name:
                return None, f"System Error: Unknown native tool '{action.tool_name}'."
            action.tool_name = resolved
        return parsed, None

    def add_realtime_event(self, event_data: Dict[str, Any]) -> None:
        """
        Adds an incoming event to the agent's context (called externally when the agent is awake).

        Args:
            event_data: Event payload dict.
        """

        self._realtime_events.append(event_data)

    def request_steer(self, event_data: Dict[str, Any]) -> None:
        """Request a non-cancelling yield at the next safe ReAct boundary."""

        self._steer_event_buffer.append(event_data)
        self._steer_requested = True

    def get_event_buffer_snapshot(self) -> Dict[str, Any]:
        """Return payload-free active-cycle event buffer counters."""

        return {
            "realtime": self._realtime_events.snapshot(),
            "steer": self._steer_event_buffer.snapshot(),
        }

    def _dump_context_to_file(self, messages: List[Dict[str, Any]]) -> None:
        """
        Creates a context dump (system prompt) to a Markdown file for debugging.

        Args:
            messages: Messages array.
        """

        dump_prompt_to_file(
            "logs/prompts/main_prompt.md", messages, meta_header="# MAIN AGENT DUMP"
        )

    def _encode_image(self, image_path: str) -> str:
        """Encodes an image from disk to Base64."""
        with open(image_path, "rb") as image_file:
            return base64.b64encode(image_file.read()).decode("utf-8")

    @staticmethod
    def _collect_pending_media(
        primary_payload: Optional[Dict[str, Any]],
        realtime_events: List[Dict[str, Any]],
    ) -> List[str]:
        """Collect image paths from the wake event and buffered events in order."""

        ordered: List[str] = []
        seen = set()

        def add_payload(candidate: Any) -> None:
            if not isinstance(candidate, dict):
                return
            paths = candidate.get("media_paths", [])
            if not isinstance(paths, list):
                return
            for image_path in paths:
                if not isinstance(image_path, str) or not image_path or image_path in seen:
                    continue
                seen.add(image_path)
                ordered.append(image_path)

        add_payload(primary_payload)
        for event in realtime_events:
            if not isinstance(event, dict):
                continue
            add_payload(event.get("payload"))
            for sample in event.get("payload_samples", []):
                add_payload(sample)
        return ordered

    async def _inject_images_to_payload(
        self, messages: List[Dict[str, Any]], pending_media: Optional[List[str]] = None
    ) -> List[Dict[str, Any]]:
        """
        Injects Base64 images into the User prompt if a system marker is found.

        Args:
            messages: Messages list.

        Returns:
            List[Dict[str, Any]]: Messages list with Base64 payloads injected.
        """

        last_result = self.agent_state.last_actions_result or ""
        image_paths = re.findall(
            r"\[SYSTEM_MARKER_IMAGE_ATTACHED:\s*(.+?)\]", last_result
        )

        # Also include pending Telegram media
        if pending_media:
            image_paths.extend(pending_media)

        if not image_paths:
            return messages

        user_msg = messages[1]

        if isinstance(user_msg, dict) and user_msg.get("role") == "user":
            original_text = user_msg["content"]
            new_content = [{"type": "text", "text": original_text}]

            seen_paths = set()
            for img_path in image_paths:
                if img_path in seen_paths:
                    continue
                seen_paths.add(img_path)
                try:
                    path_obj = Path(img_path)
                    if path_obj.exists():
                        base64_data = await asyncio.to_thread(
                            self._encode_image, str(path_obj)
                        )
                        ext = path_obj.suffix.lower()
                        mime = "image/jpeg" if ext in [".jpg", ".jpeg"] else f"image/{ext[1:]}"

                        new_content.append(
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:{mime};base64,{base64_data}"},
                            }
                        )

                        log = f"[ReAct] Image {path_obj.name} successfully injected."
                        agent_logger.info(log)

                except Exception as e:
                    log = f"[ReAct] Base64 injection error: {e}"
                    main_logger.error(f"[ReAct] Base64 injection error: {e}")
                    agent_logger.error(log)

            user_msg["content"] = new_content

        return messages
