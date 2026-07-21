import asyncio

import pytest

from src.utils.tracing import begin_trace, current_trace, reset_trace


@pytest.mark.asyncio
async def test_trace_context_is_inherited_by_child_tasks_and_reset():
    token, trace = begin_trace("test", trace_id="trace-123", task="demo")

    async def child():
        await asyncio.sleep(0)
        return current_trace()

    try:
        inherited = await asyncio.create_task(child())
        assert inherited == trace
        assert inherited["trace_id"] == "trace-123"
    finally:
        reset_trace(token)

    assert current_trace() == {}
