"""Atomic cross-process registry for JAWL instance profiles and runtime state."""

from __future__ import annotations

import json
import os
import tempfile
import time
from copy import deepcopy
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from src.instances.models import InstanceProfile, InstanceRuntime
from src.instances.paths import validate_instance_id


def _without_updated_at(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key != "updated_at"}


class InstanceRegistry:
    VERSION = 1

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock():
            if not self.path.exists():
                self._write_unlocked(self._empty())
            else:
                self._validate_payload(self._read_unlocked())

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "version": InstanceRegistry.VERSION,
            "revision": 0,
            "profiles": {},
            "runtime": {},
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

    def _read_unlocked(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Instance registry is unreadable: {exc}") from exc
        self._validate_payload(payload)
        return payload

    def _write_unlocked(self, payload: dict[str, Any]) -> None:
        self._validate_payload(payload)
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

    @classmethod
    def _validate_payload(cls, payload: Any) -> None:
        if not isinstance(payload, dict) or payload.get("version") != cls.VERSION:
            raise ValueError("Instance registry has an unsupported format")
        if not isinstance(payload.get("revision"), int):
            raise ValueError("Instance registry revision is invalid")
        if not isinstance(payload.get("profiles"), dict) or not isinstance(
            payload.get("runtime"), dict
        ):
            raise ValueError("Instance registry collections are invalid")
        if len(payload["profiles"]) > 100:
            raise ValueError("Instance registry exceeds 100 profiles")
        for instance_id, value in payload["profiles"].items():
            validate_instance_id(instance_id)
            profile = InstanceProfile.from_dict(value)
            if profile.instance_id != instance_id:
                raise ValueError("Profile key and instance ID do not match")
        for instance_id, value in payload["runtime"].items():
            validate_instance_id(instance_id)
            runtime = InstanceRuntime.from_dict(value)
            if runtime.instance_id != instance_id:
                raise ValueError("Runtime key and instance ID do not match")

    @staticmethod
    def _validate_conflicts(profiles: dict[str, Any]) -> None:
        telethon: dict[str, str] = {}
        identities: dict[str, str] = {}
        ports: dict[int, str] = {}
        for instance_id, payload in profiles.items():
            profile = InstanceProfile.from_dict(payload)
            if not profile.enabled:
                continue
            if profile.telegram_mode in {"inherit", "telethon"}:
                owner = telethon.get(profile.telethon_session)
                if owner and owner != instance_id:
                    raise ValueError(
                        "Telethon session conflict: "
                        f"{profile.telethon_session} is used by {owner} and {instance_id}"
                    )
                telethon[profile.telethon_session] = instance_id
            if profile.telegram_identity:
                owner = identities.get(profile.telegram_identity)
                if owner and owner != instance_id:
                    raise ValueError(
                        "Telegram identity conflict between "
                        f"{owner} and {instance_id}"
                    )
                identities[profile.telegram_identity] = instance_id
            for port in profile.static_ports:
                owner = ports.get(port)
                if owner and owner != instance_id:
                    raise ValueError(
                        f"Static port {port} is reserved by {owner} and {instance_id}"
                    )
                ports[port] = instance_id

    def _mutate(
        self, callback: Callable[[dict[str, Any]], Any]
    ) -> tuple[Any, dict[str, Any]]:
        with self._lock():
            payload = self._read_unlocked()
            original = deepcopy(payload)
            result = callback(payload)
            self._validate_conflicts(payload["profiles"])
            if payload == original:
                return result, payload
            payload["revision"] += 1
            self._write_unlocked(payload)
            return result, payload

    def snapshot(self) -> dict[str, Any]:
        with self._lock():
            return self._read_unlocked()

    def list_profiles(self) -> list[InstanceProfile]:
        payload = self.snapshot()
        return [
            InstanceProfile.from_dict(value)
            for _, value in sorted(payload["profiles"].items())
        ]

    def get_profile(self, instance_id: str) -> InstanceProfile:
        instance_id = validate_instance_id(instance_id)
        payload = self.snapshot()["profiles"].get(instance_id)
        if payload is None:
            raise KeyError(f"Instance profile not found: {instance_id}")
        return InstanceProfile.from_dict(payload)

    def create_profile(self, profile: InstanceProfile) -> InstanceProfile:
        def mutate(payload: dict[str, Any]) -> None:
            if profile.instance_id in payload["profiles"]:
                raise ValueError(f"Instance profile already exists: {profile.instance_id}")
            payload["profiles"][profile.instance_id] = profile.public()
            payload["runtime"][profile.instance_id] = InstanceRuntime(
                instance_id=profile.instance_id
            ).public()

        self._mutate(mutate)
        return self.get_profile(profile.instance_id)

    def update_profile(
        self, instance_id: str, **changes: Any
    ) -> InstanceProfile:
        instance_id = validate_instance_id(instance_id)

        def mutate(payload: dict[str, Any]) -> None:
            current = payload["profiles"].get(instance_id)
            if current is None:
                raise KeyError(f"Instance profile not found: {instance_id}")
            if "instance_id" in changes and changes["instance_id"] != instance_id:
                raise ValueError("Instance ID cannot be changed")
            candidate = {
                **current,
                **changes,
                "instance_id": instance_id,
            }
            normalized = InstanceProfile.from_dict(candidate).public()
            if _without_updated_at(normalized) == _without_updated_at(current):
                return
            normalized["updated_at"] = time.time()
            payload["profiles"][instance_id] = normalized

        self._mutate(mutate)
        return self.get_profile(instance_id)

    def delete_profile(self, instance_id: str) -> None:
        instance_id = validate_instance_id(instance_id)

        def mutate(payload: dict[str, Any]) -> None:
            if instance_id not in payload["profiles"]:
                raise KeyError(f"Instance profile not found: {instance_id}")
            del payload["profiles"][instance_id]
            payload["runtime"].pop(instance_id, None)

        self._mutate(mutate)

    def set_desired_state(
        self, instance_id: str, desired_state: str
    ) -> InstanceProfile:
        return self.update_profile(instance_id, desired_state=desired_state)

    def get_runtime(self, instance_id: str) -> InstanceRuntime:
        instance_id = validate_instance_id(instance_id)
        payload = self.snapshot()["runtime"].get(instance_id)
        if payload is None:
            return InstanceRuntime(instance_id=instance_id)
        return InstanceRuntime.from_dict(payload)

    def update_runtime(
        self, instance_id: str, **changes: Any
    ) -> InstanceRuntime:
        instance_id = validate_instance_id(instance_id)

        def mutate(payload: dict[str, Any]) -> None:
            if instance_id not in payload["profiles"]:
                raise KeyError(f"Instance profile not found: {instance_id}")
            current = payload["runtime"].get(
                instance_id, InstanceRuntime(instance_id).public()
            )
            candidate = {
                **current,
                **changes,
                "instance_id": instance_id,
            }
            normalized = InstanceRuntime.from_dict(candidate).public()
            if _without_updated_at(normalized) == _without_updated_at(current):
                return
            normalized["updated_at"] = time.time()
            payload["runtime"][instance_id] = normalized

        self._mutate(mutate)
        return self.get_runtime(instance_id)
