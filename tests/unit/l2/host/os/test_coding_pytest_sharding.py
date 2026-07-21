import asyncio

import pytest

from src.l2_interfaces.host.os.skills.coding_pytest_sharding import (
    MAX_SHARDED_TEST_FILES,
    build_pytest_shard_schedule,
    run_pytest_file_shards,
)


def test_duration_aware_schedule_uses_deterministic_lpt_lanes():
    history = {
        "tests/test_a.py": 10.0,
        "tests/test_b.py": 6.0,
        "tests/test_c.py": 4.0,
        "tests/test_d.py": 1.0,
    }

    record, lanes = build_pytest_shard_schedule(
        list(reversed(history)), history, requested_workers=2
    )
    repeated, repeated_lanes = build_pytest_shard_schedule(
        list(history), history, requested_workers=2
    )

    assert record["mode"] == "file_shards"
    assert record["effective_workers"] == 2
    assert record["history_coverage"] == 4
    assert record["worker_target_counts"] == [2, 2]
    assert record["predicted_worker_duration_sec"] == [11.0, 10.0]
    assert lanes == [
        ["tests/test_a.py", "tests/test_d.py"],
        ["tests/test_b.py", "tests/test_c.py"],
    ]
    assert repeated == record
    assert repeated_lanes == lanes


def test_duration_aware_schedule_falls_back_when_shard_bound_is_exceeded():
    targets = [f"tests/test_{index}.py" for index in range(MAX_SHARDED_TEST_FILES + 1)]

    record, lanes = build_pytest_shard_schedule(targets, {}, requested_workers=8)

    assert record["mode"] == "batch"
    assert record["effective_workers"] == 1
    assert record["reason"] == "sharded_test_file_limit_exceeded"
    assert lanes == [sorted(targets)]


@pytest.mark.asyncio
async def test_file_shard_runner_cancels_sibling_workers_and_keeps_completions():
    blocked = asyncio.Event()
    completed = []

    async def runner(target):
        if target == "slow.py":
            await blocked.wait()
        return {"passed": True, "duration_sec": 0.01}

    async def on_complete(target, result):
        completed.append(target)

    task = asyncio.create_task(
        run_pytest_file_shards(
            [["fast.py"], ["slow.py"]], runner, on_complete
        )
    )
    for _ in range(100):
        if completed:
            break
        await asyncio.sleep(0.001)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert completed == ["fast.py"]
