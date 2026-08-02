"""Fast, state-first overview for the JAWL operator console."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

import psutil
from rich.panel import Panel
from rich.table import Table

from src.cli.screens.agent_control import _is_agent_running
from src.cli.screens.runtime import _provider_health
from src.cli.widgets.ui import console
from src.instances.manager import InstanceManager
from src.instances.paths import InstancePaths, get_instance_paths
from src.utils.settings import load_config


DashboardSnapshot = dict[str, Any]


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _latest_goal(data_dir: Path) -> dict[str, Any] | None:
    payload = _read_json(data_dir / "agent" / "goals.json")
    goals = payload.get("goals")
    if not isinstance(goals, list):
        return None
    valid = [item for item in goals if isinstance(item, dict)]
    if not valid:
        return None
    active = [item for item in valid if item.get("status") == "active"]
    candidates = active or valid
    return max(
        candidates,
        key=lambda item: float(item.get("updated_at") or 0),
    )


def _active_process_sessions(paths: InstancePaths) -> list[dict[str, Any]]:
    candidates = [
        paths.private_sandbox_system_dir / "process_sessions.json",
        paths.sandbox_dir
        / "_system"
        / "instances"
        / paths.instance_id
        / "process_sessions.json",
    ]
    sessions: dict[str, dict[str, Any]] = {}
    for candidate in dict.fromkeys(candidates):
        payload = _read_json(candidate)
        records = payload.get("sessions")
        if not isinstance(records, dict):
            continue
        for session_id, record in records.items():
            if isinstance(record, dict):
                sessions[str(session_id)] = record

    active: list[dict[str, Any]] = []
    for session_id, record in sessions.items():
        if record.get("status") != "running":
            continue
        try:
            pid = int(record.get("pid"))
            process = psutil.Process(pid)
            started_at = float(record.get("started_at") or 0)
            if started_at and abs(process.create_time() - started_at) > 300:
                continue
        except (TypeError, ValueError, psutil.Error, OSError):
            continue
        active.append(
            {
                "id": session_id,
                "pid": pid,
                "filepath": str(record.get("filepath") or "task"),
                "started_at": record.get("started_at"),
            }
        )
    return sorted(
        active,
        key=lambda item: float(item.get("started_at") or 0),
        reverse=True,
    )


def _model_and_name() -> tuple[str, str]:
    try:
        settings, _ = load_config()
        return settings.identity.agent_name, settings.llm.main_model
    except Exception:
        return "Primary", "unknown"


def _primary_pid(paths: InstancePaths) -> int | None:
    if not _is_agent_running() or not paths.pid_file.is_file():
        return None
    try:
        pid = int(paths.pid_file.read_text(encoding="utf-8").strip())
        return pid if psutil.pid_exists(pid) else None
    except (OSError, ValueError):
        return None


def _account_rollup(qwb: dict[str, Any]) -> tuple[int, int]:
    accounts = qwb.get("accounts")
    if not isinstance(accounts, list):
        return 0, 0
    valid = [item for item in accounts if isinstance(item, dict)]
    healthy = sum(item.get("state") == "healthy" for item in valid)
    return healthy, len(valid)


def collect_dashboard_snapshot(
    *,
    manager: InstanceManager | None = None,
    qwb_fetcher: Callable[..., dict[str, Any]] = _provider_health,
    paths: InstancePaths | None = None,
) -> DashboardSnapshot:
    """Collect a bounded snapshot without requiring the agent IPC channel."""

    current_paths = paths or get_instance_paths()
    instance_manager = manager or InstanceManager(current_paths.project_root)
    name, model = _model_and_name()
    pid = _primary_pid(current_paths)
    primary_goal = _latest_goal(current_paths.data_dir)
    primary_sessions = [
        {
            **session,
            "instance_id": current_paths.instance_id,
            "display_name": name,
        }
        for session in _active_process_sessions(current_paths)
    ]

    try:
        named = instance_manager.list_status()
    except Exception:
        named = []
    try:
        supervisor = instance_manager.supervisor_status()
    except Exception:
        supervisor = {"running": False, "pid": None}
    try:
        qwb = qwb_fetcher(timeout=0.6)
    except TypeError:
        qwb = qwb_fetcher()
    except Exception:
        qwb = {"status": "offline"}

    local_instance_id = (
        "default" if current_paths.legacy_default else current_paths.instance_id
    )
    agents = [
        {
            "instance_id": local_instance_id,
            "display_name": name,
            "primary": current_paths.legacy_default,
            "current": True,
            "state": "running" if pid else "stopped",
            "desired_state": "running" if pid else "stopped",
            "pid": pid,
            "model": model,
            "goal": primary_goal,
            "last_error": "",
        }
    ]
    alerts: list[dict[str, str]] = []

    for item in named:
        profile = item.get("profile") or {}
        if str(profile.get("instance_id")) == local_instance_id:
            # A scoped named-agent console already represents this profile in
            # the first row with its local PID/config/Goal paths.
            continue
        runtime = item.get("runtime") or {}
        data_path = Path((item.get("paths") or {}).get("data") or ".")
        state = str(runtime.get("state") or "unknown")
        desired = str(profile.get("desired_state") or "stopped")
        last_error = str(runtime.get("last_error") or "")
        agent = {
            "instance_id": str(profile.get("instance_id") or "unknown"),
            "display_name": str(
                profile.get("display_name")
                or profile.get("instance_id")
                or "Agent"
            ),
            "primary": False,
            "current": False,
            "state": state,
            "desired_state": desired,
            "pid": runtime.get("pid"),
            "model": profile.get("model_override") or "inherited",
            "goal": _latest_goal(data_path),
            "last_error": last_error,
        }
        agents.append(agent)
        paths_for = getattr(instance_manager, "paths_for", None)
        if callable(paths_for):
            try:
                primary_sessions.extend(
                    {
                        **session,
                        "instance_id": agent["instance_id"],
                        "display_name": agent["display_name"],
                    }
                    for session in _active_process_sessions(
                        paths_for(agent["instance_id"])
                    )
                )
            except Exception:
                pass
        if state in {"crashed", "quarantined"}:
            detail = last_error or "inspect the instance log"
            alerts.append(
                {
                    "severity": "error",
                    "text": f"{agent['display_name']}: {state} — {detail}",
                }
            )
        elif desired == "running" and not runtime.get("alive"):
            alerts.append(
                {
                    "severity": "warning",
                    "text": (
                        f"{agent['display_name']}: requested to run, "
                        f"currently {state}"
                    ),
                }
            )

    qwb_status = str(qwb.get("status") or "offline")
    provider_name = str(qwb.get("provider") or "QWB")
    healthy_accounts, total_accounts = _account_rollup(qwb)
    any_running = any(agent["state"] == "running" for agent in agents)
    if qwb_status != "ok" and any_running:
        alerts.append(
            {
                "severity": "error",
                "text": f"{provider_name} is unavailable while an agent is running",
            }
        )
    elif total_accounts and healthy_accounts < total_accounts:
        alerts.append(
            {
                "severity": "warning",
                "text": (
                    f"{provider_name} accounts: "
                    f"{healthy_accounts}/{total_accounts} healthy"
                ),
            }
        )

    if primary_goal and primary_goal.get("status") == "active" and not pid:
        alerts.append(
            {
                "severity": "warning",
                "text": f"{name}: active Goal is paused because the agent is stopped",
            }
        )

    return {
        "agents": agents,
        "qwb": qwb,
        "qwb_accounts": {
            "healthy": healthy_accounts,
            "total": total_accounts,
        },
        "supervisor": supervisor,
        "active_sessions": primary_sessions,
        "alerts": alerts[:6],
        "collected_at": time.time(),
    }


def _state_markup(state: str) -> str:
    color = {
        "running": "green",
        "starting": "yellow",
        "stopping": "yellow",
        "crashed": "red",
        "quarantined": "red",
        "stopped": "dim",
    }.get(state, "white")
    symbol = "●" if state == "running" else "✕" if state in {"crashed", "quarantined"} else "○"
    return f"[{color}]{symbol} {state.upper()}[/{color}]"


def _shorten(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text or "—"
    return text[: max(1, limit - 1)].rstrip() + "…"


def render_dashboard(snapshot: DashboardSnapshot) -> None:
    """Render a compact overview below the preserved text logo."""

    agents = snapshot.get("agents") or []
    qwb = snapshot.get("qwb") or {}
    accounts = snapshot.get("qwb_accounts") or {}
    supervisor = snapshot.get("supervisor") or {}
    running = sum(agent.get("state") == "running" for agent in agents)

    qwb_state = str(qwb.get("status") or "offline")
    provider_name = str(qwb.get("provider") or "QWB")
    qwb_color = "green" if qwb_state == "ok" else "red"
    account_text = (
        f"{accounts.get('healthy', 0)}/{accounts.get('total', 0)} accounts  ·  "
        if accounts.get("total", 0)
        else "·  "
    )
    status_line = (
        f"[bold]Agents[/bold]  [green]{running} running[/green] / {len(agents)}  ·  "
        f"[bold]{provider_name}[/bold]  "
        f"[{qwb_color}]{qwb_state.upper()}[/{qwb_color}] "
        f"{account_text}"
        f"[bold]Supervisor[/bold]  "
        f"{'[green]ONLINE[/green]' if supervisor.get('running') else '[dim]OFFLINE[/dim]'}"
    )
    console.print(Panel(status_line, border_style="cyan", padding=(0, 1)))

    width = console.size.width
    table = Table(title="Agents", expand=True, pad_edge=False)
    table.add_column("Agent", style="bold cyan", no_wrap=True)
    table.add_column("State", no_wrap=True)
    table.add_column("Current work", overflow="ellipsis")
    if width >= 110:
        table.add_column("Model", overflow="ellipsis")
        table.add_column("PID", justify="right", no_wrap=True)

    for agent in agents:
        goal = agent.get("goal") or {}
        work = "Idle"
        if goal.get("status") == "active":
            ledger = goal.get("task_ledger") or {}
            work = _shorten(
                ledger.get("next_action") or goal.get("objective"),
                64,
            )
        elif agent.get("last_error"):
            work = _shorten(agent.get("last_error"), 64)
        label = str(agent.get("display_name") or agent.get("instance_id"))
        if agent.get("primary"):
            label += " [dim](Primary)[/dim]"
        elif agent.get("current"):
            label += " [dim](Current)[/dim]"
        row = [
            label,
            _state_markup(str(agent.get("state") or "unknown")),
            work,
        ]
        if width >= 110:
            row.extend(
                [
                    _shorten(agent.get("model"), 28),
                    str(agent.get("pid") or "—"),
                ]
            )
        table.add_row(*row)
    console.print(table)

    active_goals = [
        (agent, agent.get("goal"))
        for agent in agents
        if (agent.get("goal") or {}).get("status") == "active"
    ]
    sessions = snapshot.get("active_sessions") or []
    if active_goals or sessions:
        work_lines: list[str] = []
        for agent, goal in active_goals:
            ledger = goal.get("task_ledger") or {}
            work_lines.extend(
                [
                    f"[bold]{agent.get('display_name')} · Goal[/bold]  "
                    f"{_shorten(goal.get('objective'), 100)}",
                    f"[dim]Phase: {ledger.get('current_phase', 'initial')}  ·  "
                    f"Next: {_shorten(ledger.get('next_action'), 100)}[/dim]",
                ]
            )
        if sessions:
            session = sessions[0]
            work_lines.append(
                f"[bold]Managed sessions[/bold]  {len(sessions)} active  ·  "
                f"{session.get('display_name')} · {session.get('id')}  ·  "
                f"{_shorten(session.get('filepath'), 80)}"
            )
        console.print(
            Panel("\n".join(work_lines), title="Active work", border_style="green")
        )

    alerts = snapshot.get("alerts") or []
    if alerts:
        lines = []
        for alert in alerts:
            severity = alert.get("severity")
            color = "red" if severity == "error" else "yellow"
            marker = "!" if severity == "error" else "•"
            lines.append(f"[{color}]{marker} {alert.get('text')}[/{color}]")
        console.print(
            Panel("\n".join(lines), title="Needs attention", border_style="yellow")
        )
