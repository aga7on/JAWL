"""Isolated multi-instance lifecycle, coordination, and conflict detection."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
import psutil

from src.instances.manager import InstanceManager
from src.instances.mesh import InstanceMesh
from src.instances.models import InstanceProfile
from src.instances.paths import get_instance_paths
from src.instances.registry import InstanceRegistry


WORKER = (
    Path(__file__).resolve().parent.parent.parent.parent.parent
    / "tests"
    / "unit"
    / "instances"
    / "fixtures"
    / "instance_worker.py"
)


def wait_until(predicate, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition was not reached before timeout")


@pytest.fixture
def manager(tmp_path: Path) -> InstanceManager:
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


def test_two_instances_run_with_separate_state(tmp_path: Path, manager: InstanceManager) -> None:
    manager.create_profile(
        "Gecko", "Gecko", visible_console=False, telegram_mode="disabled"
    )
    manager.create_profile(
        "Donkey", "Donkey", visible_console=False, telegram_mode="disabled"
    )
    manager.request_start("Gecko")
    manager.request_start("Donkey")
    manager.reconcile_once()
    try:
        wait_until(
            lambda: all(
                manager.status(name)["runtime"]["alive"]
                for name in ("Gecko", "Donkey")
            ),
            timeout=15,
        )
        gecko = manager.status("Gecko")
        donkey = manager.status("Donkey")
        assert gecko["runtime"]["pid"] != donkey["runtime"]["pid"]
        assert gecko["paths"]["data"] != donkey["paths"]["data"]
        assert gecko["paths"]["config"] != donkey["paths"]["config"]
        assert gecko["paths"]["logs"] != donkey["paths"]["logs"]
        assert gecko["paths"]["sandbox"] == donkey["paths"]["sandbox"]
        shared_evidence = tmp_path / "shared" / "instance-worker-seen.txt"
        wait_until(
            lambda: shared_evidence.is_file()
            and {"Gecko", "Donkey"}
            <= set(shared_evidence.read_text().splitlines()),
            timeout=15,
        )
    finally:
        manager.request_stop("Gecko")
        manager.request_stop("Donkey")
        manager.reconcile_once()
    wait_until(
        lambda: not any(
            manager.status(name)["runtime"]["alive"]
            for name in ("Gecko", "Donkey")
        )
    )


def test_template_profile_propagates_config(tmp_path: Path, manager: InstanceManager) -> None:
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
    assert (donkey_paths.config_dir / "settings.yaml").is_file()
    assert (donkey_paths.config_dir / "interfaces.yaml").is_file()
    donkey_profile = manager.registry.get_profile("Donkey")
    assert donkey_profile.template_profile == "Gecko"
    assert donkey_profile.instance_id == "Donkey"


def test_resource_conflict_detection(tmp_path: Path, manager: InstanceManager) -> None:
    reg = manager.registry
    reg.create_profile(
        InstanceProfile(
            instance_id="Alpha",
            display_name="Alpha",
            telegram_mode="telethon",
            telethon_session="shared",
            telegram_identity="bot-alpha",
            static_ports=[8100],
        )
    )
    with pytest.raises(ValueError, match="Telethon session conflict"):
        reg.create_profile(
            InstanceProfile(
                instance_id="Beta",
                display_name="Beta",
                telegram_mode="telethon",
                telethon_session="shared",
                telegram_identity="bot-beta",
            )
        )
    with pytest.raises(ValueError, match="Telegram identity conflict"):
        reg.create_profile(
            InstanceProfile(
                instance_id="Beta",
                display_name="Beta",
                telegram_identity="bot-alpha",
            )
        )
    with pytest.raises(ValueError, match="Static port 8100"):
        reg.create_profile(
            InstanceProfile(
                instance_id="Beta",
                display_name="Beta",
                static_ports=[8100],
            )
        )
    assert [p.instance_id for p in reg.list_profiles()] == ["Alpha"]


def test_port_block_is_contention_free(tmp_path: Path, manager: InstanceManager) -> None:
    assigned = set()
    for i in range(4):
        name = f"Agent{i}"
        manager.create_profile(
            name, name, visible_console=False, telegram_mode="disabled"
        )
        p = manager.registry.get_profile(name)
        assert p.static_ports
        for port in p.static_ports:
            assert port not in assigned, f"port {port} collision on {name}"
            assigned.add(port)


@pytest.mark.asyncio
async def test_mesh_coordination_between_three_instances(tmp_path: Path, manager: InstanceManager) -> None:
    registry_path = tmp_path / "mesh_registry.json"
    mesh_path = tmp_path / "mesh.json"
    reg = InstanceRegistry(registry_path)
    for name in ("Alpha", "Beta", "Gamma"):
        reg.create_profile(
            InstanceProfile(instance_id=name, display_name=name)
        )
    alpha = InstanceMesh("Alpha", reg, mesh_path)
    beta = InstanceMesh("Beta", reg, mesh_path)
    gamma = InstanceMesh("Gamma", reg, mesh_path)

    sent = await alpha.send_instance_message("Beta", "Inspect module core", "delegation", "task-001")
    assert sent.is_success
    inbox = await beta.read_instance_messages(limit=10, mark_read=True)
    messages = json.loads(inbox.message)
    assert any(m["sender"] == "Alpha" and m["kind"] == "delegation" for m in messages)

    task = await gamma.publish_instance_task("audit-logs", "Audit all agent logs", "Beta")
    assert task.is_success
    claimed = await beta.claim_instance_task("audit-logs")
    assert claimed.is_success
    completed = await beta.complete_instance_task("audit-logs", "Logs audit passed")
    assert completed.is_success

    context = await alpha.get_context_block()
    assert "No unread peer messages" in context


def test_crash_recovery_restarts_only_failed_instance(tmp_path: Path, manager: InstanceManager) -> None:
    manager.create_profile(
        "Gecko", "Gecko", visible_console=False,
        restart_limit=2, restart_window_sec=60,
        telegram_mode="disabled",
    )
    manager.create_profile(
        "Donkey", "Donkey", visible_console=False, telegram_mode="disabled"
    )
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
        lambda: (
            manager.status("Gecko")["runtime"]["alive"]
            and manager.status("Gecko")["runtime"]["pid"] != gecko_pid
        )
    )
    assert manager.status("Donkey")["runtime"]["alive"]
    assert manager.registry.get_runtime("Gecko").generation == 2
    manager.request_stop("Gecko")
    manager.request_stop("Donkey")
    manager.reconcile_once()


def test_supervisor_reconcile_loop(tmp_path: Path, manager: InstanceManager) -> None:
    manager.create_profile(
        "Keep", "Keep", visible_console=False, telegram_mode="disabled"
    )
    manager.request_start("Keep")
    manager.reconcile_once()
    wait_until(lambda: manager.status("Keep")["runtime"]["alive"])
    pid_before = manager.status("Keep")["runtime"]["pid"]
    assert pid_before is not None

    os.kill(pid_before, 9)
    psutil.Process(pid_before).wait(timeout=5)
    manager.reconcile_once()
    wait_until(
        lambda: (
            manager.status("Keep")["runtime"]["alive"]
            and manager.status("Keep")["runtime"]["pid"] != pid_before
        )
    )
    manager.request_stop("Keep")
    manager.reconcile_once()


def test_quarantine_at_restart_limit(tmp_path: Path, manager: InstanceManager) -> None:
    manager.create_profile(
        "Boom", "Boom", visible_console=False,
        restart_limit=1, restart_window_sec=60,
        telegram_mode="disabled",
    )
    manager.request_start("Boom")
    manager.reconcile_once()
    wait_until(lambda: manager.status("Boom")["runtime"]["alive"])
    pid = manager.status("Boom")["runtime"]["pid"]
    os.kill(pid, 9)
    psutil.Process(pid).wait(timeout=5)
    manager.reconcile_once()
    wait_until(lambda: manager.status("Boom")["runtime"]["alive"])
    pid2 = manager.status("Boom")["runtime"]["pid"]

    os.kill(pid2, 9)
    psutil.Process(pid2).wait(timeout=5)
    manager.reconcile_once()
    wait_until(
        lambda: manager.registry.get_runtime("Boom").state == "quarantined"
    )
    assert not manager.status("Boom")["runtime"]["alive"]
    manager.registry.set_desired_state("Boom", "stopped")


def test_template_self_reference_is_rejected() -> None:
    with pytest.raises(ValueError, match="cannot be its own template"):
        InstanceProfile(
            instance_id="Self", display_name="Self",
            template_profile="Self",
        )


def test_invalid_instance_id_rejected() -> None:
    with pytest.raises(ValueError, match="instance ID"):
        get_instance_paths({"JAWL_INSTANCE_ID": "../escape"})


def test_legacy_default_paths_preserved() -> None:
    paths = get_instance_paths({})
    assert paths.instance_id == "default"
    assert paths.legacy_default is True
