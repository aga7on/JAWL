"""
Main CLI Menu Loop for JAWL.

Provides an interactive console selection for agent controls, chat terminal,
log viewers, configuration wizards, and database managers.
"""

import sys
import time

import psutil
import questionary

from src.cli.widgets.ui import (
    launch_in_new_window,
    draw_header,
    print_info,
    set_window_title,
    get_custom_style,
)

from src.cli.screens.agent_control import start_agent_screen, stop_agent_screen
from src.cli.screens.setup_wizard import setup_wizard_screen
from src.cli.screens.database_manager import database_manager_screen
from src.cli.screens.terminal_chat import terminal_chat_screen
from src.cli.screens.goals import goals_screen
from src.cli.screens.runtime import runtime_screen
from src.cli.screens.debug_broker import debug_broker_screen
from src.cli.screens.instances import instances_screen
from src.cli.screens.dashboard import collect_dashboard_snapshot, render_dashboard
from src.cli.screens.providers import provider_screen


def _choice(
    title: str,
    value: str,
    shortcut: str,
    description: str,
) -> questionary.Choice:
    return questionary.Choice(
        title,
        value,
        shortcut_key=shortcut,
        description=description,
    )


def _bridge_processes() -> list[psutil.Process]:
    """Return only identifiable QWB Node processes, never arbitrary port owners."""

    matches = []
    for process in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            command = " ".join(process.info.get("cmdline") or []).lower()
            executable = str(process.info.get("name") or "").lower()
            if executable not in {"node", "node.exe"}:
                continue
            if "qwb-jawl" not in command:
                continue
            if not any(
                name in command
                for name in ("qwen-bridge-server.cjs", "qwb-cli.cjs")
            ):
                continue
            matches.append(process)
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
    return matches


def _select(message: str, choices: list) -> str | None:
    return questionary.select(
        message,
        choices=choices,
        style=get_custom_style(),
        qmark="",
        use_shortcuts=True,
        use_jk_keys=False,
        instruction="Use a shortcut or the arrow keys",
    ).ask()


def _logs_menu() -> None:
    while True:
        draw_header()
        log_choice = _select(
            "Activity & logs:",
            [
                _choice("Main · everything at once", "main", "m", "Combined runtime stream"),
                _choice("Agent · thoughts and actions", "agent", "a", "Primary ReAct activity"),
                _choice("Swarm · delegated work", "swarm", "s", "Subagent activity"),
                _choice("Tree of Thoughts", "tot", "t", "Alternative reasoning branches"),
                _choice(
                    "Subconscious",
                    "subconscious",
                    "u",
                    "Background cognitive patterns",
                ),
                questionary.Separator(" "),
                _choice(
                    "Clear top-level logs",
                    "clear",
                    "c",
                    "Destructive; confirmation required",
                ),
                _choice("Back", "back", "b", "Return to the control center"),
            ],
        )
        if log_choice in {None, "back"}:
            return
        if log_choice == "clear":
            from src.cli.screens.logs import LOG_DIR

            if not questionary.confirm(
                "Clear current top-level log streams?", default=False
            ).ask():
                continue
            cleared = 0
            failed = 0
            if LOG_DIR.exists():
                for log_file in LOG_DIR.glob("*.log*"):
                    try:
                        # Preserve active descriptors while clearing.
                        with open(log_file, "w", encoding="utf-8") as stream:
                            stream.truncate(0)
                        cleared += 1
                    except OSError:
                        failed += 1
            print_info(f" Cleared {cleared} log file(s); failed: {failed}.")
            time.sleep(1.5)
        else:
            launch_in_new_window(f"--logs-{log_choice}")


def _work_menu() -> None:
    while True:
        draw_header()
        choice = _select(
            "Work & autonomy:",
            [
                _choice("Durable Goals", "goals", "g", "Objectives, checkpoints and verification"),
                _choice(
                    "Runtime & Modes",
                    "runtime",
                    "r",
                    "Live ReAct, QWB and execution settings",
                ),
                _choice("Back", "back", "b", "Return to the control center"),
            ],
        )
        if choice in {None, "back"}:
            return
        if choice == "goals":
            goals_screen()
        elif choice == "runtime":
            runtime_screen()


def _tools_menu() -> None:
    while True:
        draw_header()
        choice = _select(
            "Tools & integrations:",
            [
                _choice(
                    "Debug Broker",
                    "debug_broker",
                    "d",
                    "Managed debugger providers and sessions",
                ),
                _choice(
                    "Database Manager",
                    "db_manager",
                    "b",
                    "Memory, state and database records",
                ),
                _choice(
                    "Interface configuration",
                    "setup",
                    "i",
                    "MCP, Telegram, desktop and media interfaces",
                ),
                _choice("Back", "back", "x", "Return to the control center"),
            ],
        )
        if choice in {None, "back"}:
            return
        if choice == "debug_broker":
            debug_broker_screen()
        elif choice == "db_manager":
            database_manager_screen()
        elif choice == "setup":
            setup_wizard_screen()


def _kill_orphan_bridges() -> None:
    draw_header()
    bridges = _bridge_processes()
    if bridges and not questionary.confirm(
        "Terminate the identified QWB bridge process(es)?",
        default=False,
    ).ask():
        return
    killed = 0
    for process in bridges:
        try:
            process.terminate()
            process.wait(timeout=5)
            killed += 1
        except psutil.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
            killed += 1
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
    if killed:
        print_info(f" Terminated {killed} identified QWB process(es).")
    else:
        print_info(" No identifiable QWB bridge processes found.")
    time.sleep(1.5)


def _system_menu() -> None:
    while True:
        draw_header()
        choice = _select(
            "Settings & system:",
            [
                _choice(
                    "LLM Providers",
                    "providers",
                    "p",
                    "Switch QWB or a standard OpenAI-compatible endpoint",
                ),
                _choice("Setup Wizard", "setup", "s", "Configure JAWL and its interfaces"),
                _choice(
                    "Runtime diagnostics",
                    "runtime",
                    "r",
                    "Inspect QWB, modes and account health",
                ),
                questionary.Separator(" "),
                _choice(
                    "Kill orphan QWB bridges",
                    "kill_bridges",
                    "k",
                    "Emergency bridge cleanup",
                ),
                _choice("Back", "back", "b", "Return to the control center"),
            ],
        )
        if choice in {None, "back"}:
            return
        if choice == "providers":
            provider_screen()
        elif choice == "setup":
            setup_wizard_screen()
        elif choice == "runtime":
            runtime_screen()
        elif choice == "kill_bridges":
            _kill_orphan_bridges()


def main_menu() -> None:

    while True:
        set_window_title("JAWL - Control Center")
        draw_header()
        try:
            snapshot = collect_dashboard_snapshot()
            render_dashboard(snapshot)
            primary = snapshot["agents"][0]
            running = primary["state"] == "running"
            primary_name = primary["display_name"]
            set_window_title(f"JAWL - {primary_name} Control Center")
        except Exception:
            snapshot = {"agents": []}
            running = False
            primary_name = "Primary agent"

        primary_action = "stop" if running else "start"
        primary_label = (
            f"Stop {primary_name}" if running else f"Start {primary_name}"
        )
        result = _select(
            "Control center:",
            [
                _choice(
                    primary_label,
                    primary_action,
                    "p",
                    "Primary agent lifecycle",
                ),
                _choice("Agents", "instances", "a", "Primary and named agent instances"),
                _choice("Chat", "terminal", "c", "Talk to the primary agent"),
                _choice("Work & Goals", "work", "g", "Progress, Goals and runtime modes"),
                _choice("Activity & Logs", "logs", "l", "Structured runtime streams"),
                _choice("Tools & Integrations", "tools", "t", "Debuggers, MCP and databases"),
                _choice("Settings & System", "system", "s", "Configuration and diagnostics"),
                questionary.Separator(" "),
                _choice("Exit", "exit", "x", "Close the control center"),
            ],
        )

        if result is None or result == "exit":
            draw_header()
            print_info(" Shutting down. Goodbye.")
            time.sleep(1)
            sys.exit(0)

        draw_header()

        if result == "start":
            start_agent_screen()

        elif result == "stop":
            stop_agent_screen()

        elif result == "terminal":
            terminal_chat_screen()

        elif result == "instances":
            instances_screen()

        elif result == "logs":
            _logs_menu()

        elif result == "work":
            _work_menu()

        elif result == "tools":
            _tools_menu()

        elif result == "system":
            _system_menu()
