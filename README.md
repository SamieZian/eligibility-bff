# eligibility-bff

**GraphQL gateway + file upload** — the frontend's backend.

## What this service does

FastAPI + Strawberry GraphQL at `/graphql`. REST `POST /files/eligibility` for 834 / CSV / XLSX uploads (streams to MinIO, publishes `FileReceived` to Pub/Sub).

Talks to atlas / member / group / plan over HTTP with **circuit breakers** (open after 5 failures in a 10s window) and **DataLoader batching** to avoid N+1 in GraphQL resolvers.

Provides an aggregated `groupAdmin` query that fans out to group service for the Groups admin page. Also orchestrates **`addMember`** mutation — POST member + POST atlas command in one call.

This is **one of 7 microservices** in the [Eligibility & Enrollment Platform](https://github.com/SamieZian/eligibility-platform). Each service has its own repo, its own database, its own Dockerfile, its own deployment lifecycle.

## Prerequisites

| Tool | Version | Why |
|---|---|---|
| Docker | 24+ | Container runtime |
| Docker Compose | v2 (the `docker compose` plugin) | Local orchestration |
| Python | 3.11+ | Standalone dev (optional) |
| GNU Make | any recent | Convenience targets (optional) |

The easiest way to use this service is via the orchestration repo:
```bash
git clone https://github.com/SamieZian/eligibility-platform
cd eligibility-platform
./bootstrap.sh         # clones this repo and 6 siblings
make up                # boots the whole stack with this svc included
```

## Companion repos

| Repo | What |
|---|---|
| [`eligibility-platform`](https://github.com/SamieZian/eligibility-platform) | Orchestration + docker-compose + sample 834 + demo |
| [`eligibility-atlas`](https://github.com/SamieZian/eligibility-atlas) | Bitemporal enrollment service |
| [`eligibility-member`](https://github.com/SamieZian/eligibility-member) | Members + dependents (KMS-encrypted SSN) |
| [`eligibility-group`](https://github.com/SamieZian/eligibility-group) | Payer / employer / subgroup / plan visibility |
| [`eligibility-plan`](https://github.com/SamieZian/eligibility-plan) | Plan catalog (Redis cache-aside) |
| [`eligibility-bff`](https://github.com/SamieZian/eligibility-bff) | GraphQL gateway + file upload |
| [`eligibility-workers`](https://github.com/SamieZian/eligibility-workers) | Stateless workers — ingestion / projector / outbox-relay |
| [`eligibility-frontend`](https://github.com/SamieZian/eligibility-frontend) | React + TS UI |

## Quickstart (standalone, with this repo only)

```bash
# 1. Configure
cp .env.example .env
# (edit values if needed — defaults work for local docker)

# 2. Build the image
docker build -t eligibility-bff:local .

# 3. Spin a Postgres for it
docker run -d --name pg-bff \
  -e POSTGRES_PASSWORD=dev_pw \
  -p 5441:5432 postgres:15-alpine

# 4. Run the service against that DB
docker run --rm -p 6441:8000 \
  --env-file .env \
  -e DATABASE_URL=postgresql+psycopg://postgres:dev_pw@host.docker.internal:5441/postgres \
  eligibility-bff:local

# 5. Health check
curl http://localhost:6441/livez
```

## Develop locally without Docker

```bash
# Python venv
python3.11 -m venv .venv && source .venv/bin/activate

# Install vendored shared lib + service deps
pip install -e libs/python-common
pip install fastapi 'uvicorn[standard]' sqlalchemy asyncpg 'psycopg[binary]' \
  alembic httpx pydantic pydantic-settings structlog tenacity cryptography \
  redis google-cloud-pubsub \
  opentelemetry-api opentelemetry-sdk opentelemetry-exporter-otlp \
  opentelemetry-instrumentation-fastapi opentelemetry-instrumentation-sqlalchemy

# Configure
export $(cat .env | xargs)

# Run
PYTHONPATH=.:libs/python-common/src python -m app.main
```

## Test

```bash
pip install pytest pytest-asyncio
PYTHONPATH=.:libs/python-common/src \
  DATABASE_URL=postgresql+psycopg://x@x/x \
  python -m pytest tests -q
```

## Project layout (hexagonal)

```
.
├── app/
│   ├── domain/         # Pure business logic — no I/O
│   ├── application/    # Use-cases, command handlers
│   ├── infra/          # SQLAlchemy repos, KMS, Redis, ORM models
│   ├── interfaces/     # FastAPI routers (HTTP)
│   ├── settings.py     # Pydantic env-driven config
│   └── main.py         # FastAPI app + lifespan
├── tests/              # pytest unit tests
├── migrations/         # Alembic (prod schema migrations)
├── libs/               # Vendored shared code
│   └── python-common/  # outbox, pubsub, errors, retry, circuit breaker, kms
├── .env.example        # All env vars documented
├── Dockerfile
├── pyproject.toml
└── README.md
```

## Environment variables

See [`.env.example`](.env.example) for the full list with defaults. Required:

- `SERVICE_NAME` — used in logs/traces
- `DATABASE_URL` — Postgres connection string
- `PUBSUB_PROJECT_ID` — Pub/Sub project (any value for local emulator)
- `PUBSUB_EMULATOR_HOST` — `pubsub:8085` when running with compose, unset in prod

Optional:
- `LOG_LEVEL` (`INFO`)
- `OTEL_EXPORTER_OTLP_ENDPOINT` — when set, traces export to that endpoint
- `TENANT_DEFAULT` — fallback tenant id when no header

## API

See `app/interfaces/api.py` for the route list. Standard endpoints:

- `GET /livez` → liveness probe
- `GET /readyz` → readiness probe (checks deps reachable)

## Patterns used

- Hexagonal architecture (domain / application / infra / interfaces)
- Transactional outbox for at-least-once event delivery
- Idempotent commands (each command's effect is repeatable)
- Structured JSON logs with correlation ID propagation
- OpenTelemetry traces (BFF → service → DB)
- Circuit breakers on outbound HTTP

## License

MIT.
