"""Regression tests for the ``group_admin`` DataLoader (Fix 2 / N+1 kill).

We mount a fake ``httpx.MockTransport`` on the group client, count calls per
path, and assert the loader collapses duplicate employer ids into a single
HTTP call per (employer, resource) pair.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from typing import Any

import httpx
import pytest

from app import clients as clients_mod
from app.graphql_extensions import build_loaders
from app.schema import schema


def _fake_transport(payers: list[dict[str, Any]], employers: list[dict[str, Any]]):
    calls: Counter[str] = Counter()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls[path] += 1
        if path == "/payers":
            return httpx.Response(200, json=payers)
        if path == "/employers":
            return httpx.Response(200, json=employers)
        if path.endswith("/subgroups"):
            emp_id = path.split("/")[2]
            return httpx.Response(
                200,
                json=[{"id": f"sg-{emp_id}", "employer_id": emp_id, "name": "Default"}],
            )
        if path.endswith("/plans"):
            emp_id = path.split("/")[2]
            return httpx.Response(200, json={"employer_id": emp_id, "plan_ids": [f"p-{emp_id}"]})
        return httpx.Response(404)

    return httpx.MockTransport(handler), calls


@pytest.fixture
def patched_group_client(monkeypatch):
    """Swap the module-level ``group_client`` for one backed by MockTransport
    so we can count HTTP calls without hitting the network."""
    payers = [{"id": "pay-1", "name": "Acme Health"}]
    employers = [
        {"id": f"emp-{i}", "name": f"Employer {i}", "payer_id": "pay-1", "external_id": None}
        for i in range(10)
    ]
    transport, calls = _fake_transport(payers, employers)

    fake = clients_mod.BreakerClient.__new__(clients_mod.BreakerClient)
    fake.base_url = "http://group:8000"
    fake.name = "group"
    fake._client = httpx.AsyncClient(base_url="http://group:8000", transport=transport)

    class _NoBreaker:
        async def call(self, fn):
            return await fn()

    fake.breaker = _NoBreaker()

    monkeypatch.setattr(clients_mod, "group_client", fake)
    yield calls, employers
    asyncio.get_event_loop().run_until_complete(fake.aclose()) if False else None


def test_group_admin_hits_group_svc_at_most_twice_per_employer(patched_group_client):
    """Ten employers → exactly 1 /payers + 1 /employers + 10 /subgroups + 10 /plans.

    Critically, the loader must *not* re-hit a downstream for a duplicate
    employer id; we request 10 employers (all unique), but duplicates inside a
    single GraphQL operation would collapse to the cached load_many result.
    """
    calls, employers = patched_group_client

    query = """
    {
      groupAdmin {
        id
        name
        subgroups { id name }
        visiblePlanIds
      }
    }
    """

    loaders = build_loaders(clients_mod.group_client, concurrency=10)
    context = {"loaders": loaders}

    result = asyncio.run(schema.execute(query, context_value=context))

    assert result.errors is None, f"unexpected errors: {result.errors}"
    assert len(result.data["groupAdmin"]) == 10

    # Two non-employer calls, one of each sub-resource per unique employer.
    assert calls["/payers"] == 1
    assert calls["/employers"] == 1
    assert sum(1 for p in calls if p.endswith("/subgroups")) == 10
    assert sum(1 for p in calls if p.endswith("/plans")) == 10
    # No single employer resource path fired twice.
    for path, n in calls.items():
        if path.endswith("/subgroups") or path.endswith("/plans"):
            assert n == 1, f"{path} was called {n} times (N+1 regression)"


def test_group_admin_dedupes_duplicate_employer_ids(patched_group_client):
    """If the same employer id is ``load()``'d twice inside one operation the
    DataLoader must serve the second call from cache — i.e. one HTTP round
    trip per (employer, resource), not two."""
    calls, _ = patched_group_client

    loaders = build_loaders(clients_mod.group_client, concurrency=10)

    async def _run() -> None:
        # Issue the same key twice concurrently — DataLoader should batch them.
        a, b = await asyncio.gather(
            loaders.subgroups.load("emp-1"),
            loaders.subgroups.load("emp-1"),
        )
        assert a == b
        # And again sequentially — should hit the in-memory cache.
        c = await loaders.subgroups.load("emp-1")
        assert c == a

    asyncio.run(_run())
    assert calls["/employers/emp-1/subgroups"] == 1
