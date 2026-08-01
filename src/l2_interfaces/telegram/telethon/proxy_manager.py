"""MTProxy discovery, fallback, and caching for Telethon instances."""

from __future__ import annotations

import asyncio
import html
import json
import re
import tempfile
import time
from pathlib import Path
from collections.abc import Awaitable, Callable
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import Request, urlopen

from src.utils.logger import main_logger


class MTProxyManager:
    """Manages MTProxy discovery with fallback chain and cache."""

    CACHE_TTL_SEC = 86400
    DEFAULT_CHANNEL_URL = "https://t.me/s/mtp4tg"
    MAX_DISCOVERED = 80
    CHANNEL_PAGES = 8

    def __init__(
        self,
        cache_dir: str | Path | None = None,
        fallback_proxies: list[str] | None = None,
        channel_url: str = DEFAULT_CHANNEL_URL,
    ) -> None:
        self.cache_path = (
            Path(cache_dir) / "telethon_proxy_cache.json"
            if cache_dir
            else None
        )
        self.fallback_proxies = [
            value.strip()
            for value in (fallback_proxies or [])
            if self.is_supported_proxy(value.strip())
        ]
        self.channel_url = channel_url.strip() or self.DEFAULT_CHANNEL_URL
        self._working_proxy: str | None = None

    @staticmethod
    def is_supported_proxy(proxy_url: str) -> bool:
        if proxy_url.startswith("tg://proxy"):
            return MTProxyManager.parse_tg_proxy(proxy_url) is not None
        if not proxy_url.startswith(
            ("socks4://", "socks5://", "http://", "https://")
        ):
            return False
        try:
            parsed = urlparse(proxy_url)
            return bool(parsed.hostname and parsed.port)
        except ValueError:
            return False

    def load_cached(self) -> str | None:
        if not self.cache_path or not self.cache_path.is_file():
            return None
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            proxy = data.get("proxy")
            if (
                isinstance(proxy, str)
                and self.is_supported_proxy(proxy)
                and time.time() - float(data.get("cached_at", 0)) < self.CACHE_TTL_SEC
            ):
                return proxy
        except (OSError, TypeError, ValueError):
            pass
        return None

    def cache_proxy(self, proxy_url: str) -> None:
        if not self.cache_path:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(
            prefix=f".{self.cache_path.name}.",
            suffix=".tmp",
            dir=self.cache_path.parent,
        )
        temporary = Path(name)
        try:
            with open(descriptor, "w", encoding="utf-8") as stream:
                json.dump({"proxy": proxy_url, "cached_at": time.time()}, stream)
            temporary.replace(self.cache_path)
        finally:
            temporary.unlink(missing_ok=True)
        self._working_proxy = proxy_url

    def resolve_proxy(self, configured: str | None) -> str | None:
        cached = self.load_cached()
        if cached:
            main_logger.info("[MTProxy] Using cached proxy route.")
            self._working_proxy = cached
            return cached
        if configured and self.is_supported_proxy(configured):
            return configured
        if configured:
            main_logger.warning(
                "[MTProxy] Ignoring an invalid configured proxy URL."
            )
        return None

    def fallback_chain(self, configured: str | None) -> list[str | None]:
        chain: list[str | None] = []
        for candidate in [configured, *self.fallback_proxies, None]:
            if candidate not in chain:
                chain.append(candidate)
        return chain

    def mark_failed(self, proxy_url: str | None) -> None:
        if proxy_url and proxy_url == self._working_proxy:
            self._working_proxy = None
        if not proxy_url or not self.cache_path or not self.cache_path.is_file():
            return
        try:
            cached = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if cached.get("proxy") == proxy_url:
                self.cache_path.unlink(missing_ok=True)
        except (OSError, ValueError):
            self.cache_path.unlink(missing_ok=True)

    @staticmethod
    def parse_public_channel_html(content: str) -> list[str]:
        """Extract unique MTProxy URLs from Telegram's public channel HTML."""

        found: list[str] = []
        seen: set[str] = set()
        for pattern in (
            r"tg://proxy\?[^\s\"'<>]+",
            r"https?://t\.me/proxy\?[^\s\"'<>]+",
        ):
            for match in re.finditer(pattern, content):
                value = html.unescape(unquote(match.group(0))).rstrip("),.;")
                if value.startswith(("https://t.me/proxy?", "http://t.me/proxy?")):
                    value = "tg://proxy?" + value.split("?", 1)[1]
                if (
                    value not in seen
                    and MTProxyManager.parse_tg_proxy(value) is not None
                ):
                    seen.add(value)
                    found.append(value)
        return found

    def _fetch_public_channel(self, timeout_sec: float) -> str:
        """Fetch recent public history pages, not just the newest 20 posts."""

        pages: list[str] = []
        seen_urls: set[str] = set()
        base_url = self.channel_url.split("?", 1)[0]
        url = self.channel_url
        deadline = time.monotonic() + timeout_sec
        for _ in range(self.CHANNEL_PAGES):
            if url in seen_urls:
                break
            seen_urls.add(url)
            request = Request(
                url,
                headers={"User-Agent": "Mozilla/5.0 (JAWL MTProxy checker)"},
            )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                page_timeout = max(1.0, min(5.0, remaining))
                with urlopen(request, timeout=page_timeout) as response:
                    content = response.read(2_000_000).decode(
                        "utf-8", errors="ignore"
                    )
            except Exception:
                if pages:
                    break
                raise
            pages.append(content)
            if (
                len(self.parse_public_channel_html("\n".join(pages)))
                >= self.MAX_DISCOVERED
            ):
                break
            before_values = [
                int(value)
                for value in re.findall(r"/s/[^\"?]+\?before=(\d+)", content)
            ]
            if not before_values:
                break
            url = f"{base_url}?before={min(before_values)}"
        return "\n".join(pages)

    async def discover_candidates(self, timeout_sec: float = 20.0) -> list[str]:
        """Fetch current candidates without requiring an MTProto connection."""

        try:
            content = await asyncio.wait_for(
                asyncio.to_thread(self._fetch_public_channel, timeout_sec),
                timeout=timeout_sec + 2,
            )
        except Exception as exc:
            main_logger.warning(
                "[MTProxy] Public channel discovery failed: "
                f"{type(exc).__name__}: {exc}"
            )
            return []
        candidates = self.parse_public_channel_html(content)[: self.MAX_DISCOVERED]
        main_logger.info(
            f"[MTProxy] Discovered {len(candidates)} candidate routes "
            "from the public channel."
        )
        return candidates

    async def select_first_working(
        self,
        candidates: list[str],
        probe: Callable[[str], Awaitable[bool]],
        concurrency: int = 20,
    ) -> str | None:
        """Race bounded MTProto probes and cancel the remainder after a winner."""

        semaphore = asyncio.Semaphore(max(1, min(concurrency, 40)))

        async def check(candidate: str) -> str | None:
            async with semaphore:
                try:
                    return candidate if await probe(candidate) else None
                except asyncio.CancelledError:
                    raise
                except Exception:
                    return None

        tasks = [asyncio.create_task(check(value)) for value in candidates]
        try:
            for completed in asyncio.as_completed(tasks):
                winner = await completed
                if winner:
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    return winner
            return None
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    def parse_tg_proxy(url: str) -> tuple[tuple[str, int, str], str] | None:
        if not url.startswith("tg://proxy"):
            return None
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        host = params.get("server", [None])[0]
        port_str = params.get("port", [None])[0]
        secret = params.get("secret", [None])[0]
        if host and port_str and secret:
            try:
                port = int(port_str)
                if not 1 <= port <= 65535 or any(
                    char.isspace() for char in secret
                ):
                    return None
                return (host, port, secret), "mtproxy"
            except (ValueError, TypeError):
                pass
        return None
