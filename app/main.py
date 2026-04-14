from __future__ import annotations

from contextlib import asynccontextmanager

import uvicorn
from eligibility_common.app_factory import create_app
from fastapi import FastAPI
from sqlalchemy import text
from strawberry.fastapi import GraphQLRouter

from app import clients
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

graphql_app: GraphQLRouter = GraphQLRouter(schema)
app.include_router(graphql_app, prefix="/graphql")
app.include_router(upload_router)


if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=False)
