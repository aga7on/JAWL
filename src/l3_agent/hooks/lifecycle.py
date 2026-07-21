"""Ordered, bounded lifecycle hooks with explicit pre-action decisions."""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional

from src.utils.event.bus import EventBus
from src.utils.event.registry import EventConfig, EventLevel
from src.utils.logger import agent_logger
from src.utils.tracing import current_trace


class HookPhase(str, Enum):
    PRE_TOOL_USE = "pre_tool_use"
    POST_TOOL_USE = "post_tool_use"
    TOOL_ERROR = "tool_error"
    TOOL_CANCELLED = "tool_cancelled"
    PRE_CONTEXT_COMPACTION = "pre_context_compaction"
    POST_CONTEXT_COMPACTION = "post_context_compaction"
    PRE_SYSTEM_STOP = "pre_system_stop"
    POST_SYSTEM_STOP = "post_system_stop"
    PRE_DELEGATION = "pre_delegation"
    POST_DELEGATION = "post_delegation"
    DELEGATION_ERROR = "delegation_error"
    DELEGATION_CANCELLED = "delegation_cancelled"

    @property
    def can_deny(self) -> bool:
        return self in {self.PRE_TOOL_USE, self.PRE_DELEGATION}


class LifecycleEvents:
    """Observational events kept outside ``Events.all()`` Heartbeat routing."""

    PRE_TOOL_USE = EventConfig(
        name="LIFECYCLE_PRE_TOOL_USE",
        description="A tool call completed lifecycle preflight.",
        level=EventLevel.INFO,
        requires_attention=False,
    )
    POST_TOOL_USE = EventConfig(
        name="LIFECYCLE_POST_TOOL_USE",
        description="A tool call finished.",
        level=EventLevel.INFO,
        requires_attention=False,
    )
    TOOL_ERROR = EventConfig(
        name="LIFECYCLE_TOOL_ERROR",
        description="A tool call or its lifecycle hook failed.",
        level=EventLevel.INFO,
        requires_attention=False,
    )
    TOOL_CANCELLED = EventConfig(
        name="LIFECYCLE_TOOL_CANCELLED",
        description="A running tool call was cancelled.",
        level=EventLevel.INFO,
        requires_attention=False,
    )
    PRE_CONTEXT_COMPACTION = EventConfig(
        name="LIFECYCLE_PRE_CONTEXT_COMPACTION",
        description="Dynamic context is about to be compacted.",
        level=EventLevel.INFO,
        requires_attention=False,
    )
    POST_CONTEXT_COMPACTION = EventConfig(
        name="LIFECYCLE_POST_CONTEXT_COMPACTION",
        description="Dynamic context compaction finished.",
        level=EventLevel.INFO,
        requires_attention=False,
    )
    PRE_SYSTEM_STOP = EventConfig(
        name="LIFECYCLE_PRE_SYSTEM_STOP",
        description="Graceful system shutdown started.",
        level=EventLevel.INFO,
        requires_attention=False,
    )
    POST_SYSTEM_STOP = EventConfig(
        name="LIFECYCLE_POST_SYSTEM_STOP",
        description="Managed resources completed graceful shutdown.",
        level=EventLevel.INFO,
        requires_attention=False,
    )
    PRE_DELEGATION = EventConfig(
        name="LIFECYCLE_PRE_DELEGATION",
        description="A delegated worker is awaiting lifecycle preflight.",
        level=EventLevel.INFO,
        requires_attention=False,
    )
    POST_DELEGATION = EventConfig(
        name="LIFECYCLE_POST_DELEGATION",
        description="A delegated worker completed.",
        level=EventLevel.INFO,
        requires_attention=False,
    )
    DELEGATION_ERROR = EventConfig(
        name="LIFECYCLE_DELEGATION_ERROR",
        description="A delegated worker failed.",
        level=EventLevel.INFO,
        requires_attention=False,
    )
    DELEGATION_CANCELLED = EventConfig(
        name="LIFECYCLE_DELEGATION_CANCELLED",
        description="A delegated worker was cancelled.",
        level=EventLevel.INFO,
        requires_attention=False,
    )

    @classmethod
    def for_phase(cls, phase: HookPhase) -> EventConfig:
        return {
            HookPhase.PRE_TOOL_USE: cls.PRE_TOOL_USE,
            HookPhase.POST_TOOL_USE: cls.POST_TOOL_USE,
            HookPhase.TOOL_ERROR: cls.TOOL_ERROR,
            HookPhase.TOOL_CANCELLED: cls.TOOL_CANCELLED,
            HookPhase.PRE_CONTEXT_COMPACTION: cls.PRE_CONTEXT_COMPACTION,
            HookPhase.POST_CONTEXT_COMPACTION: cls.POST_CONTEXT_COMPACTION,
            HookPhase.PRE_SYSTEM_STOP: cls.PRE_SYSTEM_STOP,
            HookPhase.POST_SYSTEM_STOP: cls.POST_SYSTEM_STOP,
            HookPhase.PRE_DELEGATION: cls.PRE_DELEGATION,
            HookPhase.POST_DELEGATION: cls.POST_DELEGATION,
            HookPhase.DELEGATION_ERROR: cls.DELEGATION_ERROR,
            HookPhase.DELEGATION_CANCELLED: cls.DELEGATION_CANCELLED,
        }[phase]


@dataclass(frozen=True)
class HookContext:
    phase: HookPhase
    plan_id: str
    action_id: str
    tool_name: str
    parameters: Dict[str, Any]
    outcome: Optional[Dict[str, Any]] = None
    trace: Dict[str, Any] = field(default_factory=current_trace)


@dataclass(frozen=True)
class HookDecision:
    allowed: bool = True
    reason: str = ""

    @classmethod
    def deny(cls, reason: str) -> "HookDecision":
        return cls(allowed=False, reason=reason or "Denied by lifecycle policy.")


@dataclass(frozen=True)
class HookRun:
    decision: HookDecision = field(default_factory=HookDecision)
    executed: int = 0
    failures: tuple[str, ...] = ()


HookHandler = Callable[[HookContext], Any | Awaitable[Any]]


@dataclass(frozen=True)
class _RegisteredHook:
    priority: int
    order: int
    name: str
    handler: HookHandler


class LifecycleHooks:
    """Run policy hooks in stable priority/order with per-handler timeouts.

    Pre-tool and pre-delegation handlers may return ``HookDecision.deny(...)``
    or ``False``. Hook
    exceptions are isolated and fail open by default; ``fail_closed`` converts
    a deny-capable pre-hook timeout/exception into a denial. Other hooks are always
    observational and cannot rewrite an action outcome.
    """

    def __init__(
        self,
        event_bus: Optional[EventBus] = None,
        timeout_seconds: float = 5.0,
        fail_closed: bool = False,
    ) -> None:
        if timeout_seconds <= 0 or timeout_seconds > 300:
            raise ValueError("hook timeout_seconds must be between 0 and 300")
        self.event_bus = event_bus
        self.timeout_seconds = timeout_seconds
        self.fail_closed = fail_closed
        self._handlers: Dict[HookPhase, List[_RegisteredHook]] = {
            phase: [] for phase in HookPhase
        }
        self._registration_order = 0

    def subscribe(
        self,
        phase: HookPhase,
        handler: HookHandler,
        *,
        priority: int = 0,
        name: Optional[str] = None,
    ) -> None:
        if not callable(handler):
            raise TypeError("lifecycle hook handler must be callable")
        self._registration_order += 1
        registered = _RegisteredHook(
            priority=priority,
            order=self._registration_order,
            name=name or getattr(handler, "__name__", handler.__class__.__name__),
            handler=handler,
        )
        self._handlers[phase].append(registered)
        self._handlers[phase].sort(key=lambda item: (-item.priority, item.order))

    def has_handlers(self, phase: Optional[HookPhase] = None) -> bool:
        if phase is not None:
            return bool(self._handlers[phase])
        return any(self._handlers.values())

    async def run(self, context: HookContext) -> HookRun:
        handlers = list(self._handlers[context.phase])
        failures: List[str] = []
        executed = 0
        decision = HookDecision()
        for hook in handlers:
            try:
                if inspect.iscoroutinefunction(hook.handler):
                    result = await asyncio.wait_for(
                        hook.handler(context), timeout=self.timeout_seconds
                    )
                else:
                    result = await asyncio.wait_for(
                        asyncio.to_thread(hook.handler, context),
                        timeout=self.timeout_seconds,
                    )
                if inspect.isawaitable(result):
                    result = await asyncio.wait_for(
                        result, timeout=self.timeout_seconds
                    )
                executed += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failure = f"{hook.name}: {type(exc).__name__}: {exc}"
                failures.append(failure)
                agent_logger.warning(f"[Lifecycle Hook] {failure}")
                if self.fail_closed and context.phase.can_deny:
                    decision = HookDecision.deny(
                        f"Lifecycle hook '{hook.name}' failed closed."
                    )
                    break
                continue

            if context.phase.can_deny:
                if isinstance(result, HookDecision):
                    decision = result
                elif result is False:
                    decision = HookDecision.deny(
                        f"Lifecycle hook '{hook.name}' denied the action."
                    )
                if not decision.allowed:
                    break

        await self._publish(context, decision, failures)
        return HookRun(
            decision=decision,
            executed=executed,
            failures=tuple(failures),
        )

    async def _publish(
        self,
        context: HookContext,
        decision: HookDecision,
        failures: List[str],
    ) -> None:
        if self.event_bus is None:
            return
        await self.event_bus.publish(
            LifecycleEvents.for_phase(context.phase),
            phase=context.phase.value,
            plan_id=context.plan_id,
            action_id=context.action_id,
            tool_name=context.tool_name,
            operation_name=context.tool_name,
            allowed=decision.allowed,
            reason=decision.reason,
            failures=list(failures),
            outcome=context.outcome,
            trace=context.trace,
        )
