"""Operator dashboard for JAWL, QWB, modes, queues, and durable Goals."""

from __future__ import annotations

import json
import time
from typing import Any, Dict
from urllib.error import URLError
from urllib.request import urlopen

import questionary
from rich.panel import Panel
from rich.table import Table

from src.cli.control_client import request_control
from src.cli.screens.agent_control import _is_agent_running
from src.cli.widgets.ui import (
    console,
    draw_header,
    get_custom_style,
    print_error,
    set_window_title,
)
from src.utils.settings import load_config


def _qwb_health() -> Dict[str, Any]:
    try:
        with urlopen("http://127.0.0.1:8000/health", timeout=2.0) as response:
            payload = json.loads(response.read(262144).decode("utf-8"))
        return payload if isinstance(payload, dict) else {"status": "invalid"}
    except (OSError, TimeoutError, URLError, ValueError, json.JSONDecodeError):
        return {"status": "offline"}


def _offline_status() -> Dict[str, Any]:
    settings, interfaces = load_config()
    return {
        "agent": {
            "state": "stopped",
            "model": settings.llm.main_model,
            "step": 0,
            "max_steps": settings.llm.max_react_steps,
            "uptime": "—",
        },
        "modes": {
            "thinking_policy": settings.llm.thinking_policy,
            "tool_transport": settings.llm.tool_transport,
            "continuous_cycle": settings.system.continuous_cycle,
            "heartbeat_interval": settings.system.heartbeat_interval,
            "goal_mode": settings.system.goal_mode.model_dump(),
            "idle_heartbeat_backoff": (
                settings.system.idle_heartbeat_backoff.model_dump()
            ),
            "event_policy": (
                settings.system.event_acceleration.active_cycle_policy
            ),
            "mcp_enabled": interfaces.mcp.enabled,
            "media": interfaces.multimodality.model_dump(),
        },
        "goal": None,
        "heartbeat": None,
    }


def get_runtime_status() -> Dict[str, Any]:
    if not _is_agent_running():
        return _offline_status()
    return request_control("status.get", timeout=3.0)


def _render(status: Dict[str, Any], qwb: Dict[str, Any]) -> None:
    agent = status.get("agent") or {}
    modes = status.get("modes") or {}
    heartbeat = status.get("heartbeat") or {}
    goal = status.get("goal") or {}

    summary = Table.grid(padding=(0, 2))
    summary.add_column(style="bold cyan")
    summary.add_column()
    summary.add_row("Agent", str(agent.get("state", "unknown")))
    summary.add_row("Model", str(agent.get("model", "unknown")))
    summary.add_row(
        "ReAct",
        f"step {agent.get('step', 0)}/{agent.get('max_steps', 0)}",
    )
    summary.add_row("Uptime", str(agent.get("uptime", "—")))
    summary.add_row(
        "QWB",
        f"{qwb.get('status', 'offline')} · "
        f"{qwb.get('model', '—')} · {qwb.get('transport', '—')}",
    )
    console.print(Panel(summary, title="Runtime", border_style="cyan"))

    mode_table = Table(title="Execution modes", show_header=False)
    mode_table.add_column("Mode", style="bold")
    mode_table.add_column("Value", overflow="fold")
    for label, key in (
        ("Thinking", "thinking_policy"),
        ("Tool transport", "tool_transport"),
        ("Heartbeat", "heartbeat_interval"),
        ("Continuous", "continuous_cycle"),
        ("Event policy", "event_policy"),
        ("MCP", "mcp_enabled"),
    ):
        mode_table.add_row(label, str(modes.get(key, "—")))
    goal_mode = modes.get("goal_mode") or {}
    mode_table.add_row(
        "Goal Mode",
        f"enabled={goal_mode.get('enabled', False)}, "
        f"compact={goal_mode.get('compact_context', False)}, "
        f"ledger={goal_mode.get('task_ledger_enabled', False)}, "
        f"rebase={goal_mode.get('provider_rebase_prompt_tokens', 0)} tokens",
    )
    idle = modes.get("idle_heartbeat_backoff") or {}
    mode_table.add_row(
        "Idle backoff",
        (
            f"enabled={idle.get('enabled', False)}, "
            f"threshold={idle.get('no_op_threshold', '—')}, "
            f"max={idle.get('max_interval_sec', '—')}s"
        ),
    )
    media = modes.get("media") or {}
    mode_table.add_row(
        "Media",
        (
            f"vision={media.get('enabled', False)}, "
            f"video={media.get('video_understanding_enabled', False)}, "
            f"generation={media.get('media_generation_enabled', False)}"
        ),
    )
    console.print(mode_table)

    if goal:
        ledger = goal.get("task_ledger") or {}
        console.print(
            Panel(
                f"[bold]{goal.get('objective', '')}[/bold]\n"
                f"{goal.get('status', '')} / "
                f"{goal.get('last_cycle_status', '')} · "
                f"tokens={goal.get('accounted_tokens', 0)} · "
                f"verification={goal.get('verification_status', '—')}",
                title=f"Goal {goal.get('goal_id', '')}",
                border_style="green"
                if goal.get("status") == "active"
                else "yellow",
            )
        )

        console.print(
            f"[dim]Task Ledger: phase={ledger.get('current_phase', 'initial')} · "
            f"rev={ledger.get('revision', 0)} · "
            f"next={ledger.get('next_action') or 'not recorded'}[/dim]"
        )

    accounts = qwb.get("accounts")
    if isinstance(accounts, list) and accounts:
        table = Table(title="QWB accounts")
        table.add_column("Account")
        table.add_column("State")
        table.add_column("Cooldown until")
        table.add_column("In flight", justify="right")
        for account in accounts:
            if not isinstance(account, dict):
                continue
            table.add_row(
                f"{account.get('label', 'token')}[{account.get('fingerprint', '')}]",
                str(account.get("state", "unknown")),
                str(account.get("cooldownUntil") or "—"),
                str(account.get("inFlight", 0)),
            )
        console.print(table)

    media_jobs = qwb.get("mediaJobs") or {}
    if media_jobs:
        console.print(
            f"[dim]QWB media jobs: active={media_jobs.get('active', 0)} · "
            f"total={media_jobs.get('total', 0)} · "
            f"{media_jobs.get('counts') or {}}[/dim]"
        )

    backoff = heartbeat.get("idle_heartbeat_backoff") or {}
    if backoff:
        console.print(
            f"[dim]Heartbeat no-ops: {backoff.get('consecutive_no_ops', 0)} · "
            f"current interval: {backoff.get('interval_multiplier', 1)}x · "
            f"queued: {(heartbeat.get('wake_queue') or {}).get('size', 0)}[/dim]"
        )


def runtime_screen() -> None:
    set_window_title("JAWL - Runtime & Modes")
    error = ""
    while True:
        draw_header()
        try:
            _render(get_runtime_status(), _qwb_health())
            error = ""
        except (ConnectionError, RuntimeError, TimeoutError, ValueError) as exc:
            error = str(exc)
            print_error(error)

        choice = questionary.select(
            "Runtime controls:",
            choices=[
                questionary.Choice("[r] Refresh", "refresh"),
                questionary.Choice("[G] Durable Goals", "goals"),
                questionary.Choice("[*] Configure modes", "setup"),
                questionary.Choice("← Back", "back"),
            ],
            style=get_custom_style(),
            qmark="",
        ).ask()
        if choice in {None, "back"}:
            return
        if choice == "goals":
            from src.cli.screens.goals import goals_screen

            goals_screen()
        elif choice == "setup":
            from src.cli.screens.setup_wizard import setup_wizard_screen

            setup_wizard_screen()
        elif error:
            time.sleep(0.5)
