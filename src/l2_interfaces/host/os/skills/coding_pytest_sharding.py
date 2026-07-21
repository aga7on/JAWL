"""Bounded duration-aware scheduling for affected pytest files."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Sequence, Tuple


ShardRunner = Callable[[str], Awaitable[Dict[str, Any]]]
ShardCallback = Callable[[str, Dict[str, Any]], Awaitable[None]]


MAX_PYTEST_WORKERS = 8
MAX_SHARDED_TEST_FILES = 64


def _duration(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0:
        return None
    return numeric


def build_pytest_shard_schedule(
    targets: Sequence[str],
    duration_history: Mapping[str, Any],
    requested_workers: int,
) -> Tuple[Dict[str, Any], List[List[str]]]:
    """Return a public schedule contract and private deterministic worker lanes."""

    unique_targets = sorted(dict.fromkeys(str(target) for target in targets))
    if requested_workers <= 1:
        reason = "single_worker_requested"
    elif len(unique_targets) < 2:
        reason = "insufficient_affected_tests"
    elif len(unique_targets) > MAX_SHARDED_TEST_FILES:
        reason = "sharded_test_file_limit_exceeded"
    else:
        reason = "duration_aware_file_shards"
    if reason != "duration_aware_file_shards":
        record = {
            "requested_workers": requested_workers,
            "effective_workers": 1,
            "mode": "batch",
            "reason": reason,
            "target_count": len(unique_targets),
            "history_coverage": 0,
            "worker_target_counts": [len(unique_targets)] if unique_targets else [],
            "predicted_worker_duration_sec": [],
        }
        canonical = json.dumps(record, sort_keys=True, separators=(",", ":"))
        record["schedule_sha256"] = hashlib.sha256(canonical.encode()).hexdigest()
        return record, [unique_targets] if unique_targets else []

    known = {
        target: value
        for target in unique_targets
        if (value := _duration(duration_history.get(target))) is not None
    }
    default_duration = (
        sorted(known.values())[len(known) // 2] if known else 1.0
    )
    predicted = {
        target: known.get(target, default_duration) for target in unique_targets
    }
    ordered = sorted(unique_targets, key=lambda target: (-predicted[target], target))
    worker_count = min(requested_workers, len(ordered), MAX_PYTEST_WORKERS)
    lanes: List[List[str]] = [[] for _ in range(worker_count)]
    loads = [0.0 for _ in range(worker_count)]
    for target in ordered:
        lane_index = min(range(worker_count), key=lambda index: (loads[index], index))
        lanes[lane_index].append(target)
        loads[lane_index] += predicted[target]
    record = {
        "requested_workers": requested_workers,
        "effective_workers": worker_count,
        "mode": "file_shards",
        "reason": reason,
        "target_count": len(unique_targets),
        "history_coverage": len(known),
        "worker_target_counts": [len(lane) for lane in lanes],
        "predicted_worker_duration_sec": [round(load, 3) for load in loads],
    }
    canonical = json.dumps(
        {**record, "ordered_targets": ordered},
        sort_keys=True,
        separators=(",", ":"),
    )
    record["schedule_sha256"] = hashlib.sha256(canonical.encode()).hexdigest()
    return record, lanes


async def run_pytest_file_shards(
    lanes: Sequence[Sequence[str]],
    runner: ShardRunner,
    on_complete: ShardCallback,
) -> Tuple[List[Tuple[str, Dict[str, Any]]], float]:
    """Run one pytest file per process across deterministic worker lanes."""

    started = time.monotonic()
    completed: List[Tuple[str, Dict[str, Any]]] = []
    completion_lock = asyncio.Lock()

    async def run_lane(targets: Sequence[str]) -> None:
        for target in targets:
            result = await runner(target)
            async with completion_lock:
                completed.append((target, result))
                await on_complete(target, result)

    tasks = [asyncio.create_task(run_lane(lane)) for lane in lanes if lane]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    return completed, round(time.monotonic() - started, 3)
