import time
from pathlib import Path

import psutil

from src.instances.manager import InstanceManager
from src.instances.supervisor import run_supervisor


WORKER = Path(__file__).parent / "fixtures" / "instance_worker.py"


def wait_until(predicate, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition was not reached before timeout")


def make_manager(tmp_path: Path) -> InstanceManager:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.yaml").write_text(
        "identity:\n  agent_name: Base\nllm:\n  main_model: test\n"
    )
    (tmp_path / "config" / "interfaces.yaml").write_text(
        "telegram:\n"
        "  telethon:\n"
        "    enabled: false\n"
        "    session_name: base\n"
        "  aiogram:\n"
        "    enabled: false\n"
    )
    (tmp_path / "src" / "l3_agent" / "prompt").mkdir(parents=True)
    return InstanceManager(
        tmp_path,
        entrypoint=WORKER,
        instances_root=tmp_path / "instances",
        shared_sandbox=tmp_path / "shared",
    )


def test_manager_runs_isolated_profiles_with_one_shared_sandbox(
    tmp_path: Path,
) -> None:
    manager = make_manager(tmp_path)
    manager.create_profile("Gecko", "Gecko", visible_console=False)
    manager.create_profile("Donkey", "Donkey", visible_console=False)
    manager.request_start("Gecko")
    manager.request_start("Donkey")
    manager.reconcile_once()
    try:
        wait_until(
            lambda: all(
                manager.status(name)["runtime"]["alive"]
                for name in ("Gecko", "Donkey")
            )
        )
        shared_evidence = (
            tmp_path / "shared" / "instance-worker-seen.txt"
        )
        wait_until(
            lambda: shared_evidence.is_file()
            and {"Gecko", "Donkey"}
            <= set(shared_evidence.read_text().splitlines())
        )
        gecko = manager.status("Gecko")
        donkey = manager.status("Donkey")

        assert gecko["runtime"]["pid"] != donkey["runtime"]["pid"]
        assert gecko["paths"]["data"] != donkey["paths"]["data"]
        assert gecko["paths"]["logs"] != donkey["paths"]["logs"]
        assert gecko["paths"]["sandbox"] == donkey["paths"]["sandbox"]
    finally:
        manager.request_stop("Gecko")
        manager.request_stop("Donkey")
        manager.reconcile_once()
    assert not manager.status("Gecko")["runtime"]["alive"]
    assert not manager.status("Donkey")["runtime"]["alive"]


def test_reconciliation_restarts_only_failed_instance(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    manager.create_profile(
        "Gecko",
        "Gecko",
        visible_console=False,
        restart_limit=2,
        restart_window_sec=60,
    )
    manager.create_profile("Donkey", "Donkey", visible_console=False)
    manager.request_start("Gecko")
    manager.request_start("Donkey")
    manager.reconcile_once()
    wait_until(lambda: manager.status("Gecko")["runtime"]["alive"])
    gecko_pid = manager.status("Gecko")["runtime"]["pid"]
    donkey_pid = manager.status("Donkey")["runtime"]["pid"]

    psutil.Process(gecko_pid).kill()
    psutil.Process(gecko_pid).wait(timeout=5)
    manager.reconcile_once()
    wait_until(
        lambda: manager.status("Gecko")["runtime"]["alive"]
        and manager.status("Gecko")["runtime"]["pid"] != gecko_pid
    )

    assert manager.status("Donkey")["runtime"]["pid"] == donkey_pid
    assert manager.registry.get_runtime("Gecko").generation == 2
    manager.request_stop("Gecko")
    manager.request_stop("Donkey")
    manager.reconcile_once()


def test_create_profile_copies_template_config(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    manager.create_profile(
        "Gecko", "Gecko", visible_console=False, telegram_mode="disabled"
    )
    manager.create_profile(
        "Donkey",
        "Donkey",
        template_profile="Gecko",
        visible_console=False,
    )
    donkey_paths = manager.paths_for("Donkey")
    assert donkey_paths.config_dir.is_dir()
    for name in ("settings.yaml", "interfaces.yaml"):
        assert (donkey_paths.config_dir / name).is_file(), (
            f"{name} should exist in Donkey"
        )
    donkey_profile = manager.registry.get_profile("Donkey")
    assert donkey_profile.template_profile == "Gecko"
    assert donkey_profile.instance_id == "Donkey"


def test_supervisor_once_owns_and_cleans_its_pid_file(tmp_path: Path) -> None:
    assert run_supervisor(tmp_path, once=True, interval_sec=0.01) == 0
    assert not (
        tmp_path / "runtime" / "instances" / "supervisor.pid"
    ).exists()
