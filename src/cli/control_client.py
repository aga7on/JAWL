"""Short-lived localhost client for the allowlisted JAWL operator protocol."""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any, Dict


ROOT_DIR = Path(__file__).resolve().parent.parent.parent
PORT_FILE = (
    ROOT_DIR
    / "src"
    / "utils"
    / "local"
    / "data"
    / "interfaces"
    / "host"
    / "terminal"
    / "terminal.port"
)


async def request_control_async(
    action: str,
    params: Dict[str, Any] | None = None,
    *,
    timeout: float = 5.0,
    port_file: Path = PORT_FILE,
) -> Dict[str, Any]:
    if not port_file.exists():
        raise ConnectionError(
            "Host Terminal control port is unavailable. Start JAWL and enable "
            "the Host Terminal interface."
        )
    try:
        port = int(port_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError) as exc:
        raise ConnectionError("Host Terminal port file is invalid.") from exc
    if port < 1 or port > 65535:
        raise ConnectionError("Host Terminal port is outside the valid range.")

    reader = None
    writer = None
    request_id = uuid.uuid4().hex
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", port), timeout=timeout
        )
        writer.write(b"JAWL_CONTROL\n")
        envelope = {
            "type": "control",
            "id": request_id,
            "action": action,
            "params": params or {},
        }
        writer.write(
            (json.dumps(envelope, ensure_ascii=False) + "\n").encode("utf-8")
        )
        await writer.drain()
        raw = await asyncio.wait_for(reader.readline(), timeout=timeout)
        if not raw or len(raw) > 65536:
            raise ConnectionError("Invalid or oversized control response.")
        response = json.loads(raw.decode("utf-8"))
        if (
            not isinstance(response, dict)
            or response.get("type") != "control_result"
            or response.get("id") != request_id
        ):
            raise ConnectionError("Mismatched control response.")
        if not response.get("ok"):
            raise RuntimeError(str(response.get("error", "Control failed.")))
        result = response.get("result")
        if not isinstance(result, dict):
            raise ConnectionError("Control response result must be an object.")
        return result
    except asyncio.TimeoutError as exc:
        raise TimeoutError("JAWL operator control timed out.") from exc
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass


def request_control(
    action: str,
    params: Dict[str, Any] | None = None,
    *,
    timeout: float = 5.0,
    port_file: Path = PORT_FILE,
) -> Dict[str, Any]:
    return asyncio.run(
        request_control_async(
            action, params, timeout=timeout, port_file=port_file
        )
    )
