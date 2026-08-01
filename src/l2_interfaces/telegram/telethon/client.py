"""
Stateful client for working with Telegram User API (Telethon).

Stores session locally in a SQLite file (Session File).
Provides terminal authorization on the first run (manual entry of phone and code)
and provides a context provider with info about the agent's profile and chats.
"""

import asyncio
import logging
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse, parse_qs
from python_socks import parse_proxy_url
from telethon import TelegramClient
from telethon.network import ConnectionTcpMTProxyRandomizedIntermediate
from telethon.sessions import MemorySession
from telethon.tl.functions.users import GetFullUserRequest

from src.utils.logger import main_logger
from src.l2_interfaces.telegram.telethon.state import TelethonState
from src.l2_interfaces.telegram.telethon.proxy_manager import MTProxyManager


class TelethonClient:
    """
    Manager for connecting to Telegram servers and managing account sessions.
    """

    def __init__(
        self,
        state: TelethonState,
        api_id: int,
        api_hash: str,
        session_path: str,
        timezone: int,
        proxy_url: Optional[str] = None,
        proxy_manager: Optional[MTProxyManager] = None,
    ) -> None:
        self.state = state
        self.api_id = api_id
        self.api_hash = api_hash
        self.session_path = session_path
        self.timezone = timezone
        self.proxy_url = proxy_url
        self._proxy_manager = proxy_manager or MTProxyManager()
        self._client: Optional[TelegramClient] = None
        self._probe_dc: tuple[int, str, int] | None = None

    def client(self) -> TelegramClient:
        """
        Safe access to the Telethon instance.

        Returns:
            TelegramClient: Active Telethon client.

        Raises:
            RuntimeError: If `start()` has not been called yet.
        """
        if not self._client:
            raise RuntimeError("TelethonClient is not started. Instance is unavailable.")
        return self._client

    async def start(self) -> None:
        main_logger.info("[Telegram Telethon] Initializing client.")

        session_dir = Path(self.session_path).parent
        session_dir.mkdir(parents=True, exist_ok=True)

        configured_proxy = self.proxy_url
        preferred_proxy = self._proxy_manager.resolve_proxy(configured_proxy)
        candidates = self._proxy_manager.fallback_chain(preferred_proxy)
        session_file = Path(self.session_path)
        if session_file.suffix != ".session":
            session_file = session_file.with_suffix(".session")
        existing_session = session_file.is_file()

        last_error: Optional[Exception] = None
        configured_candidates = [candidate for candidate in candidates if candidate]

        # A configured/cached proxy is cheap to try first.  If it is stale,
        # discover current public MTProxy routes before falling back to a
        # direct connection which is commonly blocked on the host network.
        for candidate in configured_candidates:
            last_error = await self._try_start_candidate(candidate, existing_session)
            if self._client is not None:
                break
            if last_error is not None and not self._is_connection_failure(last_error):
                break

        should_discover = (
            self._client is None
            and (last_error is None or self._is_connection_failure(last_error))
            and bool(configured_candidates)
        )
        if should_discover:
            last_error = await self._discover_and_start(
                existing_session,
                exclude=set(configured_candidates),
                previous_error=last_error,
            )

        # Preserve direct-only setups and keep direct as the last-resort path
        # after a configured route and public discovery have failed.
        if (
            self._client is None
            and (last_error is None or self._is_connection_failure(last_error))
        ):
            last_error = await self._try_start_candidate(None, existing_session)

        # With no proxy configured, direct is attempted first.  Only perform
        # network discovery if that connection actually failed.
        if (
            self._client is None
            and not configured_candidates
            and last_error is not None
            and self._is_connection_failure(last_error)
        ):
            last_error = await self._discover_and_start(
                existing_session,
                previous_error=last_error,
            )

        if last_error is not None or self._client is None:
            main_logger.error(
                f"[Telegram Telethon] Critical error on startup: {last_error}"
            )
            raise last_error or RuntimeError("Telethon failed to start")

        me = await self._client.get_me()
        name = me.username or me.first_name or "Unknown"
        await self.update_profile_state()
        main_logger.info(f"[Telegram Telethon] Successful authorization as: @{name}")
        self.state.is_online = True

    async def _discover_and_start(
        self,
        existing_session: bool,
        *,
        exclude: set[str] | None = None,
        previous_error: Exception | None = None,
    ) -> Exception | None:
        """Discover and fully verify up to three routes against this session."""

        candidates = await self._proxy_manager.discover_candidates()
        excluded = set(exclude or ())
        remaining = [value for value in candidates if value not in excluded]
        last_error = previous_error
        for _ in range(3):
            winner = await self._proxy_manager.select_first_working(
                remaining, self._probe_proxy
            )
            if not winner:
                break
            remaining = [value for value in remaining if value != winner]
            main_logger.info(
                "[Telegram Telethon] Checker selected working route: "
                f"{self._proxy_label(winner)}"
            )
            last_error = await self._try_start_candidate(winner, existing_session)
            if self._client is not None:
                return None
            if last_error is not None and not self._is_connection_failure(last_error):
                break
        return last_error

    async def _try_start_candidate(
        self, candidate: str | None, existing_session: bool
    ) -> Exception | None:
        """Start the persistent session through one route, cleaning up on failure."""

        try:
            proxy, connection_cls = self._parse_proxy(candidate)
            kwargs: dict[str, Any] = {
                "proxy": proxy,
                "connection_retries": 1,
                "retry_delay": 1,
                "timeout": 10,
            }
            if connection_cls:
                kwargs["connection"] = connection_cls
            self._client = TelegramClient(
                self.session_path, self.api_id, self.api_hash, **kwargs
            )
            if existing_session:
                session = self._client.session
                dc_id = getattr(session, "dc_id", None)
                server_address = getattr(session, "server_address", None)
                port = getattr(session, "port", None)
                if dc_id and server_address and port:
                    self._probe_dc = (int(dc_id), str(server_address), int(port))
            main_logger.info(
                "[Telegram Telethon] Trying route: "
                f"{self._proxy_label(candidate)}"
            )
            if existing_session:
                await asyncio.wait_for(self._client.start(), timeout=30.0)
            else:
                # First authorization is interactive and must not be
                # cancelled while the operator enters phone/code/2FA.
                await self._client.start()
            if candidate:
                self._proxy_manager.cache_proxy(candidate)
            return None
        except asyncio.TimeoutError:
            error = TimeoutError(
                f"Connection timed out for {self._proxy_label(candidate)}"
            )
        except Exception as exc:
            error = exc

        main_logger.warning(
            "[Telegram Telethon] Route "
            f"{self._proxy_label(candidate)} failed: "
            f"{type(error).__name__}: {error}"
        )
        await self._disconnect_failed_client()
        self._proxy_manager.mark_failed(candidate)
        self._client = None
        return error

    async def _probe_proxy(self, candidate: str) -> bool:
        """Perform an isolated MTProto handshake without touching the session."""

        proxy, connection_cls = self._parse_proxy(candidate, log=False)
        if proxy is None or connection_cls is None:
            return False
        probe_logger = logging.getLogger("JAWL.TelethonProbe")
        probe_logger.setLevel(logging.CRITICAL)
        probe_logger.propagate = False
        probe_session = MemorySession()
        if self._probe_dc is not None:
            probe_session.set_dc(*self._probe_dc)
        probe = TelegramClient(
            probe_session,
            self.api_id,
            self.api_hash,
            proxy=proxy,
            connection=connection_cls,
            connection_retries=1,
            request_retries=1,
            retry_delay=0,
            timeout=4,
            auto_reconnect=False,
            base_logger=probe_logger,
        )
        try:
            await asyncio.wait_for(probe.connect(), timeout=5.0)
            return bool(probe.is_connected())
        except (asyncio.TimeoutError, OSError):
            return False
        finally:
            try:
                await probe.disconnect()
            except Exception:
                pass


    async def _disconnect_failed_client(self) -> None:
        client = self._client
        if client is None:
            return
        try:
            await client.disconnect()
        except Exception:
            pass

    @staticmethod
    def _is_connection_failure(error: Exception) -> bool:
        if isinstance(error, (asyncio.TimeoutError, TimeoutError, OSError)):
            return True
        return any(
            marker in str(error).lower()
            for marker in (
                "connect",
                "disconnected",
                "timed out",
                "timeout",
                "network",
                "socket",
            )
        )

    @staticmethod
    def _proxy_label(proxy_url: str | None) -> str:
        if not proxy_url:
            return "direct"
        if proxy_url.startswith("tg://proxy"):
            parsed = urlparse(proxy_url)
            params = parse_qs(parsed.query)
            host = params.get("server", ["unknown"])[0]
            port = params.get("port", ["?"])[0]
            return f"{host}:{port} (MTProxy)"
        parsed = urlparse(proxy_url)
        try:
            port = parsed.port or "?"
        except ValueError:
            port = "?"
        return f"{parsed.hostname or 'proxy'}:{port} ({parsed.scheme})"

    @staticmethod
    def _parse_proxy(
        proxy_url: str | None,
        *,
        log: bool = True,
    ) -> tuple[Any, Any]:
        if not proxy_url:
            return None, None
        if proxy_url.startswith("tg://proxy"):
            parsed = urlparse(proxy_url)
            params = parse_qs(parsed.query)
            host = params.get("server", [None])[0]
            port_str = params.get("port", [None])[0]
            secret = params.get("secret", [None])[0]
            if host and port_str and secret:
                try:
                    port = int(port_str)
                    if log:
                        main_logger.info(
                            f"[Telegram Telethon] Using MTProxy: {host}:{port}"
                        )
                    return (
                        (host, port, secret),
                        ConnectionTcpMTProxyRandomizedIntermediate,
                    )
                except (ValueError, TypeError):
                    pass
            if log:
                main_logger.warning(
                    "[Telegram Telethon] Invalid MTProxy URL, connecting directly."
                )
            return None, None
        try:
            proxy = parse_proxy_url(proxy_url)
            if log:
                main_logger.info("[Telegram Telethon] Using SOCKS/HTTP proxy.")
            return proxy, None
        except Exception:
            if log:
                main_logger.warning(
                    "[Telegram Telethon] Could not parse proxy URL, connecting directly."
                )
            return None, None

    async def stop(self) -> None:
        """Correctly disconnects from Telegram servers."""
        if self._client and self._client.is_connected():
            await self._client.disconnect()
            main_logger.info("[Telegram Telethon] Client disconnected.")
            self.state.is_online = False

    async def update_profile_state(self) -> None:
        """
        Performs GetFullUserRequest to update the agent's profile data
        in L0 State (name, username, bio, personal channel).
        """
        if not self._client:
            return

        try:
            me = await self._client.get_me()
            name = me.first_name or "Unknown"
            if getattr(me, "last_name", None):
                name += f" {me.last_name}"

            username = f"@{me.username}" if me.username else "No @username"

            # FullUser request is required to get "about" (bio) and personal channel
            full_me = await self._client(GetFullUserRequest(me))
            bio = full_me.full_user.about or "Empty"

            # Search for personal channel
            channel_info = ""
            personal_channel_id = getattr(full_me.full_user, "personal_channel_id", None)

            if personal_channel_id:
                try:
                    from telethon import utils

                    channel = await self._client.get_entity(personal_channel_id)
                    channel_name = utils.get_display_name(channel)
                    channel_username = getattr(channel, "username", None)
                    un_str = f" (@{channel_username})" if channel_username else ""

                    channel_info = f"\nPersonal Channel: {channel_name}{un_str} (ID: {personal_channel_id})"
                except Exception:
                    channel_info = f"\nPersonal Channel: ID {personal_channel_id}"

            self.state.account_info = (
                f"Profile: {name} ({username}) | Bio: {bio}{channel_info}\n---"
            )
        except Exception as e:
            main_logger.error(f"[Telegram Telethon] Error updating profile: {e}")
            self.state.account_info = "Profile: Data load error\n---"

    async def get_context_block(self, **kwargs: Any) -> str:
        """
        Context provider for ContextRegistry.
        """

        desc = "Description: Telegram User API. Connects to personal Telegram account."
        if not self.state.is_online:
            return f"### TELETHON [OFF]\n{desc}\nThe interface is disabled."

        return f"### TELETHON [ON] \n{desc}\nAccount info: {self.state.account_info}\nIMPORTANT: It is recommended to reply to users who have UNREAD. \n\n{self.state.last_chats}"
