"""Operator dashboard for native debugger and reverse-engineering sessions."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import questionary
from rich.table import Table

from src.cli.control_client import request_control
from src.cli.screens.agent_control import _is_agent_running
from src.cli.widgets.ui import (
    console,
    draw_header,
    get_custom_style,
    print_error,
    print_info,
    set_window_title,
    wait_for_enter,
)
from src.cli.widgets.yaml_editor import YamlEditor
from src.l2_interfaces.debug_broker.client import DebugBrokerClient
from src.utils.settings import load_config


ROOT_DIR = Path(__file__).resolve().parents[3]


def _offline_client() -> DebugBrokerClient:
    _, interfaces = load_config()
    return DebugBrokerClient(interfaces.debug_broker, ROOT_DIR)


def _snapshot() -> dict[str, Any]:
    if _is_agent_running():
        return request_control("debug.get", timeout=5)
    return _offline_client().session_snapshot()


def _render(snapshot: dict[str, Any]) -> None:
    providers = snapshot.get("providers") or []
    table = Table(title="Debug Broker providers")
    table.add_column("Provider", style="bold cyan")
    table.add_column("Available")
    table.add_column("Active")
    table.add_column("Ops", justify="right")
    table.add_column("Lifecycle")
    table.add_column("Resolved paths", overflow="fold")
    for row in providers:
        table.add_row(
            str(row.get("provider", "")),
            "yes" if row.get("available") else "no",
            "yes" if row.get("active") else "no",
            str(row.get("operations", 0)),
            "auto" if row.get("auto_start") else "manual",
            "\n".join(str(item) for item in row.get("paths") or []) or "—",
        )
    console.print(table)

    sessions = snapshot.get("sessions") or []
    if sessions:
        active = Table(title="Durable debug sessions")
        active.add_column("ID", style="cyan")
        active.add_column("Provider")
        active.add_column("State")
        active.add_column("PID", justify="right")
        active.add_column("Target", overflow="fold")
        for row in sessions:
            active.add_row(
                str(row.get("session_id", "")),
                str(row.get("provider", "")),
                str(row.get("status", "")),
                str(row.get("provider_pid") or "—"),
                str(row.get("target") or "—"),
            )
        console.print(active)


def _start_session(snapshot: dict[str, Any]) -> None:
    if not _is_agent_running():
        print_error("Start JAWL before creating a managed debug session.")
        wait_for_enter()
        return
    available = [
        str(item.get("provider"))
        for item in snapshot.get("providers") or []
        if item.get("available")
    ]
    provider = questionary.select(
        "Provider:",
        choices=available,
        style=get_custom_style(),
        qmark="",
    ).ask()
    if not provider:
        return
    target = questionary.text(
        "Target path/name/PID (leave empty when optional):"
    ).ask()
    raw_options = questionary.text(
        'Session options as JSON (for example {"arguments":["loop"]}):',
        default="{}",
    ).ask()
    try:
        options = json.loads(raw_options or "{}")
        if not isinstance(options, dict):
            raise ValueError("options must be a JSON object")
        result = request_control(
            "debug.start",
            {
                "provider": provider,
                "target": (target or "").strip(),
                "options": options,
            },
            timeout=45,
        )
        print_info(
            f"Started {result.get('provider')} session "
            f"{result.get('session_id')} ({result.get('status')})."
        )
    except (ConnectionError, RuntimeError, TimeoutError, ValueError) as exc:
        print_error(str(exc))
    wait_for_enter()


def _stop_session(snapshot: dict[str, Any]) -> None:
    if not _is_agent_running():
        print_error("JAWL is not running; no provider ownership can be released.")
        wait_for_enter()
        return
    choices = [
        questionary.Choice(
            f"{row.get('session_id')} · {row.get('provider')} · "
            f"{row.get('status')} · {row.get('target') or '—'}",
            str(row.get("session_id")),
        )
        for row in snapshot.get("sessions") or []
        if row.get("status") not in {"closed", "failed", "interrupted"}
    ]
    if not choices:
        print_info("No active debug sessions.")
        wait_for_enter()
        return
    session_id = questionary.select(
        "Session to close:", choices=choices, qmark=""
    ).ask()
    if not session_id:
        return
    if not questionary.confirm(
        "Close this session and its broker-owned processes?", default=False
    ).ask():
        return
    try:
        result = request_control(
            "debug.stop", {"session_id": session_id}, timeout=15
        )
        print_info(f"Session {result.get('session_id')} is closed.")
    except (ConnectionError, RuntimeError, TimeoutError, ValueError) as exc:
        print_error(str(exc))
    wait_for_enter()


def _search_operations() -> None:
    query = questionary.text("Search operation catalog:").ask()
    if query is None:
        return
    try:
        if _is_agent_running():
            result = request_control(
                "debug.search", {"query": query, "limit": 30}, timeout=5
            )
        else:
            result = _offline_client().search_operations(query, None, 30)
        table = Table(title=f"Operations matching {query!r}")
        table.add_column("Operation", style="cyan")
        table.add_column("Session")
        table.add_column("Mutates")
        table.add_column("Schema SHA-256")
        table.add_column("Description", overflow="fold")
        for item in result.get("operations") or []:
            table.add_row(
                f"{item.get('provider')}.{item.get('operation')}",
                "yes" if item.get("session_required") else "no",
                "yes" if item.get("mutating") else "no",
                str(item.get("schema_sha256", ""))[:12],
                str(item.get("description", "")),
            )
        console.print(table)
    except (ConnectionError, RuntimeError, TimeoutError, ValueError) as exc:
        print_error(str(exc))
    wait_for_enter()


def _run_live_self_test() -> None:
    test_file = (
        ROOT_DIR
        / "tests"
        / "integration"
        / "src"
        / "l2"
        / "debug_broker"
        / "test_live_debug_broker.py"
    )
    python = ROOT_DIR / "venv" / "Scripts" / "python.exe"
    executable = python if python.is_file() else Path(sys.executable)
    environment = os.environ.copy()
    environment["JAWL_DEBUG_BROKER_LIVE"] = "1"
    console.print(
        "[cyan]Running the installed seven-provider live matrix. "
        "A temporary x64dbg window is expected.[/cyan]"
    )
    result = subprocess.run(
        [str(executable), "-m", "pytest", str(test_file), "-q"],
        cwd=str(ROOT_DIR),
        env=environment,
        check=False,
    )
    if result.returncode == 0:
        print_info("Debug Broker live matrix passed.")
    else:
        print_error(f"Debug Broker live matrix failed with code {result.returncode}.")
    wait_for_enter()


def debug_broker_screen() -> None:
    set_window_title("JAWL - Debug Broker")
    while True:
        draw_header()
        try:
            snapshot = _snapshot()
            _render(snapshot)
        except (ConnectionError, RuntimeError, TimeoutError, ValueError) as exc:
            snapshot = {"providers": [], "sessions": []}
            print_error(str(exc))

        choice = questionary.select(
            "Debug Broker controls:",
            choices=[
                questionary.Choice("[r] Refresh", "refresh"),
                questionary.Choice("[+] Start managed session", "start"),
                questionary.Choice("[-] Stop managed session", "stop"),
                questionary.Choice("[?] Search operation catalog", "search"),
                questionary.Choice("[T] Run seven-provider live self-test", "test"),
                questionary.Choice("[*] Configure interfaces.yaml", "config"),
                questionary.Choice("← Back", "back"),
            ],
            style=get_custom_style(),
            qmark="",
        ).ask()
        if choice in {None, "back"}:
            return
        if choice == "start":
            _start_session(snapshot)
        elif choice == "stop":
            _stop_session(snapshot)
        elif choice == "search":
            _search_operations()
        elif choice == "test":
            _run_live_self_test()
        elif choice == "config":
            if _is_agent_running():
                print_error("Stop JAWL before changing interfaces.yaml.")
                wait_for_enter()
            else:
                YamlEditor(
                    ROOT_DIR / "config" / "interfaces.yaml",
                    title="Debug Broker · interfaces.yaml",
                ).run()
