"""Run an external coding CLI on the fixed repositories and grade its patches."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import psutil

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from benchmarks.coding_tasks.run import (
    EVAL_ROOT,
    FIXTURE_IGNORE,
    evaluate_task,
    git,
    load_manifest,
)
from src.utils._tools import redact_sensitive_text


OUTPUT_TAIL_BYTES = 64 * 1024
MAX_PATCH_BYTES = 2 * 1024 * 1024


def _redacted_output_tail(output: str, max_chars: int = 16000) -> str:
    redacted = redact_sensitive_text(output)
    if len(redacted) <= max_chars:
        return redacted
    prefix = "[earlier output truncated]\n"
    return prefix + redacted[-(max_chars - len(prefix)) :]


def _kill_process_tree(pid: int) -> None:
    """Terminate the candidate and descendants without touching unrelated PIDs."""
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    processes = parent.children(recursive=True)
    processes.append(parent)
    for process in reversed(processes):
        try:
            process.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(processes, timeout=3)
    for process in alive:
        try:
            process.kill()
        except psutil.NoSuchProcess:
            pass
    psutil.wait_procs(alive, timeout=3)


def _render_command(command: List[str], repository: Path, prompt: str) -> List[str]:
    return [
        argument.replace("{repository}", str(repository)).replace("{prompt}", prompt)
        for argument in command
    ]


def run_candidate_command(
    command: List[str], repository: Path, prompt: str, timeout_seconds: int
) -> Dict[str, Any]:
    """Run without a shell and retain only a bounded, redacted output tail."""
    rendered = _render_command(command, repository, prompt)
    if not rendered or not rendered[0].strip():
        raise ValueError("Candidate command must include an executable.")
    environment = os.environ.copy()
    environment.update(
        {
            "JAWL_BENCH_REPOSITORY": str(repository),
            "JAWL_BENCH_PROMPT": prompt,
            "JAWL_CODING_TASK_EVAL": "1",
        }
    )
    started = time.perf_counter()
    tail = bytearray()
    tail_lock = threading.Lock()
    process: Optional[subprocess.Popen[bytes]] = None
    reader: Optional[threading.Thread] = None

    def drain() -> None:
        assert process is not None and process.stdout is not None
        while True:
            chunk = process.stdout.read(8192)
            if not chunk:
                return
            with tail_lock:
                tail.extend(chunk)
                if len(tail) > OUTPUT_TAIL_BYTES:
                    del tail[:-OUTPUT_TAIL_BYTES]

    try:
        process = subprocess.Popen(
            rendered,
            cwd=repository,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        timed_out = False
        try:
            exit_code = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_process_tree(process.pid)
            exit_code = process.wait(timeout=10)
    except OSError as exc:
        timed_out = False
        exit_code = None
        tail.extend(f"{type(exc).__name__}: {exc}".encode("utf-8", errors="replace"))
    finally:
        if reader is not None:
            reader.join(timeout=10)
        if process is not None and process.stdout is not None:
            process.stdout.close()

    output = bytes(tail).decode("utf-8", errors="replace")
    return {
        "passed": exit_code == 0 and not timed_out,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        "output_tail": _redacted_output_tail(output),
        "executable": Path(rendered[0]).name,
        "argument_count": max(0, len(rendered) - 1),
    }


def initialize_cli_fixture(task: Dict[str, Any], root: Path) -> tuple[Path, str]:
    fixture = (EVAL_ROOT / task["fixture"] / "repo").resolve()
    repository = root / "repo"
    shutil.copytree(fixture, repository, ignore=FIXTURE_IGNORE)
    git(repository, "init", "-b", "main")
    git(repository, "config", "user.name", "JAWL CLI Eval")
    git(repository, "config", "user.email", "cli-eval@jawl.local")
    git(repository, "add", "--all")
    git(repository, "commit", "-m", "fixture baseline")
    return repository, git(repository, "rev-parse", "HEAD").stdout.strip()


def extract_patch(repository: Path, base_commit: str, output: Path) -> Dict[str, Any]:
    # Intent-to-add makes untracked files visible to `git diff` without staging
    # their contents or changing the candidate's commits.
    git(repository, "add", "-N", "--all", check=False)
    result = git(repository, "diff", "--binary", base_commit, "--", check=False)
    patch = result.stdout
    patch_bytes = patch.encode("utf-8")
    if len(patch_bytes) > MAX_PATCH_BYTES:
        raise ValueError(
            f"Candidate patch exceeds {MAX_PATCH_BYTES} bytes ({len(patch_bytes)})."
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_bytes(patch_bytes)
    temporary.replace(output)
    return {
        "patch_path": str(output),
        "patch_bytes": len(patch_bytes),
        "git_diff_exit_code": result.returncode,
        "patch_error": redact_sensitive_text(result.stderr, max_chars=4000),
    }


def run_cli_task(
    task: Dict[str, Any],
    output_dir: Path,
    command: List[str],
    timeout_seconds: int,
) -> Dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix=f"jawl-cli-{task['id']}-") as temporary:
        repository, base_commit = initialize_cli_fixture(task, Path(temporary))
        execution = run_candidate_command(
            command, repository, task["prompt"], timeout_seconds
        )
        patch_path = output_dir / "patches" / f"{task['id']}.patch"
        try:
            extraction = extract_patch(repository, base_commit, patch_path)
            extraction_error = ""
        except Exception as exc:
            patch_path.parent.mkdir(parents=True, exist_ok=True)
            patch_path.write_text("", encoding="utf-8")
            extraction = {"patch_path": str(patch_path), "patch_bytes": 0}
            extraction_error = redact_sensitive_text(
                f"{type(exc).__name__}: {exc}", max_chars=4000
            )
        return {
            "id": task["id"],
            "execution": execution,
            "extraction_error": extraction_error,
            **extraction,
        }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--task", action="append", default=[])
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=REPOSITORY_ROOT / ".jawl-benchmarks" / "cli-latest",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("an external command is required after --")
    if args.timeout_seconds < 10 or args.timeout_seconds > 7200:
        parser.error("--timeout-seconds must be between 10 and 7200")

    manifest = load_manifest()
    tasks = manifest["tasks"]
    if args.task:
        unknown = set(args.task) - {task["id"] for task in tasks}
        if unknown:
            parser.error("Unknown tasks: " + ", ".join(sorted(unknown)))
        tasks = [task for task in tasks if task["id"] in args.task]

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    live_results = [
        run_cli_task(task, output_dir, command, args.timeout_seconds) for task in tasks
    ]
    evaluations = [
        evaluate_task(task, "solution", output_dir / "patches") for task in tasks
    ]
    report = {
        "schema_version": 1,
        "benchmark": "jawl-external-cli-coding-eval",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "candidate": args.candidate,
        "command": {
            "executable": Path(command[0]).name,
            "argument_count": max(0, len(command) - 1),
        },
        "task_count": len(tasks),
        "execution_passed": all(
            item["execution"]["passed"] and not item["extraction_error"]
            for item in live_results
        ),
        "quality_gate_passed": all(item["gate_passed"] for item in evaluations),
        "mean_quality_score": round(
            sum(item["quality_score"] for item in evaluations) / len(evaluations), 4
        ),
        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        "runs": live_results,
        "evaluations": evaluations,
    }
    report["gate_passed"] = bool(
        report["execution_passed"] and report["quality_gate_passed"]
    )
    report_path = output_dir / "report.json"
    temporary = report_path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(report_path)
    print(f"[external-cli-eval] report: {report_path}")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
