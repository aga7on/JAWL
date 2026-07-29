import json
import shutil
from pathlib import Path

import pytest

from src.l2_interfaces.host.os.skills.execution import HostOSExecution
from src.l2_interfaces.host.os.skills.process_sessions import HostOSProcessSessions
from src.l2_interfaces.host.os.client import HostOSAccessLevel


def install_sandbox_runner(os_client) -> None:
    project_root = Path(__file__).resolve().parents[6]
    source = project_root / "src" / "utils" / "templates"
    target = os_client.framework_dir / "src" / "utils" / "templates"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, dirs_exist_ok=True)


@pytest.mark.asyncio
async def test_script_session_can_be_waited_and_reports_output(os_client):
    install_sandbox_runner(os_client)
    script = os_client.sandbox_dir / "short_job.py"
    script.write_text(
        "import sys\nprint('DONE:' + sys.argv[1], flush=True)\n",
        encoding="utf-8",
    )
    execution = HostOSExecution(os_client)
    sessions = HostOSProcessSessions(os_client, execution)
    await sessions.start()

    started = await sessions.start_script_session(
        "sandbox/short_job.py", arguments=["ok"], timeout_seconds=10
    )
    assert started.is_success is True, started.message
    session_id = json.loads(started.message)["id"]

    completed = await sessions.wait_for_script_session(
        session_id, wait_seconds=5, log_tail_chars=1000
    )
    payload = json.loads(completed.message)
    assert payload["status"] == "completed"
    assert payload["exit_code"] == 0
    assert "DONE:ok" in payload["log_tail"]
    await sessions.stop()


@pytest.mark.asyncio
async def test_script_session_timeout_kills_process_tree(os_client):
    install_sandbox_runner(os_client)
    script = os_client.sandbox_dir / "slow_job.py"
    script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    execution = HostOSExecution(os_client)
    sessions = HostOSProcessSessions(os_client, execution)
    await sessions.start()

    started = await sessions.start_script_session(
        "sandbox/slow_job.py", timeout_seconds=1
    )
    session_id = json.loads(started.message)["id"]
    completed = await sessions.wait_for_script_session(session_id, wait_seconds=5)

    assert json.loads(completed.message)["status"] == "timed_out"
    await sessions.stop()


@pytest.mark.asyncio
async def test_root_script_session_uses_managed_host_process(os_client):
    os_client.access_level = HostOSAccessLevel.ROOT
    script = os_client.sandbox_dir / "host_job.py"
    script.write_text(
        "import ctypes\nimport sys\nprint('HOST:' + sys.argv[1], flush=True)\n",
        encoding="utf-8",
    )
    execution = HostOSExecution(os_client)
    sessions = HostOSProcessSessions(os_client, execution)
    await sessions.start()

    started = await sessions.start_script_session(
        "sandbox/host_job.py", arguments=["ok"], timeout_seconds=10
    )
    started_payload = json.loads(started.message)
    assert started_payload["execution_mode"] == "host"

    completed = await sessions.wait_for_script_session(
        started_payload["id"], wait_seconds=5, log_tail_chars=1000
    )
    payload = json.loads(completed.message)
    assert payload["status"] == "completed"
    assert payload["exit_code"] == 0
    assert "HOST:ok" in payload["log_tail"]
    await sessions.stop()
