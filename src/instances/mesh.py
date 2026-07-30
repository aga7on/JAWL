"""Bounded shared coordination plane for otherwise isolated JAWL instances."""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from src.instances.paths import validate_instance_id
from src.instances.registry import InstanceRegistry
from src.l3_agent.skills.registry import SkillResult, skill
from src.utils._tools import redact_sensitive_text, truncate_text


class InstanceMesh:
    VERSION = 1
    MAX_MESSAGES = 1000
    MAX_TASKS = 500

    def __init__(
        self,
        instance_id: str,
        registry: InstanceRegistry,
        path: Path,
    ) -> None:
        self.instance_id = validate_instance_id(instance_id)
        self.registry = registry
        self.path = path.resolve()
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock():
            if not self.path.exists():
                self._write(
                    {
                        "version": self.VERSION,
                        "revision": 0,
                        "messages": [],
                        "tasks": {},
                    }
                )
            else:
                self._read()

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

    def _read(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Instance mesh is unreadable: {exc}") from exc
        if (
            payload.get("version") != self.VERSION
            or not isinstance(payload.get("messages"), list)
            or not isinstance(payload.get("tasks"), dict)
            or not isinstance(payload.get("revision"), int)
        ):
            raise ValueError("Instance mesh has an unsupported format")
        return payload

    def _write(self, payload: dict[str, Any]) -> None:
        descriptor, name = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=self.path.parent,
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

    def _known_instances(self) -> set[str]:
        return {"default"} | {
            profile.instance_id for profile in self.registry.list_profiles()
        }

    @skill()
    async def list_instances(self) -> SkillResult:
        """Lists peer main-agent instances without exposing private memory."""
        rows = []
        for profile in self.registry.list_profiles():
            runtime = self.registry.get_runtime(profile.instance_id)
            rows.append(
                {
                    "instance_id": profile.instance_id,
                    "display_name": profile.display_name,
                    "desired_state": profile.desired_state,
                    "runtime_state": runtime.state,
                    "pid": runtime.pid,
                    "tags": profile.tags,
                }
            )
        return SkillResult.ok(
            json.dumps(
                {
                    "current_instance": self.instance_id,
                    "instances": rows,
                },
                ensure_ascii=False,
                indent=2,
            )
        )

    @skill()
    async def send_instance_message(
        self,
        recipient: str,
        message: str,
        kind: str = "message",
        correlation_id: str = "",
    ) -> SkillResult:
        """Sends bounded steering/delegation to another main JAWL instance."""
        try:
            recipient = validate_instance_id(recipient)
        except ValueError as exc:
            return SkillResult.fail(str(exc))
        if recipient not in self._known_instances():
            return SkillResult.fail(f"Unknown JAWL instance: {recipient}")
        clean = truncate_text(redact_sensitive_text(str(message).strip()), 4000)
        if not clean:
            return SkillResult.fail("Instance message cannot be empty")
        clean_kind = str(kind).strip().lower()
        if clean_kind not in {"message", "delegation", "result", "steering"}:
            return SkillResult.fail("Unsupported instance message kind")
        if correlation_id:
            try:
                correlation_id = validate_instance_id(correlation_id)
            except ValueError as exc:
                return SkillResult.fail(str(exc))
        record = {
            "id": uuid.uuid4().hex,
            "sender": self.instance_id,
            "recipient": recipient,
            "kind": clean_kind,
            "correlation_id": correlation_id,
            "message": clean,
            "created_at": time.time(),
            "read_at": None,
        }
        with self._lock():
            payload = self._read()
            payload["messages"].append(record)
            payload["messages"] = payload["messages"][-self.MAX_MESSAGES :]
            payload["revision"] += 1
            self._write(payload)
        return SkillResult.ok(
            json.dumps(
                {
                    key: record[key]
                    for key in (
                        "id",
                        "sender",
                        "recipient",
                        "kind",
                        "correlation_id",
                        "created_at",
                    )
                },
                ensure_ascii=False,
            )
        )

    @skill()
    async def read_instance_messages(
        self, limit: int = 20, mark_read: bool = True
    ) -> SkillResult:
        """Reads this main instance's inbox and optionally acknowledges it."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            return SkillResult.fail("limit must be an integer between 1 and 100")
        now = time.time()
        with self._lock():
            payload = self._read()
            records = [
                record
                for record in payload["messages"]
                if record.get("recipient") == self.instance_id
            ][-limit:]
            if mark_read:
                selected = {record["id"] for record in records}
                for record in payload["messages"]:
                    if record.get("id") in selected and record.get("read_at") is None:
                        record["read_at"] = now
                payload["revision"] += 1
                self._write(payload)
        return SkillResult.ok(
            json.dumps(records, ensure_ascii=False, indent=2)
        )

    @skill()
    async def publish_instance_task(
        self,
        task_id: str,
        objective: str,
        preferred_instance: str = "",
    ) -> SkillResult:
        """Publishes one claimable cross-instance task for a main agent/Swarm."""
        try:
            task_id = validate_instance_id(task_id)
            if preferred_instance:
                preferred_instance = validate_instance_id(preferred_instance)
        except ValueError as exc:
            return SkillResult.fail(str(exc))
        if preferred_instance and preferred_instance not in self._known_instances():
            return SkillResult.fail(
                f"Unknown preferred instance: {preferred_instance}"
            )
        clean = truncate_text(
            redact_sensitive_text(str(objective).strip()), 4000
        )
        if not clean:
            return SkillResult.fail("Task objective cannot be empty")
        with self._lock():
            payload = self._read()
            if task_id in payload["tasks"]:
                return SkillResult.fail(f"Instance task already exists: {task_id}")
            if len(payload["tasks"]) >= self.MAX_TASKS:
                return SkillResult.fail("Instance task board is full")
            record = {
                "task_id": task_id,
                "objective": clean,
                "created_by": self.instance_id,
                "preferred_instance": preferred_instance,
                "status": "open",
                "claimed_by": "",
                "created_at": time.time(),
                "updated_at": time.time(),
                "result_summary": "",
            }
            payload["tasks"][task_id] = record
            payload["revision"] += 1
            self._write(payload)
        return SkillResult.ok(json.dumps(record, ensure_ascii=False, indent=2))

    @skill()
    async def claim_instance_task(self, task_id: str) -> SkillResult:
        """Atomically claims one open cross-instance task for this instance."""
        try:
            task_id = validate_instance_id(task_id)
        except ValueError as exc:
            return SkillResult.fail(str(exc))
        with self._lock():
            payload = self._read()
            record = payload["tasks"].get(task_id)
            if record is None:
                return SkillResult.fail(f"Instance task not found: {task_id}")
            if record["status"] != "open":
                return SkillResult.fail(
                    f"Instance task is already {record['status']} by "
                    f"{record.get('claimed_by') or 'unknown'}"
                )
            preferred = record.get("preferred_instance")
            if preferred and preferred != self.instance_id:
                return SkillResult.fail(
                    f"Instance task is reserved for {preferred}"
                )
            record["status"] = "claimed"
            record["claimed_by"] = self.instance_id
            record["updated_at"] = time.time()
            payload["revision"] += 1
            self._write(payload)
        return SkillResult.ok(json.dumps(record, ensure_ascii=False, indent=2))

    @skill()
    async def complete_instance_task(
        self, task_id: str, result_summary: str
    ) -> SkillResult:
        """Completes this instance's claimed task with a bounded handoff."""
        try:
            task_id = validate_instance_id(task_id)
        except ValueError as exc:
            return SkillResult.fail(str(exc))
        summary = truncate_text(
            redact_sensitive_text(str(result_summary).strip()), 4000
        )
        if not summary:
            return SkillResult.fail("Task result summary cannot be empty")
        with self._lock():
            payload = self._read()
            record = payload["tasks"].get(task_id)
            if record is None:
                return SkillResult.fail(f"Instance task not found: {task_id}")
            if (
                record.get("status") != "claimed"
                or record.get("claimed_by") != self.instance_id
            ):
                return SkillResult.fail(
                    "Only the claiming instance can complete this task"
                )
            record["status"] = "completed"
            record["result_summary"] = summary
            record["updated_at"] = time.time()
            payload["revision"] += 1
            self._write(payload)
        return SkillResult.ok(json.dumps(record, ensure_ascii=False, indent=2))

    async def get_context_block(self, **_: Any) -> str:
        with self._lock():
            payload = self._read()
        unread = [
            record
            for record in payload["messages"]
            if record.get("recipient") == self.instance_id
            and record.get("read_at") is None
        ][-10:]
        available_tasks = [
            record
            for record in payload["tasks"].values()
            if record.get("status") == "open"
            and (
                not record.get("preferred_instance")
                or record.get("preferred_instance") == self.instance_id
            )
        ][:10]
        if not unread and not available_tasks:
            return (
                "[INSTANCE MESH]\n"
                f"Current main instance: {self.instance_id}. "
                "No unread peer messages or open assigned tasks."
            )
        return (
            "[INSTANCE MESH]\n"
            f"Current main instance: {self.instance_id}.\n"
            "Unread peer messages:\n"
            + json.dumps(unread, ensure_ascii=False, indent=2)
            + "\nOpen cross-instance tasks:\n"
            + json.dumps(available_tasks, ensure_ascii=False, indent=2)
            + "\nUse InstanceMesh skills; a claimed task may be delegated "
            "internally through Swarm, then completed with a concise result."
        )
