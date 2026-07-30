"""Validated public records used by the multi-instance control plane."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from src.instances.paths import validate_instance_id


DesiredState = Literal["running", "stopped"]
RuntimeState = Literal[
    "stopped",
    "starting",
    "running",
    "stopping",
    "crashed",
    "quarantined",
]
TelegramMode = Literal["inherit", "disabled", "telethon", "aiogram"]


@dataclass
class InstanceProfile:
    instance_id: str
    display_name: str
    description: str = ""
    desired_state: DesiredState = "stopped"
    enabled: bool = True
    auto_restart: bool = True
    restart_limit: int = 3
    restart_window_sec: int = 300
    visible_console: bool = True
    model_override: str = ""
    telegram_mode: TelegramMode = "inherit"
    telethon_session: str = ""
    telegram_identity: str = ""
    template_profile: str = ""
    static_ports: list[int] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        self.instance_id = validate_instance_id(self.instance_id)
        self.display_name = str(self.display_name).strip()
        if not self.display_name or len(self.display_name) > 100:
            raise ValueError("Instance display name must contain 1-100 characters")
        self.description = str(self.description).strip()
        if len(self.description) > 1000:
            raise ValueError("Instance description exceeds 1000 characters")
        if self.desired_state not in {"running", "stopped"}:
            raise ValueError("Unsupported desired state")
        if self.telegram_mode not in {
            "inherit",
            "disabled",
            "telethon",
            "aiogram",
        }:
            raise ValueError("Unsupported Telegram mode")
        if self.restart_limit < 0 or self.restart_limit > 20:
            raise ValueError("restart_limit must be between 0 and 20")
        if self.restart_window_sec < 10 or self.restart_window_sec > 86400:
            raise ValueError("restart_window_sec must be between 10 and 86400")
        if self.model_override and len(self.model_override) > 200:
            raise ValueError("model_override exceeds 200 characters")
        if self.template_profile:
            self.template_profile = validate_instance_id(self.template_profile)
            if self.template_profile.lower() == self.instance_id.lower():
                raise ValueError("A profile cannot be its own template")
        if not self.telethon_session:
            self.telethon_session = f"{self.instance_id}_telethon"
        self.telethon_session = validate_instance_id(self.telethon_session)
        self.telegram_identity = str(self.telegram_identity).strip()
        if len(self.telegram_identity) > 200:
            raise ValueError("telegram_identity exceeds 200 characters")
        if self.telegram_mode in {"telethon", "aiogram"} and not self.telegram_identity:
            raise ValueError(
                "Explicit Telegram modes require a non-secret identity label "
                "so duplicate accounts/bots can be detected"
            )
        if len(self.static_ports) > 32:
            raise ValueError("At most 32 static ports can be reserved")
        normalized_ports = []
        for port in self.static_ports:
            if isinstance(port, bool) or not isinstance(port, int):
                raise ValueError("Static ports must be integers")
            if port < 1 or port > 65535:
                raise ValueError("Static port must be between 1 and 65535")
            normalized_ports.append(port)
        if len(set(normalized_ports)) != len(normalized_ports):
            raise ValueError("Static ports must be unique within a profile")
        self.static_ports = sorted(normalized_ports)
        clean_tags = []
        for tag in self.tags:
            normalized = str(tag).strip()
            if normalized and normalized not in clean_tags:
                clean_tags.append(normalized[:64])
        self.tags = clean_tags[:32]

    def public(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "InstanceProfile":
        return cls(**payload)


@dataclass
class InstanceRuntime:
    instance_id: str
    state: RuntimeState = "stopped"
    pid: int | None = None
    generation: int = 0
    started_at: float | None = None
    stopped_at: float | None = None
    last_exit_code: int | None = None
    last_error: str = ""
    restart_timestamps: list[float] = field(default_factory=list)
    supervisor_pid: int | None = None
    updated_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        self.instance_id = validate_instance_id(self.instance_id)
        if self.state not in {
            "stopped",
            "starting",
            "running",
            "stopping",
            "crashed",
            "quarantined",
        }:
            raise ValueError("Unsupported runtime state")
        if self.pid is not None and (
            isinstance(self.pid, bool) or not isinstance(self.pid, int) or self.pid < 1
        ):
            raise ValueError("Runtime PID must be a positive integer")
        self.last_error = str(self.last_error)[:2000]
        self.restart_timestamps = [
            float(item) for item in self.restart_timestamps[-100:]
        ]

    def public(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "InstanceRuntime":
        return cls(**payload)
