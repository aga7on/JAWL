"""Live QWB regression smoke test: the legacy adapter keeps every extension.

Enable with a running QWB bridge::

    JAWL_LIVE_QWB=1
    JAWL_LIVE_QWB_URL=http://127.0.0.1:8000/v1
    JAWL_LIVE_QWB_MODEL=qwen3.8-max-preview

The point of this file is symmetry with ``test_live_provider_goal.py``: the same
Durable Goal machinery must complete a real coding task through QWB, while the
QWB-only features (lane continuity, thinking transport, account diagnostics)
stay observable and stay inside the QWB adapter.
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
from src.l3_agent.llm.providers.contracts import RetryPolicy
from src.l3_agent.llm.providers.factory import build_llm_provider
from src.utils.settings import LLMProviderConfig
from src.utils.token_tracker import TokenTracker

pytestmark = pytest.mark.skipif(
    os.environ.get("JAWL_LIVE_QWB") != "1",
    reason="set JAWL_LIVE_QWB=1 and start the QWB bridge to run this test",
)

QWB_URL = os.environ.get("JAWL_LIVE_QWB_URL", "http://127.0.0.1:8000/v1")
QWB_MODEL = os.environ.get("JAWL_LIVE_QWB_MODEL", "qwen3.8-max-preview")
QWB_KEY = os.environ.get("JAWL_LIVE_QWB_KEY", "local_dummy_key")

logger = logging.getLogger("live-qwb")


def _live_qwb() -> tuple[LLMExecutor, object]:
    """Build the production QWB stack exactly as the system builder does."""

    config = LLMProviderConfig(
        kind="qwb",
        display_name="QWB",
        request_timeout_seconds=900,
        read_timeout_seconds=900,
    )
    client = LLMClient(
        api_url=QWB_URL,
        api_keys_rotator=APIKeyRotator([QWB_KEY]),
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
            max_delay_seconds=8.0,
        ),
    )
    return executor, provider


@pytest.mark.asyncio
async def test_live_qwb_health_still_exposes_account_pool_without_leaking_tokens():
    executor, provider = _live_qwb()
    try:
        health = await provider.health()
    finally:
        await provider.close()

    public = health.public()
    assert public["provider"] == "qwb"
    assert public["status"] in {"ok", "degraded"}, public["detail"]
    # QWB-only diagnostics must survive the provider-neutral refactor.
    assert public["capabilities"]["server_side_conversation"] is True
    assert public["capabilities"]["vision"] is True
    accounts = public["metadata"]["accounts"]
    assert accounts, "QWB must report its account pool"
    assert all(item["state"] for item in accounts)
    # Fingerprints are safe to show; raw tokens are not.
    assert all("token" not in item for item in accounts)
    rendered = repr(public)
    assert "eyJ" not in rendered, "a JWT leaked into provider health output"
    print(
        f"\n[qwb] status={public['status']} accounts={len(accounts)} "
        f"latency={public['latency_ms']}ms model={public['metadata'].get('model')}"
    )


@pytest.mark.asyncio
async def test_live_qwb_lane_continuity_and_thinking_transport_still_work():
    """A shared lane keeps server-side context that a stateless provider lacks.

    QWB only maintains a parent chain for genuine JAWL traffic: one
    ``execute_skill`` tool plus a stable system block. That is exactly the
    shape the ReAct loop sends, so the test reproduces it rather than a bare
    chat turn, which the bridge deliberately treats as a fresh conversation.
    """

    import uuid

    from src.l3_agent.skills.schema import ACTION_SCHEMA

    executor, provider = _live_qwb()
    # A fresh lane per run: reusing one would inherit a previous chat's state
    # and make the continuity assertion pass or fail for the wrong reason.
    lane = f"jawl-live-qwb-lane-{uuid.uuid4().hex[:12]}"
    system_block = (
        "You are JAWL, a coding agent. Always answer by calling execute_skill "
        "with observation, reasoning, reflection and actions."
    )

    def turn(user_text: str) -> list[dict]:
        return [
            {"role": "system", "content": system_block},
            {"role": "user", "content": user_text},
        ]

    try:
        first = await executor.execute(
            model_name=QWB_MODEL,
            messages=turn(
                "Remember this token for the rest of our chat: MAGENTA-7788. "
                "Call execute_skill with an empty actions list and put the "
                "word stored in reflection."
            ),
            temperature=0.1,
            logger=logger,
            log_prefix="[QWB]",
            tools=ACTION_SCHEMA,
            tool_choice="required",
            tool_transport="json_envelope",
            session_id=lane,
            enable_thinking=False,
        )
        first_metrics = dict(executor.last_call_metrics)
        second = await executor.execute(
            model_name=QWB_MODEL,
            messages=turn(
                "Which token did I ask you to remember earlier? Call "
                "execute_skill with an empty actions list and put that exact "
                "token in reflection."
            ),
            temperature=0.1,
            logger=logger,
            log_prefix="[QWB]",
            tools=ACTION_SCHEMA,
            tool_choice="required",
            tool_transport="json_envelope",
            session_id=lane,
            enable_thinking=False,
        )
        second_metrics = dict(executor.last_call_metrics)
    finally:
        await provider.close()

    assert first, "QWB returned nothing for the first lane turn"
    assert second, "QWB returned nothing for the second lane turn"
    assert first_metrics["status"] == "completed"
    assert second_metrics["status"] == "completed"
    assert first_metrics["provider"] == "qwb"
    assert first_metrics["session_mode"] == "goal"
    assert first_metrics["provider_metadata"]["conversation"] == "server_side"
    assert first_metrics["provider_metadata"]["lane"] == lane
    assert second_metrics["provider_metadata"]["lane"] == lane

    # Upstream may drop the chat between turns (CHAT_NOT_FOUND). That is a
    # supported outcome, not a regression: the provider session is only an
    # optimization, so JAWL must degrade to its own snapshot instead of
    # inventing an answer. Both branches are therefore valid — but a silently
    # fabricated token never is.
    lane_was_rebuilt = second_metrics["retry_counters"].get("invalid_request", 0) > 0
    if lane_was_rebuilt:
        assert "MAGENTA-7788" not in second.upper(), (
            "QWB fabricated a token after its chat chain was rebuilt"
        )
        print(
            "\n[qwb] lane was rebuilt upstream (CHAT_NOT_FOUND); "
            "the adapter degraded honestly instead of hallucinating recall"
        )
    else:
        assert "MAGENTA-7788" in second.upper(), (
            f"lane continuity lost without a rebuild; QWB answered: {second[:300]}"
        )
        print(
            f"\n[qwb] lane continuity ok; recall={second.strip()[:120]!r} "
            f"prompt_tokens={second_metrics['provider_prompt_tokens']}"
        )


@pytest.mark.asyncio
async def test_live_qwb_json_action_plan_contract_is_unchanged():
    """QWB delivers the JAWL envelope through the ``execute_skill`` tool.

    The bridge injects the action protocol only for real JAWL traffic, so the
    test sends the production request shape (system block + ACTION_SCHEMA)
    instead of asking the web model for JSON in free text.
    """

    from src.l3_agent.skills.schema import ACTION_SCHEMA, parse_llm_json

    executor, provider = _live_qwb()
    parsed = None
    attempts: list[str] = []
    try:
        # A web model occasionally emits a shorthand the protocol does not
        # define. That is exactly what the bounded tool-protocol repair budget
        # exists for, so allow the same small number of retries here.
        for _ in range(3):
            answer = await executor.execute(
                model_name=QWB_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are JAWL, an autonomous coding agent. Every "
                            "reply must call execute_skill with the keys "
                            "observation, reasoning, reflection and actions, "
                            "where each action has tool_name and parameters.\n"
                            "Available tool: HostOSReader.read_file(path)."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            "Read the file src/main.py. Emit exactly one entry "
                            "in actions with tool_name "
                            '"HostOSReader.read_file" and parameters '
                            '{"path": "src/main.py"}.'
                        ),
                    },
                ],
                temperature=0.1,
                logger=logger,
                log_prefix="[QWB]",
                tools=ACTION_SCHEMA,
                tool_choice="required",
                tool_transport="json_envelope",
            )
            assert answer, "QWB returned no answer"
            attempts.append(answer[:200])
            parsed, _error = parse_llm_json(answer)
            if parsed is not None and parsed.actions:
                break
    finally:
        await provider.close()

    # The authoritative check is JAWL's own parser, not a hand-rolled one.
    assert parsed is not None and parsed.actions, (
        "JAWL could not parse a usable action plan from QWB.\n"
        + "\n---\n".join(attempts)
    )
    action = parsed.actions[0]
    assert action.tool_name == "HostOSReader.read_file"
    assert str(action.parameters.get("path", "")).endswith("main.py")
    print(f"\n[qwb] JSON plan action={action.tool_name} params={action.parameters}")


@pytest.mark.asyncio
async def test_live_qwb_durable_goal_completes_a_real_coding_task_with_evidence(
    tmp_path: Path,
):
    """The identical Goal scenario as the standard provider, driven by QWB."""

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

    assert run_tests().returncode != 0, "the fixture must start red"

    agent_state = AgentState(max_react_steps=6)
    goals = GoalManager(
        path=tmp_path / "goals.json",
        agent_state=agent_state,
        enabled=True,
        server_side_conversation=True,
        recover_on_start=False,
    )
    goal = await goals.create(
        objective=(
            "Fix subtract() in calc.py so the project test suite passes, "
            "without changing add() or the tests."
        ),
        linked_task_id="live-qwb-calc-fix",
        verification_policy="required",
    )
    executor, provider = _live_qwb()

    try:
        await goals.begin_cycle("live_qwb_start")
        source = (project / "calc.py").read_text(encoding="utf-8")
        answer = await executor.execute(
            model_name=QWB_MODEL,
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
            log_prefix="[QWB Goal]",
            session_id=goals.lane_id,
        )
        assert answer, "QWB produced no patch proposal"
        metrics = dict(executor.last_call_metrics)
        assert metrics["status"] == "completed"
        assert metrics["provider"] == "qwb"
        await goals.record_usage(metrics)

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
        await goals.finish_cycle(
            state="continue",
            summary="subtract() patched via QWB; verification pending.",
            wake_after_seconds=1,
        )

        # Losing the QWB chat must not lose Goal progress: JAWL is the source
        # of truth, and the provider session is only an optimization.
        reloaded = GoalManager(
            path=tmp_path / "goals.json",
            agent_state=AgentState(max_react_steps=6),
            enabled=True,
            server_side_conversation=True,
            recover_on_start=True,
        )
        recovered = reloaded.active_goal
        assert recovered is not None, "durable goal lost across a restart"
        assert recovered.goal_id == goal.goal_id
        assert recovered.lane_epoch > goal.lane_epoch
        assert recovered.verification_status == "pending"

        await reloaded.begin_cycle("live_qwb_verify")
        after = run_tests()
        verification_output = (after.stdout + after.stderr).strip()[-2000:]
        await reloaded.record_action_result(
            "* [action_id=action_2; status="
            f"{'success' if after.returncode == 0 else 'failed'}; "
            "tool=HostOSCodingVerification.run_coding_verification] "
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
        assert reloaded.active_goal.verification_status == "passed"

        final = await reloaded.finish_cycle(
            state="completed",
            summary=(
                "subtract() now returns a - b; project suite passes via QWB "
                f"(exit={after.returncode})."
            ),
        )
        assert final is not None
        assert final.status == "complete", final.last_summary
        assert any(
            item.get("kind") == "verification:passed" for item in final.evidence
        )
    finally:
        await provider.close()

    print(
        "\n[qwb] goal completed: "
        f"verification={final.verification_status} "
        f"evidence_items={len(final.evidence)} "
        f"tokens_accounted={final.accounted_tokens}\n"
        f"[qwb] final calc.py subtract -> {return_line}"
    )
