"""Bounded Windows UI Automation observe/act/verify broker."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Any, Optional

from src.utils._tools import redact_sensitive_text, truncate_text


class DesktopAutomationError(RuntimeError):
    """Safe model-facing desktop automation failure."""


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _rectangle(value: Any) -> dict[str, int]:
    return {
        "left": int(value.left),
        "top": int(value.top),
        "right": int(value.right),
        "bottom": int(value.bottom),
    }


@dataclass(frozen=True)
class _ElementLocator:
    window_handle: int
    runtime_id: tuple[int, ...]
    fingerprint_sha256: str


class WindowsDesktopAutomation:
    """Use Windows UIA without persisting screen or control contents."""

    ACTIONS = {
        "invoke",
        "click",
        "focus",
        "set_value",
        "toggle",
        "select",
        "expand",
        "collapse",
    }

    def __init__(
        self,
        *,
        max_windows: int = 20,
        max_elements: int = 250,
        max_text_chars: int = 500,
        max_result_chars: int = 60000,
    ) -> None:
        self.max_windows = max_windows
        self.max_elements = max_elements
        self.max_text_chars = max_text_chars
        self.max_result_chars = max_result_chars
        self._snapshots: OrderedDict[str, dict[str, _ElementLocator]] = OrderedDict()
        self._lock = threading.RLock()

    @staticmethod
    def _desktop():
        try:
            from pywinauto import Desktop
        except ImportError as exc:
            raise DesktopAutomationError(
                "Semantic Windows automation requires pywinauto."
            ) from exc
        return Desktop(backend="uia")

    def _safe_text(self, value: Any, *, limit: Optional[int] = None) -> str:
        return redact_sensitive_text(
            truncate_text(str(value or ""), max_chars=limit or self.max_text_chars)
        )

    @staticmethod
    def _runtime_id(wrapper: Any) -> tuple[int, ...]:
        value = getattr(wrapper.element_info, "runtime_id", None)
        if value is None:
            handle = int(getattr(wrapper, "handle", 0) or 0)
            return (handle,)
        return tuple(int(part) for part in value)

    def _element_projection(self, wrapper: Any) -> dict[str, Any]:
        info = wrapper.element_info
        try:
            is_password = bool(getattr(info, "is_password", False))
        except Exception:
            is_password = False
        projection: dict[str, Any] = {
            "name": self._safe_text(getattr(info, "name", "")),
            "automation_id": self._safe_text(
                getattr(info, "automation_id", ""), limit=256
            ),
            "control_type": self._safe_text(
                getattr(info, "control_type", ""), limit=128
            ),
            "class_name": self._safe_text(
                getattr(info, "class_name", ""), limit=256
            ),
            "rectangle": _rectangle(info.rectangle),
            "enabled": bool(wrapper.is_enabled()),
            "visible": bool(wrapper.is_visible()),
            "password": is_password,
        }
        try:
            projection["focused"] = bool(wrapper.has_keyboard_focus())
        except Exception:
            projection["focused"] = False
        try:
            value = wrapper.get_value()
        except Exception:
            value = None
        if value not in (None, ""):
            projection["value"] = self._safe_text(value)
        try:
            projection["selected"] = bool(wrapper.is_selected())
        except Exception:
            pass
        try:
            projection["toggle_state"] = int(wrapper.get_toggle_state())
        except Exception:
            pass
        try:
            projection["expand_state"] = int(wrapper.get_expand_state())
        except Exception:
            pass
        return projection

    def _fingerprint(self, wrapper: Any) -> str:
        return _canonical_hash(self._element_projection(wrapper))

    def _bounded_descendants(
        self, window: Any, *, max_depth: int, max_elements: int
    ) -> tuple[list[tuple[Any, int]], bool]:
        pending = deque((child, 1) for child in window.children())
        found: list[tuple[Any, int]] = []
        truncated = False
        while pending:
            wrapper, depth = pending.popleft()
            if len(found) >= max_elements:
                truncated = True
                break
            found.append((wrapper, depth))
            if depth < max_depth:
                try:
                    pending.extend((child, depth + 1) for child in wrapper.children())
                except Exception:
                    continue
        return found, truncated

    def observe(
        self,
        *,
        window_title: str = "",
        max_depth: int = 6,
        max_elements: Optional[int] = None,
    ) -> dict[str, Any]:
        with self._lock:
            return self._observe_locked(
                window_title=window_title,
                max_depth=max_depth,
                max_elements=max_elements,
            )

    def _observe_locked(
        self,
        *,
        window_title: str,
        max_depth: int,
        max_elements: Optional[int],
    ) -> dict[str, Any]:
        if max_depth < 1 or max_depth > 12:
            raise DesktopAutomationError("max_depth must be between 1 and 12.")
        element_limit = self.max_elements if max_elements is None else max_elements
        if element_limit < 1 or element_limit > self.max_elements:
            raise DesktopAutomationError(
                f"max_elements must be between 1 and {self.max_elements}."
            )
        title_query = window_title.strip().casefold()
        try:
            from win32gui import GetForegroundWindow

            foreground = int(GetForegroundWindow())
            all_candidates = self._desktop().windows(visible_only=True)
        except Exception as exc:
            raise DesktopAutomationError(
                f"Windows UI Automation observation failed: {exc}"
            ) from exc

        candidates = []
        for window in all_candidates:
            try:
                if not title_query or title_query in window.window_text().casefold():
                    candidates.append(window)
            except Exception:
                continue
        windows_truncated = len(candidates) > self.max_windows
        candidates = candidates[: self.max_windows]
        snapshot_seed = {
            "time_ns": time.time_ns(),
            "handles": [int(window.handle) for window in candidates],
        }
        snapshot_id = _canonical_hash(snapshot_seed)[:16]
        locators: dict[str, _ElementLocator] = {}
        windows: list[dict[str, Any]] = []
        remaining = element_limit
        overall_truncated = False
        for window in candidates:
            try:
                window_data: dict[str, Any] = {
                    "window_ref": f"window:{int(window.handle):x}",
                    "handle": int(window.handle),
                    "title": self._safe_text(window.window_text(), limit=1000),
                    "foreground": int(window.handle) == foreground,
                    "rectangle": _rectangle(window.rectangle()),
                    "elements": [],
                }
            except Exception:
                continue
            try:
                window_data["process_id"] = int(window.element_info.process_id)
            except Exception:
                pass
            descendants, truncated = self._bounded_descendants(
                window, max_depth=max_depth, max_elements=remaining
            )
            overall_truncated = overall_truncated or truncated
            for wrapper, depth in descendants:
                try:
                    projection = self._element_projection(wrapper)
                    runtime_id = self._runtime_id(wrapper)
                except Exception:
                    continue
                fingerprint = _canonical_hash(projection)
                ref_seed = {
                    "snapshot": snapshot_id,
                    "window": int(window.handle),
                    "runtime_id": runtime_id,
                    "index": len(locators),
                }
                element_ref = "element:" + _canonical_hash(ref_seed)[:24]
                projection.update(
                    {
                        "element_ref": element_ref,
                        "element_sha256": fingerprint,
                        "depth": depth,
                    }
                )
                locators[element_ref] = _ElementLocator(
                    window_handle=int(window.handle),
                    runtime_id=runtime_id,
                    fingerprint_sha256=fingerprint,
                )
                window_data["elements"].append(projection)
            remaining -= len(descendants)
            windows.append(window_data)
            if remaining <= 0:
                overall_truncated = True
                break

        projection = {
            "backend": "windows_uia",
            "snapshot_id": snapshot_id,
            "window_count": len(windows),
            "element_count": len(locators),
            "truncated": overall_truncated or windows_truncated,
            "windows": windows,
        }
        projection["snapshot_sha256"] = _canonical_hash(projection)
        self._snapshots[snapshot_id] = locators
        self._snapshots.move_to_end(snapshot_id)
        while len(self._snapshots) > 5:
            self._snapshots.popitem(last=False)
        projection["returned_element_count"] = len(locators)
        while len(json.dumps(projection, ensure_ascii=False)) > self.max_result_chars:
            target = next(
                (
                    window
                    for window in reversed(projection["windows"])
                    if window["elements"]
                ),
                None,
            )
            if target is None:
                break
            target["elements"].pop()
            projection["returned_element_count"] -= 1
            projection["truncated"] = True
        return projection

    def _resolve(self, element_ref: str) -> tuple[Any, _ElementLocator]:
        locator = next(
            (
                snapshot[element_ref]
                for snapshot in reversed(self._snapshots.values())
                if element_ref in snapshot
            ),
            None,
        )
        if locator is None:
            raise DesktopAutomationError(
                "Unknown or expired element_ref; observe the desktop again."
            )
        try:
            window = self._desktop().window(handle=locator.window_handle).wrapper_object()
            if self._runtime_id(window) == locator.runtime_id:
                return window, locator
            descendants, _ = self._bounded_descendants(
                window, max_depth=12, max_elements=self.max_elements
            )
            for wrapper, _ in descendants:
                if self._runtime_id(wrapper) == locator.runtime_id:
                    return wrapper, locator
        except Exception as exc:
            raise DesktopAutomationError(
                "The observed UI element is no longer available."
            ) from exc
        raise DesktopAutomationError("The observed UI element is no longer available.")

    @staticmethod
    def _invoke_action(wrapper: Any, action: str, value: Optional[str]) -> None:
        if action == "invoke":
            wrapper.invoke()
        elif action == "click":
            wrapper.click_input()
        elif action == "focus":
            wrapper.set_focus()
        elif action == "set_value":
            if value is None:
                raise DesktopAutomationError("set_value requires value.")
            if hasattr(wrapper, "set_edit_text"):
                wrapper.set_edit_text(value)
            elif hasattr(wrapper, "set_value"):
                wrapper.set_value(value)
            else:
                raise DesktopAutomationError(
                    "This control does not expose a writable Value pattern."
                )
        elif action == "toggle":
            wrapper.toggle()
        elif action == "select":
            wrapper.select()
        elif action == "expand":
            wrapper.expand()
        elif action == "collapse":
            wrapper.collapse()
        else:
            raise DesktopAutomationError(f"Unsupported desktop action '{action}'.")

    def act(
        self,
        *,
        element_ref: str,
        expected_element_sha256: str,
        action: str,
        value: Optional[str] = None,
        settle_sec: float = 0.5,
    ) -> dict[str, Any]:
        with self._lock:
            return self._act_locked(
                element_ref=element_ref,
                expected_element_sha256=expected_element_sha256,
                action=action,
                value=value,
                settle_sec=settle_sec,
            )

    def _act_locked(
        self,
        *,
        element_ref: str,
        expected_element_sha256: str,
        action: str,
        value: Optional[str],
        settle_sec: float,
    ) -> dict[str, Any]:
        if action not in self.ACTIONS:
            raise DesktopAutomationError(
                "action must be one of: " + ", ".join(sorted(self.ACTIONS))
            )
        if len(value or "") > 20000:
            raise DesktopAutomationError("Desktop action value exceeds 20000 characters.")
        if not re.fullmatch(r"[0-9a-f]{64}", expected_element_sha256):
            raise DesktopAutomationError(
                "expected_element_sha256 must be a lowercase SHA-256 hash."
            )
        if settle_sec < 0 or settle_sec > 10:
            raise DesktopAutomationError("settle_sec must be between 0 and 10.")
        wrapper, locator = self._resolve(element_ref)
        before = self._element_projection(wrapper)
        before_hash = _canonical_hash(before)
        if expected_element_sha256 != locator.fingerprint_sha256:
            raise DesktopAutomationError(
                "The supplied element hash does not match the observation."
            )
        if before_hash != expected_element_sha256:
            raise DesktopAutomationError(
                "The UI element changed after observation; observe again before acting."
            )
        if action == "set_value" and before.get("password"):
            raise DesktopAutomationError(
                "Writing password controls through desktop automation is prohibited."
            )
        try:
            self._invoke_action(wrapper, action, value)
        except DesktopAutomationError:
            raise
        except Exception as exc:
            raise DesktopAutomationError(f"Desktop action failed: {exc}") from exc
        if settle_sec:
            time.sleep(settle_sec)

        try:
            after = self._element_projection(wrapper)
            after_hash: Optional[str] = _canonical_hash(after)
            changed = after_hash != before_hash
            target_exists = True
        except Exception:
            after = None
            after_hash = None
            changed = True
            target_exists = False
        verified = changed
        if action == "focus" and after is not None:
            verified = bool(after.get("focused"))
        elif action == "set_value" and after is not None:
            verified = after.get("value") == self._safe_text(value)
        elif action == "select" and after is not None:
            verified = bool(after.get("selected"))
        elif action in {"invoke", "click"}:
            verified = changed
        return {
            "action": action,
            "dispatched": True,
            "verified": verified,
            "verification": (
                "target_state_changed"
                if verified
                else "no_semantic_postcondition_observed"
            ),
            "target_exists": target_exists,
            "before_sha256": before_hash,
            "after_sha256": after_hash,
            "after": after,
            "next_step": (
                None
                if verified
                else "Observe or wait for an explicit postcondition before continuing."
            ),
        }

    def wait_for_element(
        self,
        *,
        window_title: str = "",
        name: str = "",
        automation_id: str = "",
        control_type: str = "",
        expected_exists: bool = True,
        timeout_sec: float = 10,
        poll_interval_sec: float = 0.25,
    ) -> dict[str, Any]:
        with self._lock:
            return self._wait_for_element_locked(
                window_title=window_title,
                name=name,
                automation_id=automation_id,
                control_type=control_type,
                expected_exists=expected_exists,
                timeout_sec=timeout_sec,
                poll_interval_sec=poll_interval_sec,
            )

    def _wait_for_element_locked(
        self,
        *,
        window_title: str,
        name: str,
        automation_id: str,
        control_type: str,
        expected_exists: bool,
        timeout_sec: float,
        poll_interval_sec: float,
    ) -> dict[str, Any]:
        if timeout_sec <= 0 or timeout_sec > 60:
            raise DesktopAutomationError("timeout_sec must be between 0 and 60.")
        if poll_interval_sec < 0.1 or poll_interval_sec > 5:
            raise DesktopAutomationError(
                "poll_interval_sec must be between 0.1 and 5."
            )
        if not any((window_title, name, automation_id, control_type)):
            raise DesktopAutomationError(
                "At least one window or element selector is required."
            )
        deadline = time.monotonic() + timeout_sec
        last_snapshot: Optional[dict[str, Any]] = None
        while True:
            last_snapshot = self.observe(
                window_title=window_title, max_depth=12, max_elements=self.max_elements
            )
            matches = []
            for window in last_snapshot["windows"]:
                for element in window["elements"]:
                    if name and name.casefold() not in element["name"].casefold():
                        continue
                    if automation_id and automation_id != element["automation_id"]:
                        continue
                    if control_type and control_type.casefold() != element[
                        "control_type"
                    ].casefold():
                        continue
                    matches.append(element)
            condition_met = bool(matches) is expected_exists
            if condition_met:
                return {
                    "condition_met": True,
                    "expected_exists": expected_exists,
                    "matches": matches[:20],
                    "snapshot_id": last_snapshot["snapshot_id"],
                    "snapshot_sha256": last_snapshot["snapshot_sha256"],
                }
            if time.monotonic() >= deadline:
                return {
                    "condition_met": False,
                    "expected_exists": expected_exists,
                    "matches": matches[:20],
                    "snapshot_id": last_snapshot["snapshot_id"],
                    "snapshot_sha256": last_snapshot["snapshot_sha256"],
                    "timed_out": True,
                }
            time.sleep(poll_interval_sec)
