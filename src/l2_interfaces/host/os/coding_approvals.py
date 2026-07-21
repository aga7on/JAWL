"""Cross-process, one-shot approvals for exact coding command subjects."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List

from src.utils._tools import redact_sensitive_text, truncate_text


def format_coding_approval_notification(
    record: Dict[str, Any], max_chars: int = 3900
) -> str:
    """Format only the bounded public approval projection for operator push."""

    if max_chars < 200 or max_chars > 10000:
        raise ValueError("Approval notification max_chars must be 200..10000.")
    expires_at = record.get("expires_at")
    try:
        expires = datetime.fromtimestamp(
            float(expires_at), timezone.utc
        ).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (TypeError, ValueError, OSError, OverflowError):
        expires = "unknown"
    lines = [
        "JAWL coding command approval requested",
        f"ID: {str(record.get('id', ''))[:16]}",
        f"Task: {truncate_text(str(record.get('task_id', '')), 200)}",
        f"Backend: {truncate_text(str(record.get('backend', '')), 50)}",
        f"CWD: {truncate_text(str(record.get('relative_cwd', '.')), 300)}",
        f"Timeout: {record.get('timeout_seconds', '?')}s",
        f"Command: {truncate_text(str(record.get('argv_preview', '')), 2000)}",
        f"Expires: {expires}",
        "Approve: python jawl.py --approvals approve <ID>",
        "Deny: python jawl.py --approvals deny <ID>",
    ]
    return truncate_text(redact_sensitive_text("\n".join(lines)), max_chars)


class CodingApprovalStore:
    """Persist approval decisions outside model-writable workspace paths."""

    _SCHEMA_VERSION = 1
    _MAX_RECORDS = 500
    _MAX_ACTIVE = 100
    _ID = re.compile(r"^[0-9a-f]{16}$")
    _SENSITIVE_FLAG = re.compile(
        r"(?i)(?:api[-_]?key|credential|password|secret|token)"
    )

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            with self._lock():
                if not self.path.exists():
                    self._atomic_write(
                        {"version": self._SCHEMA_VERSION, "requests": {}}
                    )

    @staticmethod
    def subject_fingerprint(subject: Dict[str, Any]) -> str:
        encoded = json.dumps(
            subject,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(b"jawl-coding-approval-v1\x00" + encoded).hexdigest()

    @staticmethod
    def build_subject(
        *,
        task_id: str,
        backend: str,
        argv: List[str],
        workspace_fingerprint: str,
        relative_cwd: str,
        timeout_seconds: int,
        execution_identity: Dict[str, Any],
    ) -> Dict[str, Any]:
        return {
            "task_id": task_id,
            "backend": backend,
            "argv": list(argv),
            "workspace_fingerprint": workspace_fingerprint,
            "relative_cwd": relative_cwd,
            "timeout_seconds": int(timeout_seconds),
            "execution_identity": dict(execution_identity),
        }

    @contextmanager
    def _lock(self) -> Iterator[None]:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
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
            raise ValueError(f"Coding approval registry is unreadable: {exc}") from exc
        if payload.get("version") != self._SCHEMA_VERSION or not isinstance(
            payload.get("requests"), dict
        ):
            raise ValueError("Coding approval registry has an unsupported format.")
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

    @staticmethod
    def _public(record: Dict[str, Any]) -> Dict[str, Any]:
        return {
            key: record[key]
            for key in (
                "id",
                "status",
                "task_id",
                "backend",
                "argv_preview",
                "workspace_fingerprint",
                "relative_cwd",
                "timeout_seconds",
                "subject_fingerprint",
                "created_at",
                "expires_at",
                "decided_at",
                "consumed_at",
                "actor",
            )
            if key in record
        }

    @classmethod
    def _validate_id(cls, approval_id: str) -> str:
        normalized = str(approval_id).strip().lower()
        if not cls._ID.fullmatch(normalized):
            raise ValueError("Coding approval ID must be 16 hexadecimal characters.")
        return normalized

    @classmethod
    def _argv_preview(cls, argv: List[str]) -> str:
        preview: List[str] = []
        redact_next = False
        for argument in argv:
            if redact_next:
                preview.append("[REDACTED]")
                redact_next = False
                continue
            if "=" in argument:
                key, _ = argument.split("=", 1)
                if cls._SENSITIVE_FLAG.search(key):
                    preview.append(f"{key}=[REDACTED]")
                    continue
            preview.append(redact_sensitive_text(argument))
            if argument.startswith("-") and cls._SENSITIVE_FLAG.search(argument):
                redact_next = True
        return truncate_text(json.dumps(preview, ensure_ascii=False), 4000)

    @staticmethod
    def _expire(records: Dict[str, Dict[str, Any]], now: float) -> None:
        for record in records.values():
            if record.get("status") in {"pending", "approved"} and float(
                record.get("expires_at", 0)
            ) <= now:
                record["status"] = "expired"

    def request(self, subject: Dict[str, Any], ttl_seconds: int) -> Dict[str, Any]:
        if ttl_seconds < 60 or ttl_seconds > 86400:
            raise ValueError("Coding approval TTL must be between 60 and 86400 seconds.")
        now = time.time()
        fingerprint = self.subject_fingerprint(subject)
        with self._lock():
            payload = self._load()
            records = payload["requests"]
            self._expire(records, now)
            for existing in records.values():
                if (
                    existing.get("status") == "pending"
                    and existing.get("subject_fingerprint") == fingerprint
                ):
                    self._atomic_write(payload)
                    return self._public(existing)
            active = sum(
                record.get("status") in {"pending", "approved"}
                for record in records.values()
            )
            if active >= self._MAX_ACTIVE:
                raise ValueError(
                    "Coding approval registry already has 100 active requests."
                )
            identifier = uuid.uuid4().hex[:16]
            while identifier in records:
                identifier = uuid.uuid4().hex[:16]
            argv_preview = self._argv_preview(subject["argv"])
            record = {
                "id": identifier,
                "status": "pending",
                "task_id": subject["task_id"],
                "backend": subject["backend"],
                "argv_preview": argv_preview,
                "workspace_fingerprint": subject["workspace_fingerprint"],
                "relative_cwd": subject["relative_cwd"],
                "timeout_seconds": subject["timeout_seconds"],
                "subject_fingerprint": fingerprint,
                "created_at": now,
                "expires_at": now + ttl_seconds,
            }
            records[identifier] = record
            if len(records) > self._MAX_RECORDS:
                oldest = sorted(
                    (
                        item
                        for item in records.values()
                        if item.get("status")
                        in {"denied", "consumed", "expired"}
                    ),
                    key=lambda item: float(item.get("created_at", 0)),
                )[: len(records) - self._MAX_RECORDS]
                for item in oldest:
                    records.pop(item["id"], None)
            self._atomic_write(payload)
            return self._public(record)

    def decide(self, approval_id: str, approved: bool, actor: str) -> Dict[str, Any]:
        approval_id = self._validate_id(approval_id)
        now = time.time()
        with self._lock():
            payload = self._load()
            records = payload["requests"]
            self._expire(records, now)
            record = records.get(approval_id)
            if record is None:
                raise ValueError(f"Coding approval request not found ({approval_id}).")
            if record.get("status") != "pending":
                raise ValueError(
                    f"Coding approval request is already {record.get('status')}."
                )
            record["status"] = "approved" if approved else "denied"
            record["decided_at"] = now
            record["actor"] = truncate_text(str(actor), 200)
            self._atomic_write(payload)
            return self._public(record)

    def consume(self, approval_id: str, subject: Dict[str, Any]) -> Dict[str, Any]:
        approval_id = self._validate_id(approval_id)
        now = time.time()
        fingerprint = self.subject_fingerprint(subject)
        with self._lock():
            payload = self._load()
            records = payload["requests"]
            self._expire(records, now)
            record = records.get(approval_id)
            if record is None:
                raise PermissionError(
                    f"Coding approval request not found ({approval_id})."
                )
            if record.get("status") != "approved":
                raise PermissionError(
                    f"Coding approval request is {record.get('status')}, not approved."
                )
            if record.get("subject_fingerprint") != fingerprint:
                raise PermissionError(
                    "Coding approval does not match the exact task, argv, workspace, "
                    "cwd, backend, runtime/image policy, and timeout contract."
                )
            record["status"] = "consumed"
            record["consumed_at"] = now
            self._atomic_write(payload)
            return self._public(record)

    def get(self, approval_id: str) -> Dict[str, Any]:
        approval_id = self._validate_id(approval_id)
        now = time.time()
        with self._lock():
            payload = self._load()
            self._expire(payload["requests"], now)
            record = payload["requests"].get(approval_id)
            if record is None:
                raise ValueError(f"Coding approval request not found ({approval_id}).")
            self._atomic_write(payload)
            return self._public(record)

    def list(self, status: str | None = None) -> List[Dict[str, Any]]:
        now = time.time()
        with self._lock():
            payload = self._load()
            self._expire(payload["requests"], now)
            records = sorted(
                payload["requests"].values(),
                key=lambda item: float(item.get("created_at", 0)),
                reverse=True,
            )
            self._atomic_write(payload)
            return [
                self._public(record)
                for record in records
                if status is None or record.get("status") == status
            ]
