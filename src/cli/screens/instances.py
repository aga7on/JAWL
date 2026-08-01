"""Unified operator screen for independent named JAWL processes."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import psutil
import questionary
from rich.panel import Panel
from rich.table import Table

from src.cli.widgets.ui import (
    console,
    draw_header,
    get_custom_style,
    launch_in_new_window,
    print_error,
    print_info,
    print_success,
    set_window_title,
    wait_for_enter,
)
from src.instances.manager import InstanceManager
from src.instances.paths import get_instance_paths


ROOT_DIR = get_instance_paths().project_root


def _manager() -> InstanceManager:
    return InstanceManager(ROOT_DIR)


def _render(manager: InstanceManager) -> None:
    supervisor = manager.supervisor_status()
    table = Table(
        title=(
            "JAWL Multi-Instance Manager — supervisor "
            + (
                f"online (PID {supervisor['pid']})"
                if supervisor["running"]
                else "offline"
            )
        )
    )
    table.add_column("Instance", style="bold cyan")
    table.add_column("Desired")
    table.add_column("Runtime")
    table.add_column("PID", justify="right")
    table.add_column("Model")
    table.add_column("Telegram")
    table.add_column("Recovery")

    default_paths = get_instance_paths()
    default_pid = None
    if default_paths.pid_file.is_file():
        try:
            candidate = int(default_paths.pid_file.read_text().strip())
            if psutil.pid_exists(candidate):
                default_pid = candidate
        except (OSError, ValueError):
            pass
    table.add_row(
        "default",
        "legacy",
        "[green]running[/green]" if default_pid else "stopped",
        str(default_pid or "—"),
        "profile config",
        "profile config",
        "legacy control",
    )

    for item in manager.list_status():
        profile = item["profile"]
        runtime = item["runtime"]
        state = runtime["state"]
        state_markup = (
            f"[green]{state}[/green]"
            if runtime["alive"]
            else (
                f"[red]{state}[/red]"
                if state in {"crashed", "quarantined"}
                else state
            )
        )
        table.add_row(
            f"{profile['display_name']} [{profile['instance_id']}]",
            profile["desired_state"],
            state_markup,
            str(runtime["pid"] or "—"),
            profile["model_override"] or "inherited",
            (
                f"{profile['telegram_mode']}:"
                f"{profile['telethon_session']}"
            ),
            (
                f"auto {profile['restart_limit']}/"
                f"{profile['restart_window_sec']}s"
                if profile["auto_restart"]
                else "manual"
            ),
        )
    console.print(table)


def _choose_profile(manager: InstanceManager) -> str | None:
    profiles = manager.registry.list_profiles()
    if not profiles:
        print_info(" No named profiles exist yet.")
        time.sleep(1)
        return None
    return questionary.select(
        "Select an instance:",
        choices=[
            questionary.Choice(
                f"{profile.display_name} [{profile.instance_id}]",
                profile.instance_id,
            )
            for profile in profiles
        ]
        + [questionary.Choice("← Back", None)],
        style=get_custom_style(),
    ).ask()


def _create(manager: InstanceManager) -> None:
    instance_id = questionary.text(
        "Stable instance ID (letters, numbers, ._-):"
    ).ask()
    if not instance_id:
        return
    display_name = questionary.text(
        "Agent display name:", default=instance_id
    ).ask()
    if not display_name:
        return
    profiles = manager.registry.list_profiles()
    template_choices = [questionary.Choice("(clean start — no template)", "")]
    for p in profiles:
        template_choices.append(
            questionary.Choice(
                f"{p.display_name} [{p.instance_id}]", p.instance_id
            )
        )
    template_profile = questionary.select(
        "Base configuration template:",
        choices=template_choices,
    ).ask() or ""
    model = questionary.text(
        "Model override (blank = inherit from template or default):",
        default="",
    ).ask()
    telegram_mode = questionary.select(
        "Telegram mode:",
        choices=[
            questionary.Choice("Disabled", "disabled"),
            questionary.Choice("Telethon user session", "telethon"),
            questionary.Choice("Aiogram bot", "aiogram"),
            questionary.Choice("Inherit copied configuration", "inherit"),
        ],
    ).ask()
    telegram_identity = ""
    if telegram_mode in {"telethon", "aiogram"}:
        telegram_identity = (
            questionary.text(
                "Non-secret Telegram account/bot identity label "
                "(must be unique):"
            ).ask()
            or ""
        ).strip()
    visible = questionary.confirm(
        "Open a separate visible console for this agent?", default=True
    ).ask()
    auto_restart = questionary.confirm(
        "Automatically restart after a crash?", default=True
    ).ask()
    try:
        profile = manager.create_profile(
            instance_id,
            display_name,
            template_profile=template_profile,
            telegram_mode=telegram_mode or "disabled",
            telegram_identity=telegram_identity,
            visible_console=bool(visible),
            auto_restart=bool(auto_restart),
        )
        manager.ensure_supervisor()
        print_success(
            f"Profile {profile.instance_id} created with isolated state."
        )
    except Exception as exc:
        print_error(f"Could not create profile: {exc}")
    wait_for_enter()


def _wait_runtime(
    manager: InstanceManager,
    instance_id: str,
    expected_alive: bool,
    timeout: float = 15,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if manager.status(instance_id)["runtime"]["alive"] is expected_alive:
            return True
        time.sleep(0.25)
    return False


def _combined_logs(manager: InstanceManager) -> None:
    sections = []
    for item in manager.list_status():
        profile = item["profile"]
        path = Path(item["paths"]["logs"]) / "main.log"
        if path.is_file():
            try:
                lines = path.read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines()[-12:]
            except OSError as exc:
                lines = [f"Could not read log: {exc}"]
        else:
            lines = ["No log has been created yet."]
        sections.append(
            f"[bold cyan]{profile['display_name']} "
            f"[{profile['instance_id']}][/bold cyan]\n"
            + "\n".join(lines)
        )
    console.print(
        Panel(
            "\n\n".join(sections) or "No named instance logs.",
            title="Combined instance log tail",
            border_style="cyan",
        )
    )
    wait_for_enter()


def _profile_actions(manager: InstanceManager, instance_id: str) -> None:
    while True:
        draw_header()
        item = manager.status(instance_id)
        profile = item["profile"]
        runtime = item["runtime"]
        console.print(
            Panel(
                f"State: {runtime['state']} | PID: {runtime['pid'] or '—'}\n"
                f"Home: {item['paths']['home']}\n"
                f"Private data: {item['paths']['data']}\n"
                f"Shared sandbox: {item['paths']['sandbox']}\n"
                f"Reserved ports: {profile['static_ports'] or 'dynamic only'}\n"
                f"Last error: {runtime['last_error'] or '—'}",
                title=f"{profile['display_name']} [{instance_id}]",
            )
        )
        action = questionary.select(
            "Instance action:",
            choices=[
                questionary.Choice("▶ Start / clear quarantine", "start"),
                questionary.Choice("■ Graceful stop", "stop"),
                questionary.Choice("↻ Restart", "restart"),
                questionary.Choice("💬 Open this agent's chat", "chat"),
                questionary.Choice("📋 Open this agent's logs", "logs"),
                questionary.Choice(
                    "⚙ Open scoped JAWL menu/config/goals", "menu"
                ),
                questionary.Choice("📁 Open profile directory", "folder"),
                questionary.Choice("Tune lifecycle/profile", "edit"),
                questionary.Choice("Archive stopped profile", "archive"),
                questionary.Choice("✕ Delete profile permanently", "delete"),
                questionary.Choice("← Back", "back"),
            ],
            style=get_custom_style(),
        ).ask()
        if not action or action == "back":
            return
        try:
            if action == "start":
                manager.request_start(instance_id)
                manager.ensure_supervisor()
                if _wait_runtime(manager, instance_id, True):
                    print_success("Instance is running.")
                else:
                    print_error("Start is still pending; inspect supervisor log.")
                wait_for_enter()
            elif action == "stop":
                manager.request_stop(instance_id)
                manager.ensure_supervisor()
                if _wait_runtime(manager, instance_id, False, 25):
                    print_success("Instance stopped gracefully.")
                else:
                    print_error("Stop is still pending.")
                wait_for_enter()
            elif action == "restart":
                manager.request_stop(instance_id)
                manager.reconcile_once()
                manager.request_start(instance_id)
                manager.ensure_supervisor()
                _wait_runtime(manager, instance_id, True)
            elif action in {"chat", "logs", "menu"}:
                paths = manager.paths_for(instance_id)
                argument = {
                    "chat": "--terminal",
                    "logs": "--logs-main",
                    "menu": "--instance-menu",
                }[action]
                launch_in_new_window(
                    argument,
                    environment=paths.child_environment(),
                )
            elif action == "folder":
                home = manager.paths_for(instance_id).instance_home
                if os.name == "nt":
                    os.startfile(home)  # type: ignore[attr-defined]
                else:
                    print_info(f" {home}")
            elif action == "edit":
                current = manager.registry.get_profile(instance_id)
                model = questionary.text(
                    "Model override:",
                    default=current.model_override,
                ).ask()
                auto = questionary.confirm(
                    "Automatic crash recovery?",
                    default=current.auto_restart,
                ).ask()
                visible = questionary.confirm(
                    "Visible agent console?",
                    default=current.visible_console,
                ).ask()
                limit = questionary.text(
                    "Restart limit in one window:",
                    default=str(current.restart_limit),
                ).ask()
                manager.registry.update_profile(
                    instance_id,
                    model_override=(model or "").strip(),
                    auto_restart=bool(auto),
                    visible_console=bool(visible),
                    restart_limit=int(limit),
                )
                print_success("Profile updated; restart to apply config changes.")
                wait_for_enter()
            elif action == "archive":
                if not questionary.confirm(
                    "Archive this stopped profile recoverably?",
                    default=False,
                ).ask():
                    continue
                destination = manager.archive_profile(instance_id)
                print_success(f"Profile data moved to {destination}")
                wait_for_enter()
                return
            elif action == "delete":
                confirmation = questionary.text(
                    "Permanent deletion cannot be undone. "
                    f"Type '{instance_id}' to continue:"
                ).ask()
                if confirmation != instance_id:
                    print_info(" Permanent deletion cancelled.")
                    continue
                manager.delete_profile(instance_id)
                print_success(f"Profile {instance_id} deleted.")
                wait_for_enter()
                return
        except Exception as exc:
            print_error(f"Instance action failed: {exc}")
            wait_for_enter()


def instances_screen() -> None:
    manager = _manager()
    try:
        manager.ensure_supervisor()
    except Exception as exc:
        print_error(f"Supervisor unavailable: {exc}")
        wait_for_enter()
    while True:
        set_window_title("JAWL - Multi-Instance Manager")
        draw_header()
        _render(manager)
        action = questionary.select(
            "Multi-instance action:",
            choices=[
                questionary.Choice("＋ Create named agent", "create"),
                questionary.Choice("Manage existing agent", "manage"),
                questionary.Choice("Combined log tails", "logs"),
                questionary.Choice("Refresh", "refresh"),
                questionary.Choice("← Back", "back"),
            ],
            style=get_custom_style(),
        ).ask()
        if not action or action == "back":
            return
        if action == "create":
            _create(manager)
        elif action == "manage":
            selected = _choose_profile(manager)
            if selected:
                _profile_actions(manager, selected)
        elif action == "logs":
            _combined_logs(manager)
