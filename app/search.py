"""Search routing: OpenSearch for fuzzy, Postgres keyset for filtered lists.

If the caller provides a `q` (free-text name / card), we fan out to OpenSearch
for fuzziness and then hydrate row data from the Postgres eligibility_view.
Otherwise it's a pure SQL query — keyset-paginated so deep pages stay cheap.

Graceful degradation: if OS is unreachable we log a WARN and fall through to
the SQL path so a flaky OS cluster doesn't take down search altogether.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import httpx
from eligibility_common.logging import get_logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.settings import settings

log = get_logger(__name__)

# ─── eligibility_view columns (projector builds this) ─────────────────────────
# id, tenant_id, enrollment_id, employer_id, employer_name, plan_id, plan_name,
# plan_code, member_id, member_name, first_name, last_name, dob, gender,
# card_number, ssn_last4, relationship, status, effective_date,
# termination_date, subgroup_id, subgroup_name
_SELECT_COLS = (
    "enrollment_id, tenant_id, employer_id, employer_name, plan_id, plan_name, "
    "plan_code, member_id, member_name, first_name, last_name, dob, gender, "
    "card_number, ssn_last4, relationship, status, effective_date, termination_date"
)

_read_engine: AsyncEngine | None = None


def _engine() -> AsyncEngine:
    global _read_engine
    if _read_engine is None:
        url = settings.read_model_db_url
        if url.startswith("postgresql+psycopg://"):
            url = url.replace("postgresql+psycopg://", "postgresql+asyncpg://", 1)
        elif url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        _read_engine = create_async_engine(url, pool_pre_ping=True)
    return _read_engine


@dataclass
class _Filters:
    q: str | None = None
    card_number: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    ssn_last4: str | None = None
    employer_id: str | None = None
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


def _encode_cursor(effective_date: date, enrollment_id: str) -> str:
    raw = f"{effective_date.isoformat()}|{enrollment_id}".encode()
    return base64.urlsafe_b64encode(raw).decode()


def _decode_cursor(cursor: str) -> tuple[date, str] | None:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        d, eid = raw.split("|", 1)
        return date.fromisoformat(d), eid
    except Exception:
        return None


def _where_clauses(f: _Filters) -> tuple[list[str], dict[str, Any]]:
    where: list[str] = []
    params: dict[str, Any] = {}
    if f.card_number:
        where.append("card_number = :card_number")
        params["card_number"] = f.card_number
    if f.first_name:
        where.append("first_name ILIKE :first_name")
        params["first_name"] = f"{f.first_name}%"
    if f.last_name:
        where.append("last_name ILIKE :last_name")
        params["last_name"] = f"{f.last_name}%"
    if f.ssn_last4:
        where.append("ssn_last4 = :ssn_last4")
        params["ssn_last4"] = f.ssn_last4
    if f.employer_id:
        where.append("employer_id = :employer_id")
        params["employer_id"] = f.employer_id
    if f.employer_name:
        where.append("employer_name ILIKE :employer_name")
        params["employer_name"] = f"%{f.employer_name}%"
    if f.subgroup_name:
        where.append("subgroup_name ILIKE :subgroup_name")
        params["subgroup_name"] = f"%{f.subgroup_name}%"
    if f.plan_name:
        where.append("plan_name ILIKE :plan_name")
        params["plan_name"] = f"%{f.plan_name}%"
    if f.plan_code:
        where.append("plan_code = :plan_code")
        params["plan_code"] = f.plan_code
    if f.dob:
        where.append("dob = :dob")
        params["dob"] = f.dob
    if f.effective_date_from:
        where.append("effective_date >= :effective_date_from")
        params["effective_date_from"] = f.effective_date_from
    if f.effective_date_to:
        where.append("effective_date <= :effective_date_to")
        params["effective_date_to"] = f.effective_date_to
    if f.termination_date_from:
        where.append("termination_date >= :termination_date_from")
        params["termination_date_from"] = f.termination_date_from
    if f.termination_date_to:
        where.append("termination_date <= :termination_date_to")
        params["termination_date_to"] = f.termination_date_to
    if f.member_type:
        where.append("relationship = :member_type")
        params["member_type"] = f.member_type
    if f.status:
        where.append("status = :status")
        params["status"] = f.status
    return where, params


async def _pg_search(
    f: _Filters,
    limit: int,
    cursor: str | None,
    sort: str,
    enrollment_ids: list[str] | None = None,
) -> tuple[list[dict[str, Any]], int, str | None]:
    where, params = _where_clauses(f)
    if enrollment_ids is not None:
        if not enrollment_ids:
            return [], 0, None
        where.append("enrollment_id = ANY(:enrollment_ids)")
        params["enrollment_ids"] = enrollment_ids

    # Keyset pagination on (effective_date, enrollment_id)
    if cursor:
        decoded = _decode_cursor(cursor)
        if decoded:
            cd, ceid = decoded
            # DESC sort: next page has (effective_date < cd) OR (= cd AND id < ceid)
            where.append("(effective_date, enrollment_id) < (:c_eff, :c_eid)")
            params["c_eff"] = cd
            params["c_eid"] = ceid

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    order_sql = "ORDER BY effective_date DESC, enrollment_id DESC"
    if sort == "effective_date_asc":
        order_sql = "ORDER BY effective_date ASC, enrollment_id ASC"

    sql = text(f"SELECT {_SELECT_COLS} FROM eligibility_view {where_sql} {order_sql} LIMIT :lim")
    count_sql = text(f"SELECT COUNT(*) FROM eligibility_view {where_sql}")
    params["lim"] = limit + 1  # fetch one extra to know if there's a next page

    async with _engine().connect() as conn:
        try:
            res = await conn.execute(sql, params)
            rows = [dict(r._mapping) for r in res]
            total_params = {k: v for k, v in params.items() if k not in ("lim", "c_eff", "c_eid")}
            count_where_sql = (
                f"WHERE {' AND '.join([w for w in where if not w.startswith('(effective_date, enrollment_id)')])}"
                if any(not w.startswith("(effective_date, enrollment_id)") for w in where)
                else ""
            )
            total_sql = text(f"SELECT COUNT(*) FROM eligibility_view {count_where_sql}")
            total_res = await conn.execute(total_sql, total_params)
            total = int(total_res.scalar() or 0)
        except Exception as e:
            # Table not yet created (projector not up) → empty result rather than a 500.
            log.warning("bff.search.pg.error", error=str(e))
            return [], 0, None

    next_cursor: str | None = None
    if len(rows) > limit:
        last = rows[limit - 1]
        rows = rows[:limit]
        next_cursor = _encode_cursor(last["effective_date"], str(last["enrollment_id"]))
    return rows, total, next_cursor


async def _opensearch_ids(f: _Filters, limit: int) -> list[str] | None:
    """Return matching enrollment_ids from OS, or None on failure (→ fallback)."""
    must: list[dict[str, Any]] = []
    if f.q:
        must.append(
            {
                "multi_match": {
                    "query": f.q,
                    "fields": ["member_name^2", "first_name", "last_name", "card_number"],
                    "fuzziness": "AUTO",
                }
            }
        )
    filter_terms: list[dict[str, Any]] = []
    if f.card_number:
        filter_terms.append({"term": {"card_number": f.card_number}})
    if f.employer_id:
        filter_terms.append({"term": {"employer_id": f.employer_id}})
    if f.plan_code:
        filter_terms.append({"term": {"plan_code": f.plan_code}})
    if f.status:
        filter_terms.append({"term": {"status": f.status}})

    body = {
        "size": limit * 5,  # overfetch, hydrate filters may narrow further
        "_source": ["enrollment_id"],
        "query": {"bool": {"must": must, "filter": filter_terms}},
    }
    url = f"{settings.opensearch_url}/eligibility_view/_search"
    try:
        async with httpx.AsyncClient(timeout=2.0) as c:
            r = await c.post(url, json=body)
            r.raise_for_status()
            data = r.json()
            return [h["_source"]["enrollment_id"] for h in data.get("hits", {}).get("hits", [])]
    except Exception as e:
        log.warning("bff.search.os.unreachable", error=str(e))
        return None


async def search(
    f: _Filters,
    limit: int = 25,
    cursor: str | None = None,
    sort: str = "effective_date_desc",
) -> tuple[list[dict[str, Any]], int, str | None]:
    if f.q:
        ids = await _opensearch_ids(f, limit)
        if ids is not None:
            return await _pg_search(f, limit, cursor, sort, enrollment_ids=ids)
        # degrade: fall through to pg scan with other filters
        log.info("bff.search.degraded_to_pg")
    return await _pg_search(f, limit, cursor, sort)


async def find_by_card(card_number: str) -> dict[str, Any] | None:
    rows, _, _ = await _pg_search(_Filters(card_number=card_number), 1, None, "effective_date_desc")
    return rows[0] if rows else None


async def timeline_for_member(
    member_id: str, tenant_id: str, as_of: datetime | None = None
) -> list[dict[str, Any]]:
    """Delegate to atlas — it's the source of truth for bitemporal segments."""
    from app.clients import atlas_client

    headers = {"X-Tenant-Id": tenant_id}
    try:
        r = await atlas_client.get(f"/members/{member_id}/timeline", headers=headers)
        r.raise_for_status()
        return r.json().get("segments", [])
    except Exception as e:
        log.warning("bff.timeline.error", error=str(e))
        return []
