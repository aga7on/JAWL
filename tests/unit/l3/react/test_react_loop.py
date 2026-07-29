import pytest
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch
from src.l0_state.agent.state import AgentStatus
from src.l3_agent.goals.manager import GoalManager
from src.l3_agent.react.loop import ReactLoop, _normalize_tool_output


def test_react_dump_context_to_file(mock_dependencies):
    loop = ReactLoop(**mock_dependencies)
    messages = [
        {"role": "system", "content": "You are AI"},
        {"role": "user", "content": "Hello"},
    ]
    with patch("builtins.open") as mock_open:
        mock_file = MagicMock()
        mock_open.return_value.__enter__.return_value = mock_file
        loop._dump_context_to_file(messages)
        args, kwargs = mock_open.call_args
        assert str(args[0]).replace("\\", "/") == "logs/prompts/main_prompt.md"


def test_react_first_step_thinking_policy(mock_dependencies):
    loop = ReactLoop(**mock_dependencies, thinking_policy="first_step")

    loop.agent_state.current_step = 1
    assert loop._thinking_enabled_for_step() is True
    loop.agent_state.current_step = 2
    assert loop._thinking_enabled_for_step() is False


def test_react_realtime_event_context_is_bounded_and_explicitly_coalesced(
    mock_dependencies,
):
    loop = ReactLoop(
        **mock_dependencies,
        event_queue_max=3,
        event_coalesce_window_sec=60,
        event_coalesce_names=["OS_FILE_MODIFIED"],
    )
    loop.add_realtime_event(
        {"name": "OS_FILE_MODIFIED", "level": "LOW", "payload": {"path": "a"}}
    )
    loop.add_realtime_event(
        {"name": "OS_FILE_MODIFIED", "level": "LOW", "payload": {"path": "b"}}
    )
    for message in ("one", "two"):
        loop.add_realtime_event(
            {
                "name": "TELETHON_MESSAGE_INCOMING",
                "level": "CRITICAL",
                "payload": {"message": message},
            }
        )

    assert len(loop.current_events) == 3
    assert loop.current_events[0]["coalesced_count"] == 2
    assert [item["payload"]["message"] for item in loop.current_events[1:]] == [
        "one",
        "two",
    ]

    loop.add_realtime_event(
        {
            "name": "TELETHON_MESSAGE_INCOMING",
            "level": "CRITICAL",
            "payload": {"message": "three"},
        }
    )
    snapshot = loop.get_event_buffer_snapshot()["realtime"]
    assert snapshot["size"] == 3
    assert snapshot["names"] == {"TELETHON_MESSAGE_INCOMING": 3}
    assert snapshot["dropped_total"] == 1


@pytest.mark.asyncio
@patch("src.l3_agent.react.loop.execute_skill", new_callable=AsyncMock)
async def test_react_empty_actions_exit(mock_execute_skill, mock_dependencies):
    deps = mock_dependencies
    loop = ReactLoop(**deps)

    # Мокаем экзекутор, чтобы он вернул emptyой список действий
    deps["executor"].execute.return_value = (
        '{"reflection": "Мне нечего делать.", "actions": []}'
    )

    await loop.run("HEARTBEAT", {}, missed_events=[])

    assert deps["agent_state"].state == AgentStatus.IDLE
    mock_execute_skill.assert_not_called()
    deps["sql_ticks"].save_tick.assert_awaited_once()
    saved = deps["sql_ticks"].save_tick.await_args.kwargs
    assert saved["results"]["trace"]["kind"] == "react_cycle"
    assert saved["results"]["trace"]["trace_id"]
    assert deps["agent_state"].current_step == 1
    assert deps["agent_state"].current_trace_id == ""


@pytest.mark.asyncio
@patch("src.l3_agent.react.loop.execute_skill", new_callable=AsyncMock)
async def test_react_max_steps_limit(mock_execute_skill, mock_dependencies):
    deps = mock_dependencies
    deps["agent_state"].max_react_steps = 2
    loop = ReactLoop(**deps)

    deps["executor"].execute.return_value = (
        '{"reflection": "Делаю шаг", "actions": [{"tool_name": "test", "parameters": {}}]}'
    )
    mock_execute_skill.return_value = "Result"

    await loop.run("TEST", {}, missed_events=[])

    assert deps["executor"].execute.call_count == 2
    assert deps["agent_state"].current_step == 3
    assert deps["sql_ticks"].save_tick.await_count == 3
    terminal = deps["sql_ticks"].save_tick.await_args_list[-1].kwargs
    assert terminal["results"]["status"] == "max_steps_exhausted"


@pytest.mark.asyncio
async def test_react_protocol_error_is_persisted_before_retry(mock_dependencies):
    deps = mock_dependencies
    loop = ReactLoop(**deps)
    deps["executor"].execute.side_effect = [
        "not valid tool json",
        '{"observation": "recovered", "reasoning": "", "reflection": "", "actions": []}',
    ]

    await loop.run("TEST", {}, missed_events=[])

    assert deps["executor"].execute.call_count == 2
    assert deps["sql_ticks"].save_tick.await_count == 2
    protocol = deps["sql_ticks"].save_tick.await_args_list[0].kwargs
    assert protocol["results"]["status"] == "protocol_error"
    assert "not valid tool json" in protocol["results"]["response_excerpt"]
    assert protocol["results"]["response_tail"] == "not valid tool json"
    assert deps["agent_state"].last_action_error


@pytest.mark.asyncio
async def test_react_cancellation_persists_terminal_tick(mock_dependencies):
    deps = mock_dependencies
    started = asyncio.Event()

    async def wait_for_cancel(**kwargs):
        started.set()
        await asyncio.Event().wait()

    deps["executor"].execute.side_effect = wait_for_cancel
    loop = ReactLoop(**deps)
    task = asyncio.create_task(loop.run("TEST", {}, missed_events=[]))
    await asyncio.wait_for(started.wait(), timeout=0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    saved = deps["sql_ticks"].save_tick.await_args.kwargs
    assert saved["results"]["status"] == "cycle_cancelled"
    assert saved["results"]["trace"]["trace_id"]
    assert deps["agent_state"].state == AgentStatus.IDLE
    assert deps["agent_state"].current_trace_id == ""


@pytest.mark.asyncio
@patch("src.l3_agent.react.loop.execute_skill", new_callable=AsyncMock)
async def test_react_deferred_steer_waits_for_llm_and_skips_stale_actions(
    mock_execute_skill, mock_dependencies
):
    deps = mock_dependencies
    loop = ReactLoop(**deps)

    async def complete_after_steer(**kwargs):
        loop.request_steer(
            {
                "name": "TELETHON_MESSAGE_INCOMING",
                "level": "CRITICAL",
                "time": "12:00:00",
                "payload": {"message": "new request"},
            }
        )
        return json.dumps(
            {
                "reflection": "stale answer",
                "actions": [{"tool_name": "dangerous", "parameters": {}}],
            }
        )

    deps["executor"].execute.side_effect = complete_after_steer
    await loop.run("OLD_REQUEST", {}, missed_events=[])

    deps["executor"].execute.assert_awaited_once()
    mock_execute_skill.assert_not_called()
    saved = deps["sql_ticks"].save_tick.await_args.kwargs
    assert saved["results"]["status"] == "cycle_steered"
    assert saved["results"]["queued_events"][0]["name"] == (
        "TELETHON_MESSAGE_INCOMING"
    )
    assert loop._steer_requested is False


@pytest.mark.asyncio
async def test_react_steer_during_context_build_prevents_new_provider_call(
    mock_dependencies,
):
    deps = mock_dependencies
    loop = ReactLoop(**deps)

    async def context_then_steer(*args, **kwargs):
        loop.request_steer(
            {
                "name": "TELETHON_MESSAGE_INCOMING",
                "level": "CRITICAL",
                "time": "12:00:00",
                "payload": {},
            }
        )
        return "stale context"

    deps["context_builder"].build.side_effect = context_then_steer
    await loop.run("OLD_REQUEST", {}, missed_events=[])

    deps["executor"].execute.assert_not_awaited()
    saved = deps["sql_ticks"].save_tick.await_args.kwargs
    assert saved["results"]["status"] == "cycle_steered"


@pytest.mark.asyncio
@patch("src.l3_agent.react.loop.resolve_native_tool_name")
@patch("src.l3_agent.react.loop.execute_skill", new_callable=AsyncMock)
async def test_react_executes_resolved_native_tool_and_dynamic_schema(
    mock_execute_skill, mock_resolve, mock_dependencies
):
    deps = mock_dependencies
    tools_provider = MagicMock(
        return_value=[{"type": "function", "function": {"name": "jawl_read_hash"}}]
    )
    deps["tools"] = tools_provider
    deps["tool_transport"] = "native"
    deps["executor"].execute.side_effect = [
        json.dumps(
            {
                "observation": "need file",
                "reasoning": "",
                "reflection": "",
                "actions": [
                    {
                        "tool_name": "jawl_read_hash",
                        "parameters": {"path": "README.md"},
                    }
                ],
            }
        ),
        json.dumps(
            {
                "observation": "done",
                "reasoning": "",
                "reflection": "complete",
                "actions": [],
            }
        ),
    ]
    mock_resolve.side_effect = lambda name: (
        "HostOSReader.read_file" if name == "jawl_read_hash" else name
    )
    mock_execute_skill.return_value = "file contents"
    loop = ReactLoop(**deps)

    await loop.run("TEST", {}, missed_events=[])

    tools_provider.assert_called()
    first_call = deps["executor"].execute.await_args_list[0].kwargs
    assert first_call["tool_transport"] == "native"
    assert first_call["tools"] == tools_provider.return_value
    action = mock_execute_skill.await_args.kwargs["actions"][0]
    assert action.tool_name == "HostOSReader.read_file"


@pytest.mark.asyncio
async def test_react_inject_images_success(mock_dependencies, tmp_path):
    deps = mock_dependencies
    loop = ReactLoop(**deps)

    fake_img = tmp_path / "test.jpg"
    fake_img.write_bytes(b"hello")

    loop.agent_state.last_actions_result = (
        f"Result:[SYSTEM_MARKER_IMAGE_ATTACHED: {fake_img.resolve()}]"
    )

    messages = [
        {"role": "system", "content": "Система"},
        {"role": "user", "content": "Анализируй"},
    ]

    result = await loop._inject_images_to_payload(messages.copy())
    last_msg_content = result[-1]["content"]

    assert isinstance(last_msg_content, list)
    assert last_msg_content[0]["type"] == "text"
    assert last_msg_content[1]["type"] == "image_url"


@pytest.mark.asyncio
async def test_react_injects_pending_image_on_first_step(mock_dependencies, tmp_path):
    loop = ReactLoop(**mock_dependencies)
    loop.agent_state.last_actions_result = ""
    fake_img = tmp_path / "telegram.png"
    fake_img.write_bytes(b"image")
    messages = [
        {"role": "system", "content": "System"},
        {"role": "user", "content": "Inspect"},
    ]

    result = await loop._inject_images_to_payload(
        messages, pending_media=[str(fake_img)]
    )

    assert result[1]["content"][1]["type"] == "image_url"
    assert result[1]["content"][1]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )


@pytest.mark.asyncio
async def test_react_injects_pending_video_as_video_url(mock_dependencies, tmp_path):
    loop = ReactLoop(**mock_dependencies)
    loop.agent_state.last_actions_result = ""
    fake_video = tmp_path / "telegram.mp4"
    fake_video.write_bytes(b"video")
    messages = [
        {"role": "system", "content": "System"},
        {"role": "user", "content": "Inspect video"},
    ]

    result = await loop._inject_images_to_payload(
        messages, pending_media=[str(fake_video)]
    )

    assert result[1]["content"][1]["type"] == "video_url"
    assert result[1]["content"][1]["video_url"]["url"].startswith(
        "data:video/mp4;base64,"
    )


def test_collect_pending_media_includes_buffered_and_coalesced_events():
    primary = {"media_paths": ["primary.jpg"]}
    events = [
        {
            "payload": {"media_paths": ["buffered.png", "primary.jpg"]},
            "payload_samples": [{"media_paths": ["album.webp"]}],
        }
    ]

    assert ReactLoop._collect_pending_media(primary, events) == [
        "primary.jpg",
        "buffered.png",
        "album.webp",
    ]


def test_tool_output_normalization_preserves_valid_unicode():
    source = "Привет 😀 漢字 العربية ∑→\nnext\x00"
    normalized = _normalize_tool_output(source)

    assert normalized == "Привет 😀 漢字 العربية ∑→\nnext"
    repaired = _normalize_tool_output("bad\ud800text")
    assert repaired == "bad?text"
    repaired.encode("utf-8", errors="strict")


@pytest.mark.asyncio
async def test_goal_v2_done_completes_durable_goal(mock_dependencies, tmp_path):
    deps = dict(mock_dependencies)
    manager = GoalManager(tmp_path / "goals.json", deps["agent_state"])
    created = await manager.create("Finish the exact task", token_budget=100)
    deps["goal_manager"] = manager
    deps["executor"].last_call_metrics = {
        "provider_total_tokens": 12,
        "estimated_input_tokens": 500,
    }
    deps["executor"].execute.return_value = (
        '{"v":2,"state":"done","summary":"Diff reviewed and tests passed."}'
    )
    loop = ReactLoop(**deps)

    await loop.run("HEARTBEAT", {}, [])

    finished = manager.get(created.goal_id)
    assert finished.status == "complete"
    assert finished.accounted_tokens == 12
    assert "tests passed" in finished.completion_summary
    assert deps["executor"].execute.await_args.kwargs["session_id"].startswith(
        f"goal-{created.goal_id}-e"
    )


@pytest.mark.asyncio
@patch("src.l3_agent.react.loop.execute_skill", new_callable=AsyncMock)
async def test_goal_action_result_is_durable_before_done(
    mock_execute_skill, mock_dependencies, tmp_path
):
    deps = dict(mock_dependencies)
    manager = GoalManager(tmp_path / "goals.json", deps["agent_state"])
    created = await manager.create("Read then conclude")
    deps["goal_manager"] = manager
    deps["executor"].last_call_metrics = {"provider_total_tokens": 5}
    deps["executor"].execute.side_effect = [
        (
            '{"v":2,"state":"act","calls":['
            '{"tool":"HostOSReader.read_file","args":{"filepath":"README.md"}}]}'
        ),
        '{"v":2,"state":"done","summary":"Required file was inspected."}',
    ]
    mock_execute_skill.return_value = "bounded read result"
    loop = ReactLoop(**deps)

    await loop.run("HEARTBEAT", {}, [])

    finished = manager.get(created.goal_id)
    assert finished.status == "complete"
    assert finished.last_result == "bounded read result"
    assert any(item["kind"] == "tool_result" for item in finished.evidence)
    assert finished.accounted_tokens == 10


@pytest.mark.asyncio
@patch("src.l3_agent.react.loop.execute_skill", new_callable=AsyncMock)
async def test_goal_repetition_guard_forces_replan_without_dispatch(
    mock_execute_skill, mock_dependencies, tmp_path
):
    deps = dict(mock_dependencies)
    manager = GoalManager(tmp_path / "goals.json", deps["agent_state"])
    await manager.create("Stop rediscovering the same debugger operation")
    prior_action = {
        "tool_name": "MCPTools.search_tools",
        "action_id": "prior",
        "parameters": {
            "server": "x64dbg-mcp",
            "query": "launch attach process debuggee call stack",
        },
    }
    prior_result = (
        "* MCPTools.search_tools: catalog\n"
        "  [action_id=prior; status=success; duration_ms=1]"
    )
    await manager.record_action_result(prior_result, actions=[prior_action])
    prior_action["parameters"]["query"] = (
        "open attach process debuggee call stack launch"
    )
    await manager.record_action_result(prior_result, actions=[prior_action])

    deps["goal_manager"] = manager
    deps["executor"].last_call_metrics = {}
    deps["executor"].execute.side_effect = [
        json.dumps(
            {
                "v": 2,
                "state": "act",
                "calls": [
                    {
                        "tool": "MCPTools.search_tools",
                        "args": {
                            "server": "x64dbg-mcp",
                            "query": "attach launch process debuggee call stack start",
                        },
                        "action_id": "again",
                    }
                ],
            }
        ),
        '{"v":2,"state":"done","summary":"Replanned from known tool state."}',
    ]
    loop = ReactLoop(**deps)

    await loop.run("HEARTBEAT", {}, [])

    mock_execute_skill.assert_not_awaited()
    assert deps["executor"].execute.await_count == 2
    guarded_tick = deps["sql_ticks"].save_tick.await_args_list[0].kwargs
    assert guarded_tick["results"]["status"] == "repetition_guard"
    assert "repetition guard" in deps["agent_state"].last_action_error.casefold()


@pytest.mark.asyncio
@patch("src.l3_agent.react.loop.execute_skill", new_callable=AsyncMock)
async def test_goal_ledger_is_saved_before_action_and_survives_completion(
    mock_execute_skill, mock_dependencies, tmp_path
):
    deps = dict(mock_dependencies)
    manager = GoalManager(tmp_path / "goals.json", deps["agent_state"])
    created = await manager.create("Resume from an exact local checkpoint")
    deps["goal_manager"] = manager
    deps["executor"].last_call_metrics = {"provider_total_tokens": 5}
    deps["executor"].execute.side_effect = [
        json.dumps(
            {
                "v": 2,
                "state": "act",
                "calls": [
                    {
                        "tool": "HostOSReader.read_file",
                        "args": {"filepath": "README.md"},
                        "action_id": "read",
                    }
                ],
                "ledger": {
                    "phase": "inspect",
                    "acceptance_criteria": ["README evidence captured"],
                    "pending_steps": ["Read README", "Verify evidence"],
                    "next_action": "Read README",
                    "checkpoint_summary": "Ready to inspect the file.",
                },
            }
        ),
        json.dumps(
            {
                "v": 2,
                "state": "done",
                "summary": "README evidence captured.",
                "ledger": {
                    "phase": "complete",
                    "completed_add": ["Read README", "Verify evidence"],
                    "pending_steps": [],
                    "facts_add": ["README was read successfully"],
                    "next_action": "",
                    "checkpoint_summary": "Acceptance criterion satisfied.",
                },
            }
        ),
    ]
    mock_execute_skill.return_value = (
        "* HostOSReader.read_file: README contents\n"
        "  [action_id=read; status=success; duration_ms=5]"
    )
    loop = ReactLoop(**deps)

    await loop.run("HEARTBEAT", {}, [])

    finished = manager.get(created.goal_id)
    assert finished.task_ledger.current_phase == "complete"
    assert finished.task_ledger.pending_steps == []
    assert finished.task_ledger.next_action == ""
    assert "README was read successfully" in (
        finished.task_ledger.confirmed_facts
    )
    assert finished.task_ledger.last_action_batch[0].status == "success"


@pytest.mark.asyncio
async def test_goal_wait_schedules_without_marking_complete(
    mock_dependencies, tmp_path
):
    deps = dict(mock_dependencies)
    manager = GoalManager(tmp_path / "goals.json", deps["agent_state"])
    created = await manager.create("Wait for a process")
    deps["goal_manager"] = manager
    deps["executor"].last_call_metrics = {}
    deps["executor"].execute.return_value = (
        '{"v":2,"state":"wait","summary":"Process still running",'
        '"wake_after_seconds":30}'
    )
    loop = ReactLoop(**deps)

    await loop.run("HEARTBEAT", {}, [])

    waiting = manager.get(created.goal_id)
    assert waiting.status == "active"
    assert waiting.pending_work is False
    assert waiting.next_wakeup_at is not None
    assert manager.should_run_heartbeat(now=waiting.next_wakeup_at - 1) is False


@pytest.mark.asyncio
async def test_goal_retryable_invalid_request_schedules_continuation(
    mock_dependencies, tmp_path
):
    deps = dict(mock_dependencies)
    manager = GoalManager(tmp_path / "goals.json", deps["agent_state"])
    created = await manager.create("Survive a transient provider rejection")
    deps["goal_manager"] = manager
    deps["executor"].execute.return_value = None
    deps["executor"].last_call_metrics = {
        "status": "invalid_request",
        "error_kind": "session_state",
        "retryable": True,
    }
    loop = ReactLoop(**deps)

    await loop.run("HEARTBEAT", {}, [])

    pending = manager.get(created.goal_id)
    assert pending.status == "active"
    assert pending.last_cycle_status == "failed"
    assert pending.pending_work is True
    assert pending.next_wakeup_at is not None
    assert "Goal remains active" in pending.last_summary
    assert loop.last_cycle_outcome["status"] == "invalid_request"


@pytest.mark.asyncio
async def test_goal_deterministic_invalid_request_blocks(
    mock_dependencies, tmp_path
):
    deps = dict(mock_dependencies)
    manager = GoalManager(tmp_path / "goals.json", deps["agent_state"])
    created = await manager.create("Stop on a broken provider configuration")
    deps["goal_manager"] = manager
    deps["executor"].execute.return_value = None
    deps["executor"].last_call_metrics = {
        "status": "invalid_request",
        "error_kind": "configuration",
        "retryable": False,
    }
    loop = ReactLoop(**deps)

    await loop.run("HEARTBEAT", {}, [])

    blocked = manager.get(created.goal_id)
    assert blocked.status == "blocked"
    assert blocked.last_cycle_status == "blocked"
    assert blocked.pending_work is False
    assert blocked.next_wakeup_at is None
    assert "operator correction" in blocked.blocked_reason
