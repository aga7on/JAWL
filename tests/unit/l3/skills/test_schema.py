import json
import pytest

from src.l3_agent.skills.schema import parse_llm_json


def payload(tool_name="HostOSCodingWorkspaces.create_coding_workspace"):
    return {
        "observation": "repository needs a task workspace",
        "reasoning": "create it before reading task-relative files",
        "reflection": "use an explicit dependency plan",
        "actions": [
            {
                "tool_name": tool_name,
                "parameters": {
                    "repository_path": "sandbox/repo",
                    "task_id": "eval-task",
                },
            }
        ],
    }


def test_parser_recovers_last_valid_action_payload_from_qwen_format_deliberation():
    valid = json.dumps(payload())
    leaked = (
        '<tool_call>{"observation":"...", actions:[...]}</tool_call>\n'
        "The parser may expect raw JSON, so I should call the tool now.\n"
        f"<tool_call>{valid}</tool_call>"
    )

    parsed, error = parse_llm_json(leaked)

    assert error is None
    assert parsed is not None
    assert parsed.actions[0].tool_name == (
        "HostOSCodingWorkspaces.create_coding_workspace"
    )


def test_parser_recovers_openai_execute_skill_wrapper_from_noisy_text():
    wrapped = {
        "name": "execute_skill",
        "arguments": json.dumps(payload("HostOSCodingFiles.read_coding_file_range")),
    }
    leaked = "I will invoke it as metadata:\n" + json.dumps(wrapped)

    parsed, error = parse_llm_json(leaked)

    assert error is None
    assert parsed is not None
    assert parsed.actions[0].tool_name == "HostOSCodingFiles.read_coding_file_range"


def test_parser_does_not_treat_embedded_empty_example_as_completion():
    example = json.dumps(
        {
            "observation": "example",
            "reasoning": "example",
            "reflection": "example",
            "actions": [],
        }
    )

    parsed, error = parse_llm_json("Perhaps I should return this: " + example)

    assert parsed is None
    assert "Invalid JSON format" in error


def test_parser_accepts_compact_goal_v2_action():
    compact = {
        "v": 2,
        "state": "act",
        "calls": [
            {
                "tool": "HostOSCodingFiles.read_coding_file",
                "args": {"task_id": "t", "relative_path": "src/app.py"},
                "action_id": "read",
            }
        ],
        "note": "Inspect the target.",
    }

    parsed, error = parse_llm_json(json.dumps(compact))

    assert error is None
    assert parsed.protocol_version == 2
    assert parsed.goal_state == "act"
    assert parsed.actions[0].tool_name == "HostOSCodingFiles.read_coding_file"
    assert parsed.actions[0].action_id == "read"
    assert "Inspect" in parsed.thoughts


def test_parser_accepts_sparse_task_ledger_checkpoint():
    payload = {
        "v": 2,
        "state": "act",
        "calls": [
            {
                "tool": "MCPTools.call_tool",
                "args": {"server": "x64dbg-mcp"},
            }
        ],
        "note": "Continue from the local checkpoint.",
        "ledger": {
            "phase": "debug_exception",
            "completed_add": ["Confirmed debugger is paused"],
            "pending_steps": ["Read call stack"],
            "facts_add": ["Sotis is paused"],
            "failures_add": [
                {
                    "action": "UIA observation",
                    "reason": "Timed out",
                    "retry_when": "Process is responsive",
                }
            ],
            "next_action": "Read the call stack through x64dbg-mcp",
        },
    }

    parsed, error = parse_llm_json(json.dumps(payload))

    assert error is None
    assert parsed.ledger.phase == "debug_exception"
    assert parsed.ledger.pending_steps == ["Read call stack"]
    assert parsed.ledger.failures_add[0].retry_when == "Process is responsive"
    assert parsed.actions[0].tool_name == "MCPTools.call_tool"


def test_goal_parser_repairs_single_dependency_and_ledger_removals():
    payload = {
        "v": 2,
        "state": "act",
        "calls": [
            {
                "tool": "MCPTools.call_tool",
                "args": {"server": "x64dbg-mcp", "tool": "GetCallStack"},
                "depends_on": "launch",
                "resources": "debugger",
            }
        ],
        "ledger": {
            "facts_remove": "Launcher was not created",
            "failures_remove": "Old launcher attempt",
            "tool_state_remove": "mcp:x64dbg:old",
        },
    }

    parsed, error = parse_llm_json(json.dumps(payload))

    assert error is None
    assert parsed.actions[0].depends_on == ["launch"]
    assert parsed.actions[0].resources == ["debugger"]
    assert parsed.ledger.facts_remove == ["Launcher was not created"]
    assert parsed.ledger.failures_remove == ["Old launcher attempt"]
    assert parsed.ledger.tool_state_remove == ["mcp:x64dbg:old"]


def test_goal_parser_repairs_mapping_tool_state_and_bounds_wide_patch():
    payload = {
        "v": 2,
        "state": "act",
        "calls": [
            {
                "tool": "MCPTools.call_tool",
                "args": {},
            }
        ],
        "ledger": {
            "tool_state_add": {
                f"x64dbg.Tool{index:02d}": f"{index:064x}"
                for index in range(30)
            }
        },
    }

    parsed, error = parse_llm_json(json.dumps(payload))

    assert error is None
    assert parsed is not None
    assert len(parsed.ledger.tool_state_add) == 20
    assert parsed.ledger.tool_state_add[0].startswith("x64dbg.Tool00 | schema=")
    assert parsed.ledger.tool_state_add[-1].startswith("x64dbg.Tool19 | schema=")


@pytest.mark.parametrize("state", ["done", "wait", "blocked"])
def test_parser_accepts_explicit_goal_terminal_states(state):
    payload = {"v": 2, "state": state, "summary": f"{state} evidence"}
    if state == "wait":
        payload["wake_after_seconds"] = 30

    parsed, error = parse_llm_json(json.dumps(payload))

    assert error is None
    assert parsed.goal_state == state
    assert parsed.actions == []
    assert parsed.goal_summary == f"{state} evidence"


@pytest.mark.parametrize(
    "payload",
    [
        {"v": 2, "state": "act", "calls": []},
        {"v": 2, "state": "done", "summary": "", "calls": []},
        {
            "v": 2,
            "state": "wait",
            "summary": "later",
            "wake_after_seconds": 0,
        },
        {
            "v": 2,
            "state": "done",
            "summary": "done",
            "calls": [{"tool": "unsafe", "args": {}}],
        },
    ],
)
def test_parser_rejects_invalid_goal_v2(payload):
    parsed, error = parse_llm_json(json.dumps(payload))

    assert parsed is None
    assert "Invalid JSON format" in error
