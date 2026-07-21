import pytest
import json
import sqlite3
from sqlalchemy import text

from src.l1_databases.sql.db import SQLDB
from src.l1_databases.sql.management.ticks import SQLTicks


async def save(ticks: SQLTicks, label: str) -> str:
    return await ticks.save_tick(
        thoughts=label,
        actions=[],
        results={"step": 1, "max_steps": 1, "label": label},
    )


@pytest.mark.asyncio
async def test_tick_timeline_rewind_branches_without_deleting_memory(memory_db):
    ticks = SQLTicks(memory_db)
    await ticks.bootstrap_migrations()
    first = await save(ticks, "first")
    second = await save(ticks, "second")
    checkpoint = await ticks.get_active_cursor()
    third = await save(ticks, "third-on-main")
    forward_cursor = await ticks.get_active_cursor()

    branch = await ticks.branch_from_cursor(
        checkpoint["timeline_id"], checkpoint["tick_id"], "rewind to second"
    )
    fourth = await save(ticks, "fourth-on-branch")

    assert [item.id for item in await ticks.get_ticks(limit=10)] == [
        first,
        second,
        fourth,
    ]
    assert (await ticks.get_active_cursor())["timeline_id"] == branch
    history = await ticks.get_ticks_by_time(
        "2000-01-01 00:00:00", "2100-01-01 00:00:00", detail=True
    )
    assert history.is_success is True
    assert "fourth-on-branch" in history.message
    assert "third-on-main" not in history.message

    restored_forward = await ticks.branch_from_cursor(
        forward_cursor["timeline_id"],
        forward_cursor["tick_id"],
        "restore pre-rewind branch",
    )
    fifth = await save(ticks, "fifth-after-forward-restore")

    assert [item.id for item in await ticks.get_ticks(limit=10)] == [
        first,
        second,
        third,
        fifth,
    ]
    assert restored_forward != branch

    # All branches remain in the append-only physical table.
    async with memory_db.session_factory() as session:
        rows = await session.execute(text("SELECT COUNT(*) FROM ticks"))
        assert rows.scalar_one() == 5


@pytest.mark.asyncio
async def test_tick_timeline_rejects_cursor_from_unrelated_branch(memory_db):
    ticks = SQLTicks(memory_db)
    await ticks.bootstrap_migrations()
    main_tick = await save(ticks, "main")
    main_cursor = await ticks.get_active_cursor()
    branch = await ticks.branch_from_cursor("main", main_tick, "branch")
    branch_tick = await save(ticks, "branch-only")

    with pytest.raises(ValueError, match="not reachable"):
        await ticks.branch_from_cursor(
            main_cursor["timeline_id"], branch_tick, "invalid cross-branch cursor"
        )
    assert (await ticks.get_active_cursor())["timeline_id"] == branch


@pytest.mark.asyncio
async def test_tick_timeline_migrates_legacy_ticks_without_losing_rows(tmp_path):
    database_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            "CREATE TABLE ticks ("
            "id TEXT PRIMARY KEY, created_at DATETIME, thoughts TEXT NOT NULL, "
            "actions JSON NOT NULL, results JSON NOT NULL)"
        )
        connection.execute(
            "INSERT INTO ticks VALUES (?, CURRENT_TIMESTAMP, ?, ?, ?)",
            (
                "legacy-tick",
                "legacy thought",
                json.dumps([]),
                json.dumps({"step": 1, "max_steps": 1}),
            ),
        )
        connection.commit()
    finally:
        connection.close()

    db = SQLDB(str(database_path))
    await db.connect()
    try:
        ticks = SQLTicks(db)
        await ticks.bootstrap_migrations()

        restored = await ticks.get_ticks(limit=10)
        cursor = await ticks.get_active_cursor()

        assert [item.id for item in restored] == ["legacy-tick"]
        assert restored[0].timeline_id == "main"
        assert cursor == {"timeline_id": "main", "tick_id": "legacy-tick"}
    finally:
        await db.disconnect()
