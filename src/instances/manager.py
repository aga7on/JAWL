"""Process manager and reconciliation engine for named JAWL instances."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import psutil
from ruamel.yaml import YAML

from src.instances.models import InstanceProfile, InstanceRuntime
from src.instances.paths import (
    bootstrap_instance_layout,
    get_instance_paths,
    validate_instance_id,
)
from src.instances.registry import InstanceRegistry
from src.utils._tools import SystemInstanceLock
from src.utils.logger import main_logger


class InstanceManager:
    """Owns safe lifecycle transitions; durable intent lives in the registry."""

    def __init__(
        self,
        project_root: Path,
        *,
        registry: InstanceRegistry | None = None,
        python_executable: Path | None = None,
        entrypoint: Path | None = None,
        instances_root: Path | None = None,
        shared_sandbox: Path | None = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.instances_root = (
            instances_root or self.project_root / "runtime" / "instances"
        ).resolve()
        self.shared_sandbox = (
            shared_sandbox or self.project_root / "sandbox"
        ).resolve()
        self.instances_root.mkdir(parents=True, exist_ok=True)
        self.shared_sandbox.mkdir(parents=True, exist_ok=True)
        self.registry = registry or InstanceRegistry(
            self.instances_root / "registry.json"
        )
        self.python_executable = Path(
            python_executable or sys.executable
        ).resolve()
        self.entrypoint = Path(
            entrypoint or self.project_root / "src" / "main.py"
        ).resolve()

    def paths_for(self, instance_id: str):
        instance_id = validate_instance_id(instance_id)
        return get_instance_paths(
            {
                "JAWL_INSTANCE_ID": instance_id,
                "JAWL_INSTANCE_HOME": str(
                    self.instances_root / instance_id
                ),
                "JAWL_INSTANCES_ROOT": str(self.instances_root),
                "JAWL_SANDBOX_DIR": str(self.shared_sandbox),
            },
            self.project_root,
        )

    def create_profile(
        self,
        instance_id: str,
        display_name: str,
        **options: Any,
    ) -> InstanceProfile:
        if not options.get("static_ports"):
            options["static_ports"] = self._allocate_port_block()
        profile = InstanceProfile(
            instance_id=instance_id,
            display_name=display_name,
            **options,
        )
        created = self.registry.create_profile(profile)
        try:
            paths = self.paths_for(created.instance_id)
            bootstrap_instance_layout(paths)
            if created.template_profile:
                self._apply_template(created, paths.config_dir)
            self._apply_profile_configuration(created, paths.config_dir)
        except Exception:
            # Keep the durable profile for diagnosis instead of deleting an
            # operator-created identity behind their back.
            self.registry.update_runtime(
                created.instance_id,
                state="quarantined",
                last_error="Profile layout initialization failed",
            )
            raise
        return created

    def _allocate_port_block(self, size: int = 8) -> list[int]:
        reserved = {
            port
            for profile in self.registry.list_profiles()
            if profile.enabled
            for port in profile.static_ports
        }
        for base in range(20000, 60000, 16):
            candidate = list(range(base, base + size))
            if any(port in reserved for port in candidate):
                continue
            sockets = []
            try:
                for port in candidate:
                    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
                    probe.bind(("127.0.0.1", port))
                    sockets.append(probe)
            except OSError:
                continue
            finally:
                for probe in sockets:
                    probe.close()
            return candidate
        raise RuntimeError("No free JAWL instance port block is available")

    @staticmethod
    def _yaml() -> YAML:
        yaml = YAML()
        yaml.preserve_quotes = True
        yaml.indent(mapping=2, sequence=4, offset=2)
        return yaml

    def _apply_template(
        self, profile: InstanceProfile, config_dir: Path
    ) -> None:
        template = self.registry.get_profile(profile.template_profile)
        source = self.paths_for(template.instance_id).config_dir
        if not source.is_dir():
            main_logger.warning(
                f"Template profile {profile.template_profile} has "
                f"no config directory; clean start"
            )
            return
        yaml = self._yaml()
        for name in ("settings.yaml", "interfaces.yaml"):
            src = source / name
            dst = config_dir / name
            if src.is_file() and not dst.exists():
                shutil.copy2(src, dst)
            elif src.is_file():
                try:
                    data = yaml.load(src.read_text(encoding="utf-8-sig")) or {}
                except Exception as exc:
                    main_logger.warning(
                        f"Could not read template config {name}: {exc}"
                    )
                    continue
                existing = yaml.load(dst.read_text(encoding="utf-8-sig")) or {}
                for key, value in data.items():
                    existing.setdefault(key, value)
                with dst.open("w", encoding="utf-8") as stream:
                    yaml.dump(existing, stream)

    def _apply_profile_configuration(
        self, profile: InstanceProfile, config_dir: Path
    ) -> None:
        yaml = self._yaml()
        settings_path = config_dir / "settings.yaml"
        if settings_path.is_file():
            with settings_path.open("r", encoding="utf-8-sig") as stream:
                settings = yaml.load(stream) or {}
            settings.setdefault("identity", {})["agent_name"] = (
                profile.display_name
            )
            if profile.model_override:
                settings.setdefault("llm", {})["main_model"] = (
                    profile.model_override
                )
            with settings_path.open("w", encoding="utf-8") as stream:
                yaml.dump(settings, stream)

        interfaces_path = config_dir / "interfaces.yaml"
        if interfaces_path.is_file():
            with interfaces_path.open("r", encoding="utf-8-sig") as stream:
                interfaces = yaml.load(stream) or {}
            telegram = interfaces.setdefault("telegram", {})
            telethon = telegram.setdefault("telethon", {})
            aiogram = telegram.setdefault("aiogram", {})
            telethon["session_name"] = profile.telethon_session
            if profile.telegram_mode == "disabled":
                telethon["enabled"] = False
                aiogram["enabled"] = False
            elif profile.telegram_mode == "telethon":
                telethon["enabled"] = True
                aiogram["enabled"] = False
            elif profile.telegram_mode == "aiogram":
                telethon["enabled"] = False
                aiogram["enabled"] = True
            if profile.static_ports:
                interfaces.setdefault("web", {}).setdefault(
                    "hooks", {}
                )["port"] = profile.static_ports[0]
            with interfaces_path.open("w", encoding="utf-8") as stream:
                yaml.dump(interfaces, stream)

    def _process(self, instance_id: str) -> psutil.Process | None:
        runtime = self.registry.get_runtime(instance_id)
        paths = self.paths_for(instance_id)
        candidates = [runtime.pid]
        if paths.pid_file.is_file():
            try:
                candidates.append(int(paths.pid_file.read_text().strip()))
            except (OSError, ValueError):
                pass
        expected = os.path.normcase(str(self.entrypoint))
        for pid in dict.fromkeys(item for item in candidates if item):
            try:
                process = psutil.Process(int(pid))
                command = " ".join(process.cmdline())
                if expected not in os.path.normcase(command):
                    continue
                return process
            except (psutil.Error, OSError):
                continue
        return None

    def status(self, instance_id: str) -> dict[str, Any]:
        profile = self.registry.get_profile(instance_id)
        runtime = self.registry.get_runtime(instance_id)
        process = self._process(instance_id)
        paths = self.paths_for(instance_id)
        live = process is not None and process.is_running()
        return {
            "profile": profile.public(),
            "runtime": {
                **runtime.public(),
                "state": "running" if live else runtime.state,
                "pid": process.pid if live else None,
                "alive": live,
            },
            "paths": {
                "home": str(paths.instance_home),
                "data": str(paths.data_dir),
                "config": str(paths.config_dir),
                "logs": str(paths.log_dir),
                "prompts": str(paths.prompt_dir),
                "sandbox": str(paths.sandbox_dir),
                "private_system": str(paths.private_sandbox_system_dir),
                "terminal_port": str(paths.terminal_port_file),
            },
        }

    def list_status(self) -> list[dict[str, Any]]:
        return [
            self.status(profile.instance_id)
            for profile in self.registry.list_profiles()
        ]

    def supervisor_status(self) -> dict[str, Any]:
        pid_file = self.instances_root / "supervisor.pid"
        pid = None
        if pid_file.is_file():
            try:
                pid = int(pid_file.read_text().strip())
                process = psutil.Process(pid)
                command = " ".join(process.cmdline()).lower()
                if "src.instances.supervisor" not in command:
                    pid = None
            except (OSError, ValueError, psutil.Error):
                pid = None
        return {"running": pid is not None, "pid": pid}

    def ensure_supervisor(self, timeout_sec: float = 10) -> dict[str, Any]:
        current = self.supervisor_status()
        if current["running"]:
            return current
        stop_file = self.instances_root / "supervisor.stop"
        stop_file.unlink(missing_ok=True)
        log_path = self.instances_root / "supervisor.log"
        stream = log_path.open("a", encoding="utf-8")
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            subprocess.Popen(
                [
                    str(self.python_executable),
                    "-m",
                    "src.instances.supervisor",
                    "--root",
                    str(self.project_root),
                ],
                cwd=str(self.project_root),
                env={
                    **os.environ,
                    "PYTHONPATH": str(self.project_root),
                    "PYTHONIOENCODING": "utf-8",
                },
                stdout=stream,
                stderr=stream,
                close_fds=True,
                creationflags=creationflags,
            )
        finally:
            stream.close()
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            current = self.supervisor_status()
            if current["running"]:
                return current
            time.sleep(0.1)
        raise TimeoutError("Instance supervisor did not become ready")

    def stop_supervisor(self, timeout_sec: float = 10) -> None:
        status = self.supervisor_status()
        if not status["running"]:
            return
        stop_file = self.instances_root / "supervisor.stop"
        stop_file.write_text("stop\n", encoding="utf-8")
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            if not self.supervisor_status()["running"]:
                return
            time.sleep(0.1)
        raise TimeoutError("Instance supervisor graceful shutdown timed out")

    def request_start(self, instance_id: str) -> InstanceProfile:
        instance_id = validate_instance_id(instance_id)
        self.registry.update_runtime(
            instance_id,
            state="stopped",
            last_error="",
            restart_timestamps=[],
        )
        return self.registry.set_desired_state(instance_id, "running")

    def request_stop(self, instance_id: str) -> InstanceProfile:
        return self.registry.set_desired_state(instance_id, "stopped")

    @contextmanager
    def _operation_lock(self, timeout_sec: float = 10) -> Iterator[None]:
        """Serialize reconciliation with destructive profile operations."""

        lock = SystemInstanceLock(self.instances_root / "operations.lock")
        deadline = time.monotonic() + timeout_sec
        while not lock.acquire():
            if time.monotonic() >= deadline:
                raise TimeoutError("Instance operation lock timed out")
            time.sleep(0.05)
        try:
            yield
        finally:
            lock.release()

    def _launch(
        self, profile: InstanceProfile, *, restart: bool = False
    ) -> InstanceRuntime:
        if not profile.enabled:
            raise ValueError(f"Instance {profile.instance_id} is disabled")
        if self._process(profile.instance_id) is not None:
            return self.registry.update_runtime(
                profile.instance_id, state="running"
            )
        paths = self.paths_for(profile.instance_id)
        bootstrap_instance_layout(paths)
        self._apply_profile_configuration(profile, paths.config_dir)
        paths.stop_file.unlink(missing_ok=True)
        paths.pid_file.unlink(missing_ok=True)
        environment = os.environ.copy()
        environment.update(paths.child_environment())
        environment["PYTHONPATH"] = str(self.project_root)
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["JAWL_INSTANCE_GENERATION"] = str(
            self.registry.get_runtime(profile.instance_id).generation + 1
        )
        if profile.static_ports:
            environment["JAWL_INSTANCE_PORT_BASE"] = str(
                profile.static_ports[0]
            )

        creationflags = 0
        log_stream = None
        if os.name == "nt":
            if profile.visible_console:
                creationflags |= subprocess.CREATE_NEW_CONSOLE
                stdout = None
                stderr = None
            else:
                creationflags |= subprocess.CREATE_NO_WINDOW
                paths.log_dir.mkdir(parents=True, exist_ok=True)
                log_stream = (
                    paths.log_dir / "instance-console.log"
                ).open("a", encoding="utf-8")
                stdout = log_stream
                stderr = log_stream
        else:
            stdout = subprocess.DEVNULL
            stderr = subprocess.DEVNULL
        try:
            process = subprocess.Popen(
                [str(self.python_executable), str(self.entrypoint)],
                cwd=str(self.project_root),
                env=environment,
                stdout=stdout,
                stderr=stderr,
                close_fds=True,
                creationflags=creationflags,
            )
        finally:
            if log_stream is not None:
                log_stream.close()

        now = time.time()
        current = self.registry.get_runtime(profile.instance_id)
        restarts = list(current.restart_timestamps)
        if restart:
            restarts.append(now)
        return self.registry.update_runtime(
            profile.instance_id,
            state="starting",
            pid=process.pid,
            generation=current.generation + 1,
            started_at=now,
            stopped_at=None,
            last_exit_code=None,
            last_error="",
            restart_timestamps=restarts,
            supervisor_pid=os.getpid(),
        )

    @staticmethod
    def _terminate_tree(process: psutil.Process, force: bool) -> None:
        try:
            children = process.children(recursive=True)
        except psutil.Error:
            children = []
        targets = [*children, process]
        for target in reversed(targets):
            try:
                target.kill() if force else target.terminate()
            except psutil.Error:
                pass
        psutil.wait_procs(targets, timeout=5)

    def stop_now(
        self,
        instance_id: str,
        *,
        timeout_sec: float = 20,
        force_after_timeout: bool = False,
    ) -> InstanceRuntime:
        instance_id = validate_instance_id(instance_id)
        paths = self.paths_for(instance_id)
        process = self._process(instance_id)
        if process is None:
            paths.stop_file.unlink(missing_ok=True)
            return self.registry.update_runtime(
                instance_id,
                state="stopped",
                pid=None,
                stopped_at=time.time(),
            )
        self.registry.update_runtime(instance_id, state="stopping")
        paths.stop_file.parent.mkdir(parents=True, exist_ok=True)
        paths.stop_file.write_text(
            "requested by Multi-Instance Manager\n", encoding="utf-8"
        )
        try:
            process.wait(timeout=max(0.1, timeout_sec))
        except psutil.TimeoutExpired:
            if not force_after_timeout:
                return self.registry.update_runtime(
                    instance_id,
                    state="stopping",
                    last_error="Graceful shutdown timed out",
                )
            self._terminate_tree(process, force=False)
            if process.is_running():
                self._terminate_tree(process, force=True)
        finally:
            paths.stop_file.unlink(missing_ok=True)
        return self.registry.update_runtime(
            instance_id,
            state="stopped",
            pid=None,
            stopped_at=time.time(),
            last_error="",
        )

    def reconcile_once(self) -> list[dict[str, Any]]:
        with self._operation_lock():
            return self._reconcile_once_locked()

    def _reconcile_once_locked(self) -> list[dict[str, Any]]:
        outcomes = []
        now = time.time()
        for profile in self.registry.list_profiles():
            runtime = self.registry.get_runtime(profile.instance_id)
            process = self._process(profile.instance_id)
            if process is not None:
                if profile.desired_state == "stopped" or not profile.enabled:
                    stopped = self.stop_now(
                        profile.instance_id,
                        timeout_sec=10,
                        force_after_timeout=True,
                    )
                    outcomes.append(
                        {
                            "instance_id": profile.instance_id,
                            "action": "stopped",
                            "state": stopped.state,
                        }
                    )
                elif (
                    runtime.state != "running"
                    or runtime.pid != process.pid
                    or runtime.supervisor_pid != os.getpid()
                ):
                    self.registry.update_runtime(
                        profile.instance_id,
                        state="running",
                        pid=process.pid,
                        last_error="",
                        supervisor_pid=os.getpid(),
                    )
                continue

            if profile.desired_state != "running" or not profile.enabled:
                if runtime.state != "stopped" or runtime.pid is not None:
                    self.registry.update_runtime(
                        profile.instance_id,
                        state="stopped",
                        pid=None,
                        stopped_at=now,
                    )
                continue

            # These states require an explicit operator transition. Rewriting
            # them on every tick caused thousands of atomic registry writes;
            # treating quarantine as a launch candidate also defeated it on
            # the very next supervisor iteration. request_start() clears both.
            if runtime.state == "quarantined":
                continue
            if runtime.state == "crashed" and not profile.auto_restart:
                continue

            unexpected_exit = runtime.state in {
                "starting",
                "running",
                "stopping",
            }
            restarting = unexpected_exit or runtime.state == "crashed"

            recent = [
                item
                for item in runtime.restart_timestamps
                if now - item <= profile.restart_window_sec
            ]
            if unexpected_exit and not profile.auto_restart:
                self.registry.update_runtime(
                    profile.instance_id,
                    state="crashed",
                    pid=None,
                    last_error="Process exited; automatic restart is disabled",
                )
                continue
            if restarting and len(recent) >= profile.restart_limit:
                self.registry.update_runtime(
                    profile.instance_id,
                    state="quarantined",
                    pid=None,
                    restart_timestamps=recent,
                    last_error=(
                        "Restart limit reached; manual start is required"
                    ),
                )
                outcomes.append(
                    {
                        "instance_id": profile.instance_id,
                        "action": "quarantined",
                        "state": "quarantined",
                    }
                )
                continue
            try:
                launched = self._launch(profile, restart=restarting)
                outcomes.append(
                    {
                        "instance_id": profile.instance_id,
                        "action": "restarted" if restarting else "started",
                        "state": launched.state,
                        "pid": launched.pid,
                    }
                )
            except Exception as exc:
                self.registry.update_runtime(
                    profile.instance_id,
                    state="crashed",
                    pid=None,
                    last_error=f"{type(exc).__name__}: {exc}",
                    restart_timestamps=recent + [now],
                )
                outcomes.append(
                    {
                        "instance_id": profile.instance_id,
                        "action": "start_failed",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
        return outcomes

    def delete_profile(self, instance_id: str) -> None:
        """Permanently delete a stopped profile without orphaning registry state."""

        instance_id = validate_instance_id(instance_id)
        with self._operation_lock():
            if self._process(instance_id) is not None:
                raise RuntimeError("Stop the instance before deleting it")
            # Resolve the durable record before touching data and stage the home
            # with an atomic same-volume rename. If registry mutation fails, the
            # original layout can be restored intact.
            self.registry.get_profile(instance_id)
            paths = self.paths_for(instance_id)
            staged = self.instances_root / (
                f".deleting-{instance_id}-{time.time_ns()}"
            )
            if paths.instance_home.exists():
                paths.instance_home.replace(staged)
            try:
                self.registry.delete_profile(instance_id)
            except Exception:
                if staged.exists() and not paths.instance_home.exists():
                    staged.replace(paths.instance_home)
                raise
            if staged.exists():
                shutil.rmtree(staged)

    def archive_profile(self, instance_id: str) -> Path:
        """Recoverably archive a stopped profile; registry deletion is omitted."""
        instance_id = validate_instance_id(instance_id)
        with self._operation_lock():
            if self._process(instance_id) is not None:
                raise RuntimeError("Stop the instance before archiving it")
            self.registry.update_profile(
                instance_id, enabled=False, desired_state="stopped"
            )
            paths = self.paths_for(instance_id)
            archive_root = self.instances_root / "_archive"
            archive_root.mkdir(parents=True, exist_ok=True)
            destination = archive_root / (
                f"{instance_id}-{time.strftime('%Y%m%d-%H%M%S')}"
            )
            if paths.instance_home.exists():
                shutil.move(str(paths.instance_home), str(destination))
            return destination
