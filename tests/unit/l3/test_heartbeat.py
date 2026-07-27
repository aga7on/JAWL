import pytest
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

from src.utils.event.registry import EventLevel
from src.l3_agent.heartbeat import Heartbeat
from src.utils.settings import (
    EventAccelerationConfig,
    IdleHeartbeatBackoffConfig,
)
from src.l0_state.agent.state import AgentState
from src.l3_agent.goals.manager import GoalManager


@pytest.fixture
def mock_react_loop():
    return AsyncMock()


@pytest.fixture
def mock_accel_config():
    return EventAccelerationConfig(
        critical_multiplier=0.0,
        high_multiplier=0.3,
        medium_multiplier=0.6,
        low_multiplier=0.7,
        background_multiplier=0.8,
    )


def test_heartbeat_answer_to_event_high_critical(mock_react_loop, mock_accel_config):
    hb = Heartbeat(
        mock_react_loop,
        heartbeat_interval=60,
        continuous_cycle=False,
        accel_config=mock_accel_config,
        timezone=3,
    )
    hb._next_tick_time = time.time() + 60
    hb.answer_to_event(EventLevel.CRITICAL, "URGENT_EVENT", {"key": "value"})

    assert hb._wake_event.is_set()
    assert hb._wake_reason == "URGENT_EVENT"
    assert hb._wake_payload == {"key": "value"}


def test_heartbeat_answer_to_event_medium(mock_react_loop, mock_accel_config):
    hb = Heartbeat(
        mock_react_loop,
        heartbeat_interval=60,
        continuous_cycle=False,
        accel_config=mock_accel_config,
        timezone=3,
    )
    now = time.time()
    hb._next_tick_time = now + 60
    hb.answer_to_event(EventLevel.MEDIUM, "SOME_EVENT")
    expected_time = now + 36  # 60 * 0.6 = 36
    assert abs(hb._next_tick_time - expected_time) < 0.1


def test_heartbeat_answer_to_event_low(mock_react_loop, mock_accel_config):
    hb = Heartbeat(
        mock_react_loop,
        heartbeat_interval=60,
        continuous_cycle=False,
        accel_config=mock_accel_config,
        timezone=3,
    )
    now = time.time()
    hb._next_tick_time = now + 60
    hb.answer_to_event(EventLevel.BACKGROUND, "TRASH_EVENT")
    expected_time = now + 48  # 60 * 0.8 = 48
    assert abs(hb._next_tick_time - expected_time) < 0.1


@pytest.mark.asyncio
async def test_heartbeat_loop_heartbeat(mock_react_loop, mock_accel_config):
    hb = Heartbeat(
        mock_react_loop,
        heartbeat_interval=0,
        continuous_cycle=False,
        accel_config=mock_accel_config,
        timezone=3,
    )

    async def stop_hb(*args, **kwargs):
        hb.stop()

    mock_react_loop.run.side_effect = stop_hb
    await hb.start()
    mock_react_loop.run.assert_called_once_with(
        event_name="HEARTBEAT", payload={}, missed_events=[]
    )


@pytest.mark.asyncio
async def test_heartbeat_loop_event_driven(mock_react_loop, mock_accel_config):
    hb = Heartbeat(
        mock_react_loop,
        heartbeat_interval=60,
        continuous_cycle=False,
        accel_config=mock_accel_config,
        timezone=3,
    )

    async def trigger_event_and_stop(*args, **kwargs):
        hb.stop()

    mock_react_loop.run.side_effect = trigger_event_and_stop

    # Это событие попадет в память сна
    hb.answer_to_event(EventLevel.CRITICAL, "DB_DOWN", {"error": "timeout"})

    await hb.start()

    from unittest.mock import ANY

    mock_react_loop.run.assert_called_once_with(
        event_name="DB_DOWN", payload={"error": "timeout"}, missed_events=ANY
    )


def test_heartbeat_update_config(mock_react_loop, mock_accel_config):
    """Тест: обновление конфигурации на лету."""
    hb = Heartbeat(
        mock_react_loop,
        heartbeat_interval=60,
        continuous_cycle=False,
        accel_config=mock_accel_config,
        timezone=3,
    )

    # Меняем интервал
    hb.update_config("heartbeat_interval", 120)
    assert hb.heartbeat_interval == 120

    # Меняем continuous_cycle
    hb.update_config("continuous_cycle", True)
    assert hb.continuous_cycle is True


def test_empty_timer_cycles_back_off_but_real_event_resets(
    mock_react_loop, mock_accel_config
):
    mock_react_loop.last_cycle_outcome = {
        "status": "completed",
        "event_name": "HEARTBEAT",
        "had_actions": False,
    }
    hb = Heartbeat(
        mock_react_loop,
        heartbeat_interval=60,
        continuous_cycle=False,
        accel_config=mock_accel_config,
        timezone=3,
        idle_backoff_config=IdleHeartbeatBackoffConfig(
            no_op_threshold=2,
            max_multiplier=8,
        ),
    )
    hb._next_tick_time = time.time() + 60

    hb._record_cycle_outcome("HEARTBEAT", [])
    assert hb._idle_interval_multiplier == 1
    hb._record_cycle_outcome("HEARTBEAT", [])
    assert hb._idle_interval_multiplier == 2
    hb._record_cycle_outcome("HEARTBEAT", [])
    assert hb._idle_interval_multiplier == 4
    hb._record_cycle_outcome("HEARTBEAT", [])
    assert hb._idle_interval_multiplier == 8
    hb._record_cycle_outcome("HEARTBEAT", [])
    assert hb._idle_interval_multiplier == 8

    hb.answer_to_event(EventLevel.CRITICAL, "USER_MESSAGE", {"text": "wake"})
    assert hb._consecutive_idle_heartbeats == 0
    assert hb._idle_interval_multiplier == 1


def test_failed_or_active_timer_cycle_never_enters_idle_backoff(
    mock_react_loop, mock_accel_config
):
    hb = Heartbeat(
        mock_react_loop,
        heartbeat_interval=60,
        continuous_cycle=False,
        accel_config=mock_accel_config,
        timezone=3,
        idle_backoff_config=IdleHeartbeatBackoffConfig(),
    )
    for outcome in (
        {"status": "invalid_request", "had_actions": False},
        {"status": "completed", "had_actions": True},
    ):
        mock_react_loop.last_cycle_outcome = outcome
        hb._record_cycle_outcome("HEARTBEAT", [])
        assert hb._consecutive_idle_heartbeats == 0
        assert hb._idle_interval_multiplier == 1


def test_idle_backoff_stays_below_qwb_chat_expiry(
    mock_react_loop, mock_accel_config
):
    mock_react_loop.last_cycle_outcome = {
        "status": "completed",
        "had_actions": False,
    }
    hb = Heartbeat(
        mock_react_loop,
        heartbeat_interval=600,
        continuous_cycle=False,
        accel_config=mock_accel_config,
        timezone=3,
        idle_backoff_config=IdleHeartbeatBackoffConfig(
            no_op_threshold=1,
            max_multiplier=8,
            max_interval_sec=3300,
        ),
    )
    for _ in range(10):
        hb._record_cycle_outcome("HEARTBEAT", [])
    assert hb._idle_interval_multiplier == 5
    assert hb.heartbeat_interval * hb._idle_interval_multiplier == 3000


def test_heartbeat_priority_overwriting(mock_react_loop, mock_accel_config):
    """Тест: Heartbeat корректно обновляет причину пробуждения в зависимости от приоритета события."""
    hb = Heartbeat(
        mock_react_loop,
        heartbeat_interval=60,
        continuous_cycle=False,
        accel_config=mock_accel_config,
        timezone=3,
    )
    # Ставим очень маленькое оставшееся время сна (0.015 сек), чтобы любое событие
    # при умножении на множитель давало < 0.01 и вызывало экстренное пробуждение.
    hb._next_tick_time = time.time() + 0.015

    # 1. Прилетает MEDIUM событие (0.015 * 0.6 = 0.009 <= 0.01). Триггерит экстренное пробуждение.
    hb.answer_to_event(EventLevel.MEDIUM, "MEDIUM_EVENT")
    assert hb._wake_level == EventLevel.MEDIUM.value
    assert hb._wake_reason == "MEDIUM_EVENT"

    # 2. Во время сна прилетает CRITICAL (множитель 0.0, он важнее, должен перезаписать причину)
    hb.answer_to_event(EventLevel.CRITICAL, "CRITICAL_EVENT")
    assert hb._wake_level == EventLevel.CRITICAL.value
    assert hb._wake_reason == "CRITICAL_EVENT"

    # 3. Следом прилетает фоновый спам LOW (НЕ должен переписать важную причину, т.к. 20 < 50)
    hb.answer_to_event(EventLevel.LOW, "LOW_EVENT")
    assert hb._wake_level == EventLevel.CRITICAL.value
    assert hb._wake_reason == "CRITICAL_EVENT"


@pytest.mark.asyncio
async def test_active_defer_policy_does_not_cancel_provider_task(mock_react_loop):
    config = EventAccelerationConfig(
        active_cycle_policy="defer", critical_multiplier=0.0
    )
    hb = Heartbeat(mock_react_loop, 60, False, config, 3)
    blocker = asyncio.Event()
    active_task = asyncio.create_task(blocker.wait())
    hb._active_react_task = active_task
    mock_react_loop.request_steer = MagicMock()

    hb.answer_to_event(
        EventLevel.CRITICAL,
        "TELETHON_MESSAGE_INCOMING",
        {"message": "new request"},
    )

    assert not active_task.done()
    assert hb._deferred_wakeup is True
    assert hb._wake_reason == "TELETHON_MESSAGE_INCOMING"
    mock_react_loop.request_steer.assert_called_once()
    mock_react_loop.add_realtime_event.assert_not_called()

    active_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await active_task


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_first_cycle", [False, True])
async def test_deferred_event_runs_as_next_primary_cycle(fail_first_cycle):
    class FakeReactLoop:
        def __init__(self):
            self.calls = []
            self.first_started = asyncio.Event()
            self.release_first = asyncio.Event()
            self.steer_events = []
            self.heartbeat = None

        async def run(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                self.first_started.set()
                await self.release_first.wait()
                if fail_first_cycle:
                    raise RuntimeError("old cycle failed after steer request")
            else:
                self.heartbeat._is_running = False

        def request_steer(self, event_data):
            self.steer_events.append(event_data)

        def add_realtime_event(self, event_data):
            raise AssertionError("defer must not append into stale cycle")

    react = FakeReactLoop()
    config = EventAccelerationConfig(
        active_cycle_policy="defer", critical_multiplier=0.0
    )
    hb = Heartbeat(react, 3600, False, config, 3)
    react.heartbeat = hb
    hb._next_tick_time = time.time()
    heartbeat_task = asyncio.create_task(hb.start())
    await asyncio.wait_for(react.first_started.wait(), timeout=0.5)

    hb.answer_to_event(
        EventLevel.CRITICAL,
        "TELETHON_MESSAGE_INCOMING",
        {"message": "queued"},
    )
    react.release_first.set()
    await asyncio.wait_for(heartbeat_task, timeout=0.5)

    assert len(react.calls) == 2
    assert react.calls[1]["event_name"] == "TELETHON_MESSAGE_INCOMING"
    assert react.calls[1]["payload"] == {"message": "queued"}
    assert react.calls[1]["missed_events"] == []
    assert react.steer_events[0]["name"] == "TELETHON_MESSAGE_INCOMING"


@pytest.mark.asyncio
async def test_waiting_goal_suppresses_noop_heartbeat_but_not_event(
    mock_react_loop, mock_accel_config, tmp_path
):
    manager = GoalManager(tmp_path / "goals.json", AgentState())
    await manager.create("Wait without spending tokens")
    await manager.begin_cycle("HEARTBEAT")
    await manager.finish_cycle(state="waiting", summary="Awaiting input")
    hb = Heartbeat(
        mock_react_loop,
        heartbeat_interval=0.01,
        continuous_cycle=False,
        accel_config=mock_accel_config,
        timezone=3,
        goal_manager=manager,
    )
    hb._next_tick_time = time.time()
    task = asyncio.create_task(hb.start())
    for _ in range(50):
        if hb._suppressed_goal_heartbeats:
            break
        await asyncio.sleep(0.002)
    hb.stop()
    await asyncio.wait_for(task, timeout=0.5)

    assert hb._suppressed_goal_heartbeats >= 1
    mock_react_loop.run.assert_not_awaited()

    async def stop_after_event(**kwargs):
        hb.stop()

    mock_react_loop.run.side_effect = stop_after_event
    hb.answer_to_event(EventLevel.CRITICAL, "USER_MESSAGE", {"message": "go"})
    await hb.start()
    mock_react_loop.run.assert_awaited_once()
    assert mock_react_loop.run.await_args.kwargs["event_name"] == "USER_MESSAGE"
