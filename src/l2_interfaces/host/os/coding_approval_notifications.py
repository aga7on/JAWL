"""Opt-in desktop delivery for passive coding approval events."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from typing import Any, Dict

from src.l2_interfaces.host.os.coding_approvals import (
    format_coding_approval_notification,
)
from src.utils.event.bus import EventBus
from src.utils.event.registry import Events
from src.utils.logger import main_logger


class HostOSCodingApprovalNotifications:
    """Lifecycle-managed desktop notifier; it never decides an approval."""

    def __init__(self, event_bus: EventBus) -> None:
        self.bus = event_bus
        self._started = False

    @staticmethod
    def _show_notification(title: str, message: str) -> None:
        if sys.platform == "win32":
            safe_title = title.replace("'", "''")
            safe_message = message.replace("'", "''")
            script = (
                "[Reflection.Assembly]::LoadWithPartialName("
                "'System.Windows.Forms') | Out-Null;"
                "$n=New-Object System.Windows.Forms.NotifyIcon;"
                "$n.Icon=[System.Drawing.SystemIcons]::Information;"
                f"$n.BalloonTipTitle='{safe_title}';"
                f"$n.BalloonTipText='{safe_message}';"
                "$n.Visible=$true;$n.ShowBalloonTip(5000);"
                "Start-Sleep -Seconds 5;$n.Dispose();"
            )
            subprocess.run(
                ["powershell", "-WindowStyle", "Hidden", "-Command", script],
                check=False,
                timeout=10,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        if sys.platform == "darwin":
            subprocess.run(
                [
                    "osascript",
                    "-e",
                    "on run argv",
                    "-e",
                    "display notification item 2 of argv with title item 1 of argv",
                    "-e",
                    "end run",
                    "--",
                    title,
                    message,
                ],
                check=False,
                timeout=10,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        subprocess.run(
            ["notify-send", title, message],
            check=False,
            timeout=10,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    async def _on_requested(self, approval: Dict[str, Any], **_: Any) -> None:
        if not isinstance(approval, dict):
            return
        message = format_coding_approval_notification(approval, max_chars=1000)
        try:
            await asyncio.to_thread(
                self._show_notification,
                "JAWL approval required",
                message,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            main_logger.warning(
                "[Host OS] Coding approval desktop notification failed: "
                f"{type(exc).__name__}."
            )

    async def start(self) -> None:
        if self._started:
            return
        self.bus.subscribe(
            Events.CODING_APPROVAL_REQUESTED, self._on_requested
        )
        self._started = True
        main_logger.info("[Host OS] Coding approval desktop notifications enabled.")

    async def stop(self) -> None:
        if not self._started:
            return
        self.bus.unsubscribe(
            Events.CODING_APPROVAL_REQUESTED, self._on_requested
        )
        self._started = False

