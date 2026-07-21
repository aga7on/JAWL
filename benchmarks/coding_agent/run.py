"""Run the deterministic JAWL coding capability benchmark."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List


BENCHMARK_DIR = Path(__file__).resolve().parent
REPOSITORY_ROOT = BENCHMARK_DIR.parents[1]
DEFAULT_MANIFEST = BENCHMARK_DIR / "manifest.json"


def load_manifest(path: Path) -> Dict[str, Any]:
    """Load and validate the small, versioned benchmark manifest."""

    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != 1:
        raise ValueError("Unsupported benchmark manifest version.")
    capabilities = data.get("capabilities")
    if not isinstance(capabilities, list) or not capabilities:
        raise ValueError("Benchmark manifest has no capabilities.")
    identifiers = set()
    for capability in capabilities:
        identifier = capability.get("id")
        tests = capability.get("tests")
        if not isinstance(identifier, str) or not identifier:
            raise ValueError("Every capability requires a non-empty id.")
        if identifier in identifiers:
            raise ValueError(f"Duplicate capability id: {identifier}")
        if not isinstance(tests, list) or not tests:
            raise ValueError(f"Capability '{identifier}' has no tests.")
        if not all(isinstance(node, str) and "::" in node for node in tests):
            raise ValueError(f"Capability '{identifier}' has an invalid test node.")
        identifiers.add(identifier)
    minimum = data.get("minimum_capability_pass_rate")
    if not isinstance(minimum, (int, float)) or not 0 <= minimum <= 1:
        raise ValueError("minimum_capability_pass_rate must be between 0 and 1.")
    return data


def git_revision() -> str | None:
    """Return the benchmarked Git revision without making Git interactive."""

    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPOSITORY_ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def run_capability(capability: Dict[str, Any]) -> Dict[str, Any]:
    """Run one capability as an isolated pytest process."""

    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "--disable-warnings",
        "--tb=short",
        *capability["tests"],
    ]
    environment = os.environ.copy()
    environment["PYTHONHASHSEED"] = "0"
    started = time.perf_counter()
    result = subprocess.run(
        command,
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    duration_ms = round((time.perf_counter() - started) * 1000, 1)
    output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    payload: Dict[str, Any] = {
        "id": capability["id"],
        "description": capability["description"],
        "status": "passed" if result.returncode == 0 else "failed",
        "test_count": len(capability["tests"]),
        "duration_ms": duration_ms,
        "exit_code": result.returncode,
    }
    if result.returncode != 0:
        payload["failure_output_tail"] = output[-8000:]
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--capability",
        action="append",
        default=[],
        help="Run only this capability id; may be repeated.",
    )
    parser.add_argument("--list", action="store_true", help="List capabilities.")
    parser.add_argument("--output", type=Path, help="Write JSON to this path.")
    parser.add_argument(
        "--no-output",
        action="store_true",
        help="Do not persist the default JSON report.",
    )
    return parser


def main(argv: List[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = load_manifest(args.manifest.resolve())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Benchmark manifest error: {exc}", file=sys.stderr)
        return 2

    available = {item["id"]: item for item in manifest["capabilities"]}
    if args.list:
        for identifier, capability in available.items():
            print(f"{identifier}: {capability['description']}")
        return 0

    unknown = sorted(set(args.capability) - set(available))
    if unknown:
        print(f"Unknown capabilities: {', '.join(unknown)}", file=sys.stderr)
        return 2
    selected = (
        [available[identifier] for identifier in args.capability]
        if args.capability
        else list(available.values())
    )

    started = time.perf_counter()
    results = []
    for capability in selected:
        print(f"[benchmark] {capability['id']} ...", flush=True)
        result = run_capability(capability)
        results.append(result)
        print(
            f"[benchmark] {result['id']}: {result['status']} "
            f"({result['duration_ms']} ms)",
            flush=True,
        )

    passed = sum(item["status"] == "passed" for item in results)
    pass_rate = passed / len(results)
    threshold = float(manifest["minimum_capability_pass_rate"])
    report = {
        "schema_version": 1,
        "benchmark": manifest["name"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repository_root": str(REPOSITORY_ROOT),
        "git_revision": git_revision(),
        "python": sys.version.split()[0],
        "selected_capability_count": len(results),
        "test_count": sum(item["test_count"] for item in results),
        "passed_capability_count": passed,
        "capability_pass_rate": round(pass_rate, 6),
        "minimum_capability_pass_rate": threshold,
        "gate_passed": pass_rate >= threshold,
        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        "capabilities": results,
    }

    if not args.no_output:
        output = args.output
        if output is None:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            output = REPOSITORY_ROOT / ".jawl-benchmarks" / f"coding-{stamp}.json"
        output = output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(output.suffix + ".tmp")
        temporary.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(output)
        print(f"[benchmark] report: {output}")

    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
