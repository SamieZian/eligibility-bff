"""Async HTTP clients to downstream domain services, each wrapped in a breaker.

Two-second timeout matches the BFF SLA — any downstream taking longer than that
is considered a soft-fail; the breaker opens after repeated timeouts so one
slow downstream cannot drag the whole BFF down.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from eligibility_common.circuit import CircuitBreaker

from app.settings import settings


class BreakerClient:
    """Thin wrapper — httpx.AsyncClient but every call goes through the breaker."""

    def __init__(self, base_url: str, name: str, timeout: float = 2.0) -> None:
        self.base_url = base_url
        self.name = name
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout)
        self.breaker = CircuitBreaker(name=name)

    async def _call(self, fn: Callable[[], Awaitable[httpx.Response]]) -> httpx.Response:
        return await self.breaker.call(fn)

    async def get(self, path: str, **kw: Any) -> httpx.Response:
        return await self._call(lambda: self._client.get(path, **kw))

    async def post(self, path: str, **kw: Any) -> httpx.Response:
        return await self._call(lambda: self._client.post(path, **kw))

    async def put(self, path: str, **kw: Any) -> httpx.Response:
        return await self._call(lambda: self._client.put(path, **kw))

    async def delete(self, path: str, **kw: Any) -> httpx.Response:
        return await self._call(lambda: self._client.delete(path, **kw))

    async def aclose(self) -> None:
        await self._client.aclose()


atlas_client = BreakerClient(settings.atlas_url, name="atlas")
member_client = BreakerClient(settings.member_url, name="member")
group_client = BreakerClient(settings.group_url, name="group")
plan_client = BreakerClient(settings.plan_url, name="plan")


async def close_all() -> None:
    for c in (atlas_client, member_client, group_client, plan_client):
        try:
            await c.aclose()
        except Exception:
            pass
