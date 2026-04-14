from __future__ import annotations

from eligibility_common.settings import CommonSettings
from pydantic import Field


class Settings(CommonSettings):
    service_name: str = "bff"

    atlas_url: str = Field("http://atlas:8000", alias="ATLAS_URL")
    member_url: str = Field("http://member:8000", alias="MEMBER_URL")
    group_url: str = Field("http://group:8000", alias="GROUP_URL")
    plan_url: str = Field("http://plan:8000", alias="PLAN_URL")
    opensearch_url: str = Field("http://opensearch:9200", alias="OPENSEARCH_URL")

    minio_endpoint: str = Field("http://minio:9000", alias="MINIO_ENDPOINT")
    minio_bucket: str = Field("eligibility-files", alias="MINIO_BUCKET")
    minio_user: str = Field("minio", alias="MINIO_ROOT_USER")
    minio_password: str = Field("minio12345", alias="MINIO_ROOT_PASSWORD")

    # BFF reads eligibility_view projected into atlas_db for local dev.
    read_model_db_url: str = Field(
        "postgresql+psycopg://postgres:dev_pw@atlas_db:5432/atlas_db",
        alias="ATLAS_DB_URL",
    )


settings = Settings()
