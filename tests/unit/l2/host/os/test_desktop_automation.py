from types import SimpleNamespace

import pytest

from src.l2_interfaces.host.os.desktop_automation import (
    DesktopAutomationError,
    WindowsDesktopAutomation,
)


class Rectangle:
    left = 10
    top = 20
    right = 210
    bottom = 120


class FakeElement:
    def __init__(
        self,
        name,
        runtime_id,
        *,
        control_type="Button",
        automation_id="",
        children=None,
        value=None,
    ):
        self.handle = runtime_id[0]
        self._children = children or []
        self._value = value
        self._focused = False
        self._selected = False
        self._toggle = 0
        self.invoked = False
        self.element_info = SimpleNamespace(
            name=name,
            automation_id=automation_id,
            control_type=control_type,
            class_name="FakeClass",
            rectangle=Rectangle(),
            runtime_id=runtime_id,
            process_id=123,
            is_password=False,
        )

    def children(self):
        return list(self._children)

    def window_text(self):
        return self.element_info.name

    def rectangle(self):
        return self.element_info.rectangle

    def is_enabled(self):
        return True

    def is_visible(self):
        return True

    def has_keyboard_focus(self):
        return self._focused

    def get_value(self):
        if self._value is None:
            raise RuntimeError("no value pattern")
        return self._value

    def is_selected(self):
        return self._selected

    def get_toggle_state(self):
        return self._toggle

    def get_expand_state(self):
        raise RuntimeError("no expand pattern")

    def wrapper_object(self):
        return self

    def invoke(self):
        self.invoked = True
        self._toggle = 1

    def click_input(self):
        self.invoke()

    def set_focus(self):
        self._focused = True

    def set_edit_text(self, value):
        self._value = value

    def toggle(self):
        self._toggle = 1 - self._toggle

    def select(self):
        self._selected = True

    def expand(self):
        pass

    def collapse(self):
        pass


class FakeDesktop:
    def __init__(self, window):
        self._window = window

    def windows(self, visible_only=True):
        return [self._window]

    def window(self, handle):
        assert handle == self._window.handle
        return self._window


def _client(monkeypatch):
    edit = FakeElement(
        "Document",
        (2, 20),
        control_type="Edit",
        automation_id="editor",
        value="before",
    )
    button = FakeElement("Save", (3, 30), automation_id="save")
    window = FakeElement(
        "Editor", (1, 10), control_type="Window", children=[edit, button]
    )
    client = WindowsDesktopAutomation(max_elements=20, max_result_chars=10000)
    monkeypatch.setattr(client, "_desktop", lambda: FakeDesktop(window))
    monkeypatch.setattr("win32gui.GetForegroundWindow", lambda: window.handle)
    return client, edit, button


def _element(snapshot, automation_id):
    return next(
        element
        for window in snapshot["windows"]
        for element in window["elements"]
        if element["automation_id"] == automation_id
    )


def test_semantic_desktop_observe_act_and_verify(monkeypatch):
    client, edit, _ = _client(monkeypatch)
    snapshot = client.observe(window_title="Editor")
    target = _element(snapshot, "editor")

    result = client.act(
        element_ref=target["element_ref"],
        expected_element_sha256=target["element_sha256"],
        action="set_value",
        value="after",
        settle_sec=0,
    )

    assert edit._value == "after"
    assert result["dispatched"] is True
    assert result["verified"] is True
    assert result["after"]["value"] == "after"


def test_semantic_desktop_rejects_stale_target_before_action(monkeypatch):
    client, _, button = _client(monkeypatch)
    snapshot = client.observe(window_title="Editor")
    target = _element(snapshot, "save")
    button.element_info.name = "Save As"

    with pytest.raises(DesktopAutomationError, match="changed after observation"):
        client.act(
            element_ref=target["element_ref"],
            expected_element_sha256=target["element_sha256"],
            action="invoke",
            settle_sec=0,
        )
    assert button.invoked is False


def test_semantic_desktop_wait_requires_selector_and_returns_exact_ref(monkeypatch):
    client, _, _ = _client(monkeypatch)
    with pytest.raises(DesktopAutomationError, match="selector"):
        client.wait_for_element(timeout_sec=0.1)

    result = client.wait_for_element(
        window_title="Editor",
        automation_id="save",
        timeout_sec=0.1,
    )
    assert result["condition_met"] is True
    assert result["matches"][0]["element_ref"].startswith("element:")


def test_semantic_desktop_observation_enforces_serialized_budget(monkeypatch):
    children = [
        FakeElement("control-" + ("x" * 300), (index + 2, index + 20))
        for index in range(20)
    ]
    window = FakeElement(
        "Large Editor", (1, 10), control_type="Window", children=children
    )
    client = WindowsDesktopAutomation(
        max_elements=50, max_text_chars=500, max_result_chars=1800
    )
    monkeypatch.setattr(client, "_desktop", lambda: FakeDesktop(window))
    monkeypatch.setattr("win32gui.GetForegroundWindow", lambda: window.handle)

    snapshot = client.observe(window_title="Editor")

    import json

    assert len(json.dumps(snapshot, ensure_ascii=False)) <= 1800
    assert snapshot["truncated"] is True
    assert snapshot["returned_element_count"] < snapshot["element_count"]
