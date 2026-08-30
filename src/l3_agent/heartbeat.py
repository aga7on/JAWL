"""
Agent Lifecycle and Rhythm (Pulse) Orchestrator.

Responsible for scheduling ReAct reasoning loops, capturing external events
from the EventBus, and dynamically adjusting sleep intervals (Event Acceleration).
"""

import asyncio
import time
from typing import Optional, Dict, Any, Literal, TYPE_CHECKING

from src.utils.logger import main_logger, agent_logger
from src.utils.event.registry import EventLevel
from src.utils.dtime import get_now_formatted, seconds_to_duration_str
from src.utils._tools import update_last_active_time
from src.l3_agent.event_buffer import BoundedEventBuffer, EventBufferOutcome

if TYPE_CHECKING:
    from src.l3_agent.react.loop import ReactLoop
    from src.l3_agent.goals.manager import GoalManager
    from src.utils.settings import (
        EventAccelerationConfig,
        IdleHeartbeatBackoffConfig,
    )


DEPTH_MULTIPLIERS: Dict[str, Dict[EventLevel, float]] = {
    "deep": {
        EventLevel.CRITICAL: 0.30,
        EventLevel.HIGH: 0.80,
        EventLevel.MEDIUM: 0.85,
        EventLevel.LOW: 0.90,
        EventLevel.BACKGROUND: 0.95,
    },
    "superficially": {
        EventLevel.CRITICAL: 0.10,
        EventLevel.HIGH: 0.70,
        EventLevel.MEDIUM: 0.75,
        EventLevel.LOW: 0.80,
        EventLevel.BACKGROUND: 0.85,
    },
}


class Heartbeat:
    """
    Main agent pulse orchestrator.
    Manages reasoning loop invocation schedules and triggers wakeups.
    """

    def __init__(
        self,
        react_loop: "ReactLoop",
        heartbeat_interval: int,
        continuous_cycle: bool,
        accel_config: "EventAccelerationConfig",
        timezone: int,
        goal_manager: Optional["GoalManager"] = None,
        idle_backoff_config: Optional["IdleHeartbeatBackoffConfig"] = None,
    ) -> None:
        """
        Initializes the heartbeat orchestrator.

        Args:
            react_loop: Active reasoning loop instance.
            heartbeat_interval: Base sleep interval in seconds.
            continuous_cycle: Continuous execution flag without sleep.
            accel_config: Wakeup acceleration multipliers configuration.
            timezone: Timezone UTC offset.
        """

        self.react_loop = react_loop
        self.heartbeat_interval = heartbeat_interval
        self.continuous_cycle = continuous_cycle
        self.accel_config = accel_config
        self.timezone = timezone
        self.goal_manager = goal_manager
        self._suppressed_goal_heartbeats = 0
        self.idle_backoff_config = idle_backoff_config
        self._consecutive_idle_heartbeats = 0
        self._idle_interval_multiplier = 1

        self._wake_event = asyncio.Event()
        self._is_running: bool = False

        self._next_tick_time: float = 0.0
        self._wake_reason: str = "HEARTBEAT"
        self._wake_payload: Dict[str, Any] = {}
        self._wake_level: int = 0

        queue_max = getattr(self.accel_config, "queue_max_events", 100)
        if not isinstance(queue_max, int) or isinstance(queue_max, bool):
            queue_max = 100
        coalesce_window = getattr(
            self.accel_config, "coalesce_window_sec", 2.0
        )
        if not isinstance(coalesce_window, (int, float)) or isinstance(
            coalesce_window, bool
        ):
            coalesce_window = 2.0
        coalesce_names = getattr(self.accel_config, "coalesce_event_names", [])
        if not isinstance(coalesce_names, (list, tuple, set, frozenset)):
            coalesce_names = []
        payload_samples = getattr(
            self.accel_config, "coalesce_payload_samples", 3
        )
        if not isinstance(payload_samples, int) or isinstance(payload_samples, bool):
            payload_samples = 3
        self._event_buffer = BoundedEventBuffer(
            capacity=queue_max,
            coalesce_window_sec=float(coalesce_window),
            coalesce_names=coalesce_names,
            payload_sample_limit=payload_samples,
        )

        self._active_react_task: Optional[asyncio.Task] = None

        self._is_interrupted: bool = False
        self._deferred_wakeup: bool = False
        self.active_multipliers: Dict[EventLevel, float] = {}
        self.current_sleep_depth = "normal"
        self._custom_sleep_active = False
        self._reset_multipliers()

    def _reset_multipliers(self) -> None:
        """Restore configured event sensitivity after a one-shot sleep."""

        self.active_multipliers = {
            EventLevel.CRITICAL: self.accel_config.critical_multiplier,
            EventLevel.HIGH: self.accel_config.high_multiplier,
            EventLevel.MEDIUM: self.accel_config.medium_multiplier,
            EventLevel.LOW: self.accel_config.low_multiplier,
            EventLevel.BACKGROUND: self.accel_config.background_multiplier,
        }
        self.current_sleep_depth = "normal"

    def set_custom_sleep(
        self, duration_sec: int, depth: Literal["deep", "superficially"] = "deep"
    ) -> None:
        """Schedule one intentional sleep without letting Goal wakeups override it."""

        duration_sec = max(1, int(duration_sec))
        self._next_tick_time = time.time() + duration_sec
        self.active_multipliers = DEPTH_MULTIPLIERS.get(
            depth, DEPTH_MULTIPLIERS["deep"]
        ).copy()
        self.current_sleep_depth = depth if depth in DEPTH_MULTIPLIERS else "deep"
        self._custom_sleep_active = True
        agent_logger.info(
            "[Heartbeat] Custom sleep mode engaged for "
            f"{seconds_to_duration_str(duration_sec)} "
            f"(depth: '{self.current_sleep_depth}')."
        )

    def _enqueue_event(self, event_data: Dict[str, Any]) -> EventBufferOutcome:
        outcome = self._event_buffer.append(event_data)
        if outcome.dropped or outcome.evicted_name is not None:
            dropped_total = self._event_buffer.snapshot()["dropped_total"]
            if dropped_total == 1 or dropped_total & (dropped_total - 1) == 0:
                agent_logger.warning(
                    "[Heartbeat] Bounded event queue overflow: "
                    f"dropped_or_evicted={dropped_total}, "
                    f"capacity={self._event_buffer.capacity}."
                )
        return outcome

    def get_queue_snapshot(self) -> Dict[str, Any]:
        """Return payload-free queue observability for tools and diagnostics."""

        realtime = getattr(self.react_loop, "get_event_buffer_snapshot", None)
        return {
            "wake_queue": self._event_buffer.snapshot(),
            "react_buffers": realtime() if callable(realtime) else {},
            "primary_wake": {
                "name": self._wake_reason,
                "level": self._wake_level,
            },
            "active_cycle": bool(
                self._active_react_task and not self._active_react_task.done()
            ),
            "deferred_wakeup": self._deferred_wakeup,
            "suppressed_goal_heartbeats": self._suppressed_goal_heartbeats,
            "idle_heartbeat_backoff": {
                "consecutive_no_ops": self._consecutive_idle_heartbeats,
                "interval_multiplier": self._idle_interval_multiplier,
            },
        }

    def _reset_idle_backoff(self) -> None:
        self._consecutive_idle_heartbeats = 0
        self._idle_interval_multiplier = 1

    def _record_cycle_outcome(
        self, event_name: str, missed_events: list[Dict[str, Any]]
    ) -> None:
        """Back off only proven empty timer cycles; events always reset it."""

        config = self.idle_backoff_config
        enabled = bool(getattr(config, "enabled", False))
        outcome = getattr(self.react_loop, "last_cycle_outcome", None)
        eligible = (
            enabled
            and not self.continuous_cycle
            and event_name == "HEARTBEAT"
            and not missed_events
            and isinstance(outcome, dict)
            and outcome.get("status") == "completed"
            and not outcome.get("had_actions")
        )
        if not eligible:
            self._reset_idle_backoff()
            return

        self._consecutive_idle_heartbeats += 1
        threshold = max(1, int(getattr(config, "no_op_threshold", 2)))
        max_multiplier = max(1, int(getattr(config, "max_multiplier", 8)))
        max_interval = max(
            60, int(getattr(config, "max_interval_sec", 3300))
        )
        if self.heartbeat_interval > 0:
            max_multiplier = min(
                max_multiplier,
                max(1, max_interval // self.heartbeat_interval),
            )
        if self._consecutive_idle_heartbeats < threshold:
            multiplier = 1
        else:
            multiplier = min(
                max_multiplier,
                2 ** (self._consecutive_idle_heartbeats - threshold + 1),
            )
        changed = multiplier != self._idle_interval_multiplier
        self._idle_interval_multiplier = multiplier
        if multiplier > 1 and self.heartbeat_interval > 0:
            self._next_tick_time = max(
                self._next_tick_time,
                time.time() + self.heartbeat_interval * multiplier,
            )
        if changed:
            agent_logger.info(
                "[Heartbeat] Repeated empty autonomous cycles detected; "
                f"next timer interval is {multiplier}x "
                f"(no_ops={self._consecutive_idle_heartbeats}). "
                "External events remain immediate."
            )

    def answer_to_event(
        self,
        level: EventLevel,
        event_name: str,
        payload: Optional[Dict[str, Any]] = None,
        requires_attention: bool = True,
    ) -> None:
        """
        Analyzes incoming events and schedules/accelerates wakeups.

        Args:
            level: Event severity level.
            event_name: Triggering event name identifier.
            payload: Event parameters.
        """

        now = time.time()
        payload = payload or {}
        self._reset_idle_backoff()
        time_str = get_now_formatted(self.timezone, fmt="%H:%M:%S")

        event_data = {
            "time": time_str,
            "level": level.name,
            "name": event_name,
            "payload": payload,
        }

        multiplier = self.active_multipliers.get(level, 1.0)

        is_awake = self._active_react_task and not self._active_react_task.done()

        # ---------------------------------------------------------------------
        # Logic for currently active (awake) agent
        # ---------------------------------------------------------------------

        if is_awake:
            active_policy = getattr(
                self.accel_config, "active_cycle_policy", "interrupt"
            )

            # A zero multiplier may interrupt now, defer to a safe boundary, or
            # merely append the event, depending on explicit runtime policy.
            if multiplier <= 0.01 and requires_attention:
                if active_policy == "defer":
                    outcome = self._enqueue_event(event_data)
                    queued_event = outcome.event or event_data
                    if level.value >= self._wake_level:
                        self._wake_reason = event_name
                        self._wake_payload = payload
                        self._wake_level = level.value
                    self._deferred_wakeup = True
                    self._next_tick_time = time.time()
                    self._wake_event.set()
                    self.react_loop.request_steer(queued_event)
                    agent_logger.warning(
                        f"[Heartbeat] Deferred active ReAct cycle at a safe "
                        f"boundary due to event: {event_name} ({level.name})"
                    )
                    return

                if requires_attention:
                    self.react_loop.add_realtime_event(event_data)
                    agent_logger.info(
                        f"[Heartbeat] Incoming event '{event_name}' ({level.name}) "
                        "received during agent execution. Data appended to context."
                    )
                if active_policy == "append":
                    return

                log = f"[Heartbeat] Interrupted current ReAct cycle due to event: {event_name} ({level.name})"
                agent_logger.warning(log)

                self._wake_reason = event_name
                self._wake_payload = payload
                self._wake_level = level.value
                self._is_interrupted = True

                # Set timer to zero to restart the cycle immediately after cancel()
                self._next_tick_time = time.time()
                self._wake_event.set()

                self._active_react_task.cancel()

                return

            # Non-immediate events remain available to the next ReAct step.
            if requires_attention:
                self.react_loop.add_realtime_event(event_data)
                agent_logger.info(
                    f"[Heartbeat] Incoming event '{event_name}' ({level.name}) "
                    "received during agent execution. Data appended to context."
                )
            return

        # ---------------------------------------------------------------------
        # Logic for currently sleeping agent
        # ---------------------------------------------------------------------

        if requires_attention:
            self._enqueue_event(event_data)

        remaining = self._next_tick_time - now

        if multiplier < 1.0:
            safe_remaining = max(0.0, remaining)

            new_remaining = safe_remaining * multiplier
            reduced_by = safe_remaining - new_remaining

            self._next_tick_time = now + new_remaining
            self._wake_event.set()

            # If remaining sleep is cut to zero, execute urgent wakeup
            if new_remaining <= 0.01:
                # Override primary reason only if the new event is of equal or higher priority
                if level.value >= self._wake_level:
                    log = f"[Heartbeat] Incoming wakeup event: '{event_name}' ({level.name}). Invoking LLM."
                    agent_logger.info(log)

                    self._wake_reason = event_name
                    self._wake_payload = payload
                    self._wake_level = level.value
                else:
                    log = f"[Heartbeat] Incoming event: '{event_name}' ({level.name}). Sleep already interrupted by a higher priority event."
                    agent_logger.info(log)
            else:
                if safe_remaining > 0:
                    log = f"[Heartbeat] Incoming event: '{event_name}' ({level.name}). Next LLM execution scheduled sooner by {reduced_by:.1f} sec. Until wakeup: {new_remaining:.1f} sec."
                    agent_logger.info(log)

    async def start(self) -> None:
        """
        Starts the heartbeat infinite polling loop.
        """
        if self._is_running:
            return

        self._is_running = True
        log = "[Heartbeat] Agent switched to autonomous mode."
        agent_logger.info(log)

        if self._next_tick_time == 0.0:
            self._next_tick_time = time.time() + self.heartbeat_interval

        update_last_active_time()

        while self._is_running:
            update_last_active_time()
            now = time.time()

            if self.continuous_cycle:
                await asyncio.sleep(0.1)
            else:
                sleep_duration = self._next_tick_time - now
                if sleep_duration > 0:
                    self._wake_event.clear()
                    try:
                        await asyncio.wait_for(self._wake_event.wait(), timeout=sleep_duration)
                    except asyncio.TimeoutError:
                        if self._next_tick_time <= time.time():
                            self._wake_reason = "HEARTBEAT"
                            self._wake_payload = {}
                            self._wake_level = 0

            if self.continuous_cycle or time.time() >= self._next_tick_time:
                missed_events = self._event_buffer.drain()

                if self._wake_reason != "HEARTBEAT":
                    for i in range(len(missed_events) - 1, -1, -1):
                        if (
                            missed_events[i]["name"] == self._wake_reason
                            and missed_events[i]["payload"] == self._wake_payload
                        ):
                            missed_events.pop(i)
                            break

                woke_from_custom_sleep = self._custom_sleep_active
                if woke_from_custom_sleep:
                    self._custom_sleep_active = False
                    self._reset_multipliers()
                self._next_tick_time = time.time() + self.heartbeat_interval
                self._deferred_wakeup = False

                if (
                    self._wake_reason == "HEARTBEAT"
                    and not missed_events
                    and self.goal_manager is not None
                    and not self.goal_manager.should_run_heartbeat()
                ):
                    self._suppressed_goal_heartbeats += 1
                    count = self._suppressed_goal_heartbeats
                    if count == 1 or count & (count - 1) == 0:
                        agent_logger.info(
                            "[Heartbeat] Suppressed deterministic no-op Goal "
                            f"heartbeat (total={count})."
                        )
                    continue

                try:
                    self._active_react_task = asyncio.create_task(
                        self.react_loop.run(
                            event_name=self._wake_reason,
                            payload=self._wake_payload,
                            missed_events=missed_events,
                        )
                    )
                    await self._active_react_task
                    self._record_cycle_outcome(self._wake_reason, missed_events)
                    if self.goal_manager is not None and not self._custom_sleep_active:
                        goal_delay = self.goal_manager.seconds_until_wakeup()
                        if goal_delay is not None:
                            self._next_tick_time = min(
                                self._next_tick_time,
                                time.time() + max(0.1, goal_delay),
                            )
                    if not self._deferred_wakeup:
                        self._wake_reason = "HEARTBEAT"
                        self._wake_payload = {}
                        self._wake_level = 0
                    
                except asyncio.CancelledError:
                    if self._is_interrupted:
                        main_logger.info(
                            "[System] Current ReAct cycle successfully cancelled."
                        )
                        self._is_interrupted = False
                    else:
                        raise
                except Exception as e:
                    log = f"[System] Critical error in ReAct reasoning cycle: {e}"
                    agent_logger.error(log)
                    if not self._deferred_wakeup:
                        self._next_tick_time = time.time() + self.heartbeat_interval
                        self._wake_reason = "HEARTBEAT"
                        self._wake_payload = {}
                        self._wake_level = 0
                finally:
                    self._active_react_task = None

    def stop(self) -> None:
        """Terminates heartbeat loop and cancels any running ReAct cycles."""
        self._is_running = False
        self._wake_event.set()
        if self._active_react_task and not self._active_react_task.done():
            self._is_interrupted = True
            self._active_react_task.cancel()

        log = "[Heartbeat] Orchestrator stopped."
        agent_logger.info(log)

    def update_config(self, key: str, value: Any) -> None:
        """
        Hot-reloads heartbeat parameters from EventBus.

        Args:
            key: Config parameter key.
            value: Value payload.
        """
        if key == "heartbeat_interval":
            self.heartbeat_interval = int(value)

            log = f"[System] Heartbeat updated interval to {self.heartbeat_interval} sec."
            agent_logger.info(log)

        elif key == "continuous_cycle":
            self.continuous_cycle = bool(value)

            log = f"[System] Heartbeat updated continuous_cycle to {self.continuous_cycle}."
            agent_logger.info(log)
