"""
Universal Pydantic schema for agent tool calls.

Defines the JSON Schema expected by the OpenAI API (or compatible).
Includes an heuristic parser (Dirty JSON Repair) to extract data
if the LLM violates formatting or escaping.
"""

import re
import json
from typing import Any, Dict, List, Literal, Tuple, Optional
from pydantic import BaseModel, Field, field_validator

from src.l3_agent.goals.ledger import TaskLedgerPatch


class ActionCall(BaseModel):
    tool_name: str
    parameters: Dict[str, Any] = Field(default_factory=dict)
    action_id: Optional[str] = None
    depends_on: List[str] = Field(default_factory=list)
    parallel_group: Optional[str] = None
    resources: List[str] = Field(default_factory=list)

    @field_validator("depends_on", "resources", mode="before")
    @classmethod
    def _coerce_string_list(cls, value: Any) -> Any:
        """Repair a common single-string form for list-valued fields."""

        if value is None:
            return []
        if isinstance(value, str):
            return [value] if value.strip() else []
        return value


class AgentResponse(BaseModel):
    """
    Typed schema of the complete LLM response.
    Contains structured internal monologue (CoT) and an array of actions.
    """

    observation: str = ""
    reasoning: str = ""
    reflection: str = ""
    actions: List[ActionCall] = Field(default_factory=list)
    protocol_version: int = 1
    goal_state: Literal["", "act", "continue", "wait", "done", "blocked"] = ""
    goal_summary: str = ""
    wake_after_seconds: Optional[int] = None
    ledger: Optional[TaskLedgerPatch] = None

    @property
    def thoughts(self) -> str:
        """
        Concatenates the structured CoT into a single string for logs and databases.
        """

        parts = []
        if self.observation.strip():
            parts.append(f"\n[Observation]: {self.observation.strip()}")
        if self.reasoning.strip():
            parts.append(f"\n[Reasoning]: {self.reasoning.strip()}")
        if self.reflection.strip():
            parts.append(f"\n[Reflection]: {self.reflection.strip()}")
        return "\n".join(parts)


ACTION_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "execute_skill",
            "description": "Main interface for interacting with the external environment. Mandatory to call for any actions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "observation": {
                        "type": "string",
                        "description": "Observation of results.",
                    },
                    "reasoning": {
                        "type": "string",
                        "description": "Description of the logic behind the next actions.",
                    },
                    "reflection": {
                        "type": "string",
                        "description": "Reflection or internal thoughts in a completely free format. Hypotheses, intermediate conclusions, or memos for the future.",
                    },
                    "actions": {
                        "type": "array",
                        "description": "List of actions to execute.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "tool_name": {
                                    "type": "string",
                                    "description": "Exact name of the function.",
                                },
                                "parameters": {
                                    "type": "object",
                                    "description": "Dictionary containing the arguments.",
                                    "additionalProperties": True,
                                },
                                "action_id": {
                                    "type": "string",
                                    "description": "Optional unique ID used by later actions in depends_on.",
                                },
                                "depends_on": {
                                    "type": "array",
                                    "description": "IDs of actions that must complete successfully first.",
                                    "items": {"type": "string"},
                                },
                                "parallel_group": {
                                    "type": "string",
                                    "description": "Optional group name. Only ready actions in the same explicit group may run concurrently; actions are sequential by default.",
                                },
                                "resources": {
                                    "type": "array",
                                    "description": "Optional shared resource identifiers used to prevent concurrent conflicting access.",
                                    "items": {"type": "string"},
                                },
                            },
                            "required": ["tool_name", "parameters"],
                            "additionalProperties": False,
                        },
                    },
                    "v": {
                        "type": "integer",
                        "enum": [2],
                        "description": "Goal Protocol version. Use only with state/calls.",
                    },
                    "state": {
                        "type": "string",
                        "enum": ["act", "continue", "wait", "done", "blocked"],
                        "description": "Explicit Goal Mode lifecycle decision.",
                    },
                    "calls": {
                        "type": "array",
                        "description": "Compact Goal Mode actions.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "tool": {"type": "string"},
                                "args": {
                                    "type": "object",
                                    "additionalProperties": True,
                                },
                                "action_id": {"type": "string"},
                                "depends_on": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "parallel_group": {"type": "string"},
                                "resources": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                            },
                            "required": ["tool", "args"],
                            "additionalProperties": False,
                        },
                    },
                    "note": {
                        "type": "string",
                        "description": "Optional short operational note, not chain of thought.",
                    },
                    "summary": {
                        "type": "string",
                        "description": "Concrete completion, wait, or blocker summary.",
                    },
                    "wake_after_seconds": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 86400,
                    },
                    "ledger": {
                        "type": "object",
                        "description": (
                            "Sparse durable Goal checkpoint. Omitted fields "
                            "remain unchanged; include only operational state, "
                            "never chain-of-thought."
                        ),
                        "properties": {
                            "phase": {"type": "string", "maxLength": 300},
                            "acceptance_criteria": {
                                "type": "array",
                                "maxItems": 20,
                                "items": {"type": "string"},
                            },
                            "completed_add": {
                                "type": "array",
                                "maxItems": 20,
                                "items": {"type": "string"},
                            },
                            "completed_steps": {
                                "type": "array",
                                "maxItems": 20,
                                "items": {"type": "string"},
                            },
                            "completed_remove": {
                                "type": "array",
                                "maxItems": 20,
                                "items": {"type": "string"},
                            },
                            "pending_steps": {
                                "type": "array",
                                "maxItems": 20,
                                "items": {"type": "string"},
                            },
                            "confirmed_facts": {
                                "type": "array",
                                "maxItems": 24,
                                "items": {"type": "string"},
                            },
                            "facts_add": {
                                "type": "array",
                                "maxItems": 20,
                                "items": {"type": "string"},
                            },
                            "facts_remove": {
                                "type": "array",
                                "maxItems": 24,
                                "items": {"type": "string"},
                            },
                            "hypotheses": {
                                "type": "array",
                                "maxItems": 12,
                                "items": {"type": "string"},
                            },
                            "failures": {
                                "type": "array",
                                "maxItems": 16,
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "action": {"type": "string"},
                                        "reason": {"type": "string"},
                                        "retry_when": {"type": "string"},
                                    },
                                    "required": ["action", "reason"],
                                    "additionalProperties": False,
                                },
                            },
                            "failures_remove": {
                                "type": "array",
                                "maxItems": 16,
                                "items": {"type": "string"},
                            },
                            "failures_add": {
                                "type": "array",
                                "maxItems": 12,
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "action": {"type": "string"},
                                        "reason": {"type": "string"},
                                        "retry_when": {"type": "string"},
                                    },
                                    "required": ["action", "reason"],
                                    "additionalProperties": False,
                                },
                            },
                            "artifacts_add": {
                                "type": "array",
                                "maxItems": 20,
                                "items": {"type": "string"},
                            },
                            "artifacts": {
                                "type": "array",
                                "maxItems": 20,
                                "items": {"type": "string"},
                            },
                            "artifacts_remove": {
                                "type": "array",
                                "maxItems": 20,
                                "items": {"type": "string"},
                            },
                            "tool_state_add": {
                                "type": "array",
                                "maxItems": 20,
                                "items": {"type": "string"},
                            },
                            "tool_state": {
                                "type": "array",
                                "maxItems": 24,
                                "items": {"type": "string"},
                            },
                            "tool_state_remove": {
                                "type": "array",
                                "maxItems": 24,
                                "items": {"type": "string"},
                            },
                            "blockers": {
                                "type": "array",
                                "maxItems": 12,
                                "items": {"type": "string"},
                            },
                            "next_action": {
                                "type": "string",
                                "maxLength": 1000,
                            },
                            "checkpoint_summary": {
                                "type": "string",
                                "maxLength": 1200,
                            },
                        },
                        "additionalProperties": False,
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
        },
    }
]


def _decoded_json_values(text: str, opener: str, limit: int = 200) -> List[Any]:
    """Decode bounded JSON values starting at candidate delimiters in noisy text."""

    decoder = json.JSONDecoder(strict=False)
    decoded = []
    for index, match in enumerate(re.finditer(re.escape(opener), text)):
        if index >= limit:
            break
        try:
            value, _ = decoder.raw_decode(text, match.start())
        except (TypeError, json.JSONDecodeError):
            continue
        decoded.append(value)
    return decoded


def _agent_payload(data: Any, allow_empty: bool = False) -> Optional[AgentResponse]:
    """Normalize a bare payload or OpenAI-style execute_skill wrapper."""

    if not isinstance(data, dict):
        return None
    function = data.get("function")
    if isinstance(function, dict):
        data = function
    if data.get("name") == "execute_skill" and "arguments" in data:
        arguments = data["arguments"]
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments, strict=False)
            except (TypeError, json.JSONDecodeError):
                return None
        data = arguments
    compact = _goal_v2_payload(data)
    if compact is not None:
        if not allow_empty and compact.goal_state in {"act", "continue"} and not compact.actions:
            return None
        return compact
    required = {"observation", "reasoning", "reflection", "actions"}
    if not isinstance(data, dict) or not required.issubset(data):
        return None
    try:
        parsed = AgentResponse(**data)
    except Exception:
        return None
    if not allow_empty and not parsed.actions:
        return None
    return parsed


def _goal_v2_payload(data: Any) -> Optional[AgentResponse]:
    """Normalize the compact discriminated Goal protocol into AgentResponse."""

    if not isinstance(data, dict) or data.get("v") != 2:
        return None
    state = data.get("state")
    if state not in {"act", "continue", "wait", "done", "blocked"}:
        return None
    calls = data.get("calls", [])
    if not isinstance(calls, list):
        return None
    actions = []
    for item in calls:
        if not isinstance(item, dict):
            return None
        tool_name = item.get("tool")
        parameters = item.get("args")
        if not isinstance(tool_name, str) or not tool_name.strip():
            return None
        if not isinstance(parameters, dict):
            return None
        try:
            actions.append(
                ActionCall(
                    tool_name=tool_name.strip(),
                    parameters=parameters,
                    action_id=item.get("action_id"),
                    depends_on=item.get("depends_on", []),
                    parallel_group=item.get("parallel_group"),
                    resources=item.get("resources", []),
                )
            )
        except Exception:
            return None
    if state == "act" and not actions:
        return None
    if state in {"done", "wait", "blocked"} and actions:
        return None
    wake_after = data.get("wake_after_seconds")
    if wake_after is not None and (
        isinstance(wake_after, bool)
        or not isinstance(wake_after, int)
        or wake_after < 1
        or wake_after > 86400
    ):
        return None
    note = data.get("note", "")
    summary = data.get("summary", "")
    if not isinstance(note, str) or not isinstance(summary, str):
        return None
    if state in {"done", "wait", "blocked"} and not summary.strip():
        return None
    ledger = data.get("ledger")
    if ledger is not None and not isinstance(ledger, dict):
        return None
    try:
        ledger_patch = (
            TaskLedgerPatch.model_validate(ledger)
            if ledger is not None
            else None
        )
    except Exception:
        return None
    return AgentResponse(
        observation="",
        reasoning="",
        reflection=note.strip(),
        actions=actions,
        protocol_version=2,
        goal_state=state,
        goal_summary=summary.strip(),
        wake_after_seconds=wake_after,
        ledger=ledger_patch,
    )


def _extract_json_array(text: str) -> Optional[str]:
    """
    Smart search for the 'actions' array taking into account bracket nesting and string literals.
    Capable of extracting the array even if the model puts it inside the 'thoughts' string.
    """
    match = re.search(r'\[\s*\{\s*["\']tool_name["\']', text)
    if not match:
        return None

    start = match.start()
    depth = 0
    in_string = False
    escape = False

    for i in range(start, len(text)):
        char = text[i]
        if escape:
            escape = False
            continue
        if char == "\\":
            escape = True
            continue

        if char == '"':
            in_string = not in_string
            continue

        if not in_string:
            if char == "[":
                depth += 1
            elif char == "]":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
    return None


def parse_llm_json(
    raw_answer: str, _depth: int = 0
) -> Tuple[Optional[AgentResponse], Optional[str]]:
    clean_answer = raw_answer.strip()
    # Repair only a duplicated outer JSON wrapper sometimes emitted by gateways;
    # escaped data inside the payload remains untouched.
    if re.match(r"^\{\s*\{", clean_answer):
        clean_answer = re.sub(r"^\{\s*\{", "{", clean_answer)
        clean_answer = re.sub(r"\}\s*\}$", "}", clean_answer)
    json_str = ""
    json_start = -1
    json_end = -1

    # Attempt 1: Strict parsing (looking for Markdown block)
    json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", clean_answer, re.DOTALL)
    if json_match:
        json_str = json_match.group(1)
        json_start, json_end = json_match.span()
    else:
        # Attempt 2: Looking for outer boundaries of JSON object
        start_idx = clean_answer.find('{"observation"')
        if start_idx == -1:
            start_idx = clean_answer.find("{")

        end_idx = clean_answer.rfind("}")
        if start_idx != -1 and end_idx > start_idx:
            json_str = clean_answer[start_idx : end_idx + 1]
            json_start, json_end = start_idx, end_idx + 1

    parsed_response = None
    error_msg = None

    if json_str:
        try:
            data = json.loads(json_str, strict=False)
            if isinstance(data, dict) and data.get("v") == 2:
                parsed_response = _goal_v2_payload(data)
                if parsed_response is None:
                    raise ValueError("Invalid Goal Protocol v2 payload.")
            else:
                if isinstance(data, dict) and len(data) == 1:
                    nested = next(iter(data.values()))
                    if isinstance(nested, dict) and "actions" in nested:
                        data = nested
                parsed_response = AgentResponse(**data)
        except Exception as e:
            error_msg = str(e)
    if parsed_response is not None and not parsed_response.actions:
        surrounding = clean_answer[:json_start] + clean_answer[json_end:]
        surrounding = re.sub(
            r"(?is)</?tool_call>|```(?:json)?|```", "", surrounding
        ).strip()
        if surrounding:
            parsed_response = None
            error_msg = "Embedded empty-actions example is not a terminal response."

    # Attempt 2.5: a provider may leak format deliberation before emitting a
    # valid bare JAWL payload inside <tool_call>. Decode every bounded object and
    # prefer the last structurally complete action payload. Embedded empty
    # actions are not accepted because a hypothetical example must not end a
    # live cycle.
    if parsed_response is None:
        for candidate in reversed(_decoded_json_values(clean_answer, "{")):
            parsed_response = _agent_payload(candidate, allow_empty=False)
            if parsed_response is not None:
                error_msg = None
                break

    # Attempt 3: Heuristic Fallback (JSON repair)
    if parsed_response is None or (
        not parsed_response.actions and '"tool_name"' in raw_answer
    ):
        try:
            actions_list = None
            for candidate in reversed(_decoded_json_values(clean_answer, "[")):
                if isinstance(candidate, list) and candidate and all(
                    isinstance(item, dict) and "tool_name" in item
                    for item in candidate
                ):
                    actions_list = candidate
                    break
            if actions_list is None:
                actions_raw = _extract_json_array(clean_answer)
                if actions_raw:
                    actions_list = json.loads(actions_raw, strict=False)
            if actions_list:

                parsed_response = AgentResponse(
                    observation="[Heuristic parse]",
                    reasoning="",
                    reflection=clean_answer,
                    actions=actions_list,
                )
                error_msg = None
        except Exception as e:
            error_msg = f"Heuristic parse failed: {e}"

    if (
        parsed_response is None
        and "System Error" in (error_msg or "")
        and "{" not in raw_answer
    ):
        return (
            AgentResponse(
                observation="[Plain Text]",
                reasoning="",
                reflection=raw_answer.strip(),
                actions=[],
            ),
            None,
        )

    # =========================================================================
    # ANTI-INCEPTION (JSON inside CoT)
    # =========================================================================

    if parsed_response is not None:
        thoughts_str = parsed_response.thoughts.strip()

        if (
            _depth < 3
            and ('"actions"' in thoughts_str or "'actions'" in thoughts_str)
            and ('"observation"' in thoughts_str or "'observation'" in thoughts_str)
            and (thoughts_str.startswith("{") or "```json" in thoughts_str)
        ):
            inner_parsed, _ = parse_llm_json(thoughts_str, _depth=_depth + 1)

            if inner_parsed is not None and (
                inner_parsed.actions or not parsed_response.actions
            ):
                parsed_response = inner_parsed

        return parsed_response, None

    return None, f"System Error: Invalid JSON format. Details: {error_msg}"
