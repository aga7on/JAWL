import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from rich.console import Console

from src.cli.screens import dashboard


def _paths(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        instance_id="default",
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        pid_file=tmp_path / "data" / "agent.pid",
        sandbox_dir=tmp_path / "sandbox",
        private_sandbox_system_dir=tmp_path / "sandbox" / "_system",
        legacy_default=True,
    )


def test_latest_goal_prefers_active_goal(tmp_path: Path) -> None:
    goal_dir = tmp_path / "agent"
    goal_dir.mkdir()
    (goal_dir / "goals.json").write_text(
        json.dumps(
            {
                "goals": [
                    {
                        "goal_id": "newer-complete",
                        "status": "complete",
                        "updated_at": 20,
                    },
                    {
                        "goal_id": "active",
                        "status": "active",
                        "updated_at": 10,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    assert dashboard._latest_goal(tmp_path)["goal_id"] == "active"


def test_active_process_sessions_ignores_stale_or_dead_records(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    paths.private_sandbox_system_dir.mkdir(parents=True)
    registry = paths.private_sandbox_system_dir / "process_sessions.json"
    registry.write_text(
        json.dumps(
            {
                "sessions": {
                    "live": {
                        "status": "running",
                        "pid": 42,
                        "filepath": "task.py",
                        "started_at": 100,
                    },
                    "done": {
                        "status": "completed",
                        "pid": 43,
                        "filepath": "done.py",
                    },
                    "dead": {
                        "status": "running",
                        "pid": 44,
                        "filepath": "dead.py",
                        "started_at": 100,
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    live_process = SimpleNamespace(create_time=lambda: 100)

    def process(pid: int):
        if pid == 42:
            return live_process
        raise dashboard.psutil.NoSuchProcess(pid)

    with patch.object(dashboard.psutil, "Process", side_effect=process):
        result = dashboard._active_process_sessions(paths)

    assert result == [
        {
            "id": "live",
            "pid": 42,
            "filepath": "task.py",
            "started_at": 100,
        }
    ]


def test_dashboard_snapshot_surfaces_crash_and_qwb_health(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    named_data = tmp_path / "mara-data"
    manager = SimpleNamespace(
        list_status=lambda: [
            {
                "profile": {
                    "instance_id": "Mara",
                    "display_name": "Mara",
                    "desired_state": "running",
                    "model_override": "",
                },
                "runtime": {
                    "state": "crashed",
                    "alive": False,
                    "pid": None,
                    "last_error": "rate limited",
                },
                "paths": {"data": str(named_data)},
            }
        ],
        supervisor_status=lambda: {"running": True, "pid": 7},
    )

    with (
        patch.object(dashboard, "_model_and_name", return_value=("Gecko", "qwen")),
        patch.object(dashboard, "_primary_pid", return_value=99),
        patch.object(dashboard, "_latest_goal", return_value=None),
        patch.object(dashboard, "_active_process_sessions", return_value=[]),
    ):
        snapshot = dashboard.collect_dashboard_snapshot(
            manager=manager,
            paths=paths,
            qwb_fetcher=lambda **_: {
                "status": "ok",
                "accounts": [
                    {"state": "healthy"},
                    {"state": "cooldown"},
                ],
            },
        )

    assert [(item["display_name"], item["state"]) for item in snapshot["agents"]] == [
        ("Gecko", "running"),
        ("Mara", "crashed"),
    ]
    assert snapshot["qwb_accounts"] == {"healthy": 1, "total": 2}
    assert any("Mara: crashed" in item["text"] for item in snapshot["alerts"])
    assert any("1/2 healthy" in item["text"] for item in snapshot["alerts"])


def test_scoped_named_dashboard_does_not_duplicate_current_agent(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    paths.instance_id = "Mara"
    paths.legacy_default = False
    manager = SimpleNamespace(
        list_status=lambda: [
            {
                "profile": {
                    "instance_id": "Mara",
                    "display_name": "Mara",
                    "desired_state": "running",
                    "model_override": "",
                },
                "runtime": {
                    "state": "running",
                    "alive": True,
                    "pid": 42,
                    "last_error": "",
                },
                "paths": {"data": str(tmp_path / "data")},
            }
        ],
        supervisor_status=lambda: {"running": True, "pid": 7},
    )

    with (
        patch.object(dashboard, "_model_and_name", return_value=("Mara", "qwen")),
        patch.object(dashboard, "_primary_pid", return_value=42),
        patch.object(dashboard, "_latest_goal", return_value=None),
        patch.object(dashboard, "_active_process_sessions", return_value=[]),
    ):
        snapshot = dashboard.collect_dashboard_snapshot(
            manager=manager,
            paths=paths,
            qwb_fetcher=lambda **_: {"status": "ok", "accounts": []},
        )

    assert len(snapshot["agents"]) == 1
    assert snapshot["agents"][0]["instance_id"] == "Mara"
    assert snapshot["agents"][0]["current"] is True
    assert snapshot["agents"][0]["primary"] is False


def test_dashboard_render_is_readable_at_eighty_columns() -> None:
    output = io.StringIO()
    test_console = Console(file=output, width=80, force_terminal=False)
    snapshot = {
        "agents": [
            {
                "instance_id": "default",
                "display_name": "Gecko",
                "primary": True,
                "state": "running",
                "pid": 123,
                "model": "qwen3.8-max-preview",
                "goal": None,
                "last_error": "",
            }
        ],
        "qwb": {"status": "ok"},
        "qwb_accounts": {"healthy": 2, "total": 2},
        "supervisor": {"running": True},
        "active_sessions": [],
        "alerts": [],
    }

    with patch.object(dashboard, "console", test_console):
        dashboard.render_dashboard(snapshot)

    rendered = output.getvalue()
    assert "Gecko (Primary)" in rendered
    assert "RUNNING" in rendered
    assert "QWB" in rendered
    assert "2/2 accounts" in rendered
    assert "qwen3.8-max-preview" not in rendered
