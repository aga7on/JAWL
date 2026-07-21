import json

import pytest

from src.l3_agent.swarm.registry import DelegationRegistry


def test_registry_redacts_prompts_and_recovers_unfinished_session(tmp_path):
    path = tmp_path / "delegations.json"
    first = DelegationRegistry(path, session_id="session-one")
    first.create("deadbeef", "coder", "fix token=super-secret in parser")
    first.transition("deadbeef", "running")

    persisted = path.read_text(encoding="utf-8")
    assert "super-secret" not in persisted
    assert "[REDACTED]" in persisted

    second = DelegationRegistry(path, session_id="session-two")
    record = second.get("deadbeef")

    assert record["status"] == "interrupted"
    assert record["session_id"] == "session-one"
    assert record["finished_at"] is not None


def test_registry_enforces_transitions_filters_and_atomic_json(tmp_path):
    path = tmp_path / "delegations.json"
    registry = DelegationRegistry(path)
    registry.create("aaaaaaaa", "coder", "first")
    registry.create("bbbbbbbb", "coder", "second")
    registry.transition("aaaaaaaa", "running")
    registry.transition("aaaaaaaa", "completed", report_path="report.md")

    assert registry.list(status="completed")[0]["id"] == "aaaaaaaa"
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 1
    with pytest.raises(ValueError, match="cannot transition"):
        registry.transition("aaaaaaaa", "running")
    with pytest.raises(ValueError, match="Unsupported"):
        registry.list(status="invented")


def test_registry_refuses_corrupt_state_without_overwriting_it(tmp_path):
    path = tmp_path / "delegations.json"
    path.write_text("not-json", encoding="utf-8")

    with pytest.raises(ValueError, match="unreadable"):
        DelegationRegistry(path)

    assert path.read_text(encoding="utf-8") == "not-json"
