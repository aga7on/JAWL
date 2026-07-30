from pathlib import Path

import pytest

from src.instances.paths import (
    bootstrap_instance_layout,
    get_instance_paths,
    validate_instance_id,
)


def test_default_instance_preserves_legacy_layout(tmp_path: Path) -> None:
    paths = get_instance_paths({}, tmp_path)

    assert paths.instance_id == "default"
    assert paths.legacy_default is True
    assert paths.data_dir == tmp_path / "src" / "utils" / "local" / "data"
    assert paths.config_dir == tmp_path / "config"
    assert paths.log_dir == tmp_path / "logs"
    assert paths.prompt_dir == tmp_path / "src" / "l3_agent" / "prompt"
    assert paths.sandbox_dir == tmp_path / "sandbox"
    assert paths.private_sandbox_system_dir == tmp_path / "sandbox" / "_system"


def test_named_instance_isolates_private_state_and_shares_sandbox(
    tmp_path: Path,
) -> None:
    paths = get_instance_paths({"JAWL_INSTANCE_ID": "Donkey"}, tmp_path)

    home = tmp_path / "runtime" / "instances" / "Donkey"
    assert paths.legacy_default is False
    assert paths.instance_home == home
    assert paths.data_dir == home / "data"
    assert paths.config_dir == home / "config"
    assert paths.log_dir == home / "logs"
    assert paths.prompt_dir == home / "prompts"
    assert paths.sandbox_dir == tmp_path / "sandbox"
    assert paths.private_sandbox_system_dir == (
        tmp_path / "sandbox" / "_system" / "instances" / "Donkey"
    )
    assert paths.terminal_port_file.is_relative_to(paths.data_dir)


def test_explicit_instance_paths_are_exported_to_children(tmp_path: Path) -> None:
    home = tmp_path / "profiles" / "Gecko"
    paths = get_instance_paths(
        {
            "JAWL_INSTANCE_ID": "Gecko",
            "JAWL_INSTANCE_HOME": str(home),
            "JAWL_SANDBOX_DIR": str(tmp_path / "shared"),
        },
        tmp_path,
    )

    exported = paths.child_environment()
    assert exported["JAWL_INSTANCE_ID"] == "Gecko"
    assert Path(exported["JAWL_DATA_DIR"]) == home / "data"
    assert Path(exported["JAWL_SANDBOX_DIR"]) == tmp_path / "shared"


def test_named_instance_bootstrap_copies_only_missing_profile_files(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config"
    prompts = tmp_path / "src" / "l3_agent" / "prompt" / "personality"
    config.mkdir(parents=True)
    prompts.mkdir(parents=True)
    (config / "settings.example.yaml").write_text("system: {}\n")
    (config / "settings.yaml").write_text("system: {shared: true}\n")
    (prompts / "SOUL.md").write_text("shared soul")
    paths = get_instance_paths({"JAWL_INSTANCE_ID": "Gecko"}, tmp_path)

    bootstrap_instance_layout(paths)
    (paths.prompt_dir / "personality" / "SOUL.md").write_text("Gecko soul")
    bootstrap_instance_layout(paths)

    assert (paths.config_dir / "settings.yaml").read_text() == (
        "system: {shared: true}\n"
    )
    assert (paths.prompt_dir / "personality" / "SOUL.md").read_text() == (
        "Gecko soul"
    )


@pytest.mark.parametrize(
    "value", ["", "../escape", "has space", "-leading", "name/child", "x" * 65]
)
def test_invalid_instance_ids_are_rejected(value: str) -> None:
    with pytest.raises(ValueError, match="instance ID"):
        validate_instance_id(value)
