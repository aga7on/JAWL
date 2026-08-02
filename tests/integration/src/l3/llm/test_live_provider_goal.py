"""Live Durable Goal smoke test over a real third-party provider endpoint.

Enable with a real OpenAI-compatible server (Ollama, vLLM, LM Studio, llama.cpp,
a cloud vendor, ...)::

    JAWL_LIVE_PROVIDER=1
    JAWL_LIVE_PROVIDER_URL=http://127.0.0.1:11434/v1
    JAWL_LIVE_PROVIDER_MODEL=<exact model name>
    JAWL_LIVE_PROVIDER_KEY=<key, or omit for a local server>

The test drives the same JAWL machinery the agent uses in production: a real
``GoalManager`` on disk, a real ``LLMExecutor``, a real provider adapter and a
real temporary project. Nothing about QWB is required for it to pass.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.l0_state.agent.state import AgentState
from src.l3_agent.goals.manager import GoalManager
from src.l3_agent.llm.api_keys.rotator import APIKeyRotator
from src.l3_agent.llm.client import LLMClient
from src.l3_agent.llm.executor import LLMExecutor
from src.l3_agent.llm.providers.contracts import ProviderCapabilities, RetryPolicy
from src.l3_agent.llm.providers.factory import build_llm_provider
from src.utils.settings import LLMProviderConfig
from src.utils.token_tracker import TokenTracker

pytestmark = pytest.mark.skipif(
    os.environ.get("JAWL_LIVE_PROVIDER") != "1",
    reason="set JAWL_LIVE_PROVIDER=1 plus URL/MODEL to run the live smoke test",
)

LIVE_URL = os.environ.get("JAWL_LIVE_PROVIDER_URL", "http://127.0.0.1:11434/v1")
LIVE_MODEL = os.environ.get("JAWL_LIVE_PROVIDER_MODEL", "")
LIVE_KEY = os.environ.get("JAWL_LIVE_PROVIDER_KEY", "local_dummy_key")

logger = logging.getLogger("live-provider")


def _live_executor() -> tuple[LLMExecutor, object]:
    """Build the production provider stack for a standard OpenAI endpoint."""

    config = LLMProviderConfig(
        kind="openai_compatible",
        display_name="live-openai-compatible",
        request_timeout_seconds=900,
        read_timeout_seconds=900,
        capabilities=ProviderCapabilities(
            native_tools=True,
            json_schema=True,
            vision=False,
            video=False,
            image_generation=False,
            reasoning=False,
            context_window=32768,
            streaming=True,
            server_side_conversation=False,
        ).public(),
    )
    client = LLMClient(
        api_url=LIVE_URL,
        api_keys_rotator=APIKeyRotator([LIVE_KEY or "local_dummy_key"]),
        connect_timeout=15.0,
        read_timeout=900.0,
    )
    provider = build_llm_provider(config, client)
    executor = LLMExecutor(
        provider,
        TokenTracker(),
        retry_policy=RetryPolicy(
            transport_retries=1,
            provider_retries=2,
            invalid_response_retries=2,
            tool_protocol_retries=2,
            base_delay_seconds=0.5,
            max_delay_seconds=4.0,
        ),
    )
    return executor, provider


@pytest.mark.asyncio
async def test_live_openai_compatible_provider_is_reachable_and_declares_capabilities():
    assert LIVE_MODEL, "JAWL_LIVE_PROVIDER_MODEL must name an exact model"
    executor, provider = _live_executor()
    try:
        health = await provider.health()
        answer = await executor.execute(
            model_name=LIVE_MODEL,
            messages=[
                {"role": "system", "content": "You are terse."},
                {"role": "user", "content": "Reply with exactly one word: ready"},
            ],
            temperature=0.1,
            logger=logger,
            log_prefix="[Live]",
        )
    finally:
        await provider.close()

    assert health.status == "ok", health.detail
    assert answer and "ready" in answer.casefold()
    metrics = executor.last_call_metrics
    assert metrics["status"] == "completed"
    assert metrics["provider"] == "live-openai-compatible"
    # A standard provider must never claim a server-side conversation.
    assert metrics["capabilities"]["server_side_conversation"] is False
    assert metrics["provider_prompt_tokens"] is not None
    assert metrics["provider_completion_tokens"] is not None
    assert metrics["static_context_hash"]
    print(
        "\n[live] health="
        f"{health.status} latency={health.latency_ms}ms "
        f"prompt={metrics['provider_prompt_tokens']} "
        f"completion={metrics['provider_completion_tokens']} "
        f"duration={metrics['duration_ms']}ms"
    )


@pytest.mark.asyncio
async def test_live_provider_emits_a_usable_jawl_json_action_plan():
    """The JSON envelope mode must work on a provider with no JAWL awareness."""

    executor, provider = _live_executor()
    instruction = (
        "You are JAWL, a coding agent. Reply with ONE JSON object and nothing "
        "else. No markdown fences. Schema:\n"
        '{"observation": string, "reasoning": string, "reflection": string, '
        '"actions": [{"tool_name": string, "parameters": object}]}\n'
        "Available tool: HostOSReader.read_file(path: string).\n"
        "Task: read the file src/main.py. Emit exactly one action."
    )
    try:
        answer = await executor.execute(
            model_name=LIVE_MODEL,
            messages=[{"role": "user", "content": instruction}],
            temperature=0.1,
            logger=logger,
            log_prefix="[Live]",
            tool_transport="json_envelope",
        )
    finally:
        await provider.close()

    assert answer, "provider returned no answer"
    text = answer.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        text = text.split("\n", 1)[1] if text.lower().startswith("json") else text
    start, end = text.find("{"), text.rfind("}")
    assert start >= 0 and end > start, f"no JSON object in answer: {answer[:400]}"
    plan = json.loads(text[start : end + 1])

    assert isinstance(plan.get("actions"), list) and plan["actions"], plan
    action = plan["actions"][0]
    assert action["tool_name"] == "HostOSReader.read_file"
    assert action["parameters"]["path"].endswith("main.py")
    print(f"\n[live] JSON plan action={action['tool_name']} params={action['parameters']}")


@pytest.mark.asyncio
async def test_live_provider_returns_native_tool_calls_when_capability_is_native():
    executor, provider = _live_executor()
    tools = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a UTF-8 text file from the project.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        }
    ]
    try:
        answer = await executor.execute(
            model_name=LIVE_MODEL,
            messages=[
                {
                    "role": "user",
                    "content": "Use the read_file tool to read src/main.py.",
                }
            ],
            temperature=0.1,
            logger=logger,
            log_prefix="[Live]",
            tools=tools,
            tool_choice="auto",
            tool_transport="native",
        )
    finally:
        await provider.close()

    assert answer, "provider returned no answer"
    result = executor.last_result
    assert result is not None
    assert result.tool_calls, f"no native tool_calls returned: {answer[:400]}"
    call = result.tool_calls[0]
    assert call.name == "read_file"
    assert json.loads(call.arguments)["path"].endswith("main.py")
    # The executor projects native calls back into JAWL's action envelope.
    projected = json.loads(answer)
    assert projected["actions"][0]["tool_name"] == "read_file"
    print(f"\n[live] native tool_call={call.name} args={call.arguments}")


@pytest.mark.asyncio
async def test_live_durable_goal_completes_a_real_coding_task_with_evidence(
    tmp_path: Path,
):
    """Full Durable Goal cycle: read, patch, run tests, checkpoint, complete.

    JAWL remains the source of truth throughout: the provider holds no session,
    yet the ledger, evidence and verification status survive every tick.
    """

    project = tmp_path / "project"
    project.mkdir()
    (project / "calc.py").write_text(
        "def add(a, b):\n"
        "    return a + b\n"
        "\n"
        "\n"
        "def subtract(a, b):\n"
        "    # BUG: this returns the sum instead of the difference.\n"
        "    return a + b\n",
        encoding="utf-8",
    )
    (project / "test_calc.py").write_text(
        "from calc import add, subtract\n"
        "\n"
        "\n"
        "def test_add():\n"
        "    assert add(2, 3) == 5\n"
        "\n"
        "\n"
        "def test_subtract():\n"
        "    assert subtract(5, 3) == 2\n",
        encoding="utf-8",
    )

    def run_tests() -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
            cwd=project,
            capture_output=True,
            text=True,
            timeout=300,
        )

    before = run_tests()
    assert before.returncode != 0, "the fixture must start red"

    agent_state = AgentState(max_react_steps=6)
    goals = GoalManager(
        path=tmp_path / "goals.json",
        agent_state=agent_state,
        enabled=True,
        server_side_conversation=False,
        recover_on_start=False,
    )
    goal = await goals.create(
        objective=(
            "Fix subtract() in calc.py so the project test suite passes, "
            "without changing add() or the tests."
        ),
        linked_task_id="live-calc-fix",
        verification_policy="required",
    )
    executor, provider = _live_executor()
    transcript: list[str] = []

    try:
        await goals.begin_cycle("live_smoke_start")

        # --- Tick 1: the model proposes the exact replacement line ------------
        source = (project / "calc.py").read_text(encoding="utf-8")
        answer = await executor.execute(
            model_name=LIVE_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are JAWL, a precise coding agent. Answer with code "
                        "only, no prose, no markdown fences."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Goal: {goal.objective}\n\n"
                        f"Current calc.py:\n{source}\n\n"
                        "Output the complete corrected body of the subtract "
                        "function only, as two lines:\n"
                        "def subtract(a, b):\n"
                        "    <correct return statement>"
                    ),
                },
            ],
            temperature=0.0,
            logger=logger,
            log_prefix="[Live Goal]",
            session_id=goals.lane_id,
        )
        assert answer, "provider produced no patch proposal"
        transcript.append(answer)
        first_metrics = dict(executor.last_call_metrics)
        assert first_metrics["status"] == "completed"
        await goals.record_usage(first_metrics)

        # The provider is stateless here, so JAWL must carry the lane itself.
        assert first_metrics["session_mode"] == "goal"

        # --- Apply the model's fix ------------------------------------------
        text = answer.strip()
        if "```" in text:
            blocks = text.split("```")
            text = max(blocks[1::2], key=len, default=text)
            if text.lower().startswith("python"):
                text = text.split("\n", 1)[1]
        return_line = next(
            (
                line.strip()
                for line in text.splitlines()
                if line.strip().startswith("return")
            ),
            "",
        )
        assert return_line, f"no return statement proposed: {answer[:400]}"
        assert return_line.replace(" ", "") == "returna-b", (
            f"model proposed a wrong fix: {return_line!r}"
        )
        (project / "calc.py").write_text(
            source.replace(
                "    # BUG: this returns the sum instead of the difference.\n"
                "    return a + b\n",
                f"    {return_line}\n",
            ),
            encoding="utf-8",
        )
        await goals.record_action_result(
            "* [action_id=action_1; status=success; tool=HostOSEditor.write] "
            f"Applied `{return_line}` to calc.py subtract().",
            [
                {
                    "action_id": "action_1",
                    "tool_name": "HostOSEditor.write",
                    "parameters": {"path": "calc.py"},
                }
            ],
        )

        # --- Checkpoint mid-goal, then prove it survives a lane reset --------
        await goals.finish_cycle(
            state="continue",
            summary="subtract() patched; verification still pending.",
            wake_after_seconds=1,
        )
        checkpointed = goals.active_goal
        assert checkpointed is not None
        assert checkpointed.status == "active"
        assert checkpointed.pending_work is True

        # Simulate losing the provider session entirely: progress must remain.
        lane_before = goals.lane_id
        reloaded = GoalManager(
            path=tmp_path / "goals.json",
            agent_state=AgentState(max_react_steps=6),
            enabled=True,
            server_side_conversation=False,
            recover_on_start=True,
        )
        recovered = reloaded.active_goal
        assert recovered is not None, "durable goal was lost across a restart"
        assert recovered.goal_id == goal.goal_id
        assert recovered.lane_epoch > checkpointed.lane_epoch
        assert reloaded.lane_id != lane_before, "a new lane must be issued"
        assert any(
            "subtract() patched" in str(item.get("summary") or "")
            for item in recovered.evidence
        ), "the checkpoint summary is missing from durable evidence"
        assert recovered.verification_status == "pending"

        # --- Verification: run the project's real test suite -----------------
        await reloaded.begin_cycle("live_smoke_verify")
        after = run_tests()
        verification_output = (after.stdout + after.stderr).strip()[-2000:]
        status = "success" if after.returncode == 0 else "failed"
        await reloaded.record_action_result(
            "* [action_id=action_2; status="
            f"{status}; tool=HostOSCodingVerification.run_coding_verification] "
            f"exit={after.returncode}\n{verification_output}",
            [
                {
                    "action_id": "action_2",
                    "tool_name": (
                        "HostOSCodingVerification.run_coding_verification"
                    ),
                    "parameters": {"command": "pytest -q"},
                }
            ],
        )
        assert after.returncode == 0, f"tests still failing:\n{verification_output}"

        verified = reloaded.active_goal
        assert verified is not None
        assert verified.verification_status == "passed"
        assert verified.verification_summary

        # --- Completion is only allowed with current passing evidence -------
        final = await reloaded.finish_cycle(
            state="completed",
            summary=(
                "subtract() now returns a - b; the project suite passes "
                f"(exit={after.returncode})."
            ),
        )
        assert final is not None
        assert final.status == "complete", final.last_summary
        assert final.last_cycle_status == "completed"
        assert final.completion_summary
        assert any(
            item.get("kind") == "verification:passed" for item in final.evidence
        ), "no verification evidence was recorded"
    finally:
        await provider.close()

    # Durable artefacts remain readable on disk after everything is closed.
    stored = json.loads((tmp_path / "goals.json").read_text(encoding="utf-8"))
    assert stored, "the goal ledger was not persisted"
    print(
        "\n[live] goal completed: "
        f"verification={final.verification_status} "
        f"evidence_items={len(final.evidence)} "
        f"tokens_accounted={final.accounted_tokens}\n"
        f"[live] final calc.py subtract -> {return_line}"
    )
