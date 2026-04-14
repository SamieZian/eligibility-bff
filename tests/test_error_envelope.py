"""Regression tests for Fix 1 — GraphQL error envelope.

Every error surfaced over GraphQL must carry ``extensions.code``,
``extensions.retryable``, and ``extensions.correlation_id``. Messages for
unknown exceptions must be sanitised to "Internal error" so stack-trace /
SQL-fragment leakage is impossible.
"""
from __future__ import annotations

import asyncio

import strawberry
import structlog
from eligibility_common.errors import DomainError, InfraError

from app.graphql_extensions import ErrorEnvelopeExtension, depth_limit_extension


@strawberry.type
class _Q:
    @strawberry.field
    def boom_domain(self) -> str:
        raise DomainError("ENROLLMENT_OVERLAP", "overlapping dates")

    @strawberry.field
    def boom_infra(self) -> str:
        raise InfraError("DOWNSTREAM_UNAVAILABLE", "atlas is down")

    @strawberry.field
    def boom_random(self) -> str:
        raise RuntimeError("secret SQL leaking: SELECT ssn FROM members")

    @strawberry.field
    def ok(self) -> str:
        return "ok"


_schema = strawberry.Schema(
    query=_Q, extensions=[ErrorEnvelopeExtension, depth_limit_extension(max_depth=3)]
)


def _exec(query: str) -> dict:
    return asyncio.run(_schema.execute(query)).__dict__


def test_domain_error_maps_to_envelope():
    structlog.contextvars.bind_contextvars(correlation_id="cid-123")
    try:
        result = asyncio.run(_schema.execute("{ boomDomain }"))
    finally:
        structlog.contextvars.unbind_contextvars("correlation_id")
    assert result.errors
    err = result.errors[0]
    assert err.extensions["code"] == "ENROLLMENT_OVERLAP"
    assert err.extensions["retryable"] is False
    assert err.extensions["correlation_id"] == "cid-123"
    # Client-facing message stays meaningful for domain errors.
    assert "overlapping" in err.message


def test_infra_error_is_retryable():
    result = asyncio.run(_schema.execute("{ boomInfra }"))
    err = result.errors[0]
    assert err.extensions["code"] == "DOWNSTREAM_UNAVAILABLE"
    assert err.extensions["retryable"] is True


def test_unknown_error_sanitised():
    """Messages for unknown exceptions must not leak anywhere to the client."""
    result = asyncio.run(_schema.execute("{ boomRandom }"))
    err = result.errors[0]
    assert err.extensions["code"] == "INTERNAL_ERROR"
    assert err.extensions["retryable"] is False
    assert err.message == "Internal error"
    assert "SELECT ssn" not in err.message


def test_depth_limit_enforced():
    deep = "{ a: ok b: ok c: ok d: ok }"  # depth 1 — ok
    assert asyncio.run(_schema.execute(deep)).errors is None

    # Build a query that's definitely deeper than max_depth=3.
    too_deep = "query X { __schema { types { fields { type { fields { name } } } } } }"
    result = asyncio.run(_schema.execute(too_deep))
    assert result.errors is not None
    assert any("maximum depth" in e.message for e in result.errors)
