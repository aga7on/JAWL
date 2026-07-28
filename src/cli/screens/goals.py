"""Interactive durable Goal lifecycle screen."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Dict

import questionary
from rich.panel import Panel
from rich.table import Table

from src.cli.control_client import request_control
from src.cli.screens.agent_control import _is_agent_running
from src.cli.widgets.ui import (
    clear_screen,
    console,
    draw_header,
    get_custom_style,
    print_error,
    print_info,
    print_success,
    set_window_title,
)
from src.l0_state.agent.state import AgentState
from src.l3_agent.goals.ledger import TaskLedgerPatch
from src.l3_agent.goals.manager import GoalManager
from src.utils.settings import load_config


ROOT_DIR = Path(__file__).resolve().parent.parent.parent.parent
GOAL_STORE = (
    ROOT_DIR / "src" / "utils" / "local" / "data" / "agent" / "goals.json"
)


def _offline_manager() -> GoalManager:
    settings, _ = load_config()
    config = settings.system.goal_mode
    return GoalManager(
        GOAL_STORE,
        AgentState(),
        enabled=config.enabled,
        compact_context=config.compact_context,
        compact_max_chars=config.compact_max_chars,
        suppress_waiting_heartbeats=config.suppress_waiting_heartbeats,
        task_ledger_enabled=config.task_ledger_enabled,
        task_ledger_max_chars=config.task_ledger_max_chars,
        provider_rebase_prompt_tokens=config.provider_rebase_prompt_tokens,
        recover_on_start=False,
    )


def _offline_control(action: str, params: Dict[str, Any]) -> Dict[str, Any]:
    manager = _offline_manager()

    async def invoke() -> Dict[str, Any]:
        if action == "goal.list":
            return {"goals": manager.list_views(int(params.get("limit", 20)))}
        if action == "goal.get":
            goal = manager.view(str(params.get("goal_id", "")))
            if goal is None:
                raise ValueError("Goal not found.")
            return goal
        if action == "goal.create":
            goal = await manager.create(
                str(params.get("objective", "")),
                token_budget=params.get("token_budget"),
                linked_task_id=str(params.get("linked_task_id", "")),
                verification_policy=str(
                    params.get("verification_policy", "auto")
                ),
            )
            return manager.view(goal.goal_id)
        if action == "goal.update":
            goal = await manager.update(
                status=str(params.get("status", "")),
                summary=str(params.get("summary", "")),
                goal_id=str(params.get("goal_id", "")),
                token_budget=params.get("token_budget"),
            )
            return manager.view(goal.goal_id)
        if action == "goal.wakeup":
            seconds = params.get("wake_after_seconds")
            if (
                isinstance(seconds, bool)
                or not isinstance(seconds, int)
                or seconds < 1
                or seconds > 86400
            ):
                raise ValueError(
                    "wake_after_seconds must be between 1 and 86400."
                )
            goal = await manager.finish_cycle(
                state="waiting",
                summary=str(params.get("summary", "")),
                wake_after_seconds=seconds,
            )
            if goal is None:
                raise ValueError("No active goal.")
            return manager.view(goal.goal_id)
        if action == "goal.ledger.update":
            goal = await manager.record_ledger_patch(
                TaskLedgerPatch.model_validate(params.get("patch", {}))
            )
            if goal is None:
                raise ValueError("No active goal.")
            return manager.view(goal.goal_id)
        raise ValueError(f"Unsupported offline action '{action}'.")

    return asyncio.run(invoke())


def invoke_goal_control(
    action: str, params: Dict[str, Any] | None = None
) -> Dict[str, Any]:
    """Use live IPC while JAWL runs and exact local state while it is stopped."""

    payload = params or {}
    if _is_agent_running():
        return request_control(action, payload)
    return _offline_control(action, payload)


def _fmt_time(timestamp: Any) -> str:
    if not isinstance(timestamp, (int, float)):
        return "—"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))


def _goal_panel(goal: Dict[str, Any] | None) -> Panel:
    if not goal:
        return Panel(
            "No Goal has been created yet.",
            title="Durable Goal",
            border_style="dim",
        )
    remaining = goal.get("remaining_tokens")
    remaining_text = "unbounded" if remaining is None else str(remaining)
    wake = _fmt_time(goal.get("next_wakeup_at"))
    ledger = goal.get("task_ledger") or {}
    body = (
        f"[bold]{goal.get('objective', '')}[/bold]\n\n"
        f"ID: [cyan]{goal.get('goal_id', '')}[/cyan]\n"
        f"State: [magenta]{goal.get('status', '')}[/magenta] / "
        f"{goal.get('last_cycle_status', '')}\n"
        f"Revision: {goal.get('revision')} | "
        f"Continuations: {goal.get('continuation_count')}\n"
        f"Tokens: {goal.get('accounted_tokens')} used, {remaining_text} left\n"
        f"QWB lane: {goal.get('lane_id', '')}\n"
        f"Wakeup: {wake}\n"
        f"Verification: {goal.get('verification_policy')} / "
        f"{goal.get('verification_status')}\n"
        f"Summary: {goal.get('last_summary') or '—'}"
    )
    body += (
        f"\nLedger: rev {ledger.get('revision', 0)} / "
        f"phase={ledger.get('current_phase', 'initial')}\n"
        f"Next: {ledger.get('next_action') or 'not recorded'}"
    )
    color = {
        "active": "green",
        "complete": "cyan",
        "blocked": "yellow",
        "cancelled": "red",
    }.get(str(goal.get("status")), "white")
    return Panel(body, title="Durable Goal", border_style=color)


def _latest_goal() -> Dict[str, Any] | None:
    result = invoke_goal_control("goal.list", {"limit": 1})
    goals = result.get("goals", [])
    return goals[0] if goals else None


def _optional_positive_int(prompt: str) -> int | None:
    raw = questionary.text(prompt, default="").ask()
    if raw is None or not raw.strip():
        return None
    value = int(raw)
    if value < 1:
        raise ValueError("Value must be a positive integer.")
    return value


def _create_goal() -> None:
    objective = questionary.text("Objective:").ask()
    if not objective or not objective.strip():
        return
    linked_task = questionary.text(
        "Linked coding task ID (optional):", default=""
    ).ask()
    policy = questionary.select(
        "Verification policy:",
        choices=[
            questionary.Choice("Auto (required for linked coding task)", "auto"),
            questionary.Choice("Always required", "required"),
            questionary.Choice("No enforced gate", "none"),
        ],
        style=get_custom_style(),
    ).ask()
    budget = _optional_positive_int("Token budget (blank = unbounded):")
    invoke_goal_control(
        "goal.create",
        {
            "objective": objective.strip(),
            "linked_task_id": (linked_task or "").strip(),
            "verification_policy": policy or "auto",
            "token_budget": budget,
        },
    )
    print_success("Durable Goal created and scheduled.")


def _update_goal(goal: Dict[str, Any], status: str) -> None:
    labels = {
        "active": "Resume summary:",
        "complete": "Verified completion summary:",
        "blocked": "Concrete blocker and required input:",
        "cancelled": "Cancellation reason:",
    }
    summary = questionary.text(labels[status]).ask()
    if not summary or not summary.strip():
        return
    params: Dict[str, Any] = {
        "goal_id": goal["goal_id"],
        "status": status,
        "summary": summary.strip(),
    }
    if status == "active":
        params["token_budget"] = _optional_positive_int(
            "New total token budget (blank = unchanged):"
        )
    invoke_goal_control("goal.update", params)
    print_success(f"Goal state changed to {status}.")


def _schedule_goal(goal: Dict[str, Any]) -> None:
    seconds = _optional_positive_int("Wake after seconds (1..86400):")
    if seconds is None or seconds > 86400:
        raise ValueError("Wake delay must be between 1 and 86400.")
    summary = questionary.text("What is JAWL waiting for?").ask()
    if not summary or not summary.strip():
        return
    invoke_goal_control(
        "goal.wakeup",
        {
            "goal_id": goal["goal_id"],
            "wake_after_seconds": seconds,
            "summary": summary.strip(),
        },
    )
    print_success("Goal wakeup scheduled.")


def _show_history() -> None:
    goals = invoke_goal_control("goal.list", {"limit": 20}).get("goals", [])
    clear_screen()
    table = Table(title="Recent durable Goals")
    table.add_column("Updated")
    table.add_column("State")
    table.add_column("Objective", overflow="fold")
    table.add_column("Tokens", justify="right")
    table.add_column("Verification")
    for goal in goals:
        table.add_row(
            _fmt_time(goal.get("updated_at")),
            str(goal.get("status", "")),
            str(goal.get("objective", ""))[:100],
            str(goal.get("accounted_tokens", 0)),
            str(goal.get("verification_status", "")),
        )
    console.print(table)
    input("\nPress Enter to return...")


def _show_ledger(goal: Dict[str, Any]) -> None:
    ledger = goal.get("task_ledger") or {}
    clear_screen()
    console.print(
        Panel(
            json.dumps(ledger, ensure_ascii=False, indent=2),
            title=f"Task Ledger · revision {ledger.get('revision', 0)}",
            border_style="cyan",
        )
    )
    input("\nPress Enter to return...")


def _edit_ledger(goal: Dict[str, Any]) -> None:
    ledger = goal.get("task_ledger") or {}
    phase = questionary.text(
        "Current phase:",
        default=str(ledger.get("current_phase") or "initial"),
    ).ask()
    if phase is None:
        return
    next_action = questionary.text(
        "Exact next action:",
        default=str(ledger.get("next_action") or ""),
    ).ask()
    if next_action is None:
        return
    summary = questionary.text(
        "Checkpoint summary:",
        default=str(ledger.get("checkpoint_summary") or ""),
    ).ask()
    if summary is None:
        return
    invoke_goal_control(
        "goal.ledger.update",
        {
            "patch": {
                "phase": phase.strip(),
                "next_action": next_action.strip(),
                "checkpoint_summary": summary.strip(),
            }
        },
    )
    print_success("Task Ledger checkpoint updated.")


def goals_screen() -> None:
    set_window_title("JAWL - Durable Goals")
    while True:
        draw_header()
        choice = None
        try:
            goal = _latest_goal()
            console.print(_goal_panel(goal))
            active = goal if goal and goal.get("status") == "active" else None
            choices = []
            if active is None:
                choices.append(questionary.Choice("[+] Create Goal", "create"))
            if goal and goal.get("status") == "blocked":
                choices.append(questionary.Choice("[>] Resume Goal", "resume"))
            if active:
                choices.extend(
                    [
                        questionary.Choice("[L] Inspect Task Ledger", "ledger"),
                        questionary.Choice(
                            "[e] Edit Ledger checkpoint", "ledger_edit"
                        ),
                    ]
                )
                choices.extend(
                    [
                        questionary.Choice("[✓] Complete Goal", "complete"),
                        questionary.Choice("[||] Block Goal", "blocked"),
                        questionary.Choice("[~] Schedule Wakeup", "wakeup"),
                        questionary.Choice("[x] Cancel Goal", "cancelled"),
                    ]
                )
            choices.extend(
                [
                    questionary.Choice("[i] Goal History", "history"),
                    questionary.Separator(" "),
                    questionary.Choice("← Back", "back"),
                ]
            )
            choice = questionary.select(
                "Goal controls:",
                choices=choices,
                style=get_custom_style(),
                qmark="",
            ).ask()
            if choice in {None, "back"}:
                return
            if choice == "create":
                _create_goal()
            elif choice == "resume":
                _update_goal(goal, "active")
            elif choice in {"complete", "blocked", "cancelled"}:
                if questionary.confirm(
                    f"Change Goal state to {choice}?", default=False
                ).ask():
                    _update_goal(goal, choice)
            elif choice == "wakeup":
                _schedule_goal(goal)
            elif choice == "history":
                _show_history()
            elif choice == "ledger":
                _show_ledger(goal)
            elif choice == "ledger_edit":
                _edit_ledger(goal)
        except (ConnectionError, RuntimeError, TimeoutError, ValueError) as exc:
            print_error(str(exc))
            print_info(
                "If JAWL is running, the Host Terminal interface must be enabled."
            )
        if choice not in {"history", "ledger", None, "back"}:
            time.sleep(1.2)
