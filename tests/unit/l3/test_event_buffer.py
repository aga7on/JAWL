import json
from types import SimpleNamespace

import pytest

from src.l3_agent.event_buffer import BoundedEventBuffer
from src.l3_agent.heartbeat import Heartbeat
from src.l3_agent.skills.event_queue import EventQueueSkills
from src.utils.event.registry import EventLevel
from src.utils.settings import EventAccelerationConfig


def event(name: str, level: str, payload_id: int) -> dict:
    return {
        "time": f"00:00:{payload_id:02d}",
        "level": level,
        "name": name,
        "payload": {"id": payload_id},
    }


def test_heartbeat_accepts_legacy_acceleration_config_without_queue_fields():
    config = SimpleNamespace(
        critical_multiplier=0.0,
        high_multiplier=0.2,
        medium_multiplier=0.6,
        low_multiplier=0.7,
        background_multiplier=0.8,
    )
    heartbeat = Heartbeat(object(), 60, False, config, 0)
    heartbeat._next_tick_time = 100
    heartbeat.answer_to_event(EventLevel.CRITICAL, "LEGACY_EVENT", {"ok": True})
    assert heartbeat.get_queue_snapshot()["wake_queue"]["capacity"] == 100
    assert heartbeat.get_queue_snapshot()["wake_queue"]["size"] == 1


def test_event_buffer_coalesces_only_explicit_noise_and_bounds_samples():
    buffer = BoundedEventBuffer(
        capacity=10,
        coalesce_window_sec=2,
        coalesce_names={"OS_FILE_MODIFIED"},
        payload_sample_limit=2,
    )
    buffer.append(event("OS_FILE_MODIFIED", "LOW", 1), received_at=10)
    buffer.append(event("OS_FILE_MODIFIED", "LOW", 2), received_at=11)
    buffer.append(event("OS_FILE_MODIFIED", "LOW", 3), received_at=12)
    buffer.append(event("TELETHON_MESSAGE_INCOMING", "CRITICAL", 1), received_at=12)
    buffer.append(event("TELETHON_MESSAGE_INCOMING", "CRITICAL", 2), received_at=12)

    items = buffer.view()
    assert [item["name"] for item in items] == [
        "OS_FILE_MODIFIED",
        "TELETHON_MESSAGE_INCOMING",
        "TELETHON_MESSAGE_INCOMING",
    ]
    noisy = items[0]
    assert noisy["coalesced_count"] == 3
    assert noisy["payload"] == {"id": 3}
    assert noisy["payload_samples"] == [{"id": 1}, {"id": 2}]
    assert noisy["payload_samples_omitted"] == 1


def test_event_buffer_priority_eviction_is_visible_and_payload_free_in_snapshot():
    buffer = BoundedEventBuffer(capacity=3)
    buffer.append(event("BACKGROUND_A", "BACKGROUND", 1), received_at=1)
    buffer.append(event("LOW_A", "LOW", 2), received_at=2)
    buffer.append(event("MEDIUM_A", "MEDIUM", 3), received_at=3)
    outcome = buffer.append(event("URGENT", "CRITICAL", 99), received_at=4)
    dropped = buffer.append(event("BACKGROUND_B", "BACKGROUND", 5), received_at=5)

    assert outcome.evicted_name == "BACKGROUND_A"
    assert dropped.dropped is True
    assert [item["name"] for item in buffer.items] == ["LOW_A", "MEDIUM_A", "URGENT"]
    snapshot = buffer.snapshot()
    assert snapshot["dropped_total"] == 2
    assert "payload" not in json.dumps(snapshot)
    view = buffer.view()
    overflow = view[-1]
    assert overflow["name"] == "EVENT_QUEUE_OVERFLOW"
    assert overflow["payload"]["dropped_total"] == 2

    downstream = BoundedEventBuffer(capacity=3)
    downstream.extend(buffer.drain())
    assert downstream.snapshot()["dropped_total"] == 2
    assert downstream.view()[-1]["name"] == "EVENT_QUEUE_OVERFLOW"


def test_event_buffer_bounds_overflow_cardinality_from_unique_event_names():
    buffer = BoundedEventBuffer(capacity=1)
    buffer.append(event("KEEP", "CRITICAL", 1))
    for index in range(250):
        buffer.append(event(f"NOISE_{index}", "BACKGROUND", index))

    snapshot = buffer.snapshot()
    assert snapshot["dropped_total"] == 250
    assert len(snapshot["dropped_by_level_and_name"]) <= 101
    assert snapshot["dropped_by_level_and_name"]["OTHER:OTHER"] > 0


@pytest.mark.asyncio
async def test_heartbeat_buffers_personal_messages_separately_and_reports_queue_status():
    class React:
        def __init__(self):
            self.calls = []
            self.heartbeat = None

        async def run(self, **kwargs):
            self.calls.append(kwargs)
            self.heartbeat._is_running = False

        def get_event_buffer_snapshot(self):
            return {"realtime": {"size": 0}, "steer": {"size": 0}}

    react = React()
    config = EventAccelerationConfig(
        queue_max_events=10,
        coalesce_window_sec=60,
        coalesce_event_names=["OS_FILE_MODIFIED"],
        critical_multiplier=0,
    )
    heartbeat = Heartbeat(react, 60, False, config, 0)
    react.heartbeat = heartbeat
    heartbeat.answer_to_event(EventLevel.LOW, "OS_FILE_MODIFIED", {"path": "a"})
    heartbeat.answer_to_event(EventLevel.LOW, "OS_FILE_MODIFIED", {"path": "b"})
    heartbeat.answer_to_event(
        EventLevel.CRITICAL, "TELETHON_MESSAGE_INCOMING", {"message": "one"}
    )
    heartbeat.answer_to_event(
        EventLevel.CRITICAL, "TELETHON_MESSAGE_INCOMING", {"message": "two"}
    )

    snapshot = heartbeat.get_queue_snapshot()
    assert snapshot["wake_queue"]["size"] == 3
    assert snapshot["wake_queue"]["coalesced_total"] == 1
    assert snapshot["wake_queue"]["names"]["TELETHON_MESSAGE_INCOMING"] == 2
    assert "message" not in json.dumps(snapshot)
    status = await EventQueueSkills(heartbeat).get_event_queue_status()
    assert status.is_success is True
    assert json.loads(status.message)["wake_queue"]["size"] == 3

    await heartbeat.start()
    assert react.calls[0]["event_name"] == "TELETHON_MESSAGE_INCOMING"
    assert react.calls[0]["payload"] == {"message": "two"}
    missed = react.calls[0]["missed_events"]
    assert sum(item["name"] == "TELETHON_MESSAGE_INCOMING" for item in missed) == 1
    assert next(item for item in missed if item["name"] == "OS_FILE_MODIFIED")[
        "coalesced_count"
    ] == 2
