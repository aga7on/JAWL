import asyncio
import json

import openai
import httpx
import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from src.l3_agent.llm.executor import LLMExecutor
from src.l3_agent.llm.exceptions import AllKeysExhaustedError
from src.utils.tracing import begin_trace, reset_trace


@pytest.fixture
def mock_executor_deps():
    llm = MagicMock()
    llm.rotator = MagicMock()
    tracker = MagicMock()
    return llm, tracker


@pytest.mark.asyncio
async def test_executor_success(mock_executor_deps):
    llm, tracker = mock_executor_deps
    mock_session = AsyncMock()
    llm.get_session.return_value = mock_session

    mock_response = MagicMock()
    mock_response.choices[0].message.tool_calls = None
    mock_response.choices[0].message.content = "Success Content"
    mock_session.chat.completions.create.return_value = mock_response

    executor = LLMExecutor(llm, tracker)
    res = await executor.execute("test-model", [], 0.7, MagicMock(), "[Log]")

    assert res == "Success Content"
    tracker.add_output_record.assert_called_once()
    assert executor.last_call_metrics["status"] == "completed"
    assert executor.last_call_metrics["output_chars"] == len("Success Content")
    assert executor.last_call_metrics["tool_call_count"] == 0


@pytest.mark.asyncio
async def test_executor_forwards_optional_thinking_extension(mock_executor_deps):
    llm, tracker = mock_executor_deps
    session = AsyncMock()
    llm.get_session.return_value = session
    response = MagicMock()
    response.choices[0].message.tool_calls = None
    response.choices[0].message.content = "ok"
    session.chat.completions.create.return_value = response

    executor = LLMExecutor(llm, tracker)
    await executor.execute(
        "model", [], 0.0, MagicMock(), "[Log]", enable_thinking=False
    )

    assert session.chat.completions.create.await_args.kwargs["extra_body"] == {
        "enable_thinking": False
    }


@pytest.mark.asyncio
async def test_executor_metrics_include_current_trace(mock_executor_deps):
    llm, tracker = mock_executor_deps
    session = AsyncMock()
    llm.get_session.return_value = session
    response = MagicMock()
    response.choices[0].message.tool_calls = None
    response.choices[0].message.content = "ok"
    session.chat.completions.create.return_value = response
    executor = LLMExecutor(llm, tracker)
    token, _ = begin_trace("test", trace_id="trace-metrics")
    try:
        await executor.execute("model", [], 0.0, MagicMock(), "[Log]")
    finally:
        reset_trace(token)

    assert executor.last_call_metrics["trace"]["trace_id"] == "trace-metrics"


@pytest.mark.asyncio
async def test_executor_records_downstream_cancellation(mock_executor_deps):
    llm, tracker = mock_executor_deps
    session = AsyncMock()
    llm.get_session.return_value = session
    started = asyncio.Event()

    async def wait_forever(**kwargs):
        started.set()
        await asyncio.Event().wait()

    session.chat.completions.create.side_effect = wait_forever
    executor = LLMExecutor(llm, tracker)
    task = asyncio.create_task(
        executor.execute("model", [], 0.0, MagicMock(), "[Log]")
    )
    await asyncio.wait_for(started.wait(), timeout=0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert executor.last_call_metrics["status"] == "cancelled"


@pytest.mark.asyncio
async def test_executor_merges_multiple_execute_skill_tool_calls(mock_executor_deps):
    llm, _ = mock_executor_deps
    mock_session = AsyncMock()
    llm.get_session.return_value = mock_session
    first = MagicMock()
    first.function.name = "execute_skill"
    first.function.arguments = json.dumps(
        {
            "observation": "first",
            "reasoning": "",
            "reflection": "",
            "actions": [{"tool_name": "read", "parameters": {}}],
        }
    )
    second = MagicMock()
    second.function.name = "execute_skill"
    second.function.arguments = json.dumps(
        {
            "observation": "second",
            "reasoning": "",
            "reflection": "",
            "actions": [{"tool_name": "search", "parameters": {}}],
        }
    )
    response = MagicMock()
    response.choices[0].message.tool_calls = [first, second]
    mock_session.chat.completions.create.return_value = response

    executor = LLMExecutor(llm, MagicMock())
    raw = await executor.execute("model", [], 0.0, MagicMock(), "[Log]")
    payload = json.loads(raw)

    assert [action["tool_name"] for action in payload["actions"]] == [
        "read",
        "search",
    ]
    assert payload["observation"] == "first\nsecond"
    assert executor.last_call_metrics["tool_call_count"] == 2


@pytest.mark.asyncio
async def test_executor_converts_native_tool_call_and_plain_completion(mock_executor_deps):
    llm, tracker = mock_executor_deps
    session = AsyncMock()
    llm.get_session.return_value = session
    call = MagicMock()
    call.function.name = "jawl_native_read_deadbeef00"
    call.function.arguments = json.dumps({"path": "README.md"})
    tool_response = MagicMock()
    tool_response.choices[0].message.tool_calls = [call]
    tool_response.choices[0].message.content = "I need the file."
    completion = MagicMock()
    completion.choices[0].message.tool_calls = None
    completion.choices[0].message.content = "Done."
    session.chat.completions.create.side_effect = [tool_response, completion]
    executor = LLMExecutor(llm, tracker)

    tool_raw = await executor.execute(
        "model", [], 0.0, MagicMock(), "[Log]", tool_transport="native"
    )
    final_raw = await executor.execute(
        "model", [], 0.0, MagicMock(), "[Log]", tool_transport="native"
    )

    tool_payload = json.loads(tool_raw)
    final_payload = json.loads(final_raw)
    assert tool_payload["actions"] == [
        {
            "tool_name": "jawl_native_read_deadbeef00",
            "parameters": {"path": "README.md"},
        }
    ]
    assert "I need the file" in tool_payload["reflection"]
    assert final_payload["actions"] == []
    assert final_payload["reflection"] == "Done."


@pytest.mark.asyncio
@patch("src.l3_agent.llm.executor.asyncio.sleep", new_callable=AsyncMock)
async def test_executor_rate_limit(mock_sleep, mock_executor_deps):
    """Тест: Executor ловит 429, отправляет ключ в кулдаун и успешно ретраит."""
    llm, tracker = mock_executor_deps

    mock_session1 = AsyncMock()
    mock_session1.api_key = "key1"

    mock_resp = MagicMock()
    mock_resp.headers = {"retry-after": "5"}
    rate_error = openai.RateLimitError("429", response=mock_resp, body={})

    mock_session2 = AsyncMock()
    mock_response = MagicMock()
    mock_response.choices[0].message.tool_calls = None
    mock_response.choices[0].message.content = "Finally Success"
    mock_session2.chat.completions.create.return_value = mock_response

    # Первый вызов падает с 429, второй (новый ключ из ротатора) проходит
    llm.get_session.side_effect = [mock_session1, mock_session2]
    mock_session1.chat.completions.create.side_effect = rate_error

    executor = LLMExecutor(llm, tracker)
    res = await executor.execute("model", [], 0.7, MagicMock(), "[Log]", max_retries=3)

    assert res == "Finally Success"
    llm.rotator.cooldown_key.assert_called_once_with("key1", 5)


@pytest.mark.asyncio
@patch("src.l3_agent.llm.executor.asyncio.sleep", new_callable=AsyncMock)
async def test_executor_retries_transient_upstream_500(mock_sleep, mock_executor_deps):
    llm, tracker = mock_executor_deps
    session = AsyncMock()
    llm.get_session.return_value = session
    response = httpx.Response(500, request=httpx.Request("POST", "http://qwb/v1"))
    high_demand = openai.InternalServerError(
        "Qwen upstream quota_limit: high demand",
        response=response,
        body={"code": "quota_limit"},
    )
    success = MagicMock()
    success.choices[0].message.tool_calls = None
    success.choices[0].message.content = "recovered"
    session.chat.completions.create.side_effect = [high_demand, success]
    executor = LLMExecutor(llm, tracker)

    result = await executor.execute(
        "model", [], 0.0, MagicMock(), "[LLM]", max_retries=3
    )

    assert result == "recovered"
    assert session.chat.completions.create.await_count == 2
    mock_sleep.assert_awaited_once_with(2)
    assert executor.last_call_metrics["attempts"] == 2


@pytest.mark.asyncio
async def test_executor_auth_error_bans_key(mock_executor_deps):
    """Тест: Executor ловит 401 и банит мертвый ключ навсегда."""
    llm, tracker = mock_executor_deps

    mock_session = AsyncMock()
    mock_session.api_key = "dead_key"
    auth_err = openai.AuthenticationError("401", response=MagicMock(), body={})

    mock_session.chat.completions.create.side_effect = [
        auth_err,
        MagicMock(choices=[MagicMock(message=MagicMock(tool_calls=None, content="OK"))]),
    ]
    llm.get_session.return_value = mock_session

    executor = LLMExecutor(llm, tracker)
    await executor.execute("model", [], 0.7, MagicMock(), "[Log]")

    llm.rotator.ban_key.assert_called_once_with("dead_key")


@pytest.mark.asyncio
@patch("src.l3_agent.llm.executor.asyncio.sleep", new_callable=AsyncMock)
async def test_executor_all_keys_exhausted(mock_sleep, mock_executor_deps):
    """Тест: Ротатор сообщает, что все в кулдауне. Executor спит и ретраит."""
    llm, tracker = mock_executor_deps

    # 1. Первый раз get_session падает, т.к. все ключи заблочены
    # 2. Второй раз возвращает сессию, которая проходит успешно
    llm.get_session.side_effect = [
        AllKeysExhaustedError(wait_time=10),
        AsyncMock(
            chat=MagicMock(
                completions=MagicMock(
                    create=AsyncMock(
                        return_value=MagicMock(
                            choices=[
                                MagicMock(message=MagicMock(tool_calls=None, content="OK"))
                            ]
                        )
                    )
                )
            )
        ),
    ]

    executor = LLMExecutor(llm, tracker)
    res = await executor.execute("model", [], 0.7, MagicMock(), "[Log]", max_retries=3)

    assert res == "OK"
    mock_sleep.assert_called_once_with(11)  # wait_time + 1
