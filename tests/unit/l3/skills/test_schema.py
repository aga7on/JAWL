import json

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
