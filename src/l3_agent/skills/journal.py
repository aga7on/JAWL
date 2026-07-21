"""Durable JSONL journal for action-plan lifecycle events."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from src.utils._tools import redact_sensitive_text


class NullActionJournal:
    """No-op journal used before the system builder configures persistence."""

    session_id = "disabled"

    async def record(self, event: str, **payload: Any) -> None:
        return None

    async def recent_plans(
        self, limit: int = 20, state: Optional[str] = None, include_events: bool = False
    ) -> List[Dict[str, Any]]:
        return []


class ActionJournal:
    """Append-only, rotated action journal that never stores raw secret fields."""

    _SECRET_KEYS = (
        "api_key",
        "apikey",
        "authorization",
        "cookie",
        "password",
        "secret",
        "session_token",
        "token",
    )

    def __init__(self, path: Path, max_bytes: int = 5 * 1024 * 1024) -> None:
        if max_bytes < 1024:
            raise ValueError("max_bytes must be at least 1024")
        self.path = Path(path)
        self.rotated_path = self.path.with_suffix(self.path.suffix + ".1")
        self.max_bytes = max_bytes
        self.session_id = uuid.uuid4().hex
        self._lock = asyncio.Lock()

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @classmethod
    def _redact_text(cls, value: str, max_chars: int = 4000) -> str:
        return redact_sensitive_text(value, max_chars=max_chars)

    @classmethod
    def _sanitize(cls, value: Any, key: str = "") -> Any:
        lowered_key = key.lower()
        if any(marker in lowered_key for marker in cls._SECRET_KEYS):
            return "[REDACTED]"
        if isinstance(value, Mapping):
            return {
                str(child_key): cls._sanitize(child_value, str(child_key))
                for child_key, child_value in value.items()
            }
        if isinstance(value, (list, tuple, set)):
            return [cls._sanitize(item) for item in value]
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, str):
            return cls._redact_text(value)
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return cls._redact_text(str(value))

    def _append_sync(self, line: bytes) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        current_size = self.path.stat().st_size if self.path.exists() else 0
        if current_size and current_size + len(line) > self.max_bytes:
            self.rotated_path.unlink(missing_ok=True)
            os.replace(self.path, self.rotated_path)
        with open(self.path, "ab") as stream:
            stream.write(line)
            stream.flush()
            os.fsync(stream.fileno())

    async def record(self, event: str, **payload: Any) -> None:
        entry = {
            "version": 1,
            "timestamp": self._utc_now(),
            "session_id": self.session_id,
            "event": event,
            **self._sanitize(payload),
        }
        line = (
            json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        async with self._lock:
            await asyncio.to_thread(self._append_sync, line)

    def _read_events_sync(self) -> List[Dict[str, Any]]:
        events: List[Dict[str, Any]] = []
        for path in (self.rotated_path, self.path):
            if not path.is_file():
                continue
            with open(path, "r", encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(entry, dict) and entry.get("plan_id"):
                        events.append(entry)
        return events

    async def recent_plans(
        self,
        limit: int = 20,
        state: Optional[str] = None,
        include_events: bool = False,
    ) -> List[Dict[str, Any]]:
        if limit < 1 or limit > 200:
            raise ValueError("limit must be between 1 and 200")
        async with self._lock:
            events = await asyncio.to_thread(self._read_events_sync)

        grouped: Dict[str, List[Dict[str, Any]]] = {}
        order: List[str] = []
        for event in events:
            plan_id = str(event["plan_id"])
            if plan_id not in grouped:
                grouped[plan_id] = []
                order.append(plan_id)
            grouped[plan_id].append(event)

        plans: List[Dict[str, Any]] = []
        for plan_id in reversed(order):
            plan_events = grouped[plan_id]
            started = next(
                (event for event in plan_events if event.get("event") == "plan_started"),
                plan_events[0],
            )
            finished = next(
                (
                    event
                    for event in reversed(plan_events)
                    if event.get("event") == "plan_finished"
                ),
                None,
            )
            if finished:
                plan_state = finished.get("state", "completed")
            elif started.get("session_id") == self.session_id:
                plan_state = "in_progress"
            else:
                plan_state = "interrupted"
            if state and plan_state != state:
                continue

            summary: Dict[str, Any] = {
                "plan_id": plan_id,
                "session_id": started.get("session_id"),
                "started_at": started.get("timestamp"),
                "finished_at": finished.get("timestamp") if finished else None,
                "state": plan_state,
                "action_count": len(started.get("actions", [])),
                "last_event": plan_events[-1].get("event"),
            }
            if finished:
                summary["outcomes"] = finished.get("outcomes", [])
            if include_events:
                summary["events"] = plan_events
            plans.append(summary)
            if len(plans) >= limit:
                break
        return plans
