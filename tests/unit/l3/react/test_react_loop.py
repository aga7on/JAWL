import pytest
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch
from src.l0_state.agent.state import AgentStatus
from src.l3_agent.react.loop import ReactLoop


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
