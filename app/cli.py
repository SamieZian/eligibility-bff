"""Operational CLI for the BFF container.

Usage (inside the bff container):
    python -m app.cli seed
    python -m app.cli replay --file-id <uuid>
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx

from app.settings import settings

# ───────────── Seed data ─────────────
PAYER_ICICI = "11111111-0000-0000-0000-000000000001"
PAYER_AETNA = "11111111-0000-0000-0000-000000000002"

EMPLOYER_SWIGGY = "22222222-0000-0000-0000-000000000001"
EMPLOYER_ZOMATO = "22222222-0000-0000-0000-000000000002"

PLANS = [
    {
        "id": "33333333-0000-0000-0000-000000000001",
        "plan_code": "PLAN-GOLD",
        "name": "Gold Health",
        "type": "HLT",
        "metal_level": "GOLD",
    },
    {
        "id": "33333333-0000-0000-0000-000000000002",
        "plan_code": "PLAN-SILVER",
        "name": "Silver Health",
        "type": "HLT",
        "metal_level": "SILVER",
    },
    {
        "id": "33333333-0000-0000-0000-000000000003",
        "plan_code": "PLAN-BRONZE",
        "name": "Bronze Health",
        "type": "HLT",
        "metal_level": "BRONZE",
    },
]


async def _post(
    client: httpx.AsyncClient,
    path: str,
    body: dict[str, Any],
    *,
    retry_on_5xx: bool = True,
) -> dict[str, Any]:
    """POST with retry. On persistent 5xx for non-idempotent paths, returns {}.

    Seed is tolerant — it's fine to run twice.
    """
    for attempt in range(5):
        try:
            r = await client.post(path, json=body, timeout=10.0)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            if attempt == 4 or not retry_on_5xx:
                if e.response.status_code >= 500 and path.endswith("/subgroups"):
                    # Subgroup create isn't idempotent and may 500 on repeat seed; tolerate.
                    return {}
                raise
            await asyncio.sleep(0.5 * (2**attempt))
        except httpx.HTTPError:
            if attempt == 4:
                raise
            await asyncio.sleep(0.5 * (2**attempt))
    raise RuntimeError("unreachable")


async def seed() -> None:
    async with (
        httpx.AsyncClient(base_url=settings.group_url) as group,
        httpx.AsyncClient(base_url=settings.plan_url) as plan,
    ):
        # Payers — API generates IDs; we capture them.
        icici = await _post(group, "/payers", {"name": "ICICI"})
        aetna = await _post(group, "/payers", {"name": "Aetna"})
        # Employers — use returned payer IDs.
        swiggy = await _post(
            group,
            "/employers",
            {"payer_id": icici["id"], "name": "Swiggy", "external_id": "SWIGGY"},
        )
        zomato = await _post(
            group,
            "/employers",
            {"payer_id": icici["id"], "name": "Zomato", "external_id": "ZOMATO"},
        )
        # Subgroups
        for emp_id, sg in (
            (swiggy["id"], "SWIGGY_GROUP_A"),
            (swiggy["id"], "SWIGGY_GROUP_B"),
            (zomato["id"], "ZOMATO_GROUP_A"),
            (zomato["id"], "ZOMATO_GROUP_B"),
        ):
            await _post(group, "/subgroups", {"employer_id": emp_id, "name": sg})
        # Plans — API may generate its own IDs; capture them.
        plan_ids: list[str] = []
        for p in PLANS:
            created = await _post(
                plan,
                "/plans",
                {
                    "plan_code": p["plan_code"],
                    "name": p["name"],
                    "type": p["type"],
                    "metal_level": p["metal_level"],
                },
            )
            plan_ids.append(created["id"])
        # Plan visibility — every plan visible to both employers
        for emp_id in (swiggy["id"], zomato["id"]):
            for pid in plan_ids:
                await _post(group, "/visibility", {"employer_id": emp_id, "plan_id": pid})
        print(f"seed complete: payers=[{icici['id']}, {aetna['id']}], employers=[swiggy={swiggy['id']}, zomato={zomato['id']}], plans={len(plan_ids)}")


async def replay(file_id: str) -> None:
    from eligibility_common.events import Topics
    from eligibility_common.pubsub import publish

    publish(
        Topics.FILE_RECEIVED,
        {
            "event_id": str(uuid.uuid4()),
            "event_type": "FileReceived",
            "tenant_id": settings.tenant_default,
            "emitted_at": datetime.now(timezone.utc).isoformat(),
            "file_id": file_id,
            "format": "X12_834",
            "object_key": f"{settings.tenant_default}/{file_id}.x12",
        },
    )
    print(f"replay published for {file_id}")


def main() -> None:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("seed")
    rp = sub.add_parser("replay")
    rp.add_argument("--file-id", required=True)
    ns = p.parse_args()
    if ns.cmd == "seed":
        asyncio.run(seed())
    elif ns.cmd == "replay":
        asyncio.run(replay(ns.file_id))
    else:
        sys.exit(2)


if __name__ == "__main__":
    main()
