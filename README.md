# eligibility-bff

**GraphQL gateway + file upload**

The frontend's backend. FastAPI + Strawberry GraphQL at `/graphql`. REST `POST /files/eligibility` for uploads (streams to MinIO, publishes `FileReceived` to Pub/Sub). Talks to atlas/member/group/plan over HTTP with circuit breakers and DataLoader batching.

This is part of the **Eligibility & Enrollment Platform** — a distributed microservices system for healthcare eligibility. Each service lives in its own repo so it can be deployed, scaled, and evolved independently.

## Companion repos

| Repo | Purpose |
|---|---|
| [`eligibility-platform`](https://github.com/SamieZian/eligibility-platform) | Meta / orchestration — docker-compose, demo flow, 834 sample files |
| [`eligibility-atlas`](https://github.com/SamieZian/eligibility-atlas) | Enrollment (bitemporal) |
| [`eligibility-member`](https://github.com/SamieZian/eligibility-member) | Members + dependents |
| [`eligibility-group`](https://github.com/SamieZian/eligibility-group) | Payers / employers / subgroups / plan visibility |
| [`eligibility-plan`](https://github.com/SamieZian/eligibility-plan) | Plan catalog |
| [`eligibility-bff`](https://github.com/SamieZian/eligibility-bff) | GraphQL gateway + file upload |
| [`eligibility-workers`](https://github.com/SamieZian/eligibility-workers) | Ingestion / projector / outbox-relay |
| [`eligibility-frontend`](https://github.com/SamieZian/eligibility-frontend) | React UI |

## Run the whole platform

Don't run this service solo for demos — go to [`eligibility-platform`](https://github.com/SamieZian/eligibility-platform) and follow the quickstart there.

## Run this service in isolation

```bash
# build
docker build -t eligibility-bff:local .

# run (needs a Postgres — simplest: spin one with docker)
docker run -d --name pg-local -e POSTGRES_PASSWORD=dev_pw -p 5441:5432 postgres:15-alpine

# wait 3s, then start the service
docker run --rm --network host \
  -e DATABASE_URL="postgresql+psycopg://postgres:dev_pw@localhost:5441/postgres" \
  -e SERVICE_NAME=bff \
  -e PUBSUB_PROJECT_ID=local \
  -p 4000:8000 \
  eligibility-bff:local

# health check
curl http://localhost:4000/livez
```

## Project layout

```
.
├── app/                 # Hexagonal layout
│   ├── domain/          # Pure business logic — no I/O
│   ├── application/     # Use-cases / command handlers
│   ├── infra/           # Repos, ORM models, KMS adapter
│   ├── interfaces/      # FastAPI routers, gRPC servicers (future)
│   ├── settings.py
│   └── main.py          # FastAPI app factory + lifespan
├── tests/               # pytest unit tests
├── migrations/          # Alembic DDL (prod)
├── libs/                # Vendored shared code
│   └── python-common/   # Outbox, pubsub, retry, circuit breaker, errors
├── Dockerfile
├── pyproject.toml
└── README.md            # this file
```

## Test

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e libs/python-common
pip install fastapi sqlalchemy 'psycopg[binary]' asyncpg pydantic pydantic-settings \
  structlog httpx tenacity cryptography redis google-cloud-pubsub \
  opentelemetry-api opentelemetry-sdk opentelemetry-exporter-otlp \
  opentelemetry-instrumentation-fastapi opentelemetry-instrumentation-sqlalchemy \
  pytest pytest-asyncio 'strawberry-graphql[fastapi]'
PYTHONPATH=.:libs/python-common/src \
  DATABASE_URL=postgresql+psycopg://x@x/x \
  python -m pytest tests -q
```

## API

See `app/interfaces/api.py` for the full route list. Health: `/livez`, `/readyz`.

## Observability

- `X-Correlation-Id` propagated on every response.
- Structured JSON logs with `trace_id`, `tenant_id`, correlation id.
- OpenTelemetry traces exported via OTLP when `OTEL_EXPORTER_OTLP_ENDPOINT` is set.

## License

MIT.
