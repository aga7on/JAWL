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
from src.utils.settings import load_config


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
    if default_paths.legacy_default:
        default_pid = None
        if default_paths.pid_file.is_file():
            try:
                candidate = int(default_paths.pid_file.read_text().strip())
                if psutil.pid_exists(candidate):
                    default_pid = candidate
            except (OSError, ValueError):
                pass
        try:
            settings, interfaces = load_config()
            default_name = settings.identity.agent_name
            default_model = settings.llm.main_model
            if interfaces.telegram.telethon.enabled:
                default_telegram = "telethon"
            elif interfaces.telegram.aiogram.enabled:
                default_telegram = "aiogram"
            else:
                default_telegram = "disabled"
        except Exception:
            default_name = "Primary"
            default_model = "unknown"
            default_telegram = "unknown"
        table.add_row(
            f"{default_name} [default] · Primary",
            "running" if default_pid else "stopped",
            "[green]running[/green]" if default_pid else "stopped",
            str(default_pid or "—"),
            default_model,
            default_telegram,
            "manual",
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
                questionary.Choice(
                    "▶ Start / clear quarantine",
                    "start",
                    shortcut_key="s",
                ),
                questionary.Choice(
                    "■ Graceful stop", "stop", shortcut_key="x"
                ),
                questionary.Choice("↻ Restart", "restart", shortcut_key="r"),
                questionary.Choice(
                    "💬 Open this agent's chat", "chat", shortcut_key="c"
                ),
                questionary.Choice(
                    "📋 Open this agent's logs", "logs", shortcut_key="l"
                ),
                questionary.Choice(
                    "⚙ Open scoped JAWL menu/config/goals",
                    "menu",
                    shortcut_key="m",
                ),
                questionary.Choice(
                    "📁 Open profile directory", "folder", shortcut_key="f"
                ),
                questionary.Choice(
                    "Tune lifecycle/profile", "edit", shortcut_key="t"
                ),
                questionary.Choice(
                    "Archive stopped profile", "archive", shortcut_key="a"
                ),
                questionary.Choice(
                    "✕ Delete profile permanently",
                    "delete",
                    shortcut_key="d",
                ),
                questionary.Choice("← Back", "back", shortcut_key="b"),
            ],
            style=get_custom_style(),
            use_shortcuts=True,
            use_jk_keys=False,
            qmark="",
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


def _local_agent_actions() -> None:
    """Present the current console's agent with one consistent mental model."""

    from src.cli.screens.agent_control import (
        _is_agent_running,
        start_agent_screen,
        stop_agent_screen,
    )

    paths = get_instance_paths()
    while True:
        draw_header()
        running = _is_agent_running()
        pid = None
        if running and paths.pid_file.is_file():
            try:
                candidate = int(paths.pid_file.read_text().strip())
                if psutil.pid_exists(candidate):
                    pid = candidate
            except (OSError, ValueError):
                pass
        try:
            settings, interfaces = load_config()
            name = settings.identity.agent_name
            model = settings.llm.main_model
            telegram = (
                "telethon"
                if interfaces.telegram.telethon.enabled
                else "aiogram"
                if interfaces.telegram.aiogram.enabled
                else "disabled"
            )
        except Exception:
            name, model, telegram = "Primary", "unknown", "unknown"

        role = "Primary" if paths.legacy_default else "Current"
        console.print(
            Panel(
                f"State: {'[green]running[/green]' if running else '[dim]stopped[/dim]'}"
                f" | PID: {pid or '—'}\n"
                f"Model: {model} | Telegram: {telegram}\n"
                f"Private data: {paths.data_dir}\n"
                f"Shared sandbox: {paths.sandbox_dir}",
                title=f"{name} [{paths.instance_id}] · {role}",
                border_style="cyan",
            )
        )

        choices = []
        if running:
            choices.extend(
                [
                    questionary.Choice(
                        "■ Graceful stop", "stop", shortcut_key="s"
                    ),
                    questionary.Choice(
                        "↻ Restart", "restart", shortcut_key="r"
                    ),
                ]
            )
        else:
            choices.append(
                questionary.Choice("▶ Start", "start", shortcut_key="s")
            )
        choices.extend(
            [
                questionary.Choice(
                    "💬 Open chat", "chat", shortcut_key="c"
                ),
                questionary.Choice(
                    "◎ Work & Goals", "goals", shortcut_key="g"
                ),
                questionary.Choice(
                    "📋 Open logs", "logs", shortcut_key="l"
                ),
                questionary.Choice(
                    "≡ Runtime diagnostics", "runtime", shortcut_key="d"
                ),
                questionary.Choice(
                    "⚙ Configure", "setup", shortcut_key="o"
                ),
                questionary.Choice(
                    "📁 Open profile directory", "folder", shortcut_key="f"
                ),
                questionary.Choice("← Back", "back", shortcut_key="b"),
            ]
        )
        action = questionary.select(
            f"{role} agent action:",
            choices=choices,
            style=get_custom_style(),
            use_shortcuts=True,
            use_jk_keys=False,
            qmark="",
        ).ask()
        if action in {None, "back"}:
            return
        if action == "start":
            start_agent_screen()
        elif action == "stop":
            stop_agent_screen()
        elif action == "restart":
            stop_agent_screen()
            if not _is_agent_running():
                start_agent_screen()
        elif action == "chat":
            if running:
                launch_in_new_window("--terminal")
            else:
                print_error("Start the primary agent before opening chat.")
                wait_for_enter()
        elif action == "logs":
            launch_in_new_window("--logs-main")
        elif action == "goals":
            from src.cli.screens.goals import goals_screen

            goals_screen()
        elif action == "runtime":
            from src.cli.screens.runtime import runtime_screen

            runtime_screen()
        elif action == "setup":
            from src.cli.screens.setup_wizard import setup_wizard_screen

            setup_wizard_screen()
        elif action == "folder":
            if os.name == "nt":
                os.startfile(paths.instance_home)  # type: ignore[attr-defined]
            else:
                print_info(f" {paths.instance_home}")


def instances_screen() -> None:
    manager = _manager()
    local_paths = get_instance_paths()
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
            "Agents:",
            choices=[
                questionary.Choice(
                    "Primary agent" if local_paths.legacy_default else "Current agent",
                    "local",
                    shortcut_key="p",
                ),
                questionary.Choice(
                    "Manage named agent", "manage", shortcut_key="a"
                ),
                questionary.Choice(
                    "＋ Create named agent", "create", shortcut_key="n"
                ),
                questionary.Choice(
                    "Combined log tails", "logs", shortcut_key="l"
                ),
                questionary.Choice("Refresh", "refresh", shortcut_key="r"),
                questionary.Choice("← Back", "back", shortcut_key="b"),
            ],
            style=get_custom_style(),
            use_shortcuts=True,
            use_jk_keys=False,
            qmark="",
        ).ask()
        if not action or action == "back":
            return
        if action == "local":
            _local_agent_actions()
        elif action == "create":
            _create(manager)
        elif action == "manage":
            selected = _choose_profile(manager)
            if selected:
                _profile_actions(manager, selected)
        elif action == "logs":
            _combined_logs(manager)
