import json
import time

import pytest

from src.l0_state.agent.state import AgentState
from src.l3_agent.goals.ledger import LedgerFailurePatch, TaskLedgerPatch
from src.l3_agent.goals.manager import GoalManager


@pytest.fixture
def manager(tmp_path):
    return GoalManager(tmp_path / "goals.json", AgentState())


@pytest.mark.asyncio
async def test_goal_persists_and_restart_forces_fresh_lane(tmp_path):
    path = tmp_path / "goals.json"
    first_state = AgentState()
    first = GoalManager(path, first_state)
    created = await first.create(
        "Finish repository verification",
        token_budget=1000,
        linked_task_id="task-1",
    )
    await first.finish_cycle(state="waiting", summary="Waiting for tests")

    second_state = AgentState()
    restored = GoalManager(path, second_state)
    active = restored.active_goal

    assert active is not None
    assert active.goal_id == created.goal_id
    assert active.lane_epoch == created.lane_epoch + 1
    assert active.pending_work is True
    assert active.last_cycle_status == "restart_recovery"
    assert second_state.active_goal_id == created.goal_id
    assert second_state.current_goal == "Finish repository verification"


@pytest.mark.asyncio
async def test_only_one_active_goal_and_terminal_summary_required(manager):
    created = await manager.create("One objective")

    with pytest.raises(ValueError, match="Active goal"):
        await manager.create("Another objective")
    with pytest.raises(ValueError, match="requires"):
        await manager.update(status="complete", summary="")

    completed = await manager.update(
        status="complete",
        summary="All acceptance checks passed.",
        goal_id=created.goal_id,
    )
    assert completed.status == "complete"
    assert manager.active_goal is None
    assert manager.agent_state.current_goal == ""


@pytest.mark.asyncio
async def test_provider_usage_is_authoritative_and_budget_blocks(manager):
    created = await manager.create("Bounded objective", token_budget=100)
    await manager.record_usage(
        {
            "provider_total_tokens": 80,
            "estimated_input_tokens": 500,
            "estimated_output_tokens": 50,
        }
    )
    view = manager.view(created.goal_id)
    assert view["accounted_tokens"] == 80
    assert view["provider_tokens"] == 80
    assert view["estimated_tokens"] == 550
    assert view["remaining_tokens"] == 20

    blocked = await manager.record_usage(
        {"provider_total_tokens": 25, "estimated_input_tokens": 1000}
    )
    assert blocked.status == "blocked"
    assert blocked.accounted_tokens == 105
    assert manager.active_goal is None
    assert "budget exhausted" in blocked.blocked_reason


@pytest.mark.asyncio
async def test_waiting_goal_suppresses_only_until_due(manager):
    await manager.create("Wait deterministically")
    await manager.begin_cycle("HEARTBEAT")
    await manager.finish_cycle(
        state="waiting", summary="Process is running", wake_after_seconds=10
    )
    active = manager.active_goal
    assert active is not None
    assert manager.should_run_heartbeat(now=active.next_wakeup_at - 1) is False
    assert manager.should_run_heartbeat(now=active.next_wakeup_at) is True
    assert manager.seconds_until_wakeup(now=active.next_wakeup_at - 3) == 3


@pytest.mark.asyncio
async def test_context_projection_is_bounded_and_contains_evidence(manager):
    await manager.create("Inspect exact repository state")
    await manager.record_action_result("x" * 10000)
    block = await manager.get_context_block()

    assert "## ACTIVE GOAL" in block
    assert "Inspect exact repository state" in block
    assert "Goal Protocol v2" in block
    assert len(block) < 16000
    assert "[truncated]" in block


def test_corrupt_goal_store_fails_closed_without_overwrite(tmp_path):
    path = tmp_path / "goals.json"
    path.write_text("{broken", encoding="utf-8")

    manager = GoalManager(path, AgentState())

    assert manager.active_goal is None
    assert path.read_text(encoding="utf-8") == "{broken"


@pytest.mark.asyncio
async def test_corrupt_goal_store_rejects_mutation(tmp_path):
    path = tmp_path / "goals.json"
    path.write_text("{broken", encoding="utf-8")
    manager = GoalManager(path, AgentState())

    with pytest.raises(ValueError, match="malformed"):
        await manager.create("Must not overwrite operator evidence")

    assert path.read_text(encoding="utf-8") == "{broken"


@pytest.mark.asyncio
async def test_linked_coding_goal_requires_passing_verification(manager):
    created = await manager.create(
        "Implement and verify the change",
        linked_task_id="coding-task",
    )

    rejected = await manager.finish_cycle(
        state="completed",
        summary="Implementation appears complete.",
    )
    assert rejected.status == "active"
    assert rejected.last_cycle_status == "verification_required"

    await manager.record_action_result(
        "* HostOSCodingVerification.run_coding_verification: all checks passed\n"
        "  [action_id=verify; status=success; duration_ms=12]",
        actions=[
            {
                "tool_name": (
                    "HostOSCodingVerification.run_coding_verification"
                ),
                "parameters": {"task_id": "coding-task"},
            }
        ],
    )
    completed = await manager.finish_cycle(
        state="completed",
        summary="Exact-state verification passed.",
    )
    assert completed.status == "complete"
    assert completed.verification_status == "passed"


@pytest.mark.asyncio
async def test_failed_coding_verification_is_durable(manager):
    created = await manager.create(
        "Fix regression",
        linked_task_id="coding-task",
        verification_policy="required",
    )
    await manager.record_action_result(
        "* HostOSCodingVerification.run_coding_verification: failed\n"
        "  [action_id=verify; status=failed; duration_ms=15]",
        actions=[
            {
                "tool_name": (
                    "HostOSCodingVerification.run_coding_verification"
                ),
                "parameters": {"task_id": "coding-task"},
            }
        ],
    )

    view = manager.view(created.goal_id)
    assert view["verification_status"] == "failed"
    with pytest.raises(ValueError, match="verification"):
        await manager.update(
            status="complete",
            summary="Cannot bypass a failed gate.",
            goal_id=created.goal_id,
        )


@pytest.mark.asyncio
async def test_atomic_store_is_valid_json_after_updates(manager):
    created = await manager.create("Persist every transition")
    for index in range(5):
        await manager.record_action_result(f"result-{index}")
    payload = json.loads(manager.path.read_text(encoding="utf-8"))

    assert payload["version"] == GoalManager.VERSION
    assert payload["goals"][0]["goal_id"] == created.goal_id
    assert not list(manager.path.parent.glob(".*.tmp"))


@pytest.mark.asyncio
async def test_task_ledger_sparse_checkpoint_survives_restart(tmp_path):
    path = tmp_path / "goals.json"
    manager = GoalManager(path, AgentState())
    created = await manager.create("Diagnose and verify the application")

    await manager.record_ledger_patch(
        TaskLedgerPatch(
            phase="diagnosis",
            acceptance_criteria=["Application starts without the key dialog"],
            pending_steps=["Inspect debugger", "Run clean verification"],
            facts_add=["Sotis is paused under x32dbg"],
            hypotheses=["Debugger is stopping on a first-chance exception"],
            failures_add=[
                LedgerFailurePatch(
                    action="Observe Sotis with UIA",
                    reason="Timed out while the process was paused",
                    retry_when="Sotis becomes responsive",
                )
            ],
            artifacts_add=["sandbox/_system/download/sotis.png"],
            tool_state_add=["x64dbg-mcp:GetState schema=abc123"],
            next_action="Read the exception call stack",
            checkpoint_summary="Debugger state established.",
        )
    )
    await manager.record_ledger_patch(
        TaskLedgerPatch(
            completed_add=["Inspect debugger"],
            pending_steps=["Run clean verification"],
            checkpoint_summary="",
        )
    )

    restored = GoalManager(path, AgentState(), recover_on_start=False)
    ledger = restored.get(created.goal_id).task_ledger

    assert ledger.current_phase == "diagnosis"
    assert ledger.completed_steps == ["Inspect debugger"]
    assert ledger.pending_steps == ["Run clean verification"]
    assert ledger.confirmed_facts == ["Sotis is paused under x32dbg"]
    assert ledger.failed_attempts[0].retry_when == "Sotis becomes responsive"
    assert ledger.next_action == "Read the exception call stack"
    assert ledger.checkpoint_summary == ""
    assert ledger.revision == 2


@pytest.mark.asyncio
async def test_task_ledger_can_remove_and_authoritatively_replace_stale_state(manager):
    await manager.create("Reconcile contradictory evidence")
    await manager.record_ledger_patch(
        TaskLedgerPatch(
            completed_add=["Old completed step"],
            facts_add=["Launcher was not created", "Crash is caused by FPU"],
            failures_add=[
                {
                    "action": "Old launcher attempt",
                    "reason": "File did not exist",
                    "retry_when": "Writer is available",
                }
            ],
            artifacts_add=["old.bin"],
            tool_state_add=["mcp:x64dbg:GetState | schema=old"],
        )
    )

    await manager.record_ledger_patch(
        TaskLedgerPatch(
            completed_remove=["Old completed step"],
            confirmed_facts=["Launcher v7 was created and executed"],
            failures_remove=["Old launcher attempt"],
            artifacts=[],
            tool_state=["mcp:x64dbg:GetState | schema=new"],
        )
    )

    ledger = manager.active_goal.task_ledger
    assert ledger.completed_steps == []
    assert ledger.confirmed_facts == ["Launcher v7 was created and executed"]
    assert ledger.failed_attempts == []
    assert ledger.artifacts == []
    assert ledger.tool_state == ["mcp:x64dbg:GetState | schema=new"]


@pytest.mark.asyncio
async def test_mcp_discovery_is_checkpointed_and_repetition_is_guarded(manager):
    await manager.create("Use the debugger without rediscovering it forever")
    actions = [
        {
            "tool_name": "MCPTools.search_tools",
            "action_id": "discover",
            "parameters": {
                "server": "x64dbg-mcp",
                "query": "create attach process debuggee call stack",
            },
        }
    ]
    result = (
        '* MCPTools.search_tools: {"matches":[{"name":"GetCallStack",'
        '"schema_sha256":"'
        + "a" * 64
        + '","allowed":true}]}\n'
        "  [action_id=discover; status=success; duration_ms=1]"
    )
    await manager.record_action_result(result, actions=actions)
    actions[0]["parameters"]["query"] = (
        "open attach create process executable debuggee call stack"
    )
    await manager.record_action_result(result, actions=actions)

    warning = manager.repeated_action_warning(
        [
            {
                "tool_name": "MCPTools.search_tools",
                "parameters": {
                    "server": "x64dbg-mcp",
                    "query": "attach create process debuggee call stack start",
                },
            }
        ]
    )

    assert "repetition guard" in warning.casefold()
    assert manager.active_goal.task_ledger.tool_state == [
        f"mcp:x64dbg-mcp:GetCallStack | allowed=true schema={'a' * 64}"
    ]


@pytest.mark.asyncio
async def test_action_failure_is_automatically_checkpointed(manager):
    await manager.create("Recover from a failed tool")

    await manager.record_action_result(
        "* MCPTools.call_tool: Error 500\n"
        "  [action_id=debug; status=failed; duration_ms=10]",
        actions=[
            {
                "tool_name": "MCPTools.call_tool",
                "action_id": "debug",
                "parameters": {"server": "x64dbg-mcp"},
            }
        ],
    )

    ledger = manager.active_goal.task_ledger
    assert ledger.last_action_batch[0].status == "failed"
    assert ledger.last_action_batch[0].evidence_id
    assert ledger.failed_attempts[0].action == "MCPTools.call_tool (debug)"
    assert "new evidence" in ledger.failed_attempts[0].retry_when


@pytest.mark.asyncio
async def test_action_failure_checkpoint_keeps_only_its_result_block(manager):
    await manager.create("Keep failure memory concise")

    await manager.record_action_result(
        "* Terminal.send: missing text\n"
        "  [action_id=notify; status=failed; duration_ms=1]\n"
        "* Shell.run: " + ("unrelated output " * 100) + "\n"
        "  [action_id=inspect; status=success; duration_ms=2]",
        actions=[
            {
                "tool_name": "Terminal.send",
                "action_id": "notify",
                "parameters": {},
            },
            {
                "tool_name": "Shell.run",
                "action_id": "inspect",
                "parameters": {},
            },
        ],
    )

    reason = manager.active_goal.task_ledger.failed_attempts[0].reason
    assert "missing text" in reason
    assert "unrelated output" not in reason


@pytest.mark.asyncio
async def test_provider_context_threshold_rebases_lane_from_local_ledger(tmp_path):
    manager = GoalManager(
        tmp_path / "goals.json",
        AgentState(),
        provider_rebase_prompt_tokens=1000,
    )
    created = await manager.create("Continue after context reset")
    original_lane = manager.lane_id

    updated = await manager.record_usage(
        {
            "provider_prompt_tokens": 1200,
            "provider_total_tokens": 1300,
        }
    )

    assert updated.lane_epoch == created.lane_epoch + 1
    assert updated.context_rebase_count == 1
    assert updated.last_provider_prompt_tokens == 1200
    assert manager.lane_id != original_lane
    block = await manager.get_context_block()
    assert "Local Task Ledger" in block
    assert "rebases=1" in block


@pytest.mark.asyncio
async def test_ledger_projection_keeps_recovery_prefix_when_history_is_large(
    tmp_path,
):
    manager = GoalManager(
        tmp_path / "goals.json",
        AgentState(),
        task_ledger_max_chars=2000,
    )
    await manager.create("Recover without replaying provider history")
    await manager.record_ledger_patch(
        TaskLedgerPatch(
            phase="verification",
            pending_steps=[f"pending-{index}-" + "p" * 390 for index in range(16)],
            facts_add=[f"fact-{index}-" + "f" * 490 for index in range(20)],
            next_action="RUN_THE_EXACT_RECOVERY_CHECK",
            checkpoint_summary="The implementation is ready for verification.",
        )
    )
    await manager.record_action_result(
        "* Test.run: green\n"
        "  [action_id=focused; status=success; duration_ms=4]",
        actions=[
            {
                "tool_name": "Test.run",
                "action_id": "focused",
                "parameters": {},
            }
        ],
    )

    projection = manager._task_ledger_context(manager.active_goal)

    assert len(projection) <= 2000
    assert "RUN_THE_EXACT_RECOVERY_CHECK" in projection
    assert "Test.run [focused]: success" in projection
    assert "Pending stages" in projection


@pytest.mark.asyncio
async def test_new_lane_is_retained_while_provider_context_is_below_threshold(
    tmp_path,
):
    manager = GoalManager(
        tmp_path / "goals.json",
        AgentState(),
        provider_rebase_prompt_tokens=1000,
    )
    await manager.create("Bound provider history")

    first = await manager.record_usage({"provider_prompt_tokens": 1200})
    first_rebased_lane = first.lane_epoch
    second = await manager.record_usage({"provider_prompt_tokens": 900})

    assert second.lane_epoch == first_rebased_lane
    assert second.context_rebase_count == 1


def test_version_one_store_migrates_without_losing_active_goal(tmp_path):
    path = tmp_path / "goals.json"
    now = time.time()
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "goals": [
                    {
                        "goal_id": "legacy",
                        "objective": "Preserve this objective",
                        "created_at": now,
                        "updated_at": now,
                        "last_activity_at": now,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    manager = GoalManager(path, AgentState(), recover_on_start=False)

    assert manager.active_goal.goal_id == "legacy"
    assert manager.active_goal.task_ledger.current_phase == "legacy_recovery"
    assert "Reconcile" in manager.active_goal.task_ledger.next_action


def test_version_one_active_store_is_persisted_as_v2_on_restart(tmp_path):
    path = tmp_path / "goals.json"
    now = time.time()
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "goals": [
                    {
                        "goal_id": "legacy-live",
                        "objective": "Resume from durable state",
                        "created_at": now,
                        "updated_at": now,
                        "last_activity_at": now,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    GoalManager(path, AgentState())
    stored = json.loads(path.read_text(encoding="utf-8"))

    assert stored["version"] == GoalManager.VERSION
    assert stored["goals"][0]["task_ledger"]["current_phase"] == (
        "legacy_recovery"
    )
