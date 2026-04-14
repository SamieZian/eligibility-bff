"""Bridge Redis Pub/Sub channels into async generators for Strawberry
subscriptions. Lightweight; each subscriber gets its own pubsub client."""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import redis.asyncio as redis

from app.settings import settings
from eligibility_common.logging import get_logger

log = get_logger(__name__)
CHANNEL_ENROLLMENT = "enrollment_updates"

_pool: redis.ConnectionPool | None = None


def _client() -> redis.Redis:
    """Return a Redis client backed by a module-level connection pool.

    We keep one pool per process; pubsub objects spawned off the client each
    get their own dedicated connection underneath."""
    global _pool
    if _pool is None:
        _pool = redis.ConnectionPool.from_url(settings.redis_url)
    return redis.Redis(connection_pool=_pool)


async def publish_enrollment_update(member_id: str, payload: dict[str, Any]) -> None:
    """Best-effort publish to the enrollment channel. Never raises — Redis is
    a live-refresh hint only, never authoritative."""
    try:
        await _client().publish(
            CHANNEL_ENROLLMENT,
            json.dumps({"member_id": member_id, **payload}),
        )
    except Exception as e:  # noqa: BLE001
        log.warning("pubsub_bridge.publish_failed", error=str(e))


async def subscribe_enrollment_updates(member_id: str) -> AsyncIterator[dict[str, Any]]:
    """Yield events matching `member_id` (or all, if member_id is the sentinel '*').

    Cancellation-safe: closes the underlying subscription on exit."""
    pubsub = _client().pubsub()
    await pubsub.subscribe(CHANNEL_ENROLLMENT)
    try:
        async for msg in pubsub.listen():
            if msg.get("type") != "message":
                continue
            raw = msg.get("data")
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            if member_id != "*" and data.get("member_id") != member_id:
                continue
            yield data
    finally:
        try:
            await pubsub.unsubscribe(CHANNEL_ENROLLMENT)
            await pubsub.aclose()
        except Exception:  # noqa: BLE001
            pass
