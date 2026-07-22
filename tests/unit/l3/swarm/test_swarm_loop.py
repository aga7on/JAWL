import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from src.l3_agent.swarm.loop import SubagentLoop
from src.l3_agent.swarm.roles import Subagents
from src.l3_agent.skills.schema import ActionCall


@pytest.fixture
def mock_loop_deps(mock_executor):
    return {
        "subagent_id": "123",
        "role": Subagents.CODER,
        "task_description": "Task",
        "executor": mock_executor,
        "model_name": "test-model",
        "prompt_builder": MagicMock(build=MagicMock(return_value="System")),
        "context_builder": MagicMock(build=MagicMock(return_value="Context")),
        "allowed_skills": ["Allowed.tool"],
        "max_steps": 3,
    }


@pytest.mark.asyncio
async def test_subagent_graceful_exit(mock_loop_deps):
    loop = SubagentLoop(**mock_loop_deps)
    loop.report_submitted = True

    with patch.object(loop, "_dump_context_to_file"):
        status = await loop.run()

    assert loop.is_done is True
    assert len(loop.history) == 0
    assert status == "completed"


@pytest.mark.asyncio
async def test_subagent_forces_report_submission(mock_loop_deps):
    loop = SubagentLoop(**mock_loop_deps)
    loop.max_steps = 2
    loop.report_submitted = False

    # Модель настойчиво пытается выйти (empty actions list)
    loop.executor.execute.return_value = '{"reflection": "Я хочу уйти", "actions": []}'

    with patch.object(loop, "_dump_context_to_file"):
        status = await loop.run()

    assert loop.is_done is False
    assert len(loop.history) == 2
    assert "[System Error]" in loop.history[0]["results"]
    assert "This is forbidden." in loop.history[0]["results"]
    assert status == "failed"


@pytest.mark.asyncio
@patch("src.l3_agent.swarm.loop.call_skill", new_callable=AsyncMock)
async def test_subagent_llm_crash_forces_report(mock_call_skill, mock_loop_deps):
    loop = SubagentLoop(**mock_loop_deps)
    # Экзекутор возвращает None (фатальный краш)
    loop.executor.execute.return_value = None

    with patch.object(loop, "_dump_context_to_file"):
        status = await loop.run()

    mock_call_skill.assert_called_once()
    assert mock_call_skill.call_args[0][0] == "SubagentReport.submit_final_report"
    assert status == "failed"


@pytest.mark.asyncio
@patch("src.l3_agent.swarm.loop.call_skill", new_callable=AsyncMock)
async def test_subagent_report_cannot_spoof_worker_identity(
    mock_call_skill, mock_loop_deps
):
    loop = SubagentLoop(**mock_loop_deps)
    action = ActionCall(
        tool_name="SubagentReport.submit_final_report",
        parameters={
            "subagent_id": "someone-else",
            "role": "coder",
            "report": "fake",
        },
    )

    await loop._execute_and_log_actions("trying", [action])

    mock_call_skill.assert_not_awaited()
    assert loop.report_submitted is False
    assert "identity mismatch" in loop.history[-1]["results"]


def test_subagent_injects_control_messages_once(mock_loop_deps):
    pending = ["Stop guessing and inspect the exact export table."]

    def drain():
        messages = list(pending)
        pending.clear()
        return messages

    loop = SubagentLoop(**mock_loop_deps, control_message_provider=drain)

    first = loop._prepare_messages("System")
    second = loop._prepare_messages("System")

    assert "ORCHESTRATOR CONTROL MESSAGES" in first[1]["content"]
    assert "inspect the exact export table" in first[1]["content"]
    assert "ORCHESTRATOR CONTROL MESSAGES" not in second[1]["content"]
