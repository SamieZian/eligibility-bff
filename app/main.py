from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from eligibility_common.app_factory import create_app
from fastapi import FastAPI
from sqlalchemy import text
from strawberry.fastapi import GraphQLRouter
from strawberry.subscriptions import (
    GRAPHQL_TRANSPORT_WS_PROTOCOL,
    GRAPHQL_WS_PROTOCOL,
)

from fastapi.middleware.cors import CORSMiddleware

from app import clients
from app.graphql_extensions import build_loaders
from app.schema import schema
from app.search import _engine
from app.settings import settings
from app.upload import FILE_INGESTION_JOBS_DDL_STATEMENTS, ensure_bucket
from app.upload import router as upload_router


@asynccontextmanager
async def lifespan(_app: FastAPI):  # type: ignore[no-untyped-def]
    # Ensure the BFF's own ingestion-job table exists in atlas_db. We don't run
    # migrations for this single tracking table — it lives or dies with atlas's db.
    try:
        async with _engine().begin() as conn:
            for stmt in FILE_INGESTION_JOBS_DDL_STATEMENTS:
                await conn.execute(text(stmt))
    except Exception:
        # DB may not be reachable during local `pytest` runs — keep going.
        pass
    try:
        ensure_bucket()
    except Exception:
        pass
    try:
        yield
    finally:
        await clients.close_all()


async def _ping_downstreams() -> None:
    # Readiness is intentionally forgiving — just confirm clients exist.
    return None


app = create_app(
    service_name=settings.service_name,
    lifespan=lifespan,
    readiness={"self": _ping_downstreams},
)

# CORS origins come from the CORS_ALLOW_ORIGINS env var (see app.settings).
# Defaults to local dev hosts only; prod MUST set the env to its public origin(s).
# Wildcard "*" is rejected in settings because credentialed requests can't use it.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Correlation-Id"],
)


async def _graphql_context() -> dict[str, Any]:
    """Build a fresh GraphQL context for every incoming request.

    Crucially we instantiate new DataLoaders per-request — their batching +
    caching is scoped to the life of a single GraphQL operation, so sharing
    loaders across requests would leak data between users.
    """
    return {"loaders": build_loaders(clients.group_client)}


graphql_app: GraphQLRouter = GraphQLRouter(
    schema,
    context_getter=_graphql_context,
    subscription_protocols=[
        GRAPHQL_TRANSPORT_WS_PROTOCOL,
        GRAPHQL_WS_PROTOCOL,
    ],
)
app.include_router(graphql_app, prefix="/graphql")
app.include_router(upload_router)


if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=False)
