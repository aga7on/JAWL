import asyncio

import pytest

from src.l2_interfaces.host.os.coding_approval_notifications import (
    HostOSCodingApprovalNotifications,
)
from src.l2_interfaces.telegram.coding_approval_notifications import (
    TelegramCodingApprovalNotifications,
)
from src.utils.event.bus import EventBus
from src.utils.event.registry import Events


def approval():
    return {
        "id": "0123456789abcdef",
        "status": "pending",
        "task_id": "task-1",
        "backend": "container",
        "argv_preview": '["python", "--token", "[REDACTED]"]',
        "workspace_fingerprint": "a" * 64,
        "relative_cwd": ".",
        "timeout_seconds": 60,
        "created_at": 1000.0,
        "expires_at": 1300.0,
    }


@pytest.mark.asyncio
async def test_desktop_approval_push_is_lifecycle_managed_and_redacted(monkeypatch):
    bus = EventBus()
    notifier = HostOSCodingApprovalNotifications(bus)
    calls = []
    monkeypatch.setattr(
        notifier,
        "_show_notification",
        lambda title, message: calls.append((title, message)),
    )
    await notifier.start()
    await bus.publish(Events.CODING_APPROVAL_REQUESTED, approval=approval())
    await bus.stop()
    assert len(calls) == 1
    assert calls[0][0] == "JAWL approval required"
    assert "0123456789abcdef" in calls[0][1]
    assert "[REDACTED]" in calls[0][1]

    await notifier.stop()
    await bus.publish(Events.CODING_APPROVAL_REQUESTED, approval=approval())
    await asyncio.sleep(0)
    assert len(calls) == 1


class FakeSender:
    def __init__(self):
        self.calls = []

    async def send_message(self, *args, **kwargs):
        self.calls.append((args, kwargs))


class FakeTelethonClient:
    def __init__(self):
        self.sender = FakeSender()

    def client(self):
        return self.sender


class FakeAiogramClient:
    def __init__(self):
        self.sender = FakeSender()

    def bot(self):
        return self.sender


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("transport", "client"),
    [
        ("telethon", FakeTelethonClient()),
        ("aiogram", FakeAiogramClient()),
    ],
)
async def test_telegram_approval_push_uses_configured_transport(
    transport, client
):
    bus = EventBus()
    notifier = TelegramCodingApprovalNotifications(
        bus, client, "12345", transport
    )
    await notifier.start()
    await bus.publish(Events.CODING_APPROVAL_REQUESTED, approval=approval())
    await bus.stop()
    assert len(client.sender.calls) == 1
    args, kwargs = client.sender.calls[0]
    message = args[1] if transport == "telethon" else kwargs["text"]
    target = args[0] if transport == "telethon" else kwargs["chat_id"]
    assert target == 12345
    assert "Approve: python jawl.py --approvals approve <ID>" in message
    assert "[REDACTED]" in message
    await notifier.stop()
