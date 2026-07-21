import json

import pytest

from src.cli.coding_approvals import approval_store, run_approval_cli
from src.l2_interfaces.host.os.coding_approvals import CodingApprovalStore


def subject(**overrides):
    payload = CodingApprovalStore.build_subject(
        task_id="task-1",
        backend="host",
        argv=["python", "--token", "very-secret-value", "-m", "pytest"],
        workspace_fingerprint="a" * 64,
        relative_cwd=".",
        timeout_seconds=60,
        execution_identity={"kind": "host", "executable_sha256": "b" * 64},
    )
    payload.update(overrides)
    return payload


def test_approval_is_exact_one_shot_and_registry_redacts_argv(tmp_path):
    path = tmp_path / "coding_approvals.json"
    store = CodingApprovalStore(path)
    exact = subject()
    request = store.request(exact, ttl_seconds=300)

    persisted = path.read_text(encoding="utf-8")
    assert "very-secret-value" not in persisted
    assert "[REDACTED]" in request["argv_preview"]
    approved = store.decide(request["id"], approved=True, actor="test-operator")
    assert approved["status"] == "approved"

    with pytest.raises(PermissionError, match="does not match"):
        store.consume(request["id"], subject(timeout_seconds=61))
    with pytest.raises(PermissionError, match="does not match"):
        store.consume(
            request["id"],
            subject(
                execution_identity={
                    "kind": "host",
                    "executable_sha256": "c" * 64,
                }
            ),
        )

    consumed = store.consume(request["id"], exact)
    assert consumed["status"] == "consumed"
    with pytest.raises(PermissionError, match="consumed"):
        store.consume(request["id"], exact)


def test_approval_expires_and_ids_are_bounded(tmp_path, monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(
        "src.l2_interfaces.host.os.coding_approvals.time.time",
        lambda: clock["now"],
    )
    store = CodingApprovalStore(tmp_path / "coding_approvals.json")
    request = store.request(subject(), ttl_seconds=60)
    store.decide(request["id"], approved=True, actor="operator")
    clock["now"] = 1061.0

    assert store.get(request["id"])["status"] == "expired"
    with pytest.raises(ValueError, match="16 hexadecimal"):
        store.get("unbounded-or-invalid-id")


def test_local_cli_lists_and_decides_pending_requests(tmp_path, capsys):
    store = approval_store(tmp_path)
    request = store.request(subject(), ttl_seconds=300)

    assert run_approval_cli(tmp_path, ["list", "--status", "pending"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [item["id"] for item in listed] == [request["id"]]

    assert run_approval_cli(tmp_path, ["approve", request["id"]]) == 0
    approved = json.loads(capsys.readouterr().out)
    assert approved["status"] == "approved"
    assert approved["actor"] == "local-cli"
