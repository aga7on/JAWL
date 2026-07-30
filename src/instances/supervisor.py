"""Persistent reconciliation loop for named JAWL processes."""

from __future__ import annotations

import argparse
import os
import signal
import time
from pathlib import Path

from src.instances.manager import InstanceManager
from src.instances.paths import project_root
from src.utils._tools import SystemInstanceLock


def run_supervisor(
    root: Path,
    *,
    interval_sec: float = 1.0,
    once: bool = False,
) -> int:
    manager = InstanceManager(root)
    lock = SystemInstanceLock(manager.instances_root / "supervisor.lock")
    if not lock.acquire():
        return 0
    pid_file = manager.instances_root / "supervisor.pid"
    stop_file = manager.instances_root / "supervisor.stop"
    pid_file.write_text(str(os.getpid()), encoding="utf-8")
    stop_requested = False

    def request_stop(*_: object) -> None:
        nonlocal stop_requested
        stop_requested = True

    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)
    if hasattr(signal, "SIGINT"):
        signal.signal(signal.SIGINT, request_stop)
    try:
        while not stop_requested:
            if stop_file.exists():
                stop_file.unlink(missing_ok=True)
                break
            manager.reconcile_once()
            if once:
                break
            time.sleep(max(0.1, interval_sec))
        return 0
    finally:
        pid_file.unlink(missing_ok=True)
        lock.release()


def main() -> int:
    parser = argparse.ArgumentParser(description="JAWL instance supervisor")
    parser.add_argument("--root", type=Path, default=project_root())
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args()
    return run_supervisor(
        args.root,
        interval_sec=args.interval,
        once=args.once,
    )


if __name__ == "__main__":
    raise SystemExit(main())
