"""Authoritative filesystem layout for default and named JAWL instances."""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


INSTANCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
DEFAULT_INSTANCE_ID = "default"


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def validate_instance_id(value: str) -> str:
    normalized = str(value).strip()
    if not INSTANCE_ID_PATTERN.fullmatch(normalized):
        raise ValueError(
            "JAWL instance ID must match [A-Za-z0-9][A-Za-z0-9_.-]{0,63}"
        )
    return normalized


def _resolved_path(
    raw: str | None,
    fallback: Path,
    root: Path,
    *,
    name: str,
) -> Path:
    candidate = Path(raw).expanduser() if raw else fallback
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    if resolved == Path(resolved.anchor):
        raise ValueError(f"{name} cannot target a filesystem root")
    return resolved


@dataclass(frozen=True)
class InstancePaths:
    instance_id: str
    project_root: Path
    instances_root: Path
    instance_home: Path
    data_dir: Path
    config_dir: Path
    log_dir: Path
    prompt_dir: Path
    sandbox_dir: Path
    env_file: Path
    legacy_default: bool

    @property
    def pid_file(self) -> Path:
        return self.data_dir / "agent.pid"

    @property
    def lock_file(self) -> Path:
        return self.data_dir / "agent.lock"

    @property
    def stop_file(self) -> Path:
        return self.data_dir / "agent.stop"

    @property
    def uptime_file(self) -> Path:
        return self.data_dir / "system_uptime.json"

    @property
    def terminal_port_file(self) -> Path:
        return (
            self.data_dir
            / "interfaces"
            / "host"
            / "terminal"
            / "terminal.port"
        )

    @property
    def private_sandbox_system_dir(self) -> Path:
        if self.legacy_default:
            return self.sandbox_dir / "_system"
        return self.sandbox_dir / "_system" / "instances" / self.instance_id

    @property
    def telegram_media_dir(self) -> Path:
        if self.legacy_default:
            return self.sandbox_dir / "telegram_media"
        return self.private_sandbox_system_dir / "telegram_media"

    def ensure_private_directories(self) -> None:
        for directory in (
            self.instance_home,
            self.data_dir,
            self.config_dir,
            self.log_dir,
            self.prompt_dir,
            self.sandbox_dir,
            self.private_sandbox_system_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def child_environment(self) -> dict[str, str]:
        return {
            "JAWL_INSTANCE_ID": self.instance_id,
            "JAWL_INSTANCE_HOME": str(self.instance_home),
            "JAWL_DATA_DIR": str(self.data_dir),
            "JAWL_CONFIG_DIR": str(self.config_dir),
            "JAWL_LOG_DIR": str(self.log_dir),
            "JAWL_PROMPT_DIR": str(self.prompt_dir),
            "JAWL_SANDBOX_DIR": str(self.sandbox_dir),
            "JAWL_ENV_FILE": str(self.env_file),
        }


def get_instance_paths(
    environment: Mapping[str, str] | None = None,
    root: Path | None = None,
) -> InstancePaths:
    env = os.environ if environment is None else environment
    repo = (root or project_root()).resolve()
    instance_id = validate_instance_id(
        env.get("JAWL_INSTANCE_ID", DEFAULT_INSTANCE_ID)
    )
    instances_root = _resolved_path(
        env.get("JAWL_INSTANCES_ROOT"),
        repo / "runtime" / "instances",
        repo,
        name="JAWL_INSTANCES_ROOT",
    )
    explicit_home = bool(env.get("JAWL_INSTANCE_HOME"))
    legacy_default = instance_id == DEFAULT_INSTANCE_ID and not explicit_home

    if legacy_default:
        home = repo
        data_fallback = repo / "src" / "utils" / "local" / "data"
        config_fallback = repo / "config"
        log_fallback = repo / "logs"
        prompt_fallback = repo / "src" / "l3_agent" / "prompt"
        env_fallback = repo / ".env"
    else:
        home = _resolved_path(
            env.get("JAWL_INSTANCE_HOME"),
            instances_root / instance_id,
            repo,
            name="JAWL_INSTANCE_HOME",
        )
        data_fallback = home / "data"
        config_fallback = home / "config"
        log_fallback = home / "logs"
        prompt_fallback = home / "prompts"
        env_fallback = home / ".env"

    data_dir = _resolved_path(
        env.get("JAWL_DATA_DIR"), data_fallback, repo, name="JAWL_DATA_DIR"
    )
    config_dir = _resolved_path(
        env.get("JAWL_CONFIG_DIR"),
        config_fallback,
        repo,
        name="JAWL_CONFIG_DIR",
    )
    log_dir = _resolved_path(
        env.get("JAWL_LOG_DIR"), log_fallback, repo, name="JAWL_LOG_DIR"
    )
    prompt_dir = _resolved_path(
        env.get("JAWL_PROMPT_DIR"),
        prompt_fallback,
        repo,
        name="JAWL_PROMPT_DIR",
    )
    sandbox_dir = _resolved_path(
        env.get("JAWL_SANDBOX_DIR"),
        repo / "sandbox",
        repo,
        name="JAWL_SANDBOX_DIR",
    )
    env_file = _resolved_path(
        env.get("JAWL_ENV_FILE"), env_fallback, repo, name="JAWL_ENV_FILE"
    )

    return InstancePaths(
        instance_id=instance_id,
        project_root=repo,
        instances_root=instances_root,
        instance_home=home,
        data_dir=data_dir,
        config_dir=config_dir,
        log_dir=log_dir,
        prompt_dir=prompt_dir,
        sandbox_dir=sandbox_dir,
        env_file=env_file,
        legacy_default=legacy_default,
    )


def bootstrap_instance_layout(paths: InstancePaths) -> None:
    """Populate a named profile from current templates without overwriting it."""
    paths.ensure_private_directories()
    if paths.legacy_default:
        return

    shared_config = paths.project_root / "config"
    for name in (
        "settings.example.yaml",
        "interfaces.example.yaml",
        "settings.yaml",
        "interfaces.yaml",
    ):
        source = shared_config / name
        target = paths.config_dir / name
        if source.is_file() and not target.exists():
            shutil.copy2(source, target)

    shared_prompts = paths.project_root / "src" / "l3_agent" / "prompt"
    if shared_prompts.is_dir():
        for source in shared_prompts.rglob("*"):
            if not source.is_file():
                continue
            relative = source.relative_to(shared_prompts)
            target = paths.prompt_dir / relative
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
