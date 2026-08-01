import json
import asyncio
from pathlib import Path

import pytest

from src.l2_interfaces.telegram.telethon.proxy_manager import MTProxyManager


PROXY = (
    "tg://proxy?server=proxy.example&port=443&secret="
    "ee00112233445566778899aabbccddeeff"
)


def test_fallback_chain_is_configurable_deduplicated_and_direct_last(
    tmp_path: Path,
) -> None:
    manager = MTProxyManager(tmp_path, fallback_proxies=[PROXY, PROXY, "invalid"])

    assert manager.fallback_chain(PROXY) == [PROXY, None]
    assert MTProxyManager(tmp_path).fallback_chain(None) == [None]


def test_failed_cached_proxy_is_invalidated(tmp_path: Path) -> None:
    manager = MTProxyManager(tmp_path)
    manager.cache_proxy(PROXY)
    assert manager.load_cached() == PROXY

    manager.mark_failed(PROXY)

    assert manager.load_cached() is None
    assert not (tmp_path / "telethon_proxy_cache.json").exists()


def test_invalid_or_expired_cache_is_ignored(tmp_path: Path) -> None:
    cache = tmp_path / "telethon_proxy_cache.json"
    cache.write_text(
        json.dumps({"proxy": "not-a-proxy", "cached_at": 0}),
        encoding="utf-8",
    )

    assert MTProxyManager(tmp_path).load_cached() is None


def test_invalid_proxy_ports_are_rejected() -> None:
    assert not MTProxyManager.is_supported_proxy(
        "tg://proxy?server=proxy.example&port=99999&secret=abc"
    )
    assert not MTProxyManager.is_supported_proxy("http://proxy.example:99999")


def test_invalid_configured_proxy_is_not_added_to_fallback_chain() -> None:
    manager = MTProxyManager()

    preferred = manager.resolve_proxy("not-a-proxy")

    assert preferred is None
    assert manager.fallback_chain(preferred) == [None]


def test_parse_public_channel_html_handles_entities_and_deduplicates() -> None:
    first = (
        "tg://proxy?server=one.example&amp;port=443&amp;secret="
        "ee00112233445566778899aabbccddeeff"
    )
    second = (
        "https://t.me/proxy?server=two.example&amp;port=8443&amp;secret="
        "ddffeeddccbbaa99887766554433221100"
    )
    content = (
        f'<a href="{first}">one</a>'
        f'<a href="{first}">dup</a>'
        f'<a href="{second}">two</a>'
    )

    assert MTProxyManager.parse_public_channel_html(content) == [
        PROXY.replace("proxy.example", "one.example"),
        (
            "tg://proxy?server=two.example&port=8443&secret="
            "ddffeeddccbbaa99887766554433221100"
        ),
    ]


@pytest.mark.asyncio
async def test_select_first_working_returns_fastest_success_and_cancels_rest() -> None:
    manager = MTProxyManager()
    cancelled = asyncio.Event()

    async def probe(candidate: str) -> bool:
        if candidate == "fast":
            await asyncio.sleep(0)
            return True
        try:
            await asyncio.sleep(5)
            return False
        except asyncio.CancelledError:
            cancelled.set()
            raise

    winner = await manager.select_first_working(
        ["slow", "fast", "slower"], probe, concurrency=3
    )

    assert winner == "fast"
    assert cancelled.is_set()
