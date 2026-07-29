from types import SimpleNamespace

import pytest

from src.l0_state.agent.state import AgentState
from src.system.operator_control import OperatorControl
from src.utils.settings import InterfacesConfig, SettingsConfig


@pytest.mark.asyncio
async def test_runtime_status_does_not_require_goal_manager():
    settings = SettingsConfig()
    settings.llm.main_model = "qwen3.8-max-preview"
    state = AgentState(llm_model=settings.llm.main_model)
    container = SimpleNamespace(
        settings=settings,
        interfaces_config=InterfacesConfig(),
        agent_state=state,
        goal_manager=None,
        heartbeat=None,
    )

    result = await OperatorControl(container).handle("status.get", {})

    assert result["agent"]["model"] == "qwen3.8-max-preview"
    assert result["modes"]["idle_heartbeat_backoff"]["enabled"] is True
    assert result["modes"]["debug_broker"]["x64dbg_port_start"] == 8888
    assert result["goal"] is None


@pytest.mark.asyncio
async def test_debug_operator_controls_do_not_require_goal_mode():
    class Broker:
        def session_snapshot(self, session_id=None):
            return {"session_id": session_id, "sessions": []}

        def search_operations(self, query, provider, limit):
            return {
                "query": query,
                "provider": provider,
                "limit": limit,
                "operations": [],
            }

        async def start_session(self, provider, target, options):
            return {"session_id": "debug-1", "provider": provider, "target": target}

        async def stop_session(self, session_id):
            return {"session_id": session_id, "status": "closed"}

    container = SimpleNamespace(
        settings=SettingsConfig(),
        interfaces_config=InterfacesConfig(),
        agent_state=AgentState(),
        goal_manager=None,
        heartbeat=None,
        l2_clients={"debug_broker": Broker()},
    )
    control = OperatorControl(container)

    assert (await control.handle("debug.get", {}))["sessions"] == []
    started = await control.handle(
        "debug.start",
        {"provider": "triton", "options": {}},
    )
    assert started["session_id"] == "debug-1"
    stopped = await control.handle(
        "debug.stop", {"session_id": "debug-1"}
    )
    assert stopped["status"] == "closed"


@pytest.mark.asyncio
async def test_goal_create_wakes_heartbeat():
    goal = SimpleNamespace(goal_id="goal-test")

    class Manager:
        async def create(self, *args, **kwargs):
            return goal

        def view(self, goal_id=""):
            return {"goal_id": goal_id, "status": "active"}

    heartbeat = SimpleNamespace(answer_to_event=lambda *args, **kwargs: None)
    container = SimpleNamespace(
        settings=SettingsConfig(),
        interfaces_config=InterfacesConfig(),
        agent_state=AgentState(),
        goal_manager=Manager(),
        heartbeat=heartbeat,
    )

    result = await OperatorControl(container).handle(
        "goal.create",
        {"objective": "Finish the repository"},
    )

    assert result == {"goal_id": "goal-test", "status": "active"}


@pytest.mark.asyncio
async def test_goal_ledger_update_validates_and_forwards_sparse_patch():
    goal = SimpleNamespace(goal_id="goal-ledger")

    class Manager:
        def __init__(self):
            self.patch = None

        async def record_ledger_patch(self, patch):
            self.patch = patch
            return goal

        def view(self, goal_id=""):
            return {
                "goal_id": goal_id,
                "task_ledger": {
                    "current_phase": self.patch.phase,
                    "next_action": self.patch.next_action,
                },
            }

    manager = Manager()
    container = SimpleNamespace(
        settings=SettingsConfig(),
        interfaces_config=InterfacesConfig(),
        agent_state=AgentState(),
        goal_manager=manager,
        heartbeat=None,
    )

    result = await OperatorControl(container).handle(
        "goal.ledger.update",
        {
            "patch": {
                "phase": "verify",
                "next_action": "Run the full unit suite",
            }
        },
    )

    assert manager.patch.phase == "verify"
    assert result["task_ledger"]["next_action"] == "Run the full unit suite"
