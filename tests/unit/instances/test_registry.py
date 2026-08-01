from pathlib import Path

import pytest

from src.instances.models import InstanceProfile
from src.instances.registry import InstanceRegistry


def profile(name: str, **changes) -> InstanceProfile:
    return InstanceProfile(instance_id=name, display_name=name, **changes)


def test_registry_persists_profiles_runtime_and_revision(tmp_path: Path) -> None:
    registry = InstanceRegistry(tmp_path / "registry.json")
    created = registry.create_profile(profile("Gecko"))
    registry.set_desired_state("Gecko", "running")
    runtime = registry.update_runtime(
        "Gecko", state="running", pid=1234, generation=1
    )
    reopened = InstanceRegistry(tmp_path / "registry.json")

    assert created.telethon_session == "Gecko_telethon"
    assert reopened.get_profile("Gecko").desired_state == "running"
    assert runtime.pid == 1234
    assert reopened.snapshot()["revision"] == 3


def test_registry_rejects_telegram_and_port_conflicts(tmp_path: Path) -> None:
    registry = InstanceRegistry(tmp_path / "registry.json")
    registry.create_profile(
        profile(
            "Gecko",
            telegram_mode="telethon",
            telethon_session="shared",
            telegram_identity="account-a",
            static_ports=[8100],
        )
    )

    with pytest.raises(ValueError, match="Telethon session conflict"):
        registry.create_profile(
            profile(
                "Donkey",
                telegram_mode="telethon",
                telethon_session="shared",
                telegram_identity="account-b",
            )
        )
    with pytest.raises(ValueError, match="Telegram identity conflict"):
        registry.create_profile(
            profile("Donkey", telegram_identity="account-a")
        )
    with pytest.raises(ValueError, match="Static port 8100"):
        registry.create_profile(profile("Donkey", static_ports=[8100]))

    assert [item.instance_id for item in registry.list_profiles()] == ["Gecko"]


def test_disabled_profile_does_not_reserve_runtime_resources(
    tmp_path: Path,
) -> None:
    registry = InstanceRegistry(tmp_path / "registry.json")
    registry.create_profile(
        profile(
            "Gecko",
            enabled=False,
            telethon_session="shared",
            static_ports=[8100],
        )
    )
    registry.create_profile(
        profile("Donkey", telethon_session="shared", static_ports=[8100])
    )

    assert len(registry.list_profiles()) == 2


def test_profile_cannot_be_its_own_template(tmp_path: Path) -> None:
    registry = InstanceRegistry(tmp_path / "registry.json")
    registry.create_profile(profile("Gecko"))
    with pytest.raises(ValueError, match="cannot be its own template"):
        registry.create_profile(
            profile("Donkey", template_profile="Donkey")
        )
    with pytest.raises(ValueError, match="cannot be its own template"):
        registry.create_profile(
            profile("Donkey", template_profile="donkey")
        )


def test_profile_update_is_validated_atomically(tmp_path: Path) -> None:
    registry = InstanceRegistry(tmp_path / "registry.json")
    registry.create_profile(profile("Gecko", static_ports=[8100]))
    registry.create_profile(profile("Donkey", static_ports=[8200]))

    with pytest.raises(ValueError, match="Static port 8100"):
        registry.update_profile("Donkey", static_ports=[8100])

    assert registry.get_profile("Donkey").static_ports == [8200]


def test_noop_updates_do_not_rewrite_registry(tmp_path: Path) -> None:
    registry = InstanceRegistry(tmp_path / "registry.json")
    registry.create_profile(profile("Quiet"))
    registry.update_runtime("Quiet", state="crashed", last_error="stopped")
    revision = registry.snapshot()["revision"]
    modified = registry.path.stat().st_mtime_ns

    registry.update_runtime("Quiet", state="crashed", last_error="stopped")
    registry.update_profile("Quiet", desired_state="stopped")

    assert registry.snapshot()["revision"] == revision
    assert registry.path.stat().st_mtime_ns == modified
