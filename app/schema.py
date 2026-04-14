"""Strawberry GraphQL schema for the BFF.

Queries are read-only projections (mostly served from eligibility_view);
mutations fan out to atlas / member for writes and return the IDs of whatever
was created or mutated. The BFF never writes enrollments directly — that's
atlas's job — but it owns the GraphQL contract the frontend speaks.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import date, datetime
from typing import Any

import asyncio
import strawberry
from eligibility_common.logging import get_logger
from strawberry.types import Info

from app import clients, search
from app.graphql_extensions import (
    ErrorEnvelopeExtension,
    GroupAdminLoaders,
    depth_limit_extension,
)
from app.pubsub_bridge import subscribe_enrollment_updates
from app.settings import settings

log = get_logger(__name__)


# ─────────────────────────── Types ────────────────────────────────────────────


@strawberry.type
class Enrollment:
    enrollment_id: strawberry.ID
    tenant_id: strawberry.ID
    employer_id: strawberry.ID
    employer_name: str | None
    subgroup_name: str | None
    plan_id: strawberry.ID
    plan_name: str | None
    plan_code: str | None
    member_id: strawberry.ID
    member_name: str
    first_name: str
    last_name: str
    dob: date | None
    gender: str | None
    card_number: str | None
    ssn_last4: str | None
    relationship: str
    status: str
    effective_date: date
    termination_date: date


@strawberry.type
class TimelineSegment:
    id: strawberry.ID
    plan_id: strawberry.ID
    plan_name: str | None
    status: str
    valid_from: date
    valid_to: date
    txn_from: datetime
    txn_to: datetime
    is_in_force: bool
    source_file_id: strawberry.ID | None
    source_segment_ref: str | None


@strawberry.type
class FileJob:
    id: strawberry.ID
    file_id: strawberry.ID
    object_key: str
    format: str
    status: str
    uploaded_at: datetime
    total_rows: int | None
    success_rows: int | None
    failed_rows: int | None


@strawberry.type
class EmployerSummary:
    id: strawberry.ID
    name: str
    external_id: str | None
    payer_id: strawberry.ID | None


@strawberry.type
class PlanSummary:
    id: strawberry.ID
    plan_code: str
    name: str
    type: str
    metal_level: str | None


@strawberry.type
class Payer:
    id: strawberry.ID
    name: str


@strawberry.type
class Subgroup:
    id: strawberry.ID
    employer_id: strawberry.ID
    name: str


@strawberry.type
class GroupAdminView:
    """One row in the Groups admin page — employer + nested subgroups + visible plans."""

    id: strawberry.ID
    name: str
    external_id: str | None
    payer_id: strawberry.ID | None
    payer_name: str | None
    subgroups: list[Subgroup]
    visible_plan_ids: list[strawberry.ID]


@strawberry.type
class SearchResult:
    items: list[Enrollment]
    total: int
    next_cursor: str | None


# ─────────────────────────── Inputs ───────────────────────────────────────────


@strawberry.input
class SearchFilter:
    q: str | None = None
    card_number: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    ssn_last4: str | None = None
    employer_id: strawberry.ID | None = None
    employer_name: str | None = None
    subgroup_name: str | None = None
    plan_name: str | None = None
    plan_code: str | None = None
    dob: date | None = None
    effective_date_from: date | None = None
    effective_date_to: date | None = None
    termination_date_from: date | None = None
    termination_date_to: date | None = None
    member_type: str | None = None
    status: str | None = None


@strawberry.input
class Page:
    limit: int = 25
    cursor: str | None = None
    sort: str = "effective_date_desc"


# ─────────────────────────── Helpers ──────────────────────────────────────────


def _row_to_enrollment(row: dict[str, Any]) -> Enrollment:
    return Enrollment(
        enrollment_id=strawberry.ID(str(row["enrollment_id"])),
        tenant_id=strawberry.ID(str(row["tenant_id"])),
        employer_id=strawberry.ID(str(row["employer_id"])),
        employer_name=row.get("employer_name"),
        subgroup_name=row.get("subgroup_name"),
        plan_id=strawberry.ID(str(row["plan_id"])),
        plan_name=row.get("plan_name"),
        plan_code=row.get("plan_code"),
        member_id=strawberry.ID(str(row["member_id"])),
        member_name=row.get("member_name") or "",
        first_name=row.get("first_name") or "",
        last_name=row.get("last_name") or "",
        dob=row.get("dob"),
        gender=row.get("gender"),
        card_number=row.get("card_number"),
        ssn_last4=row.get("ssn_last4"),
        relationship=row.get("relationship") or "",
        status=row.get("status") or "",
        effective_date=row["effective_date"],
        termination_date=row["termination_date"],
    )


def _filters_from_input(f: SearchFilter | None) -> search._Filters:
    if f is None:
        return search._Filters()
    return search._Filters(
        q=f.q,
        card_number=f.card_number,
        first_name=f.first_name,
        last_name=f.last_name,
        ssn_last4=f.ssn_last4,
        employer_id=str(f.employer_id) if f.employer_id else None,
        employer_name=f.employer_name,
        subgroup_name=f.subgroup_name,
        plan_name=f.plan_name,
        plan_code=f.plan_code,
        dob=f.dob,
        effective_date_from=f.effective_date_from,
        effective_date_to=f.effective_date_to,
        termination_date_from=f.termination_date_from,
        termination_date_to=f.termination_date_to,
        member_type=f.member_type,
        status=f.status,
    )


# ─────────────────────────── Query root ───────────────────────────────────────


@strawberry.type
class Query:
    @strawberry.field
    async def search_enrollments(
        self,
        filter: SearchFilter | None = None,
        page: Page | None = None,
    ) -> SearchResult:
        p = page or Page()
        rows, total, next_cursor = await search.search(
            _filters_from_input(filter),
            limit=p.limit,
            cursor=p.cursor,
            sort=p.sort,
        )
        return SearchResult(
            items=[_row_to_enrollment(r) for r in rows],
            total=total,
            next_cursor=next_cursor,
        )

    @strawberry.field
    async def member_by_card(self, card_number: str) -> Enrollment | None:
        row = await search.find_by_card(card_number)
        return _row_to_enrollment(row) if row else None

    @strawberry.field
    async def enrollment_timeline(
        self,
        member_id: strawberry.ID,
        as_of: datetime | None = None,
    ) -> list[TimelineSegment]:
        tenant = settings.tenant_default
        segments = await search.timeline_for_member(str(member_id), tenant, as_of)
        out: list[TimelineSegment] = []
        for seg in segments:
            out.append(
                TimelineSegment(
                    id=strawberry.ID(str(seg["id"])),
                    plan_id=strawberry.ID(str(seg["plan_id"])),
                    plan_name=seg.get("plan_name"),
                    status=seg["status"],
                    valid_from=date.fromisoformat(seg["valid_from"]),
                    valid_to=date.fromisoformat(seg["valid_to"]),
                    txn_from=datetime.fromisoformat(seg["txn_from"]),
                    txn_to=datetime.fromisoformat(seg["txn_to"]),
                    is_in_force=bool(seg["is_in_force"]),
                    source_file_id=(
                        strawberry.ID(str(seg["source_file_id"]))
                        if seg.get("source_file_id")
                        else None
                    ),
                    source_segment_ref=seg.get("source_segment_ref"),
                )
            )
        return out

    @strawberry.field
    async def file_job(self, file_id: strawberry.ID) -> FileJob | None:
        from sqlalchemy import text

        from app.search import _engine

        sql = text(
            """
            SELECT id, file_id, object_key, format, status, uploaded_at,
                   total_rows, success_rows, failed_rows
            FROM file_ingestion_jobs WHERE file_id = :fid LIMIT 1
            """
        )
        try:
            async with _engine().connect() as conn:
                res = await conn.execute(sql, {"fid": str(file_id)})
                row = res.mappings().first()
        except Exception as e:
            log.warning("bff.file_job.error", error=str(e))
            return None
        if not row:
            return None
        return FileJob(
            id=strawberry.ID(str(row["id"])),
            file_id=strawberry.ID(str(row["file_id"])),
            object_key=row["object_key"],
            format=row["format"],
            status=row["status"],
            uploaded_at=row["uploaded_at"],
            total_rows=row["total_rows"],
            success_rows=row["success_rows"],
            failed_rows=row["failed_rows"],
        )

    @strawberry.field
    async def plans(self) -> list[PlanSummary]:
        """List all plans available in the catalog."""
        try:
            r = await clients.plan_client.get("/plans")
            r.raise_for_status()
            items = r.json()
        except Exception as e:
            log.warning("bff.plans.error", error=str(e))
            return []
        return [
            PlanSummary(
                id=strawberry.ID(str(it["id"])),
                plan_code=it.get("plan_code", ""),
                name=it.get("name", ""),
                type=it.get("type", ""),
                metal_level=it.get("metal_level"),
            )
            for it in items
        ]

    @strawberry.field
    async def payers(self) -> list[Payer]:
        try:
            r = await clients.group_client.get("/payers")
            r.raise_for_status()
            items = r.json()
        except Exception as e:
            log.warning("bff.payers.error", error=str(e))
            return []
        return [Payer(id=strawberry.ID(str(it["id"])), name=it["name"]) for it in items]

    @strawberry.field
    async def group_admin(self, info: Info) -> list[GroupAdminView]:
        """Aggregated view for the Groups admin page: each employer + its
        subgroups + visible plans, plus the parent payer's name.

        Uses per-request DataLoaders (see ``app.graphql_extensions``) so that
        duplicate employer ids collapse to a single HTTP call and the overall
        fan-out is capped by an ``asyncio.Semaphore`` inside the loader. The
        two top-level calls (``/payers``, ``/employers``) plus the loaders give
        us 2 + 2·N unique-employer HTTP calls instead of 2 + 2·N-with-dupes.
        """
        # Pull all in parallel
        try:
            payers_r, employers_r = await asyncio.gather(
                clients.group_client.get("/payers"),
                clients.group_client.get("/employers", params={"name": "%"}),
            )
            payers_r.raise_for_status()
            employers_r.raise_for_status()
            payer_map = {p["id"]: p["name"] for p in payers_r.json()}
            employers = employers_r.json()
        except Exception as e:
            log.warning("bff.group_admin.error", error=str(e))
            return []

        loaders: GroupAdminLoaders | None = (info.context or {}).get("loaders")  # type: ignore[assignment]
        if loaders is None:
            # Defensive: should never happen once the router wires context,
            # but keeps the resolver usable from tests / direct execute().
            from app.graphql_extensions import build_loaders

            loaders = build_loaders(clients.group_client)

        employer_ids = [str(emp["id"]) for emp in employers]
        subgroup_lists, plan_lists = await asyncio.gather(
            loaders.subgroups.load_many(employer_ids),
            loaders.visible_plans.load_many(employer_ids),
        )

        out: list[GroupAdminView] = []
        for emp, subgroups_raw, plan_ids in zip(employers, subgroup_lists, plan_lists):
            subgroups = [
                Subgroup(
                    id=strawberry.ID(str(sg["id"])),
                    employer_id=strawberry.ID(str(sg["employer_id"])),
                    name=sg["name"],
                )
                for sg in (subgroups_raw or [])
            ]
            visible = [strawberry.ID(str(pid)) for pid in (plan_ids or [])]
            out.append(
                GroupAdminView(
                    id=strawberry.ID(str(emp["id"])),
                    name=emp["name"],
                    external_id=emp.get("external_id"),
                    payer_id=strawberry.ID(str(emp["payer_id"])) if emp.get("payer_id") else None,
                    payer_name=payer_map.get(emp["payer_id"]),
                    subgroups=subgroups,
                    visible_plan_ids=visible,
                )
            )
        return out

    @strawberry.field
    async def employers(self, search: str | None = None) -> list[EmployerSummary]:
        # No search → return all employers (small list — payers × employers).
        params = {"name": search} if search else {"name": "%"}
        try:
            r = await clients.group_client.get("/employers", params=params)
            r.raise_for_status()
            items = r.json()
        except Exception as e:
            log.warning("bff.employers.error", error=str(e))
            return []
        return [
            EmployerSummary(
                id=strawberry.ID(str(it["id"])),
                name=it["name"],
                external_id=it.get("external_id"),
                payer_id=strawberry.ID(str(it["payer_id"])) if it.get("payer_id") else None,
            )
            for it in items
        ]


# ─────────────────────────── Mutation root ────────────────────────────────────


@strawberry.type
class AddMemberResult:
    member_id: strawberry.ID
    enrollment_id: strawberry.ID
    member_name: str


@strawberry.input
class AddMemberInput:
    first_name: str
    last_name: str
    dob: date
    gender: str | None = None
    card_number: str | None = None
    ssn_last4: str | None = None
    employer_id: strawberry.ID
    subgroup_name: str | None = None
    plan_id: strawberry.ID
    relationship: str = "subscriber"
    effective_date: date


@strawberry.type
class Mutation:
    @strawberry.mutation
    async def add_member(self, input: AddMemberInput) -> AddMemberResult:
        """Orchestrate: POST /members on member svc → POST /commands ADD on atlas.

        This is the BFF doing what saga-orchestration would do in prod — it
        keeps the frontend simple (one round-trip) and lets the BFF apply
        idempotency keys, retries, and error envelope consistently.
        """
        tenant = settings.tenant_default

        # Auto-generate a Member ID Card when the caller didn't supply one.
        # Pattern: payer-prefix `M` + 9-digit zero-padded random — matches
        # what most real payer systems do (insurer-assigned card on enrollment).
        import random as _random
        card_number = (input.card_number or "").strip() or f"M{_random.randint(0, 999_999_999):09d}"

        member_body = {
            "tenant_id": tenant,
            "employer_id": str(input.employer_id),
            "first_name": input.first_name.strip().upper(),
            "last_name": input.last_name.strip().upper(),
            "dob": input.dob.isoformat(),
            "gender": input.gender,
            "card_number": card_number,
            # member svc expects full SSN; we forward whatever the form gave us.
            # The svc encrypts + extracts last4 server-side.
            "ssn": input.ssn_last4,
        }
        member_body = {k: v for k, v in member_body.items() if v is not None}
        mr = await clients.member_client.post("/members", json=member_body)
        mr.raise_for_status()
        member = mr.json()

        cmd_body = {
            "command_type": "ADD",
            "tenant_id": tenant,
            "employer_id": str(input.employer_id),
            "subgroup_name": input.subgroup_name,
            "plan_id": str(input.plan_id),
            "member_id": member["id"],
            "relationship": input.relationship,
            "valid_from": input.effective_date.isoformat(),
        }
        ar = await clients.atlas_client.post("/commands", json=cmd_body)
        ar.raise_for_status()
        atlas_resp = ar.json()
        eids = atlas_resp.get("enrollment_ids", [])
        return AddMemberResult(
            member_id=strawberry.ID(str(member["id"])),
            enrollment_id=strawberry.ID(str(eids[0]) if eids else ""),
            member_name=f"{input.first_name.upper()} {input.last_name.upper()}",
        )

    @strawberry.mutation
    async def terminate_enrollment(
        self,
        member_id: strawberry.ID,
        plan_id: strawberry.ID,
        valid_to: date,
    ) -> list[strawberry.ID]:
        body = {
            "command_type": "TERMINATE",
            "tenant_id": settings.tenant_default,
            "member_id": str(member_id),
            "plan_id": str(plan_id),
            "valid_to": valid_to.isoformat(),
        }
        r = await clients.atlas_client.post("/commands", json=body)
        r.raise_for_status()
        data = r.json()
        return [strawberry.ID(str(eid)) for eid in data.get("enrollment_ids", [])]

    @strawberry.mutation
    async def update_member_demographics(
        self,
        member_id: strawberry.ID,
        first_name: str | None = None,
        last_name: str | None = None,
        dob: date | None = None,
        gender: str | None = None,
    ) -> bool:
        """Update member demographics via member svc POST /members (upsert by card).
        Emits MemberUpserted so the projector refreshes the denormalized view."""
        tenant_header = {"X-Tenant-Id": settings.tenant_default}
        try:
            r = await clients.member_client.get(
                f"/members/{member_id}", headers=tenant_header
            )
            r.raise_for_status()
            existing = r.json()
        except Exception as e:
            log.warning("bff.update_member.lookup_failed", error=str(e))
            return False
        body: dict[str, Any] = {
            "tenant_id": existing["tenant_id"],
            "employer_id": existing["employer_id"],
            "first_name": (first_name or existing["first_name"] or "").strip().upper(),
            "last_name": (last_name or existing["last_name"] or "").strip().upper(),
            "dob": (dob.isoformat() if dob else existing["dob"]),
            "gender": gender if gender is not None else existing.get("gender"),
            "card_number": existing.get("card_number"),
        }
        body = {k: v for k, v in body.items() if v is not None}
        try:
            ur = await clients.member_client.post("/members", json=body)
            ur.raise_for_status()
            return True
        except Exception as e:
            log.warning("bff.update_member.upsert_failed", error=str(e))
            return False

    @strawberry.mutation
    async def change_enrollment_plan(
        self,
        member_id: strawberry.ID,
        old_plan_id: strawberry.ID,
        new_plan_id: strawberry.ID,
        employer_id: strawberry.ID,
        new_valid_from: date,
        relationship: str = "subscriber",
    ) -> strawberry.ID:
        """Plan change = TERMINATE old (day before new effective) + ADD new.
        Bitemporal-correct: closes the in-force segment, opens a new one."""
        from datetime import timedelta as _td
        tenant = settings.tenant_default
        # 1. Close the old enrollment
        valid_to_old = new_valid_from - _td(days=1)
        try:
            tr = await clients.atlas_client.post("/commands", json={
                "command_type": "TERMINATE",
                "tenant_id": tenant,
                "member_id": str(member_id),
                "plan_id": str(old_plan_id),
                "valid_to": valid_to_old.isoformat(),
            })
            tr.raise_for_status()
        except Exception as e:
            log.warning("bff.change_plan.terminate_failed", error=str(e))
        # 2. Open new enrollment with new plan
        ar = await clients.atlas_client.post("/commands", json={
            "command_type": "ADD",
            "tenant_id": tenant,
            "employer_id": str(employer_id),
            "plan_id": str(new_plan_id),
            "member_id": str(member_id),
            "relationship": relationship,
            "valid_from": new_valid_from.isoformat(),
        })
        ar.raise_for_status()
        eids = ar.json().get("enrollment_ids", [])
        return strawberry.ID(str(eids[0]) if eids else "")

    @strawberry.mutation
    async def add_dependent(
        self,
        member_id: strawberry.ID,
        first_name: str,
        last_name: str,
        dob: date,
        relationship: str,
    ) -> strawberry.ID:
        body = {
            "relationship": relationship,
            "first_name": first_name,
            "last_name": last_name,
            "dob": dob.isoformat(),
        }
        r = await clients.member_client.post(f"/members/{member_id}/dependents", json=body)
        r.raise_for_status()
        return strawberry.ID(str(r.json()["id"]))

    # ─── Group admin (bonus task — payer / employer / subgroup CRUD) ───
    @strawberry.mutation
    async def create_payer(self, name: str) -> Payer:
        r = await clients.group_client.post("/payers", json={"name": name})
        r.raise_for_status()
        d = r.json()
        return Payer(id=strawberry.ID(str(d["id"])), name=d["name"])

    @strawberry.mutation
    async def create_employer(
        self, payer_id: strawberry.ID, name: str, external_id: str | None = None
    ) -> EmployerSummary:
        r = await clients.group_client.post(
            "/employers",
            json={"payer_id": str(payer_id), "name": name, "external_id": external_id},
        )
        r.raise_for_status()
        d = r.json()
        return EmployerSummary(
            id=strawberry.ID(str(d["id"])),
            name=d["name"],
            external_id=d.get("external_id"),
            payer_id=strawberry.ID(str(d["payer_id"])),
        )

    @strawberry.mutation
    async def delete_employer(self, employer_id: strawberry.ID) -> bool:
        r = await clients.group_client.delete(f"/employers/{employer_id}")
        return r.status_code in (200, 204)

    @strawberry.mutation
    async def create_subgroup(self, employer_id: strawberry.ID, name: str) -> Subgroup:
        r = await clients.group_client.post(
            "/subgroups", json={"employer_id": str(employer_id), "name": name}
        )
        r.raise_for_status()
        d = r.json()
        return Subgroup(
            id=strawberry.ID(str(d["id"])),
            employer_id=strawberry.ID(str(d["employer_id"])),
            name=d["name"],
        )

    @strawberry.mutation
    async def delete_subgroup(self, subgroup_id: strawberry.ID) -> bool:
        r = await clients.group_client.delete(f"/subgroups/{subgroup_id}")
        return r.status_code in (200, 204)

    @strawberry.mutation
    async def attach_plan(self, employer_id: strawberry.ID, plan_id: strawberry.ID) -> bool:
        r = await clients.group_client.post(
            "/visibility",
            json={"employer_id": str(employer_id), "plan_id": str(plan_id), "action": "attach"},
        )
        r.raise_for_status()
        return bool(r.json().get("changed"))

    @strawberry.mutation
    async def detach_plan(self, employer_id: strawberry.ID, plan_id: strawberry.ID) -> bool:
        r = await clients.group_client.post(
            "/visibility",
            json={"employer_id": str(employer_id), "plan_id": str(plan_id), "action": "detach"},
        )
        r.raise_for_status()
        return bool(r.json().get("changed"))

    @strawberry.mutation
    async def replay_file(self, file_id: strawberry.ID) -> bool:
        """Republish FileReceived for an already-uploaded file."""
        from sqlalchemy import text

        from eligibility_common.events import Topics
        from eligibility_common.pubsub import publish

        from app.search import _engine

        sql = text(
            "SELECT object_key, format, tenant_id FROM file_ingestion_jobs "
            "WHERE file_id = :fid LIMIT 1"
        )
        try:
            async with _engine().connect() as conn:
                res = await conn.execute(sql, {"fid": str(file_id)})
                row = res.mappings().first()
        except Exception as e:
            log.warning("bff.replay.lookup_error", error=str(e))
            return False
        if not row:
            return False
        try:
            import uuid
            from datetime import datetime as _dt
            from datetime import timezone

            publish(
                Topics.FILE_RECEIVED,
                {
                    "event_id": str(uuid.uuid4()),
                    "event_type": "FileReceived",
                    "tenant_id": str(row["tenant_id"]),
                    "emitted_at": _dt.now(timezone.utc).isoformat(),
                    "file_id": str(file_id),
                    "format": row["format"],
                    "object_key": row["object_key"],
                },
            )
            return True
        except Exception as e:
            log.warning("bff.replay.publish_error", error=str(e))
            return False


# ─────────────────────────── Subscription root ────────────────────────────────


@strawberry.type
class EnrollmentUpdate:
    """A slim event payload describing a change that impacts a member's
    timeline. Enough for the frontend to decide whether to invalidate its
    TanStack Query cache for the open Member Detail drawer."""

    member_id: strawberry.ID
    event_type: str  # "MemberUpserted" | "EnrollmentAdded" | "EnrollmentTerminated" | "EnrollmentChanged"
    occurred_at: datetime


@strawberry.type
class Subscription:
    @strawberry.subscription
    async def enrollment_updated(
        self, member_id: strawberry.ID
    ) -> AsyncGenerator[EnrollmentUpdate, None]:
        """Stream events for a specific member. Closes on client disconnect."""
        async for evt in subscribe_enrollment_updates(str(member_id)):
            occurred_raw = evt.get("occurred_at")
            try:
                occurred = (
                    datetime.fromisoformat(occurred_raw)
                    if occurred_raw
                    else datetime.utcnow()
                )
            except (TypeError, ValueError):
                occurred = datetime.utcnow()
            yield EnrollmentUpdate(
                member_id=strawberry.ID(str(evt.get("member_id", ""))),
                event_type=str(evt.get("event_type", "Unknown")),
                occurred_at=occurred,
            )


schema = strawberry.Schema(
    query=Query,
    mutation=Mutation,
    subscription=Subscription,
    extensions=[
        ErrorEnvelopeExtension,
        depth_limit_extension(),
    ],
)
