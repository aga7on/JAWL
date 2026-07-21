"""Lightweight async-safe correlation context for agent work."""

from __future__ import annotations

import uuid
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple


_TRACE_CONTEXT: ContextVar[Optional[Dict[str, Any]]] = ContextVar(
    "jawl_trace_context", default=None
)


def begin_trace(
    kind: str,
    trace_id: Optional[str] = None,
    parent_trace_id: Optional[str] = None,
    **labels: Any,
) -> Tuple[Token, Dict[str, Any]]:
    """Start a trace in the current async context and return its reset token."""

    context = {
        "trace_id": trace_id or uuid.uuid4().hex,
        "kind": kind,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    if parent_trace_id:
        context["parent_trace_id"] = parent_trace_id
    context.update(
        {
            key: value
            for key, value in labels.items()
            if isinstance(value, (str, int, float, bool)) or value is None
        }
    )
    token = _TRACE_CONTEXT.set(context)
    return token, dict(context)


def current_trace() -> Dict[str, Any]:
    """Return a detached copy of the current trace context."""

    context = _TRACE_CONTEXT.get()
    return dict(context) if context else {}


def reset_trace(token: Token) -> None:
    """Restore the parent trace context."""

    _TRACE_CONTEXT.reset(token)
