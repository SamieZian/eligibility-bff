import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent.parent / "libs" / "python-common" / "src"))

os.environ.setdefault("SERVICE_NAME", "bff")
os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ.setdefault("ATLAS_URL", "http://atlas:8000")
os.environ.setdefault("MEMBER_URL", "http://member:8000")
os.environ.setdefault("GROUP_URL", "http://group:8000")
os.environ.setdefault("PLAN_URL", "http://plan:8000")
os.environ.setdefault("OPENSEARCH_URL", "http://opensearch:9200")
os.environ.setdefault("MINIO_ENDPOINT", "http://minio:9000")
os.environ.setdefault("MINIO_BUCKET", "eligibility-files")
os.environ.setdefault("MINIO_ROOT_USER", "minio")
os.environ.setdefault("MINIO_ROOT_PASSWORD", "minio12345")
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://postgres:dev_pw@atlas_db:5432/atlas_db")
os.environ.setdefault("ATLAS_DB_URL", "postgresql+psycopg://postgres:dev_pw@atlas_db:5432/atlas_db")
