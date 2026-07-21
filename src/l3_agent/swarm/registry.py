"""Durable, bounded state registry for delegated Swarm work."""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from src.utils._tools import redact_sensitive_text, truncate_text


class DelegationRegistry:
    """Persist public delegation state outside task worktrees.

    Raw task descriptions are never stored. On construction, unfinished records
    from an earlier process session become ``interrupted`` rather than silently
    appearing active forever.
    """

    _VERSION = 1
    _MAX_RECORDS = 500
    _ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
    _ACTIVE = {"queued", "running"}
    _TERMINAL = {"completed", "failed", "cancelled", "interrupted"}

    def __init__(self, path: Path, session_id: Optional[str] = None) -> None:
        self.path = path.resolve()
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.session_id = session_id or uuid.uuid4().hex
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock():
            if self.path.exists():
                payload = self._load()
            else:
                payload = {"version": self._VERSION, "delegations": {}}
            now = time.time()
            changed = False
            for record in payload["delegations"].values():
                if (
                    record.get("status") in self._ACTIVE
                    and record.get("session_id") != self.session_id
                ):
                    record["status"] = "interrupted"
                    record["updated_at"] = now
                    record["finished_at"] = now
                    changed = True
            if changed or not self.path.exists():
                self._atomic_write(payload)

    @contextmanager
    def _lock(self) -> Iterator[None]:
        with self.lock_path.open("a+b") as stream:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _load(self) -> Dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Delegation registry is unreadable: {exc}") from exc
        if payload.get("version") != self._VERSION or not isinstance(
            payload.get("delegations"), dict
        ):
            raise ValueError("Delegation registry has an unsupported format.")
        return payload

    def _atomic_write(self, payload: Dict[str, Any]) -> None:
        descriptor, name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    @classmethod
    def _validate_id(cls, delegation_id: str) -> str:
        normalized = str(delegation_id).strip()
        if not cls._ID.fullmatch(normalized):
            raise ValueError(
                "Delegation ID must match [A-Za-z0-9_-] and contain 1-64 characters."
            )
        return normalized

    @staticmethod
    def _public(record: Dict[str, Any]) -> Dict[str, Any]:
        return dict(record)

    def create(
        self,
        delegation_id: str,
        role: str,
        task_description: str,
        parent: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        delegation_id = self._validate_id(delegation_id)
        now = time.time()
        with self._lock():
            payload = self._load()
            records = payload["delegations"]
            if delegation_id in records:
                raise ValueError(f"Delegation already exists ({delegation_id}).")
            record = {
                "id": delegation_id,
                "role": truncate_text(str(role), 64),
                "status": "queued",
                "task_summary": truncate_text(
                    redact_sensitive_text(str(task_description)), 1000
                ),
                "session_id": self.session_id,
                "created_at": now,
                "updated_at": now,
                "started_at": None,
                "finished_at": None,
            }
            if parent:
                if set(parent) != {"task_id", "step_id", "expected_revision"}:
                    raise ValueError("Delegation parent metadata has unsupported fields.")
                task_id = str(parent["task_id"])
                step_id = str(parent["step_id"])
                expected_revision = parent["expected_revision"]
                if (
                    not self._ID.fullmatch(task_id)
                    or not self._ID.fullmatch(step_id)
                    or not isinstance(expected_revision, int)
                    or isinstance(expected_revision, bool)
                    or expected_revision < 1
                ):
                    raise ValueError("Delegation parent metadata is invalid.")
                record["parent"] = {
                    "task_id": task_id,
                    "step_id": step_id,
                    "expected_revision": expected_revision,
                }
            records[delegation_id] = record
            terminal = sorted(
                (
                    item
                    for item in records.values()
                    if item.get("status") in self._TERMINAL
                ),
                key=lambda item: float(item.get("updated_at", 0)),
            )
            while len(records) > self._MAX_RECORDS and terminal:
                records.pop(terminal.pop(0)["id"], None)
            if len(records) > self._MAX_RECORDS:
                raise ValueError("Delegation registry has too many active records.")
            self._atomic_write(payload)
            return self._public(record)

    def transition(
        self,
        delegation_id: str,
        status: str,
        *,
        detail: str = "",
        report_path: str = "",
    ) -> Dict[str, Any]:
        delegation_id = self._validate_id(delegation_id)
        allowed = {
            "queued": {"running", "failed", "cancelled"},
            "running": {"completed", "failed", "cancelled"},
        }
        if status not in self._ACTIVE | self._TERMINAL:
            raise ValueError(f"Unsupported delegation status '{status}'.")
        now = time.time()
        with self._lock():
            payload = self._load()
            record = payload["delegations"].get(delegation_id)
            if record is None:
                raise ValueError(f"Delegation not found ({delegation_id}).")
            current = str(record.get("status"))
            if status != current and status not in allowed.get(current, set()):
                raise ValueError(
                    f"Delegation cannot transition from {current} to {status}."
                )
            record["status"] = status
            record["updated_at"] = now
            if status == "running" and record.get("started_at") is None:
                record["started_at"] = now
            if status in self._TERMINAL:
                record["finished_at"] = now
            if detail:
                record["detail"] = truncate_text(redact_sensitive_text(detail), 1000)
            if report_path:
                record["report_path"] = truncate_text(report_path, 1000)
            self._atomic_write(payload)
            return self._public(record)

    def get(self, delegation_id: str) -> Dict[str, Any]:
        delegation_id = self._validate_id(delegation_id)
        with self._lock():
            record = self._load()["delegations"].get(delegation_id)
            if record is None:
                raise ValueError(f"Delegation not found ({delegation_id}).")
            return self._public(record)

    def list(self, limit: int = 20, status: str = "") -> List[Dict[str, Any]]:
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        if status and status not in self._ACTIVE | self._TERMINAL:
            raise ValueError(f"Unsupported delegation status '{status}'.")
        with self._lock():
            records = list(self._load()["delegations"].values())
        if status:
            records = [record for record in records if record.get("status") == status]
        records.sort(key=lambda item: float(item.get("created_at", 0)), reverse=True)
        return [self._public(record) for record in records[:limit]]

    def unreconciled_interruptions(self, limit: int = 100) -> List[Dict[str, Any]]:
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        with self._lock():
            records = [
                record
                for record in self._load()["delegations"].values()
                if record.get("status") == "interrupted"
                and record.get("parent")
                and not record.get("parent_reconciled")
            ]
        records.sort(key=lambda item: float(item.get("created_at", 0)))
        return [self._public(record) for record in records[:limit]]

    def mark_parent_reconciled(self, delegation_id: str, detail: str = "") -> None:
        delegation_id = self._validate_id(delegation_id)
        with self._lock():
            payload = self._load()
            record = payload["delegations"].get(delegation_id)
            if record is None:
                raise ValueError(f"Delegation not found ({delegation_id}).")
            record["parent_reconciled"] = True
            if detail:
                record["parent_reconciliation_detail"] = truncate_text(
                    redact_sensitive_text(detail), 1000
                )
            self._atomic_write(payload)
