"""Evaluate candidate patches on fixed repositories with hidden oracle tests."""

from __future__ import annotations

import argparse
import hashlib
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


EVAL_ROOT = Path(__file__).resolve().parent
REPOSITORY_ROOT = EVAL_ROOT.parents[1]
MANIFEST_PATH = EVAL_ROOT / "manifest.json"


def run_command(
    command: List[str], cwd: Path, timeout: int = 60, env: Optional[Dict[str, str]] = None
) -> Dict[str, Any]:
    started = time.perf_counter()
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return {
            "passed": result.returncode == 0,
            "exit_code": result.returncode,
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            "output_tail": (result.stdout + result.stderr)[-4000:],
        }
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + (exc.stderr or "")
        return {
            "passed": False,
            "exit_code": None,
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            "timed_out": True,
            "output_tail": output[-4000:],
        }


def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )


def load_manifest() -> Dict[str, Any]:
    payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if payload.get("version") != 1 or not payload.get("tasks"):
        raise ValueError("Unsupported or empty coding task manifest.")
    identifiers = set()
    for task in payload["tasks"]:
        identifier = task.get("id")
        if not isinstance(identifier, str) or identifier in identifiers:
            raise ValueError("Coding task IDs must be unique strings.")
        identifiers.add(identifier)
        for field in ("fixture", "solution_patch"):
            path = (EVAL_ROOT / task[field]).resolve()
            if not path.is_relative_to(EVAL_ROOT) or not path.exists():
                raise ValueError(f"Task '{identifier}' has invalid {field}.")
    return payload


def patch_for_task(
    task: Dict[str, Any], candidate: str, patch_dir: Optional[Path]
) -> Optional[Path]:
    if candidate == "none":
        return None
    if patch_dir is not None:
        path = (patch_dir / f"{task['id']}.patch").resolve()
    else:
        path = (EVAL_ROOT / task["solution_patch"]).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Candidate patch not found: {path}")
    return path


def changed_files_and_lines(workspace: Path) -> tuple[List[str], int, str]:
    names = git(workspace, "diff", "--name-only", "HEAD", "--").stdout.splitlines()
    untracked = git(
        workspace, "ls-files", "--others", "--exclude-standard", "--"
    ).stdout.splitlines()
    numstat = git(workspace, "diff", "--numstat", "HEAD", "--").stdout.splitlines()
    changed_lines = 0
    for line in numstat:
        added, removed, _ = line.split("\t", maxsplit=2)
        if added.isdigit():
            changed_lines += int(added)
        if removed.isdigit():
            changed_lines += int(removed)
    diff = git(workspace, "diff", "--binary", "HEAD", "--").stdout
    for relative in untracked:
        path = workspace / relative
        data = path.read_bytes()
        changed_lines += len(data.splitlines())
        diff += f"\n[untracked] {relative}\nsha256:{hashlib.sha256(data).hexdigest()}\n"
    changed_files = {path.replace("\\", "/") for path in (*names, *untracked)}
    return sorted(changed_files), changed_lines, diff


def evaluate_task(
    task: Dict[str, Any], candidate: str, patch_dir: Optional[Path]
) -> Dict[str, Any]:
    fixture = (EVAL_ROOT / task["fixture"]).resolve()
    with tempfile.TemporaryDirectory(prefix=f"jawl-eval-{task['id']}-") as temporary:
        workspace = Path(temporary) / "repo"
        shutil.copytree(fixture / "repo", workspace)
        git(workspace, "init", "-b", "main")
        git(workspace, "config", "user.name", "JAWL Eval")
        git(workspace, "config", "user.email", "eval@jawl.local")
        git(workspace, "add", "--all")
        git(workspace, "commit", "-m", "fixture baseline")

        patch = patch_for_task(task, candidate, patch_dir)
        patch_applied = True
        patch_error = ""
        patch_sha256 = None
        if patch is not None:
            patch_bytes = patch.read_bytes()
            patch_sha256 = hashlib.sha256(patch_bytes).hexdigest()
            applied = git(workspace, "apply", "--whitespace=error", str(patch), check=False)
            patch_applied = applied.returncode == 0
            patch_error = (applied.stdout + applied.stderr)[-4000:]

        changed_files, changed_lines, diff = changed_files_and_lines(workspace)
        allowed = set(task["allowed_files"])
        scope_passed = bool(changed_files) and set(changed_files) <= allowed
        economy_passed = changed_lines <= int(task["max_changed_lines"])
        test_env = os.environ.copy()
        test_env["PYTHONPATH"] = str(workspace)
        test_env["PYTHONDONTWRITEBYTECODE"] = "1"
        test_env["JAWL_CODING_TASK_EVAL"] = "1"
        public = run_command(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
            workspace,
            env=test_env,
        )
        hidden = run_command(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "--rootdir",
                str(workspace),
                str(fixture / "oracle"),
            ],
            workspace,
            env=test_env,
        )
        score = round(
            0.25 * public["passed"]
            + 0.45 * hidden["passed"]
            + 0.20 * scope_passed
            + 0.10 * economy_passed,
            4,
        )
        gate_passed = bool(
            patch_applied
            and public["passed"]
            and hidden["passed"]
            and scope_passed
            and economy_passed
        )
        return {
            "id": task["id"],
            "prompt": task["prompt"],
            "gate_passed": gate_passed,
            "quality_score": score,
            "patch_applied": patch_applied,
            "patch_error": patch_error,
            "patch_sha256": patch_sha256,
            "changed_files": changed_files,
            "allowed_files": sorted(allowed),
            "scope_passed": scope_passed,
            "changed_lines": changed_lines,
            "max_changed_lines": task["max_changed_lines"],
            "economy_passed": economy_passed,
            "diff_sha256": hashlib.sha256(diff.encode("utf-8")).hexdigest(),
            "public_tests": public,
            "hidden_tests": hidden,
        }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", choices=["solution", "none"], default="solution")
    parser.add_argument("--patch-dir", type=Path)
    parser.add_argument("--task", action="append", default=[])
    parser.add_argument("--tool-metrics", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--no-output", action="store_true")
    args = parser.parse_args(argv)
    manifest = load_manifest()
    tasks = manifest["tasks"]
    if args.task:
        unknown = set(args.task) - {task["id"] for task in tasks}
        if unknown:
            parser.error("Unknown tasks: " + ", ".join(sorted(unknown)))
        tasks = [task for task in tasks if task["id"] in args.task]

    started = time.perf_counter()
    results = [evaluate_task(task, args.candidate, args.patch_dir) for task in tasks]
    tool_metrics = None
    if args.tool_metrics:
        tool_metrics = json.loads(args.tool_metrics.read_text(encoding="utf-8"))
    report = {
        "schema_version": 1,
        "benchmark": manifest["name"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "candidate": "custom" if args.patch_dir else args.candidate,
        "task_count": len(results),
        "passed_task_count": sum(item["gate_passed"] for item in results),
        "mean_quality_score": round(
            sum(item["quality_score"] for item in results) / len(results), 4
        ),
        "gate_passed": all(item["gate_passed"] for item in results),
        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        "tool_metrics": tool_metrics,
        "tasks": results,
    }
    if not args.no_output:
        output = args.output
        if output is None:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            output = REPOSITORY_ROOT / ".jawl-benchmarks" / f"tasks-{stamp}.json"
        output = output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(output.suffix + ".tmp")
        temporary.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        temporary.replace(output)
        print(f"[coding-task-eval] report: {output}")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
