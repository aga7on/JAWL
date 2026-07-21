"""Run fixed coding tasks through an isolated real JAWL ReAct loop."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from benchmarks.coding_tasks.run import (
    EVAL_ROOT,
    FIXTURE_IGNORE,
    evaluate_task,
    load_manifest,
)
from src.l0_state.agent.state import AgentState
from src.l1_databases.sql.db import SQLDB
from src.l1_databases.sql.management.ticks import SQLTicks
from src.l2_interfaces.host.os.client import HostOSClient
from src.l2_interfaces.host.os.skills.coding_context import HostOSCodingContext
from src.l2_interfaces.host.os.skills.coding_dependencies import HostOSCodingDependencies
from src.l2_interfaces.host.os.skills.coding_files import HostOSCodingFiles
from src.l2_interfaces.host.os.skills.coding_lsp import HostOSCodingLanguageServer
from src.l2_interfaces.host.os.skills.coding_plans import HostOSCodingPlans
from src.l2_interfaces.host.os.skills.coding_verification import HostOSCodingVerification
from src.l2_interfaces.host.os.skills.coding_workspaces import HostOSCodingWorkspaces
from src.l2_interfaces.host.os.skills.files.editor import HostOSEditor
from src.l2_interfaces.host.os.skills.files.reader import HostOSReader
from src.l2_interfaces.host.os.skills.files.search import HostOSSearch
from src.l2_interfaces.host.os.skills.files.workspace import HostOSWorkspace
from src.l2_interfaces.host.os.skills.files.writer import HostOSWriter
from src.l2_interfaces.host.os.state import HostOSState
from src.l3_agent.context.builder import ContextBuilder
from src.l3_agent.context.registry import ContextRegistry, ContextSection
from src.l3_agent.llm.api_keys.rotator import APIKeyRotator
from src.l3_agent.llm.client import LLMClient
from src.l3_agent.llm.executor import LLMExecutor
from src.l3_agent.prompt.builder import PromptBuilder
from src.l3_agent.react.loop import ReactLoop
from src.l3_agent.skills.registry import (
    build_tools_schema,
    clear_registry,
    configure_action_journal,
    register_instance,
)
from src.utils._tools import redact_sensitive_text
from src.utils.event.bus import EventBus
from src.utils.settings import HostOSConfig
from src.utils.token_tracker import TokenTracker


def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )


def initialize_fixture(task: Dict[str, Any], runtime_root: Path) -> Path:
    """Copy only the candidate-visible repository and create a clean Git baseline."""

    fixture = (EVAL_ROOT / task["fixture"] / "repo").resolve()
    repository = runtime_root / "sandbox" / "repo"
    repository.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(fixture, repository, ignore=FIXTURE_IGNORE)
    git(repository, "init", "-b", "main")
    git(repository, "config", "user.name", "JAWL Live Eval")
    git(repository, "config", "user.email", "live-eval@jawl.local")
    git(repository, "add", "--all")
    git(repository, "commit", "-m", "fixture baseline")
    return repository


def build_task_prompt(task: Dict[str, Any], task_id: str) -> str:
    return (
        "You are running an isolated software-engineering benchmark. "
        "The only candidate-visible repository is sandbox/repo. "
        f"Use the exact coding task_id '{task_id}'. Objective: {task['prompt']} "
        "Create a task-scoped coding workspace, maintain a concrete coding plan, "
        "inspect bounded context through task-scoped coding file skills, implement "
        "the smallest correct change, run repository verification, satisfy the plan "
        "with evidence, and commit the verified workspace. Batch causally safe "
        "operations with action_id/depends_on so workspace creation can precede a "
        "task-relative read and patch can precede verify/evidence/commit in the same "
        "action plan. Do not modify files outside the managed workspace. "
        "Conclude the ReAct cycle only after the commit or after recording concrete "
        "failure evidence. Hidden tests are intentionally unavailable."
    )


def _host_config() -> HostOSConfig:
    return HostOSConfig(
        enabled=True,
        access_level=1,
        env_access=False,
        require_deploy_sessions=False,
        execution_timeout_sec=180,
        file_read_max_chars=12000,
        file_list_limit=300,
        file_diff_max_chars=12000,
        workspace_max_opened_files=10,
        workspace_max_file_chars=12000,
    )


async def extract_candidate_patch(
    workspaces: HostOSCodingWorkspaces,
    task_id: str,
    base_repository: Path,
    output_path: Path,
) -> Dict[str, Any]:
    """Extract the managed branch against its exact base, with a base-repo fallback."""

    registry = workspaces._load_registry()
    entry = registry["workspaces"].get(task_id)
    source = "managed_workspace"
    lifecycle_ok = True
    if entry:
        workspace = Path(entry["workspace_path"])
        base_commit = entry["base_commit"]
        status = git(workspace, "status", "--porcelain=v1", "--untracked-files=all").stdout
        untracked = git(
            workspace, "ls-files", "--others", "--exclude-standard"
        ).stdout.splitlines()
        patch = git(workspace, "diff", "--binary", base_commit, "--").stdout
        head = git(workspace, "rev-parse", "HEAD").stdout.strip()
        committed = head != base_commit
        plan = entry.get("task_plan")
        plan_complete = bool(
            plan
            and all(step.get("status") == "completed" for step in plan.get("steps", []))
            and all(
                requirement.get("status") == "satisfied"
                for requirement in plan.get("requirements", [])
            )
        )
        verification_bypassed = entry.get("last_commit_verification_bypassed")
        plan_bypassed = entry.get("last_commit_plan_bypassed")
        lifecycle_ok = bool(
            committed
            and entry.get("last_commit") == head
            and not status.strip()
            and not untracked
            and verification_bypassed is False
            and plan_bypassed is False
            and plan_complete
        )
    else:
        source = "base_repository_fallback"
        base_commit = git(base_repository, "rev-parse", "HEAD").stdout.strip()
        patch = git(base_repository, "diff", "--binary", "HEAD", "--").stdout
        status = git(
            base_repository, "status", "--porcelain=v1", "--untracked-files=all"
        ).stdout
        untracked = git(
            base_repository, "ls-files", "--others", "--exclude-standard"
        ).stdout.splitlines()
        head = base_commit
        committed = False
        plan_complete = False
        verification_bypassed = None
        plan_bypassed = None
        lifecycle_ok = False
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(patch, encoding="utf-8", newline="\n")
    return {
        "patch_source": source,
        "patch_path": str(output_path),
        "patch_bytes": len(patch.encode("utf-8")),
        "workspace_found": entry is not None,
        "base_commit": base_commit,
        "head": head,
        "committed": committed,
        "plan_complete": plan_complete,
        "verification_bypassed": verification_bypassed,
        "plan_bypassed": plan_bypassed,
        "worktree_clean": not status.strip(),
        "untracked_files": untracked[:20],
        "lifecycle_gate_passed": lifecycle_ok,
    }


def summarize_ticks(tick_rows: List[Any]) -> Dict[str, Any]:
    """Return bounded protocol/action evidence without serializing chain-of-thought."""

    summaries = []
    llm_calls = []
    for row in tick_rows:
        results = row.results if isinstance(row.results, dict) else {}
        actions = row.actions if isinstance(row.actions, list) else []
        metrics = results.get("llm_metrics")
        if isinstance(metrics, dict) and metrics:
            llm_calls.append(metrics)
        summary: Dict[str, Any] = {
            "step": results.get("step"),
            "status": results.get("status", "action" if actions else "unknown"),
            "tool_names": [
                action.get("tool_name", "unknown")
                for action in actions[:20]
                if isinstance(action, dict)
            ],
        }
        for field, limit in (
            ("error", 1000),
            ("response_excerpt", 1600),
            ("response_tail", 1600),
            ("execution_report", 1200),
        ):
            value = results.get(field)
            if isinstance(value, str) and value:
                summary[field] = redact_sensitive_text(value, max_chars=limit)
        summaries.append(summary)
    return {
        "tick_count": len(tick_rows),
        "action_tick_count": sum(bool(item["tool_names"]) for item in summaries),
        "protocol_error_count": sum(
            item["status"] == "protocol_error" for item in summaries
        ),
        "tick_summaries": summaries,
        "llm_calls": llm_calls,
    }


async def run_live_task(
    task: Dict[str, Any],
    output_dir: Path,
    api_url: str,
    api_key: str,
    model: str,
    transport: str,
    max_steps: int,
    timeout_seconds: int,
) -> Dict[str, Any]:
    started = time.perf_counter()
    task_id = f"eval-{task['id']}"
    with tempfile.TemporaryDirectory(prefix=f"jawl-live-{task['id']}-") as temporary:
        runtime_root = Path(temporary).resolve()
        repository = initialize_fixture(task, runtime_root)
        (runtime_root / "src" / "utils" / "templates").mkdir(parents=True)
        (runtime_root / "src" / "utils" / "templates" / "framework_api.py").write_text(
            "# Isolated benchmark placeholder.\n", encoding="utf-8"
        )

        clear_registry()
        state = HostOSState()
        host = HostOSClient(runtime_root, _host_config(), state, timezone=0)
        workspaces = HostOSCodingWorkspaces(host)
        coding_context = HostOSCodingContext(host)
        reader = HostOSReader(host)
        editor = HostOSEditor(host)
        search = HostOSSearch(host)
        coding_instances = [
            reader,
            HostOSWriter(host),
            editor,
            search,
            HostOSWorkspace(host),
            coding_context,
            HostOSCodingLanguageServer(host, coding_context),
            HostOSCodingDependencies(host),
            workspaces,
            HostOSCodingFiles(host, workspaces, reader, editor, search),
            HostOSCodingPlans(host, workspaces),
            HostOSCodingVerification(host, workspaces),
        ]
        for instance in coding_instances:
            register_instance(instance)
        configure_action_journal(host.system_dir / "action_journal.jsonl")

        db = SQLDB(db_path=":memory:")
        db.engine = db.engine.execution_options(compiled_cache=None)
        db.engine.url = db.engine.url.set(database=":memory:")
        await db.connect()
        ticks = SQLTicks(db=db)
        register_instance(ticks)

        agent_state = AgentState(
            llm_model=model,
            temperature=0.2,
            max_react_steps=max_steps,
            proactive_guidance=False,
        )
        context_registry = ContextRegistry()
        context_registry.register_provider(
            "agent_state",
            agent_state.get_context_block,
            section=ContextSection.AGENT_STATE,
        )
        context_registry.register_provider(
            "host_os", host.get_context_block, section=ContextSection.INTERFACES
        )
        context_registry.register_provider(
            "ticks", ticks.get_context_block, section=ContextSection.RECENT_TICKS
        )
        context_builder = ContextBuilder(
            agent_state, context_registry, tool_transport=transport
        )
        prompt_builder = PromptBuilder(
            REPOSITORY_ROOT / "src" / "l3_agent" / "prompt",
            tool_transport=transport,
        )
        tracker = TokenTracker(maxlen=max_steps + 5)
        llm_client = LLMClient(api_url, APIKeyRotator([api_key]))
        executor = LLMExecutor(llm_client, tracker)
        event_bus = EventBus()
        loop = ReactLoop(
            executor=executor,
            prompt_builder=prompt_builder,
            context_builder=context_builder,
            agent_state=agent_state,
            sql_ticks=ticks,
            vector_manager=None,
            tools=lambda: build_tools_schema(transport=transport),
            event_bus=event_bus,
            tool_transport=transport,
            cooldown_sec=0,
        )

        timed_out = False
        error = ""
        previous_cwd = Path.cwd()
        try:
            os.chdir(runtime_root)
            await asyncio.wait_for(
                loop.run(
                    "CODING_BENCHMARK",
                    {"message": build_task_prompt(task, task_id)},
                    [],
                ),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            timed_out = True
            error = f"live task timed out after {timeout_seconds} seconds"
        except Exception as exc:
            error = redact_sensitive_text(
                f"{type(exc).__name__}: {exc}", max_chars=2000
            )
        finally:
            os.chdir(previous_cwd)

        try:
            patch_path = output_dir / "patches" / f"{task['id']}.patch"
            extraction = await extract_candidate_patch(
                workspaces, task_id, repository, patch_path
            )
            tick_rows = await ticks.get_ticks(limit=max_steps + 5)
            statuses = [
                row.results.get("status")
                for row in tick_rows
                if isinstance(row.results, dict) and row.results.get("status")
            ]
            tick_diagnostics = summarize_ticks(tick_rows)
            return {
                "id": task["id"],
                "task_id": task_id,
                "timed_out": timed_out,
                "error": error,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                "react_steps_used": agent_state.current_step,
                "terminal_statuses": statuses,
                "input_tokens": sum(
                    item["total"] for item in tracker.input_history
                ),
                "output_tokens": sum(
                    item["total"] for item in tracker.output_history
                ),
                "last_llm_metrics": executor.last_call_metrics,
                **tick_diagnostics,
                **extraction,
            }
        finally:
            await asyncio.gather(
                event_bus.stop(),
                llm_client.close(),
                db.disconnect(),
                return_exceptions=True,
            )
            clear_registry()


async def async_main(args: argparse.Namespace) -> int:
    manifest = load_manifest()
    tasks = manifest["tasks"]
    if args.task:
        unknown = set(args.task) - {task["id"] for task in tasks}
        if unknown:
            raise ValueError("Unknown tasks: " + ", ".join(sorted(unknown)))
        tasks = [task for task in tasks if task["id"] in args.task]
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    live_results = []
    for task in tasks:
        live_results.append(
            await run_live_task(
                task,
                output_dir,
                args.api_url,
                args.api_key,
                args.model,
                args.transport,
                args.max_steps,
                args.timeout_seconds,
            )
        )
    evaluation = [
        evaluate_task(task, "solution", output_dir / "patches") for task in tasks
    ]
    report = {
        "schema_version": 1,
        "benchmark": "jawl-live-coding-task-eval",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": args.model,
        "transport": args.transport,
        "task_count": len(tasks),
        "live_lifecycle_passed": all(
            item["lifecycle_gate_passed"] and not item["error"] for item in live_results
        ),
        "quality_gate_passed": all(item["gate_passed"] for item in evaluation),
        "mean_quality_score": round(
            sum(item["quality_score"] for item in evaluation) / len(evaluation), 4
        ),
        "totals": {
            "input_tokens": sum(item["input_tokens"] for item in live_results),
            "output_tokens": sum(item["output_tokens"] for item in live_results),
            "duration_ms": round(sum(item["duration_ms"] for item in live_results), 1),
        },
        "live_tasks": live_results,
        "evaluations": evaluation,
    }
    report_path = output_dir / "report.json"
    temporary = report_path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(report_path)
    print(f"[jawl-live-eval] report: {report_path}")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["live_lifecycle_passed"] and report["quality_gate_passed"] else 1


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default=os.environ.get("LLM_API_URL", ""))
    parser.add_argument("--api-key", default=os.environ.get("LLM_API_KEY_1", "local_dummy_key"))
    parser.add_argument("--model", required=True)
    parser.add_argument("--transport", choices=["wrapper", "native", "hybrid"], default="wrapper")
    parser.add_argument("--max-steps", type=int, default=15)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--task", action="append", default=[])
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=REPOSITORY_ROOT / ".jawl-benchmarks" / "live-latest",
    )
    args = parser.parse_args(argv)
    if not args.api_url:
        parser.error("--api-url or LLM_API_URL is required")
    if args.max_steps < 1 or args.max_steps > 50:
        parser.error("--max-steps must be between 1 and 50")
    if args.timeout_seconds < 30:
        parser.error("--timeout-seconds must be at least 30")
    try:
        return asyncio.run(async_main(args))
    except ValueError as exc:
        parser.error(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
