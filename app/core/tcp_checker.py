"""Lightweight liveness check — a bare TCP connect, no protocol handshake."""
from __future__ import annotations

import asyncio
from collections.abc import Iterable, Sequence

DEFAULT_TIMEOUT = 3.0
_MAX_CONCURRENCY = 100


async def tcp_alive(host: str, port: int, timeout: float = DEFAULT_TIMEOUT) -> bool:
    if not host or not (0 < port < 65536):
        return False
    try:
        fut = asyncio.open_connection(host, port)
        reader, writer = await asyncio.wait_for(fut, timeout=timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except (OSError, asyncio.TimeoutError):
            pass
        return True
    except (OSError, asyncio.TimeoutError, ValueError):
        return False


async def check_many(
    targets: Sequence[tuple[str, int]],
    timeout: float = DEFAULT_TIMEOUT,
    concurrency: int = _MAX_CONCURRENCY,
) -> list[bool]:
    """Validate many (host, port) targets concurrently, preserving order."""
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _guarded(host: str, port: int) -> bool:
        async with sem:
            return await tcp_alive(host, port, timeout)

    return await asyncio.gather(*(_guarded(h, p) for h, p in targets))


async def check_iter(
    items: Iterable,
    key,
    timeout: float = DEFAULT_TIMEOUT,
    concurrency: int = _MAX_CONCURRENCY,
) -> list[bool]:
    items = list(items)
    targets = [key(i) for i in items]
    return await check_many(targets, timeout=timeout, concurrency=concurrency)
