"""Unit tests for the enrollment subscription bridge.

Uses fakeredis to stand in for real Redis — publish + pubsub semantics are
preserved so we can verify that the member_id filter works end-to-end."""
from __future__ import annotations

import asyncio
import json

import pytest


@pytest.fixture
def fake_redis(monkeypatch):
    """Swap the module-level Redis client for an in-memory fakeredis one.

    Reset the shared connection pool so subscribers and the publisher both
    talk to the SAME fakeredis backend."""
    import fakeredis.aioredis as fakeredis_async

    from app import pubsub_bridge

    fake = fakeredis_async.FakeRedis()
    monkeypatch.setattr(pubsub_bridge, "_client", lambda: fake)
    monkeypatch.setattr(pubsub_bridge, "_pool", None)
    yield fake


async def test_subscription_filters_by_member_id(fake_redis):
    """Publish one matching + one non-matching event; assert only the
    matching one is yielded to the subscriber."""
    from app.pubsub_bridge import CHANNEL_ENROLLMENT, subscribe_enrollment_updates

    target_member = "11111111-1111-1111-1111-111111111111"
    other_member = "22222222-2222-2222-2222-222222222222"

    received: list[dict] = []

    async def consume():
        async for evt in subscribe_enrollment_updates(target_member):
            received.append(evt)
            if len(received) == 1:
                break

    task = asyncio.create_task(consume())

    # Give the subscriber a beat to register its subscription.
    for _ in range(20):
        await asyncio.sleep(0.01)
        # One subscriber on the channel is enough to signal readiness.
        num_subs = await fake_redis.pubsub_numsub(CHANNEL_ENROLLMENT)
        if num_subs and num_subs[0][1] >= 1:
            break

    # Non-matching member — should be filtered out.
    await fake_redis.publish(
        CHANNEL_ENROLLMENT,
        json.dumps({
            "member_id": other_member,
            "event_type": "EnrollmentAdded",
            "occurred_at": "2026-04-14T00:00:00",
        }),
    )
    # Matching member — should arrive.
    await fake_redis.publish(
        CHANNEL_ENROLLMENT,
        json.dumps({
            "member_id": target_member,
            "event_type": "MemberUpserted",
            "occurred_at": "2026-04-14T00:01:00",
        }),
    )

    await asyncio.wait_for(task, timeout=2.0)

    assert len(received) == 1
    assert received[0]["member_id"] == target_member
    assert received[0]["event_type"] == "MemberUpserted"


async def test_subscription_wildcard_receives_all(fake_redis):
    """Subscribing with member_id='*' is the admin sentinel — every event
    flows through regardless of member."""
    from app.pubsub_bridge import CHANNEL_ENROLLMENT, subscribe_enrollment_updates

    received: list[dict] = []

    async def consume():
        async for evt in subscribe_enrollment_updates("*"):
            received.append(evt)
            if len(received) == 2:
                break

    task = asyncio.create_task(consume())

    for _ in range(20):
        await asyncio.sleep(0.01)
        num_subs = await fake_redis.pubsub_numsub(CHANNEL_ENROLLMENT)
        if num_subs and num_subs[0][1] >= 1:
            break

    await fake_redis.publish(
        CHANNEL_ENROLLMENT,
        json.dumps({"member_id": "a", "event_type": "EnrollmentAdded", "occurred_at": "2026-04-14T00:00:00"}),
    )
    await fake_redis.publish(
        CHANNEL_ENROLLMENT,
        json.dumps({"member_id": "b", "event_type": "EnrollmentTerminated", "occurred_at": "2026-04-14T00:00:00"}),
    )

    await asyncio.wait_for(task, timeout=2.0)
    assert {e["member_id"] for e in received} == {"a", "b"}
