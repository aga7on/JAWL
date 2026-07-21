import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.l2_interfaces.host.os.coding_approvals import CodingApprovalStore
from src.l2_interfaces.telegram.coding_approval_notifications import (
    TelegramCodingApprovalControl,
)
from src.utils.event.bus import EventBus
from src.utils.event.registry import Events


def approval_subject():
    return CodingApprovalStore.build_subject(
        task_id="task-remote",
        backend="host",
        argv=["python", "--token", "never-publish-this", "-m", "pytest"],
        workspace_fingerprint="a" * 64,
        relative_cwd=".",
        timeout_seconds=60,
        execution_identity={"kind": "host", "executable_sha256": "b" * 64},
    )


class FakeSender:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def send_message(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.fail:
            raise ConnectionError("offline")


class FakeClient:
    def __init__(self, transport, fail=False):
        self.transport = transport
        self.sender = FakeSender(fail=fail)

    def client(self):
        return self.sender

    def bot(self):
        return self.sender


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["telethon", "aiogram"])
async def test_remote_approval_is_actor_chat_bound_one_shot_and_redacted(
    tmp_path, transport
):
    store = CodingApprovalStore(tmp_path / "coding_approvals.json")
    request = store.request(approval_subject(), ttl_seconds=300)
    bus = MagicMock(spec=EventBus)
    bus.publish = AsyncMock()
    client = FakeClient(transport)
    store_resolver = MagicMock(return_value=store)
    control = TelegramCodingApprovalControl(
        bus,
        client,
        store_resolver,
        chat_id=-100123,
        actor_id=456,
        transport=transport,
    )
    command = f"/jawl_approve {request['id']}"

    assert not await control.handle_message(
        raw_text="ordinary message",
        chat_id=-100123,
        sender_id=456,
        message_id=1,
    )
    assert not await control.handle_message(
        raw_text=command + " ",
        chat_id=-100123,
        sender_id=456,
        message_id=2,
    )
    assert await control.handle_message(
        raw_text=command,
        chat_id=-100999,
        sender_id=456,
        message_id=3,
    )
    assert store.get(request["id"])["status"] == "pending"
    assert client.sender.calls == []

    assert await control.handle_message(
        raw_text=command,
        chat_id=-100123,
        sender_id=999,
        message_id=4,
    )
    assert store.get(request["id"])["status"] == "pending"
    store_resolver.assert_not_called()
    bus.publish.assert_not_awaited()

    assert await control.handle_message(
        raw_text=command,
        chat_id=-100123,
        sender_id=456,
        message_id=5,
    )
    approved = store.get(request["id"])
    assert approved["status"] == "approved"
    assert approved["actor"] == (
        f"telegram:{transport}:chat:-100123:actor:456:message:5"
    )
    bus.publish.assert_awaited_once()
    (event,) = bus.publish.await_args.args
    payload = bus.publish.await_args.kwargs
    assert event is Events.CODING_APPROVAL_DECIDED
    assert payload["approval"] == approved
    serialized = json.dumps(payload)
    assert "never-publish-this" not in serialized
    assert '"argv"' not in serialized
    assert "/jawl_approve" not in serialized
    store_resolver.assert_called_once_with()

    assert await control.handle_message(
        raw_text=f"/jawl_deny {request['id']}",
        chat_id=-100123,
        sender_id=456,
        message_id=6,
    )
    assert store.get(request["id"])["status"] == "approved"
    assert bus.publish.await_count == 1
    assert store_resolver.call_count == 2


@pytest.mark.asyncio
async def test_remote_approval_expiry_and_ack_failure_cannot_hide_decision(
    tmp_path, monkeypatch
):
    clock = {"now": 1000.0}
    monkeypatch.setattr(
        "src.l2_interfaces.host.os.coding_approvals.time.time",
        lambda: clock["now"],
    )
    store = CodingApprovalStore(tmp_path / "coding_approvals.json")
    expired_request = store.request(approval_subject(), ttl_seconds=60)
    bus = MagicMock(spec=EventBus)
    bus.publish = AsyncMock()
    control = TelegramCodingApprovalControl(
        bus,
        FakeClient("telethon", fail=True),
        lambda: store,
        chat_id=123,
        actor_id=456,
        transport="telethon",
    )

    clock["now"] = 1061.0
    assert await control.handle_message(
        raw_text=f"/jawl_approve {expired_request['id']}",
        chat_id=123,
        sender_id=456,
        message_id=7,
    )
    assert store.get(expired_request["id"])["status"] == "expired"
    bus.publish.assert_not_awaited()

    fresh_request = store.request(
        approval_subject() | {"timeout_seconds": 61}, ttl_seconds=60
    )
    assert await control.handle_message(
        raw_text=f"/jawl_deny {fresh_request['id']}",
        chat_id=123,
        sender_id=456,
        message_id=8,
    )
    assert store.get(fresh_request["id"])["status"] == "denied"
    bus.publish.assert_awaited_once()
