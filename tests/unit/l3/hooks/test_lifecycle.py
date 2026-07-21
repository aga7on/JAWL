import asyncio

import pytest

from src.l3_agent.hooks.lifecycle import (
    HookContext,
    HookDecision,
    HookPhase,
    LifecycleHooks,
    LifecycleEvents,
)
from src.utils.event.bus import EventBus
from src.utils.event.registry import Events


def context(phase: HookPhase = HookPhase.PRE_TOOL_USE) -> HookContext:
    return HookContext(
        phase=phase,
        plan_id="plan-1",
        action_id="action-1",
        tool_name="HostOSCodingFiles.read",
        parameters={"path": "src/app.py"},
    )


@pytest.mark.asyncio
async def test_pre_hooks_run_by_priority_then_registration_and_can_deny():
    hooks = LifecycleHooks()
    order = []

    async def low(_context):
        order.append("low")

    async def first_high(_context):
        order.append("first-high")

    async def deny(_context):
        order.append("deny")
        return HookDecision.deny("protected path")

    async def never(_context):
        order.append("never")

    hooks.subscribe(HookPhase.PRE_TOOL_USE, low, priority=0)
    hooks.subscribe(HookPhase.PRE_TOOL_USE, first_high, priority=10)
    hooks.subscribe(HookPhase.PRE_TOOL_USE, deny, priority=10)
    hooks.subscribe(HookPhase.PRE_TOOL_USE, never, priority=-1)

    run = await hooks.run(context())

    assert order == ["first-high", "deny"]
    assert run.decision == HookDecision.deny("protected path")
    assert run.executed == 2


@pytest.mark.asyncio
async def test_hook_timeout_is_fail_open_or_fail_closed_by_policy():
    async def slow(_context):
        await asyncio.sleep(0.05)

    open_hooks = LifecycleHooks(timeout_seconds=0.005, fail_closed=False)
    open_hooks.subscribe(HookPhase.PRE_TOOL_USE, slow)
    open_run = await open_hooks.run(context())

    closed_hooks = LifecycleHooks(timeout_seconds=0.005, fail_closed=True)
    closed_hooks.subscribe(HookPhase.PRE_TOOL_USE, slow, name="slow-policy")
    closed_run = await closed_hooks.run(context())

    assert open_run.decision.allowed is True
    assert open_run.failures and "TimeoutError" in open_run.failures[0]
    assert closed_run.decision.allowed is False
    assert "slow-policy" in closed_run.decision.reason


@pytest.mark.asyncio
async def test_observational_hook_failure_cannot_rewrite_outcome():
    hooks = LifecycleHooks(fail_closed=True)

    async def broken(_context):
        raise RuntimeError("observer failed")

    hooks.subscribe(HookPhase.POST_TOOL_USE, broken)
    run = await hooks.run(context(HookPhase.POST_TOOL_USE))

    assert run.decision.allowed is True
    assert run.failures and "observer failed" in run.failures[0]


@pytest.mark.asyncio
async def test_pre_delegation_can_deny_but_compaction_cannot():
    hooks = LifecycleHooks(fail_closed=True)

    async def deny(_context):
        return False

    hooks.subscribe(HookPhase.PRE_DELEGATION, deny)
    hooks.subscribe(HookPhase.PRE_CONTEXT_COMPACTION, deny)

    delegation = await hooks.run(context(HookPhase.PRE_DELEGATION))
    compaction = await hooks.run(context(HookPhase.PRE_CONTEXT_COMPACTION))

    assert delegation.decision.allowed is False
    assert compaction.decision.allowed is True


@pytest.mark.asyncio
async def test_lifecycle_observation_uses_event_bus_without_heartbeat_routing():
    bus = EventBus()
    hooks = LifecycleHooks(event_bus=bus)
    observed = []

    async def observer(**payload):
        observed.append(payload)

    bus.subscribe(LifecycleEvents.PRE_TOOL_USE, observer)
    await hooks.run(context())
    await bus.stop()

    assert observed[0]["tool_name"] == "HostOSCodingFiles.read"
    assert observed[0]["allowed"] is True
    assert LifecycleEvents.PRE_TOOL_USE.name == "LIFECYCLE_PRE_TOOL_USE"
    assert LifecycleEvents.PRE_TOOL_USE.name not in {
        event.name for event in Events.all()
    }
    assert all(
        LifecycleEvents.for_phase(phase).name
        not in {event.name for event in Events.all()}
        for phase in HookPhase
    )
