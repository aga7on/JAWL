"""
Isolated LLM Request Executor.

Encapsulates the logic of communicating with OpenAI-compatible APIs:
- Retries management
- Handling Rate Limits and extracting cooldown time from response headers
- Banning dead or invalid keys (HTTP 401)
- Counting and tracking token usage

Adheres strictly to Single Responsibility Principle (SRP): agent reasoning loops
(ReAct, Swarm) remain completely unaware of raw HTTP errors.
"""

import asyncio
import hashlib
import json
import logging
import time
import uuid
from typing import Dict, Any, List, Optional

from src.l3_agent.llm.client import LLMClient
from src.l3_agent.llm.exceptions import AllKeysExhaustedError
from src.l3_agent.llm.providers import (
    LLMMessage,
    LLMProvider,
    LLMRequest,
    LLMResult,
    LLMToolDefinition,
    ProviderError,
    QWBProvider,
    RetryPolicy,
    normalize_tool_transport,
)
from src.utils._tools import redact_sensitive_text
from src.utils.token_tracker import TokenTracker
from src.utils.tracing import current_trace


class LLMExecutor:
    """
    Unified entry point for invoking language models.
    Hides all complexity of handling network and API errors.
    """

    def __init__(
        self,
        llm_client: LLMClient | LLMProvider,
        token_tracker: TokenTracker,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        """
        Args:
            llm_client: Client for retrieving HTTP sessions (AsyncOpenAI).
            token_tracker: Tool for tracking input and output tokens.
        """

        if isinstance(llm_client, LLMProvider):
            self.provider = llm_client
            self.llm = getattr(llm_client, "client", llm_client)
        else:
            # Compatibility for existing embedders/tests. New construction
            # always supplies an explicit provider from the factory.
            self.provider = QWBProvider(
                llm_client,
                bridge_managed_accounts=False,
            )
            self.llm = llm_client
        self.tracker = token_tracker
        self.retry_policy = retry_policy or RetryPolicy()
        self.last_call_metrics: Dict[str, Any] = {}
        self.last_result: LLMResult | None = None

    async def execute(
        self,
        model_name: str,
        messages: List[Dict[str, Any]],
        temperature: float,
        logger: logging.Logger,
        log_prefix: str,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = None, 
        tool_transport: str = "json_envelope",
        enable_thinking: Optional[bool] = None,
        max_retries: int = 1,
        max_timeout_retries: int = 1,
        max_invalid_request_retries: int = 1,
        session_id: str = "",
        stream: bool = False,
        retry_policy: RetryPolicy | None = None,
    ) -> Optional[str]:
        """
        Executes a request to the LLM with a robust retry system.

        Args:
            model_name: Name of the target model (e.g., 'gemini-3.1-flash-lite').
            messages: Formatted message context list.
            temperature: Creativity parameter (0.0 to 2.0).
            logger: Target subsystem logger (ReAct, Swarm, ToT).
            log_prefix: Logging prefix (e.g., '[ReAct LLM]').
            tools: Optional JSON Schema list of available tools.
            tool_choice: Optional string to force a specific tool call.
            enable_thinking: Optional provider extension. When set, sends the
                top-level ``enable_thinking`` field through ``extra_body``.
            max_retries: Total retry attempts limit for any exceptions.
            max_timeout_retries: Specific retry attempts limit for timeouts.
            max_invalid_request_retries: Bounded retries for provider input or
                stale-session rejections. Deterministic configuration errors
                are never retried.
            session_id: Optional durable provider lane, used by Goal Mode.

        Returns:
            Optional[str]: Raw assistant response text or tool call JSON arguments.
                          Returns None if all retries failed or a fatal error occurred.
        """

        estimated_input_tokens = self.tracker.add_input_record(
            messages, log_prefix=log_prefix, logger=logger
        )
        request_id = str(uuid.uuid4().hex)
        started = time.perf_counter()
        self.last_call_metrics = {
            "request_id": request_id,
            "model": model_name,
            "status": "running",
            "attempts": 0,
            "thinking_enabled": enable_thinking,
            "estimated_input_tokens": self._plain_metric(estimated_input_tokens),
            "session_mode": "goal" if session_id else "trace",
        }
        trace_id = str(current_trace().get("trace_id") or "").strip()
        lane = str(session_id or "").strip()
        effective_lane = lane or (f"jawl-{trace_id}" if trace_id else "")
        try:
            requested_transport = normalize_tool_transport(tool_transport)
            effective_transport = self.provider.resolve_tool_transport(
                requested_transport
            )
            typed_messages = tuple(
                LLMMessage.from_openai_dict(item) for item in messages
            )
            static_projection = json.dumps(
                [
                    message.as_openai_dict()
                    for message in typed_messages
                    if message.role in {"system", "developer"}
                ],
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
            static_context_hash = hashlib.sha256(
                static_projection.encode("utf-8")
            ).hexdigest()
            request = LLMRequest(
                model=model_name,
                messages=typed_messages,
                temperature=temperature,
                tools=tuple(
                    LLMToolDefinition.from_openai_dict(item)
                    for item in (tools or [])
                ),
                tool_choice=tool_choice,
                tool_transport=effective_transport,
                stream=stream,
                enable_reasoning=enable_thinking,
                session_id=effective_lane,
                metadata={
                    "trace": current_trace(),
                    "static_context_hash": static_context_hash,
                    "static_context_chars": len(static_projection),
                },
            )
        except (TypeError, ValueError) as exc:
            self._finish_error_metrics(
                request_id,
                model_name,
                0,
                started,
                "invalid_request",
                str(exc),
            )
            self.last_call_metrics.update(
                {
                    "provider": self.provider.name,
                    "error_kind": "configuration",
                    "retryable": False,
                }
            )
            logger.error(f"{log_prefix} Invalid provider request: {exc}")
            return None

        policy = retry_policy or self.retry_policy
        limits = {
            "transport": min(
                policy.transport_retries,
                max(0, int(max_timeout_retries) - 1),
            ),
            "provider": min(
                policy.provider_retries,
                max(0, int(max_retries) - 1),
            ),
            "invalid_request": max(0, int(max_invalid_request_retries)),
            "invalid_response": policy.invalid_response_retries,
            "tool_protocol": policy.tool_protocol_retries,
        }
        counters = {key: 0 for key in limits}
        max_attempts = 1 + sum(limits.values())
        attempt = 0

        while attempt < max_attempts:
            attempt += 1
            self.last_call_metrics["attempts"] = attempt
            try:
                result = await self.provider.complete(request)
                self.last_result = result
                raw_answer = self._result_to_jawl_text(
                    result,
                    effective_transport,
                )
                estimated_output_tokens = self.tracker.add_output_record(
                    raw_answer, log_prefix=log_prefix, logger=logger
                )
                self.last_call_metrics = self._result_metrics(
                    result=result,
                    request_id=request_id,
                    model_name=model_name,
                    attempts=attempt,
                    duration_ms=(time.perf_counter() - started) * 1000,
                    output_chars=len(raw_answer),
                    estimated_input_tokens=estimated_input_tokens,
                    estimated_output_tokens=estimated_output_tokens,
                    retry_counters=counters,
                    effective_transport=effective_transport,
                )
                self.last_call_metrics["static_context_hash"] = (
                    static_context_hash
                )
                self.last_call_metrics["static_context_chars"] = len(
                    static_projection
                )
                self.last_call_metrics["thinking_enabled"] = enable_thinking
                self.last_call_metrics["session_mode"] = (
                    "goal" if lane else "trace"
                )
                self._log_usage_summary(logger, log_prefix)
                return raw_answer

            except asyncio.CancelledError:
                self._finish_error_metrics(
                    request_id,
                    model_name,
                    attempt,
                    started,
                    "cancelled",
                    "LLM request cancelled by downstream cycle",
                )
                self.last_call_metrics["provider"] = self.provider.name
                self.last_call_metrics["retry_counters"] = dict(counters)
                raise
            except ProviderError as exc:
                bucket = self._retry_bucket(exc.category)
                if exc.category == "rate_limit" and not isinstance(
                    exc.cause, AllKeysExhaustedError
                ):
                    self.provider.on_rate_limit(exc)
                elif exc.category == "authentication":
                    self.provider.on_authentication_error(exc)

                can_retry = (
                    exc.retryable
                    and counters[bucket] < limits[bucket]
                    and attempt < max_attempts
                )
                if can_retry:
                    counters[bucket] += 1
                    delay = self._retry_delay(exc, policy, counters[bucket])
                    logger.warning(
                        f"{log_prefix} {self.provider.name} {exc.category} "
                        f"failure. Retrying in {delay:g}s "
                        f"({counters[bucket]}/{limits[bucket]} {bucket})."
                    )
                    if delay:
                        await asyncio.sleep(delay)
                    continue

                status = self._metric_status_for_error(exc)
                safe_error = redact_sensitive_text(str(exc))[:1000]
                prefix = (
                    "Invalid upstream request"
                    if status == "invalid_request"
                    else f"{self.provider.name} request failed"
                )
                logger.error(
                    f"{log_prefix} {prefix} "
                    f"({exc.category}, retryable={exc.retryable}): {safe_error}"
                )
                self._finish_error_metrics(
                    request_id,
                    model_name,
                    attempt,
                    started,
                    status,
                    safe_error,
                )
                self.last_call_metrics.update(
                    {
                        "provider": self.provider.name,
                        "error_kind": exc.category,
                        "error_code": exc.code,
                        "status_code": exc.status_code,
                        "retryable": exc.retryable,
                        "retry_counters": dict(counters),
                        "tool_transport": effective_transport,
                    }
                )
                return None
            except Exception as exc:
                safe_error = redact_sensitive_text(str(exc))[:1000]
                logger.error(f"{log_prefix} Provider boundary failure: {safe_error}")
                self._finish_error_metrics(
                    request_id,
                    model_name,
                    attempt,
                    started,
                    "error",
                    safe_error,
                )
                self.last_call_metrics.update(
                    {
                        "provider": self.provider.name,
                        "error_kind": "provider_boundary",
                        "retryable": False,
                        "retry_counters": dict(counters),
                    }
                )
                return None

        self._finish_error_metrics(
            request_id,
            model_name,
            attempt,
            started,
            "exhausted",
            "provider retries exhausted",
        )
        self.last_call_metrics["provider"] = self.provider.name
        self.last_call_metrics["retry_counters"] = dict(counters)
        return None

    # -------------------------------------------------------------------------
    # Private Helpers
    # -------------------------------------------------------------------------

    @staticmethod
    def _retry_bucket(category: str) -> str:
        """Map a provider error to its independently budgeted retry lane."""

        if category in {"transport", "timeout"}:
            return "transport"
        if category == "invalid_response":
            return "invalid_response"
        if category == "tool_protocol":
            return "tool_protocol"
        if category in {"configuration", "input_rejected", "session_state"}:
            return "invalid_request"
        return "provider"

    def _retry_delay(
        self,
        error: ProviderError,
        policy: RetryPolicy,
        retry_number: int,
    ) -> float:
        """Return a bounded delay without leaking provider-specific policy."""

        if isinstance(error.cause, AllKeysExhaustedError):
            return float(error.retry_after or error.cause.wait_time + 1)
        if error.category == "rate_limit":
            rotator = getattr(getattr(self.provider, "client", None), "rotator", None)
            total_keys = getattr(rotator, "total_keys", lambda: 1)()
            # Rotate immediately when another local credential is available.
            if isinstance(total_keys, int) and total_keys > 1:
                return 1.0
            if error.retry_after is not None:
                return max(0.0, float(error.retry_after))
        return min(
            policy.base_delay_seconds * (2 ** max(1, retry_number)),
            policy.max_delay_seconds,
        )

    @staticmethod
    def _metric_status_for_error(error: ProviderError) -> str:
        if error.category in {"configuration", "input_rejected", "session_state"}:
            return "invalid_request"
        if error.category == "timeout":
            return "timeout"
        if error.category in {"transport", "provider", "rate_limit"}:
            return "upstream_unavailable"
        if error.category in {"authentication", "authorization"}:
            return error.category
        return error.category

    @staticmethod
    def _result_to_jawl_text(result: LLMResult, tool_transport: str) -> str:
        """Project a provider-neutral result into JAWL's action envelope."""

        calls = list(result.tool_calls)
        if calls:
            if len(calls) == 1 and (
                tool_transport == "json_envelope"
                or calls[0].name == "execute_skill"
            ):
                return calls[0].arguments

            merged: dict[str, list[Any]] = {
                "observation": [],
                "reasoning": [],
                "reflection": [],
                "actions": [],
            }
            if result.content.strip():
                merged["reflection"].append(result.content.strip())
            raw_arguments: list[str] = []
            for call in calls:
                raw_arguments.append(call.arguments)
                try:
                    payload = json.loads(call.arguments)
                except (TypeError, json.JSONDecodeError):
                    return "\n".join(raw_arguments)
                if not isinstance(payload, dict):
                    return "\n".join(raw_arguments)
                if tool_transport == "json_envelope" or call.name == "execute_skill":
                    actions = payload.get("actions", [])
                    if not isinstance(actions, list):
                        return "\n".join(raw_arguments)
                    for field in ("observation", "reasoning", "reflection"):
                        value = payload.get(field)
                        if isinstance(value, str) and value.strip():
                            merged[field].append(value.strip())
                    merged["actions"].extend(actions)
                else:
                    merged["actions"].append(
                        {"tool_name": call.name, "parameters": payload}
                    )
            return json.dumps(
                {
                    "observation": "\n".join(merged["observation"]),
                    "reasoning": "\n".join(merged["reasoning"]),
                    "reflection": "\n".join(merged["reflection"]),
                    "actions": merged["actions"],
                },
                ensure_ascii=False,
            )

        content = result.content or ""
        if tool_transport == "native":
            try:
                existing = json.loads(content)
            except (TypeError, json.JSONDecodeError):
                existing = None
            if isinstance(existing, dict) and "actions" in existing:
                return content
            return json.dumps(
                {
                    "observation": "[Native response]",
                    "reasoning": "",
                    "reflection": content,
                    "actions": [],
                },
                ensure_ascii=False,
            )
        return content

    def _result_metrics(
        self,
        *,
        result: LLMResult,
        request_id: str,
        model_name: str,
        attempts: int,
        duration_ms: float,
        output_chars: int,
        estimated_input_tokens: Any,
        estimated_output_tokens: Any,
        retry_counters: dict[str, int],
        effective_transport: str,
    ) -> Dict[str, Any]:
        usage = result.usage
        provider_prompt = self._plain_metric(usage.prompt_tokens)
        estimated_input = self._plain_metric(estimated_input_tokens)
        prompt_savings = None
        prompt_ratio = None
        if isinstance(estimated_input, (int, float)) and isinstance(
            provider_prompt, (int, float)
        ):
            prompt_savings = max(0, int(estimated_input - provider_prompt))
            if estimated_input > 0:
                prompt_ratio = round(provider_prompt / estimated_input, 4)
        return {
            "request_id": request_id,
            "response_id": self._plain_metric(result.response_id),
            "model": model_name,
            "provider_model": self._plain_metric(result.model),
            "provider": self.provider.name,
            "capabilities": self.provider.capabilities.public(),
            "status": "completed",
            "attempts": attempts,
            "retry_counters": dict(retry_counters),
            "duration_ms": round(duration_ms, 1),
            "finish_reason": self._plain_metric(result.finish_reason),
            "tool_transport": effective_transport,
            "tool_call_count": len(result.tool_calls),
            "output_chars": output_chars,
            "estimated_input_tokens": estimated_input,
            "estimated_output_tokens": self._plain_metric(estimated_output_tokens),
            "provider_prompt_tokens": provider_prompt,
            "provider_completion_tokens": self._plain_metric(
                usage.completion_tokens
            ),
            "provider_total_tokens": self._plain_metric(usage.total_tokens),
            "provider_reasoning_tokens": self._plain_metric(
                usage.reasoning_tokens
            ),
            "provider_cached_tokens": self._plain_metric(usage.cached_tokens),
            "provider_prompt_savings_tokens": prompt_savings,
            "provider_prompt_ratio": prompt_ratio,
            "reasoning_chars": len(result.reasoning),
            "provider_metadata": dict(result.provider_metadata),
            "trace": current_trace(),
        }

    @staticmethod
    def _plain_metric(value: Any) -> Optional[Any]:
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return None

    def _log_usage_summary(
        self, logger: logging.Logger, log_prefix: str
    ) -> None:
        """Expose provider-accounted context separately from local snapshots."""

        metrics = self.last_call_metrics
        logger.info(
            f"{log_prefix} Usage: lane={metrics.get('session_mode')}, "
            f"local_input≈{metrics.get('estimated_input_tokens')}, "
            f"provider_prompt={metrics.get('provider_prompt_tokens')}, "
            f"provider_completion={metrics.get('provider_completion_tokens')}, "
            f"reasoning={metrics.get('provider_reasoning_tokens')}, "
            f"prompt_reuse≈{metrics.get('provider_prompt_savings_tokens')}."
        )

    def _finish_error_metrics(
        self,
        request_id: str,
        model_name: str,
        attempts: int,
        started: float,
        status: str,
        error: str,
    ) -> None:
        thinking_enabled = self.last_call_metrics.get("thinking_enabled")
        session_mode = self.last_call_metrics.get("session_mode")
        estimated_input_tokens = self.last_call_metrics.get(
            "estimated_input_tokens"
        )
        self.last_call_metrics = {
            "request_id": request_id,
            "model": model_name,
            "status": status,
            "attempts": attempts,
            "thinking_enabled": thinking_enabled,
            "session_mode": session_mode,
            "estimated_input_tokens": estimated_input_tokens,
            "estimated_output_tokens": None,
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            "error": redact_sensitive_text(error)[:1000],
            "trace": current_trace(),
        }

