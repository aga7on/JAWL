"""Small progressive-discovery tool surface for the JAWL Debug Broker."""

from __future__ import annotations

import re
from typing import Any, Optional

from src.l2_interfaces.debug_broker.client import DebugBrokerClient, DebugBrokerError
from src.l3_agent.skills.registry import SkillResult, skill


class DebugBroker:
    """Model-facing orchestration API; provider-specific schemas stay discoverable."""

    _IDENTIFIER = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")

    def __init__(self, client: DebugBrokerClient) -> None:
        self.client = client

    @classmethod
    def _identifier(cls, value: str, field: str) -> str:
        normalized = value.strip()
        if not cls._IDENTIFIER.fullmatch(normalized):
            raise DebugBrokerError(f"Invalid {field}")
        return normalized

    @skill()
    async def list_providers(self) -> SkillResult:
        """List installed RE providers, availability, lifecycle, and operation counts."""

        return SkillResult.ok(
            self.client.bounded_json(
                {"providers": self.client.provider_snapshot()}
            )
        )

    @skill()
    async def search_operations(
        self,
        query: str,
        provider: Optional[str] = None,
        limit: int = 12,
    ) -> SkillResult:
        """Search exact broker operations and return current schemas plus hashes."""

        try:
            if not isinstance(query, str) or len(query) > 1000:
                raise DebugBrokerError("query must be a string of at most 1000 chars")
            if provider is not None:
                provider = self._identifier(provider, "provider")
            return SkillResult.ok(
                self.client.bounded_json(
                    self.client.search_operations(query, provider, limit)
                )
            )
        except DebugBrokerError as exc:
            return SkillResult.fail(str(exc))
    @skill()
    async def start_session(
        self,
        provider: str,
        target: Optional[str] = None,
        options: Optional[dict[str, Any]] = None,
    ) -> SkillResult:
        """Create a durable provider session and auto-start required local processes."""

        try:
            provider = self._identifier(provider, "provider")
            if target is not None and (
                not isinstance(target, str)
                or not target.strip()
                or len(target) > 4096
                or "\x00" in target
            ):
                raise DebugBrokerError("target must be a bounded NUL-free string")
            return SkillResult.ok(
                self.client.bounded_json(
                    await self.client.start_session(provider, target, options)
                )
            )
        except (DebugBrokerError, OSError, TimeoutError) as exc:
            return SkillResult.fail(str(exc))

    @skill()
    async def call_operation(
        self,
        provider: str,
        operation: str,
        arguments: dict[str, Any],
        expected_schema_sha256: str,
        session_id: Optional[str] = None,
    ) -> SkillResult:
        """Execute one exact typed provider operation after a current schema check."""

        try:
            provider = self._identifier(provider, "provider")
            operation = self._identifier(operation, "operation")
            if session_id is not None:
                session_id = self._identifier(session_id, "session_id")
            if not re.fullmatch(r"[0-9a-f]{64}", expected_schema_sha256):
                raise DebugBrokerError("Invalid expected_schema_sha256")
            payload = await self.client.call_operation(
                provider,
                operation,
                arguments,
                expected_schema_sha256,
                session_id,
            )
            return SkillResult.ok(self.client.bounded_json(payload))
        except (DebugBrokerError, OSError, TimeoutError) as exc:
            return SkillResult.fail(str(exc))

    @skill()
    async def wait_session(
        self, session_id: str, timeout_seconds: float = 30
    ) -> SkillResult:
        """Wait briefly for a provider state transition without blocking a ReAct cycle."""

        try:
            session_id = self._identifier(session_id, "session_id")
            if not isinstance(timeout_seconds, (int, float)) or isinstance(
                timeout_seconds, bool
            ):
                raise DebugBrokerError("timeout_seconds must be numeric")
            if timeout_seconds < 0 or timeout_seconds > 60:
                raise DebugBrokerError("timeout_seconds must be between 0 and 60")
            return SkillResult.ok(
                self.client.bounded_json(
                    await self.client.wait_session(session_id, timeout_seconds)
                )
            )
        except (DebugBrokerError, OSError, TimeoutError) as exc:
            return SkillResult.fail(str(exc))

    @skill()
    async def session_snapshot(
        self, session_id: Optional[str] = None
    ) -> SkillResult:
        """Inspect one or all durable debug sessions without touching providers."""

        try:
            if session_id is not None:
                session_id = self._identifier(session_id, "session_id")
            return SkillResult.ok(
                self.client.bounded_json(
                    self.client.session_snapshot(session_id)
                )
            )
        except DebugBrokerError as exc:
            return SkillResult.fail(str(exc))

    @skill()
    async def stop_session(self, session_id: str) -> SkillResult:
        """Close a broker session and only the provider processes it owns."""

        try:
            session_id = self._identifier(session_id, "session_id")
            return SkillResult.ok(
                self.client.bounded_json(
                    await self.client.stop_session(session_id)
                )
            )
        except (DebugBrokerError, OSError, TimeoutError) as exc:
            return SkillResult.fail(str(exc))
