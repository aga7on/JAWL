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
import json
import logging
import time
import uuid
from typing import Dict, Any, List, Literal, Optional

import openai

from src.l3_agent.llm.client import LLMClient
from src.l3_agent.llm.exceptions import AllKeysExhaustedError
from src.utils._tools import redact_sensitive_text
from src.utils.token_tracker import TokenTracker
from src.utils.tracing import current_trace


class LLMExecutor:
    """
    Unified entry point for invoking language models.
    Hides all complexity of handling network and API errors.
    """

    def __init__(self, llm_client: LLMClient, token_tracker: TokenTracker) -> None:
        """
        Args:
            llm_client: Client for retrieving HTTP sessions (AsyncOpenAI).
            token_tracker: Tool for tracking input and output tokens.
        """

        self.llm = llm_client
        self.tracker = token_tracker
        self.last_call_metrics: Dict[str, Any] = {}

    async def execute(
        self,
        model_name: str,
        messages: List[Dict[str, Any]],
        temperature: float,
        logger: logging.Logger,
        log_prefix: str,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = None, 
        tool_transport: Literal["wrapper", "native", "hybrid"] = "wrapper",
        max_retries: int = 1,
        max_timeout_retries: int = 1,
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
            max_retries: Total retry attempts limit for any exceptions.
            max_timeout_retries: Specific retry attempts limit for timeouts.

        Returns:
            Optional[str]: Raw assistant response text or tool call JSON arguments.
                          Returns None if all retries failed or a fatal error occurred.
        """

        self.tracker.add_input_record(messages, log_prefix=log_prefix, logger=logger)
        request_id = str(uuid.uuid4().hex)
        started = time.perf_counter()
        self.last_call_metrics = {
            "request_id": request_id,
            "model": model_name,
            "status": "running",
            "attempts": 0,
        }
        timeout_count = 0

        for attempt in range(max_retries):
            self.last_call_metrics["attempts"] = attempt + 1
            try:
                # Retrieve an active authenticated session
                session = self.llm.get_session()

                # Execute request
                kwargs = {
                    "model": model_name,
                    "messages": messages,
                    "temperature": temperature,
                }
                if tools:
                    kwargs["tools"] = tools
                if tool_choice is not None:
                    kwargs["tool_choice"] = tool_choice

                response = await session.chat.completions.create(**kwargs)

                # Extract content and count tokens
                raw_answer = self._extract_response_text(response, tool_transport)

                self.tracker.add_output_record(
                    raw_answer, log_prefix=log_prefix, logger=logger
                )

                self.last_call_metrics = self._response_metrics(
                    response=response,
                    request_id=request_id,
                    model_name=model_name,
                    attempts=attempt + 1,
                    duration_ms=(time.perf_counter() - started) * 1000,
                    output_chars=len(raw_answer),
                )

                return raw_answer

            except asyncio.CancelledError:
                self._finish_error_metrics(
                    request_id,
                    model_name,
                    attempt + 1,
                    started,
                    "cancelled",
                    "LLM request cancelled by downstream cycle",
                )
                raise

            except AllKeysExhaustedError as e:
                logger.warning(
                    f"{log_prefix} All keys in cooldown. Waiting {e.wait_time} sec."
                )
                await asyncio.sleep(e.wait_time + 1)
                continue

            except openai.RateLimitError as e:
                wait_time = self._calculate_rate_limit_cooldown(e)
                logger.warning(
                    f"{log_prefix} Rate Limit (429). Key {session.api_key[:8]} cooled down for {wait_time}s."
                )

                self.llm.rotator.cooldown_key(session.api_key, wait_time)

                if self.llm.rotator.total_keys() == 1:
                    await asyncio.sleep(wait_time + 1)
                else:
                    await asyncio.sleep(1)  # Fast transition to the next key
                continue

            except openai.AuthenticationError:
                logger.warning(
                    f"{log_prefix} Invalid API key (401). Removing from pool ({session.api_key[:10]})."
                )
                self.llm.rotator.ban_key(session.api_key)
                continue

            except (openai.APITimeoutError, asyncio.TimeoutError):
                timeout_count += 1
                if timeout_count >= max_timeout_retries:
                    logger.error(
                        f"{log_prefix} API unavailable after {max_timeout_retries} timeouts. Aborting."
                    )
                    self._finish_error_metrics(
                        request_id,
                        model_name,
                        attempt + 1,
                        started,
                        "timeout",
                        "API request timeout",
                    )
                    return None

                logger.warning(
                    f"{log_prefix} API request timed out. Retrying ({timeout_count}/{max_timeout_retries})."
                )
                continue

            except Exception as e:
                # Pause briefly before retry on unexpected system/network errors
                if attempt == max_retries - 1:
                    logger.error(f"{log_prefix} Fatal API error: {e}")
                    self._finish_error_metrics(
                        request_id,
                        model_name,
                        attempt + 1,
                        started,
                        "error",
                        str(e),
                    )
                    return None

                logger.error(f"{log_prefix} Internal API error: {e}. Retrying request.")
                await asyncio.sleep(2)
                continue

        self._finish_error_metrics(
            request_id,
            model_name,
            max_retries,
            started,
            "exhausted",
            "LLM retries exhausted",
        )
        return None

    # -------------------------------------------------------------------------
    # Private Helpers
    # -------------------------------------------------------------------------

    def _extract_response_text(
        self,
        response: Any,
        tool_transport: Literal["wrapper", "native", "hybrid"] = "wrapper",
    ) -> str:
        """
        Extracts raw text content or tool JSON arguments from the OpenAI response.
        """

        message_obj = response.choices[0].message

        if message_obj.tool_calls:
            calls = list(message_obj.tool_calls)
            if len(calls) == 1 and (
                tool_transport == "wrapper"
                or calls[0].function.name == "execute_skill"
            ):
                return str(calls[0].function.arguments)
            merged = {
                "observation": [],
                "reasoning": [],
                "reflection": [],
                "actions": [],
            }
            if isinstance(message_obj.content, str) and message_obj.content.strip():
                merged["reflection"].append(message_obj.content.strip())
            raw_arguments = []
            for call in calls:
                argument = str(call.function.arguments)
                raw_arguments.append(argument)
                try:
                    payload = json.loads(argument)
                except (TypeError, json.JSONDecodeError):
                    # Preserve all provider output so the protocol parser can
                    # reject it visibly instead of silently dropping calls.
                    return "\n".join(raw_arguments)
                if not isinstance(payload, dict):
                    return "\n".join(raw_arguments)
                if (
                    tool_transport == "wrapper"
                    or call.function.name == "execute_skill"
                ):
                    if not isinstance(payload.get("actions", []), list):
                        return "\n".join(raw_arguments)
                    for field in ("observation", "reasoning", "reflection"):
                        value = payload.get(field)
                        if isinstance(value, str) and value.strip():
                            merged[field].append(value.strip())
                    merged["actions"].extend(payload.get("actions", []))
                else:
                    merged["actions"].append(
                        {
                            "tool_name": call.function.name,
                            "parameters": payload,
                        }
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

        content = message_obj.content or ""
        if tool_transport in {"native", "hybrid"}:
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

    @staticmethod
    def _plain_metric(value: Any) -> Optional[Any]:
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return None

    def _response_metrics(
        self,
        response: Any,
        request_id: str,
        model_name: str,
        attempts: int,
        duration_ms: float,
        output_chars: int,
    ) -> Dict[str, Any]:
        choice = response.choices[0]
        message = choice.message
        usage = getattr(response, "usage", None)
        return {
            "request_id": request_id,
            "response_id": self._plain_metric(getattr(response, "id", None)),
            "model": model_name,
            "status": "completed",
            "attempts": attempts,
            "duration_ms": round(duration_ms, 1),
            "finish_reason": self._plain_metric(
                getattr(choice, "finish_reason", None)
            ),
            "tool_call_count": len(getattr(message, "tool_calls", None) or []),
            "output_chars": output_chars,
            "provider_prompt_tokens": self._plain_metric(
                getattr(usage, "prompt_tokens", None)
            ),
            "provider_completion_tokens": self._plain_metric(
                getattr(usage, "completion_tokens", None)
            ),
            "provider_total_tokens": self._plain_metric(
                getattr(usage, "total_tokens", None)
            ),
            "trace": current_trace(),
        }

    def _finish_error_metrics(
        self,
        request_id: str,
        model_name: str,
        attempts: int,
        started: float,
        status: str,
        error: str,
    ) -> None:
        self.last_call_metrics = {
            "request_id": request_id,
            "model": model_name,
            "status": status,
            "attempts": attempts,
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            "error": redact_sensitive_text(error)[:1000],
            "trace": current_trace(),
        }

    def _calculate_rate_limit_cooldown(self, error: openai.RateLimitError) -> int:
        """
        Calculates key freeze time based on the provider's response headers.
        """

        # If the provider explicitly reports insufficient funds
        err_code = getattr(error.body, "get", lambda x: None)("code")
        if err_code == "insufficient_quota" or "billing" in str(error).lower():
            return 86400  # Freeze for 24 hours

        wait_time = 30  # Default fallback

        if error.response is not None:
            headers = error.response.headers
            # Attempt to extract common rate limit reset headers
            retry_after = (
                headers.get("retry-after")
                or headers.get("x-ratelimit-reset")
                or headers.get("retry-after-ms")
            )

            if retry_after:
                try:
                    if headers.get("retry-after-ms"):
                        wait_time = max(1, int(int(retry_after) / 1000))
                    else:
                        wait_time = int(float(retry_after))

                    # If the timestamp represents a future epoch (e.g. OpenAI)
                    if wait_time > time.time():
                        wait_time = int(wait_time - time.time())
                except ValueError:
                    pass

        # Clamp boundaries: no less than 2s (avoid spam) and no more than 5 minutes
        return max(2, min(wait_time, 300))
