import asyncio
from types import SimpleNamespace

import pytest

from src.l3_agent.skills.execution import ActionExecutionEngine
from src.l3_agent.skills.schema import ACTION_SCHEMA, ActionCall


def result(success: bool = True, message: str = "ok") -> SimpleNamespace:
    return SimpleNamespace(is_success=success, message=message)


def test_legacy_action_payload_and_coordination_schema_are_compatible():
    legacy = ActionCall(tool_name="read", parameters={"filepath": "README.md"})

    assert legacy.action_id is None
    assert legacy.depends_on == []
    assert legacy.parallel_group is None
    assert legacy.resources == []

    action_properties = ACTION_SCHEMA[0]["function"]["parameters"]["properties"][
        "actions"
    ]["items"]["properties"]
    assert {"action_id", "depends_on", "parallel_group", "resources"} <= set(
        action_properties
    )


@pytest.mark.asyncio
async def test_actions_are_sequential_by_default():
    engine = ActionExecutionEngine(max_parallel_actions=4)
    order = []

    async def runner(action: ActionCall):
        order.append(f"start:{action.tool_name}")
        await asyncio.sleep(0)
        order.append(f"end:{action.tool_name}")
        return result()

    await engine.execute(
        [ActionCall(tool_name="first"), ActionCall(tool_name="second")], runner
    )

    assert order == ["start:first", "end:first", "start:second", "end:second"]


@pytest.mark.asyncio
async def test_explicit_parallel_group_runs_ready_actions_together():
    engine = ActionExecutionEngine(max_parallel_actions=4)
    both_started = asyncio.Event()
    release = asyncio.Event()
    active = 0

    async def runner(action: ActionCall):
        nonlocal active
        active += 1
        if active == 2:
            both_started.set()
        await release.wait()
        active -= 1
        return result(message=action.tool_name)

    task = asyncio.create_task(
        engine.execute(
            [
                ActionCall(tool_name="first", parallel_group="reads"),
                ActionCall(tool_name="second", parallel_group="reads"),
            ],
            runner,
        )
    )

    await asyncio.wait_for(both_started.wait(), timeout=0.5)
    release.set()
    outcomes = await task

    assert [outcome.message for outcome in outcomes] == ["first", "second"]


@pytest.mark.asyncio
async def test_dependencies_control_execution_order():
    engine = ActionExecutionEngine()
    order = []

    async def runner(action: ActionCall):
        order.append(action.tool_name)
        return result()

    outcomes = await engine.execute(
        [
            ActionCall(
                tool_name="consumer", action_id="consume", depends_on=["produce"]
            ),
            ActionCall(tool_name="producer", action_id="produce"),
        ],
        runner,
    )

    assert order == ["producer", "consumer"]
    assert all(outcome.is_success for outcome in outcomes)


@pytest.mark.asyncio
async def test_failed_dependency_skips_dependant():
    engine = ActionExecutionEngine()
    executed = []

    async def runner(action: ActionCall):
        executed.append(action.tool_name)
        return result(success=False, message="failed")

    outcomes = await engine.execute(
        [
            ActionCall(tool_name="producer", action_id="produce"),
            ActionCall(tool_name="consumer", depends_on=["produce"]),
        ],
        runner,
    )

    assert executed == ["producer"]
    assert outcomes[0].message == "failed"
    assert outcomes[1].message == "Skipped: failed dependencies produce."


@pytest.mark.asyncio
async def test_invalid_dependencies_are_reported_without_crashing():
    engine = ActionExecutionEngine()

    async def runner(action: ActionCall):
        return result()

    outcomes = await engine.execute(
        [
            ActionCall(tool_name="missing", depends_on=["does-not-exist"]),
            ActionCall(tool_name="cycle-a", action_id="a", depends_on=["b"]),
            ActionCall(tool_name="cycle-b", action_id="b", depends_on=["a"]),
        ],
        runner,
    )

    assert "missing dependencies" in outcomes[0].message
    assert "cyclic or unresolved" in outcomes[1].message
    assert "cyclic or unresolved" in outcomes[2].message


@pytest.mark.asyncio
async def test_duplicate_explicit_ids_are_rejected():
    engine = ActionExecutionEngine()
    executed = []

    async def runner(action: ActionCall):
        executed.append(action.tool_name)
        return result()

    outcomes = await engine.execute(
        [
            ActionCall(tool_name="first", action_id="same"),
            ActionCall(tool_name="second", action_id="same"),
        ],
        runner,
    )

    assert executed == []
    assert all("duplicate action_id" in outcome.message for outcome in outcomes)


@pytest.mark.asyncio
async def test_shared_file_resource_serializes_explicit_parallel_group():
    engine = ActionExecutionEngine(max_parallel_actions=4)
    active = 0
    max_active = 0

    async def runner(action: ActionCall):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return result()

    await engine.execute(
        [
            ActionCall(
                tool_name="patch-one",
                parameters={"filepath": "src/app.py"},
                parallel_group="edits",
            ),
            ActionCall(
                tool_name="patch-two",
                parameters={"filepath": "src/app.py"},
                parallel_group="edits",
            ),
        ],
        runner,
    )

    assert max_active == 1


@pytest.mark.asyncio
async def test_resource_locks_are_shared_across_concurrent_plans():
    engine = ActionExecutionEngine(max_parallel_actions=4)
    active = 0
    max_active = 0

    async def runner(action: ActionCall):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return result()

    await asyncio.gather(
        engine.execute(
            [ActionCall(tool_name="main", resources=["workspace:shared"])], runner
        ),
        engine.execute(
            [ActionCall(tool_name="subagent", resources=["workspace:shared"])], runner
        ),
    )

    assert max_active == 1


@pytest.mark.asyncio
async def test_cancellation_reaches_running_action():
    engine = ActionExecutionEngine()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def runner(action: ActionCall):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    task = asyncio.create_task(engine.execute([ActionCall(tool_name="wait")], runner))
    await asyncio.wait_for(started.wait(), timeout=0.5)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert cancelled.is_set()
