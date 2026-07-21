"""Local operator CLI for coding-command approvals."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import List, Optional

from src.l2_interfaces.host.os.coding_approvals import CodingApprovalStore


def approval_store(root_dir: Path) -> CodingApprovalStore:
    return CodingApprovalStore(
        root_dir.resolve() / "sandbox" / "_system" / "coding_approvals.json"
    )


def run_approval_cli(root_dir: Path, argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jawl.py --approvals",
        description="Review one-shot coding command approvals.",
    )
    subparsers = parser.add_subparsers(dest="operation", required=True)
    list_parser = subparsers.add_parser("list")
    list_parser.add_argument(
        "--status",
        choices=["pending", "approved", "denied", "consumed", "expired"],
    )
    for operation in ("show", "approve", "deny"):
        operation_parser = subparsers.add_parser(operation)
        operation_parser.add_argument("approval_id")
    args = parser.parse_args(argv)
    store = approval_store(root_dir)
    try:
        if args.operation == "list":
            payload = store.list(args.status)
        elif args.operation == "show":
            payload = store.get(args.approval_id)
        else:
            payload = store.decide(
                args.approval_id,
                approved=args.operation == "approve",
                actor="local-cli",
            )
    except (OSError, PermissionError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0
