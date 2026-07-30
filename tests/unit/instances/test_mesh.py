import json
from pathlib import Path

import pytest

from src.instances.mesh import InstanceMesh
from src.instances.models import InstanceProfile
from src.instances.registry import InstanceRegistry


def build_mesh(tmp_path: Path):
    registry = InstanceRegistry(tmp_path / "registry.json")
    registry.create_profile(
        InstanceProfile(instance_id="Gecko", display_name="Gecko")
    )
    registry.create_profile(
        InstanceProfile(instance_id="Donkey", display_name="Donkey")
    )
    path = tmp_path / "mesh.json"
    return (
        InstanceMesh("Gecko", registry, path),
        InstanceMesh("Donkey", registry, path),
    )


@pytest.mark.asyncio
async def test_mesh_delivers_bounded_messages_between_isolated_instances(
    tmp_path: Path,
) -> None:
    gecko, donkey = build_mesh(tmp_path)

    sent = await gecko.send_instance_message(
        "Donkey", "Inspect shared module", "delegation", "task-1"
    )
    inbox = await donkey.read_instance_messages()
    messages = json.loads(inbox.message)
    context = await donkey.get_context_block()

    assert sent.is_success
    assert messages[0]["sender"] == "Gecko"
    assert messages[0]["recipient"] == "Donkey"
    assert messages[0]["kind"] == "delegation"
    assert "No unread peer messages" in context


@pytest.mark.asyncio
async def test_mesh_task_claim_is_atomic_and_owner_completes(
    tmp_path: Path,
) -> None:
    gecko, donkey = build_mesh(tmp_path)

    published = await gecko.publish_instance_task(
        "audit-core", "Audit core module", preferred_instance="Donkey"
    )
    wrong_claim = await gecko.claim_instance_task("audit-core")
    claimed = await donkey.claim_instance_task("audit-core")
    duplicate = await gecko.claim_instance_task("audit-core")
    completed = await donkey.complete_instance_task(
        "audit-core", "Audit complete; tests passed"
    )

    assert published.is_success
    assert not wrong_claim.is_success
    assert claimed.is_success
    assert not duplicate.is_success
    assert completed.is_success
    assert json.loads(completed.message)["status"] == "completed"
