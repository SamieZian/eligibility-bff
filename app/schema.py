"""Strawberry GraphQL schema for the BFF.

Queries are read-only projections (mostly served from eligibility_view);
mutations fan out to atlas / member for writes and return the IDs of whatever
was created or mutated. The BFF never writes enrollments directly — that's
atlas's job — but it owns the GraphQL contract the frontend speaks.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

import strawberry
from eligibility_common.logging import get_logger

from app import clients, search
from app.settings import settings

log = get_logger(__name__)


# ─────────────────────────── Types ────────────────────────────────────────────


@strawberry.type
class Enrollment:
    enrollment_id: strawberry.ID
    tenant_id: strawberry.ID
    employer_id: strawberry.ID
    employer_name: str | None
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
    async def employers(self, search: str | None = None) -> list[EmployerSummary]:
        if not search:
            return []
        try:
            r = await clients.group_client.get("/employers", params={"name": search})
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
class Mutation:
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


schema = strawberry.Schema(query=Query, mutation=Mutation)
