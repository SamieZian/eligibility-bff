"""REST endpoint: POST /files/eligibility.

Streams the multipart body straight into MinIO (S3-compatible), records a
`file_ingestion_jobs` row for operator visibility, and publishes a single
`FileReceived` event. The ingestion worker is the one that reads the object
back — the BFF is just a drop-off point.

Idempotency: the job row's PK is file_id (uuid we generate); re-uploading the
same blob just creates a fresh job. We deliberately do NOT dedupe by content
hash — clients control replay semantics via the `replay_file` mutation.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.client import Config as BotoConfig
from eligibility_common.events import Topics
from eligibility_common.logging import get_logger
from eligibility_common.pubsub import publish
from fastapi import APIRouter, File, Header, HTTPException, UploadFile
from sqlalchemy import text

from app.search import _engine
from app.settings import settings

log = get_logger(__name__)
router = APIRouter(tags=["files"])


FILE_INGESTION_JOBS_DDL_STATEMENTS = [
    """
CREATE TABLE IF NOT EXISTS file_ingestion_jobs (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    file_id UUID NOT NULL UNIQUE,
    object_key TEXT NOT NULL,
    format TEXT NOT NULL,
    status TEXT NOT NULL,
    uploaded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    total_rows INTEGER,
    success_rows INTEGER,
    failed_rows INTEGER
)
""",
    "CREATE INDEX IF NOT EXISTS file_jobs_by_tenant ON file_ingestion_jobs (tenant_id, uploaded_at DESC)",
]
FILE_INGESTION_JOBS_DDL = ";\n".join(FILE_INGESTION_JOBS_DDL_STATEMENTS)


def _s3_client() -> Any:
    return boto3.client(
        "s3",
        endpoint_url=settings.minio_endpoint,
        aws_access_key_id=settings.minio_user,
        aws_secret_access_key=settings.minio_password,
        config=BotoConfig(signature_version="s3v4", s3={"addressing_style": "path"}),
        region_name="us-east-1",
    )


def ensure_bucket() -> None:
    c = _s3_client()
    try:
        c.head_bucket(Bucket=settings.minio_bucket)
    except Exception:
        try:
            c.create_bucket(Bucket=settings.minio_bucket)
            log.info("bff.bucket.created", bucket=settings.minio_bucket)
        except Exception as e:
            # In tests MinIO may not be reachable; don't crash startup.
            log.warning("bff.bucket.ensure_failed", error=str(e))


def _format_from_name(name: str) -> str:
    lo = (name or "").lower()
    if lo.endswith(".x12") or lo.endswith(".edi") or lo.endswith(".834"):
        return "X12_834"
    if lo.endswith(".csv"):
        return "CSV"
    if lo.endswith(".xlsx") or lo.endswith(".xls"):
        return "XLSX"
    raise HTTPException(status_code=415, detail=f"Unsupported file extension: {name}")


@router.post("/files/eligibility", status_code=202)
async def upload_eligibility(
    file: UploadFile = File(...),
    x_tenant_id: str | None = Header(None, alias="X-Tenant-Id"),
) -> dict[str, str]:
    tenant_id = x_tenant_id or settings.tenant_default
    fmt = _format_from_name(file.filename or "")
    file_id = str(uuid.uuid4())
    job_id = str(uuid.uuid4())
    object_key = f"{tenant_id}/{file_id}/{file.filename}"

    # 1. Stream to MinIO. UploadFile.file is a SpooledTemporaryFile — boto3 can
    #    read it directly, avoiding loading the whole thing in memory.
    try:
        _s3_client().put_object(
            Bucket=settings.minio_bucket,
            Key=object_key,
            Body=file.file,
            ContentType=file.content_type or "application/octet-stream",
        )
    except Exception as e:
        log.exception("bff.upload.minio_error", error=str(e))
        raise HTTPException(status_code=502, detail="upload storage failed") from e

    # 2. Record the job row.
    uploaded_at = datetime.now(timezone.utc)
    try:
        async with _engine().begin() as conn:
            await conn.execute(
                text(
                    """
                    INSERT INTO file_ingestion_jobs
                      (id, tenant_id, file_id, object_key, format, status, uploaded_at)
                    VALUES
                      (:id, :tenant_id, :file_id, :object_key, :format, 'UPLOADED', :uploaded_at)
                    """
                ),
                {
                    "id": job_id,
                    "tenant_id": tenant_id,
                    "file_id": file_id,
                    "object_key": object_key,
                    "format": fmt,
                    "uploaded_at": uploaded_at,
                },
            )
    except Exception as e:
        log.warning("bff.upload.job_row_failed", error=str(e))

    # 3. Publish FileReceived — ingestion worker subscribes.
    try:
        publish(
            Topics.FILE_RECEIVED,
            {
                "event_id": str(uuid.uuid4()),
                "event_type": "FileReceived",
                "tenant_id": tenant_id,
                "emitted_at": uploaded_at.isoformat(),
                "file_id": file_id,
                "format": fmt,
                "object_key": object_key,
            },
        )
    except Exception as e:
        log.warning("bff.upload.publish_failed", error=str(e))

    return {"file_id": file_id, "job_id": job_id, "status": "UPLOADED"}
