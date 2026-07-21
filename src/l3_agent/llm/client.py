"""
Asynchronous LLM Client Wrapper.

Encapsulates HTTP session management and provides seamless integration with any
OpenAI-compatible API providers (OpenRouter, Gemini, Anthropic, local vLLM, etc.).
Includes proxy support and key rotation integration.
"""

import httpx
from openai import AsyncOpenAI
from urllib.parse import urlparse

from src.utils.logger import main_logger
from src.l3_agent.llm.api_keys.rotator import APIKeyRotator


class LLMClient:
    """
    Interface for communication between the agent's brain and the language model.
    Includes automatic key rotation and HTTP sessions caching.
    """

    def __init__(
        self, api_url: str, api_keys_rotator: APIKeyRotator, proxy_url: str = None
    ) -> None:
        """
        Initializes the client.

        Args:
            api_url: Base URL for the OpenAI-compatible API.
            api_keys_rotator: API key manager (rotator).
            proxy_url: Optional proxy server URL.
        """

        self.api_url = api_url
        self.rotator = api_keys_rotator
        self.proxy_url = proxy_url

        self._sessions: dict[str, AsyncOpenAI] = {}

        if self.api_url and not self.api_url.startswith(("http://", "https://")):
            if "localhost" in self.api_url or "127.0.0.1" in self.api_url:
                self.api_url = f"http://{self.api_url}"
            else:
                self.api_url = f"https://{self.api_url}"

        if self.api_url:
            main_logger.info(f"[LLM] Client initialized (URL: {self.api_url}).")
        else:
            main_logger.info("[LLM] Client initialized (Default OpenAI URL).")

    def _is_local_api(self) -> bool:
        """Return whether the configured API endpoint is on this machine."""
        hostname = urlparse(self.api_url).hostname if self.api_url else None
        return hostname in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}

    def get_session(self) -> AsyncOpenAI:
        """
        Returns a cached OpenAI session with an active "live" key.

        Returns:
            AsyncOpenAI: An initialized and authenticated client session.

        Raises:
            RuntimeError: If all API keys are exhausted or banned.
        """

        api_key = self.rotator.get_next_key()

        if not api_key:
            raise RuntimeError("[LLM] No API keys available. Limits exhausted.")

        if api_key not in self._sessions:
            # Qwen can legitimately think for several minutes. Keep connection,
            # write and pool acquisition bounded, but let QWB own generation
            # inactivity/total watchdogs instead of imposing a JAWL read deadline.
            timeout = httpx.Timeout(
                connect=15.0,
                read=None,
                write=120.0,
                pool=30.0,
            )
            if self._is_local_api():
                # Ignore HTTP(S)_PROXY/ALL_PROXY from the environment too.
                # Otherwise a cancelled local request can stay alive inside the
                # proxy and continue consuming a Qwen account in QWB.
                http_client = httpx.AsyncClient(trust_env=False)
            elif self.proxy_url:
                http_client = httpx.AsyncClient(proxy=self.proxy_url)
            else:
                http_client = None

            self._sessions[api_key] = AsyncOpenAI(
                api_key=api_key,
                base_url=self.api_url if self.api_url else None,
                http_client=http_client,
                timeout=timeout,
                max_retries=0,
            )

        return self._sessions[api_key]

    async def close(self) -> None:
        """
        Correctly closes all active HTTP connection pools.
        Called during system shutdown (Graceful Shutdown).
        """
        for session in self._sessions.values():
            await session.close()
        self._sessions.clear()
        main_logger.info("[LLM] All HTTP sessions closed.")
