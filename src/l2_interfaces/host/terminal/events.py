"""
Terminal events orchestrator.

Acts as a bridge (Consumer) between the internal async queue of the TCP server
and the global EventBus of the agent.
"""

import asyncio
from typing import Any
from src.utils.logger import main_logger
from src.utils.event.bus import EventBus
from src.utils.event.registry import Events
from src.l2_interfaces.host.terminal.client import HostTerminalClient


class HostTerminalEvents:
    """Background worker: forwards messages and connection events from the socket queue to EventBus."""

    def __init__(self, client: HostTerminalClient, event_bus: EventBus):
        self.client = client
        self.bus = event_bus
        self._task = None
        self._is_running = False

    async def start(self):
        if self._is_running:
            return
        self._is_running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self):
        self._is_running = False
        if self._task:
            self._task.cancel()
            self._task = None

    @staticmethod
    def _format_status(status: dict[str, Any]) -> str:
        agent = status.get("agent") or {}
        goal = status.get("goal") or {}
        heartbeat = status.get("heartbeat") or {}
        lines = [
            "### JAWL runtime status",
            (
                f"Agent: {agent.get('state', 'unknown')} · "
                f"step {agent.get('step', 0)}/{agent.get('max_steps', 0)} · "
                f"model {agent.get('model', 'unknown')}"
            ),
        ]
        if goal:
            ledger = goal.get("task_ledger") or {}
            lines.extend(
                [
                    (
                        f"Goal: {goal.get('status', 'unknown')} · "
                        f"{goal.get('last_cycle_status', '')}"
                    ),
                    f"Phase: {ledger.get('current_phase', '—')}",
                    f"Next: {ledger.get('next_action') or '—'}",
                ]
            )
        lines.append(
            "Active cycle: "
            + ("yes" if heartbeat.get("active_cycle") else "no")
            + (" · status request did not interrupt it" if heartbeat.get("active_cycle") else "")
        )
        return "\n".join(lines)

    async def _handle_local_command(self, payload: str) -> bool:
        """Serve exact slash commands without steering the active ReAct cycle."""

        command = str(payload or "").strip().casefold()
        if command not in {"/status", "/goal", "/goal status"}:
            return False
        if self.client.control_handler is None:
            await self.client.broadcast_message("Runtime controls are unavailable.")
            return True
        try:
            status = await self.client.control_handler("status.get", {})
            await self.client.broadcast_message(self._format_status(status))
        except Exception as exc:
            await self.client.broadcast_message(
                f"Unable to read runtime status: {str(exc)[:500]}"
            )
        return True

    async def _loop(self):
        while self._is_running:
            try:
                # Await data from the TCP server
                action, payload = await self.client.incoming_queue.get()

                # Publish the corresponding event
                if action == "_CONNECTION_OPENED":
                    await self.bus.publish(
                        Events.HOST_TERMINAL_OPENED,
                        message="Chat terminal opened.",
                    )
                elif action == "_CONNECTION_CLOSED":
                    await self.bus.publish(
                        Events.HOST_TERMINAL_CLOSED,
                        message="Chat terminal closed.",
                    )
                elif action == "_MESSAGE":
                    if await self._handle_local_command(payload):
                        continue
                    await self.bus.publish(
                        Events.HOST_TERMINAL_MESSAGE, sender_name="User", message=payload
                    )

            except asyncio.CancelledError:
                break
            except Exception as e:
                main_logger.error(f"[Host OS] Error processing terminal: {e}")
