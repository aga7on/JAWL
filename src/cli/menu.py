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


def main_menu() -> None:
    choices = [
        questionary.Choice("[>] Start Agent", "start"),
        questionary.Choice("[■] Stop Agent", "stop"),
        questionary.Choice("[=] Runtime & Modes", "runtime"),
        questionary.Choice("[G] Durable Goals", "goals"),
        questionary.Choice("[D] Debug Broker", "debug_broker"),
        questionary.Choice("[M] Multi-Instance Manager", "instances"),
        questionary.Choice("[@] Chat", "terminal"),
        questionary.Choice("[i] Logs", "logs"),
        questionary.Choice("[*] Setup Wizard", "setup"),
        questionary.Choice("[#] Database Manager", "db_manager"),
        questionary.Separator(" "),
        questionary.Choice("[!] Kill orphan bridges", "kill_bridges"),
        questionary.Choice("[x] Exit", "exit"),
    ]

    while True:
        set_window_title("JAWL - Main Menu")
        draw_header()

        result = questionary.select(
            "Welcome to JAWL. Choose an action:",
            choices=choices,
            style=get_custom_style(),
            qmark="",
            instruction="\n Use arrows ↑/↓ and Enter\n",
        ).ask()

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

        elif result == "runtime":
            runtime_screen()

        elif result == "goals":
            goals_screen()

        elif result == "debug_broker":
            debug_broker_screen()

        elif result == "instances":
            instances_screen()

        elif result == "logs":
            log_choice = questionary.select(
                "Select a log stream to view:",
                choices=[
                    questionary.Separator(" "),
                    questionary.Choice(" Main (everything at once)", "main"),
                    questionary.Choice(" Agent (main agent thoughts and actions)", "agent"),
                    questionary.Choice(" Swarm (subagents activity)", "swarm"),
                    questionary.Choice(" ToT (thoughts simulation tree)", "tot"),
                    questionary.Choice(
                        " Subconscious (background cognitive patterns)", "subconscious"
                    ),
                    questionary.Separator(" "),
                    questionary.Separator("--- Controls ---"),
                    questionary.Choice(" Clear all logs", "clear"),
                    questionary.Separator(" "),
                    questionary.Choice("↩ Back", "back"),
                ],
                instruction=" ",
            ).ask()

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
                            with open(log_file, "w", encoding="utf-8") as f:
                                f.truncate(0)
                            cleared += 1
                        except OSError:
                            failed += 1
                print_info(f" Cleared {cleared} log file(s); failed: {failed}.")
                time.sleep(1.5)
            elif log_choice and log_choice != "back":
                launch_in_new_window(f"--logs-{log_choice}")

        elif result == "setup":
            setup_wizard_screen()

        elif result == "kill_bridges":
            draw_header()
            bridges = _bridge_processes()
            if bridges and not questionary.confirm(
                "Terminate the identified QWB bridge process(es)?",
                default=False,
            ).ask():
                continue
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

        elif result == "db_manager":
            database_manager_screen()
