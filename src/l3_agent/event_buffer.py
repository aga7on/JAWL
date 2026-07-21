"""Bounded, priority-aware buffering for Heartbeat and ReAct events."""

from __future__ import annotations

import copy
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Set

from src.utils.event.registry import EventLevel


@dataclass(frozen=True)
class EventBufferOutcome:
    """Result of one append without exposing payloads in diagnostics."""

    event: Optional[Dict[str, Any]]
    added: bool = False
    coalesced: bool = False
    dropped: bool = False
    evicted_name: Optional[str] = None


class BoundedEventBuffer:
    """Retain high-priority intent, coalesce only explicit noisy event types."""

    _MAX_DROP_KEYS = 100

    def __init__(
        self,
        capacity: int = 100,
        coalesce_window_sec: float = 2.0,
        coalesce_names: Optional[Iterable[str]] = None,
        payload_sample_limit: int = 3,
    ) -> None:
        if capacity < 1:
            raise ValueError("Event buffer capacity must be positive.")
        if coalesce_window_sec < 0:
            raise ValueError("Event coalesce window cannot be negative.")
        if payload_sample_limit < 1:
            raise ValueError("Event payload sample limit must be positive.")
        self.capacity = capacity
        self.coalesce_window_sec = coalesce_window_sec
        self.coalesce_names: Set[str] = {
            str(name) for name in (coalesce_names or []) if str(name)
        }
        self.payload_sample_limit = payload_sample_limit
        self._items: List[Dict[str, Any]] = []
        self._received_at: Dict[int, float] = {}
        self._dropped: Counter[str] = Counter()

    @property
    def items(self) -> List[Dict[str, Any]]:
        """Return the live bounded list for legacy read-only compatibility."""

        return self._items

    @staticmethod
    def _priority(event: Dict[str, Any]) -> int:
        level = event.get("level", "INFO")
        if isinstance(level, EventLevel):
            return int(level.value)
        if isinstance(level, int):
            return int(level)
        try:
            return int(EventLevel[str(level)].value)
        except (KeyError, TypeError, ValueError):
            return int(EventLevel.INFO.value)

    @staticmethod
    def _drop_key(event: Dict[str, Any]) -> str:
        level = str(event.get("level", "INFO"))[:32]
        name = str(event.get("name", "UNKNOWN"))[:256]
        return f"{level}:{name}"

    def _record_drop_count(self, key: str, count: int = 1) -> None:
        bounded_key = str(key)[:289]
        if bounded_key not in self._dropped and len(self._dropped) >= self._MAX_DROP_KEYS:
            bounded_key = "OTHER:OTHER"
        self._dropped[bounded_key] += max(0, int(count))

    def _record_drop(self, event: Dict[str, Any]) -> None:
        self._record_drop_count(self._drop_key(event))

    def _remove_at(self, index: int) -> Dict[str, Any]:
        removed = self._items.pop(index)
        self._received_at.pop(id(removed), None)
        return removed

    def _coalesce(
        self, event: Dict[str, Any], received_at: float
    ) -> Optional[Dict[str, Any]]:
        name = str(event.get("name", ""))
        if name not in self.coalesce_names:
            return None
        for existing in reversed(self._items):
            if existing.get("name") != name:
                continue
            previous_time = self._received_at.get(id(existing), 0.0)
            if received_at - previous_time > self.coalesce_window_sec:
                return None
            count = int(existing.get("coalesced_count", 1)) + 1
            samples = existing.setdefault(
                "payload_samples", [copy.deepcopy(existing.get("payload", {}))]
            )
            if len(samples) < self.payload_sample_limit:
                samples.append(copy.deepcopy(event.get("payload", {})))
            else:
                existing["payload_samples_omitted"] = int(
                    existing.get("payload_samples_omitted", 0)
                ) + 1
            existing["coalesced_count"] = count
            existing["last_time"] = event.get("time")
            existing["payload"] = copy.deepcopy(event.get("payload", {}))
            if self._priority(event) > self._priority(existing):
                existing["level"] = event.get("level", existing.get("level"))
            self._received_at[id(existing)] = received_at
            return existing
        return None

    def append(
        self, event: Dict[str, Any], received_at: Optional[float] = None
    ) -> EventBufferOutcome:
        """Append, explicitly coalesce, or priority-evict one event."""

        if not isinstance(event, dict):
            raise ValueError("Buffered event must be a dictionary.")
        now = time.monotonic() if received_at is None else float(received_at)
        stored = copy.deepcopy(event)
        if stored.get("name") == "EVENT_QUEUE_OVERFLOW":
            payload = stored.get("payload", {})
            counts = payload.get("dropped_by_level_and_name", {})
            if isinstance(counts, dict):
                for key, count in counts.items():
                    try:
                        normalized_count = max(0, int(count))
                    except (TypeError, ValueError):
                        continue
                    self._record_drop_count(str(key), normalized_count)
            return EventBufferOutcome(event=self._overflow_event(), coalesced=True)
        existing = self._coalesce(stored, now)
        if existing is not None:
            return EventBufferOutcome(event=existing, coalesced=True)

        evicted_name = None
        if len(self._items) >= self.capacity:
            priorities = [self._priority(item) for item in self._items]
            minimum = min(priorities)
            if self._priority(stored) < minimum:
                self._record_drop(stored)
                return EventBufferOutcome(event=None, dropped=True)
            evict_index = priorities.index(minimum)
            evicted = self._remove_at(evict_index)
            self._record_drop(evicted)
            evicted_name = str(evicted.get("name", "UNKNOWN"))

        self._items.append(stored)
        self._received_at[id(stored)] = now
        return EventBufferOutcome(
            event=stored,
            added=True,
            evicted_name=evicted_name,
        )

    def extend(self, events: Iterable[Dict[str, Any]]) -> None:
        for event in events:
            self.append(event)

    def _overflow_event(self) -> Optional[Dict[str, Any]]:
        if not self._dropped:
            return None
        return {
            "time": None,
            "level": EventLevel.INFO.name,
            "name": "EVENT_QUEUE_OVERFLOW",
            "payload": {
                "dropped_total": sum(self._dropped.values()),
                "dropped_by_level_and_name": dict(sorted(self._dropped.items())),
                "message": (
                    "The bounded event queue retained higher-priority/newer intent; "
                    "consult the source interface history for omitted payloads."
                ),
            },
        }

    def view(self) -> List[Dict[str, Any]]:
        """Return a defensive view plus a visible overflow summary."""

        events = copy.deepcopy(self._items)
        overflow = self._overflow_event()
        if overflow is not None:
            events.append(overflow)
        return events

    def drain(self) -> List[Dict[str, Any]]:
        events = self.view()
        self.clear()
        return events

    def clear(self) -> None:
        self._items.clear()
        self._received_at.clear()
        self._dropped.clear()

    def snapshot(self) -> Dict[str, Any]:
        names = Counter(str(item.get("name", "UNKNOWN")) for item in self._items)
        levels = Counter(str(item.get("level", "INFO")) for item in self._items)
        return {
            "size": len(self._items),
            "capacity": self.capacity,
            "names": dict(sorted(names.items())),
            "levels": dict(sorted(levels.items())),
            "coalesced_total": sum(
                max(0, int(item.get("coalesced_count", 1)) - 1)
                for item in self._items
            ),
            "dropped_total": sum(self._dropped.values()),
            "dropped_by_level_and_name": dict(sorted(self._dropped.items())),
        }
