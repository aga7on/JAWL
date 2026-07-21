import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.l3_agent.skills.coding_telemetry import CodingTelemetrySkills
from src.l3_agent.skills.journal import ActionJournal
from src.utils.event.bus import EventBus
from src.utils.event.registry import Events


class FakeTicks:
    def __init__(self, ticks):
        self.ticks = ticks

    async def get_ticks(self, limit=5):
        return self.ticks[-limit:]


def tick(tick_id, trace_id, actions, metrics):
    return SimpleNamespace(
        id=tick_id,
        created_at=datetime(2026, 7, 21, 12, int(tick_id[-1]), tzinfo=timezone.utc),
        actions=actions,
        results={"trace": {"trace_id": trace_id}, "llm_metrics": metrics},
    )


@pytest.mark.asyncio
async def test_coding_telemetry_rolls_up_only_coding_traces_without_payloads(tmp_path):
    journal = ActionJournal(tmp_path / "actions.jsonl")
    await journal.record(
        "plan_started",
        plan_id="plan-1",
        actions=[{"tool_name": "HostOSCodingFiles.apply_coding_file_patch"}],
        task_ids=["private-task-id"],
        trace={"trace_id": "coding-trace"},
    )
    await journal.record(
        "action_started",
        plan_id="plan-1",
        action_id="patch",
        tool_name="HostOSCodingFiles.apply_coding_file_patch",
        parameters={"task_id": "private-task-id", "secret": "do-not-leak"},
        trace={"trace_id": "coding-trace"},
    )
    await journal.record(
        "action_finished",
        plan_id="plan-1",
        action_id="patch",
        tool_name="HostOSCodingFiles.apply_coding_file_patch",
        is_success=True,
        message="private patch result",
        duration_ms=40,
        trace={"trace_id": "coding-trace"},
    )
    await journal.record(
        "action_finished",
        plan_id="plan-1",
        action_id="verify",
        tool_name="HostOSCodingVerification.run_coding_verification",
        is_success=False,
        message="private verification failure",
        duration_ms=60,
        trace={"trace_id": "coding-trace"},
    )
    await journal.record(
        "plan_finished",
        plan_id="plan-1",
        state="completed",
        outcomes=[],
        trace={"trace_id": "coding-trace"},
    )

    ticks = FakeTicks(
        [
            tick(
                "tick-1",
                "coding-trace",
                [{"tool_name": "HostOSCodingFiles.apply_coding_file_patch"}],
                {
                    "request_id": "request-1",
                    "model": "qwen-test",
                    "status": "completed",
                    "duration_ms": 1000,
                    "estimated_input_tokens": 100,
                    "estimated_output_tokens": 20,
                    "provider_prompt_tokens": 90,
                    "provider_completion_tokens": 18,
                    "provider_total_tokens": 108,
                },
            ),
            tick(
                "tick-2",
                "coding-trace",
                [],
                {
                    "request_id": "request-2",
                    "model": "qwen-test",
                    "status": "cancelled",
                    "duration_ms": 500,
                    "estimated_input_tokens": 50,
                    "estimated_output_tokens": None,
                    "provider_total_tokens": None,
                },
            ),
            tick(
                "tick-3",
                "unrelated-trace",
                [{"tool_name": "Weather.get_forecast"}],
                {
                    "request_id": "request-3",
                    "model": "unrelated-model",
                    "status": "completed",
                    "duration_ms": 9999,
                    "estimated_input_tokens": 9999,
                },
            ),
        ]
    )
    skills = CodingTelemetrySkills(journal, ticks, EventBus())

    result = await skills.inspect_coding_telemetry(tick_limit=10, plan_limit=10)
    payload = json.loads(result.message)
    serialized = json.dumps(payload, ensure_ascii=False)

    assert result.is_success is True, result.message
    assert payload["totals"]["coding_traces"] == 1
    assert payload["totals"]["coding_tasks"] == 1
    assert payload["totals"]["llm_requests"] == 2
    assert payload["totals"]["llm_duration_ms"] == 1500
    assert payload["totals"]["estimated_input_tokens"] == 150
    assert payload["totals"]["provider_total_tokens"] == 108
    assert payload["totals"]["actions"] == 2
    assert payload["totals"]["actions_succeeded"] == 1
    assert payload["totals"]["actions_failed_or_stopped"] == 1
    assert payload["totals"]["verification_actions"] == 1
    assert payload["totals"]["verification_failures"] == 1
    assert payload["models"][0]["model"] == "qwen-test"
    assert payload["models"][0]["requests"] == 2
    assert payload["coverage"]["monetary_cost_available"] is False
    assert all(payload["privacy"].values()) is False
    assert payload["privacy"] == {
        "contains_prompts": False,
        "contains_thoughts": False,
        "contains_tool_parameters": False,
        "contains_tool_results": False,
        "contains_trace_ids": False,
        "contains_task_ids": False,
    }
    for private_value in (
        "private-task-id",
        "do-not-leak",
        "private patch result",
        "private verification failure",
        "coding-trace",
        "request-1",
        "unrelated-model",
    ):
        assert private_value not in serialized


@pytest.mark.asyncio
async def test_coding_telemetry_dashboard_is_passive_bounded_and_validated(tmp_path):
    journal = ActionJournal(tmp_path / "actions.jsonl")
    event_bus = EventBus()
    received = []

    async def receive(**kwargs):
        received.append(kwargs)

    event_bus.subscribe(Events.SYSTEM_DASHBOARD_UPDATE, receive)
    skills = CodingTelemetrySkills(journal, FakeTicks([]), event_bus)

    published = await skills.publish_coding_telemetry_dashboard(
        tick_limit=10, plan_limit=10
    )
    await event_bus.stop()
    rejected = await skills.inspect_coding_telemetry(tick_limit=0)

    assert published.is_success is True, published.message
    assert received[0]["name"] == "Coding telemetry"
    assert "## Coding telemetry" in received[0]["content"]
    assert len(received[0]["content"]) <= 6000
    assert rejected.is_success is False
    assert "tick_limit must be between 1 and 200" in rejected.message
