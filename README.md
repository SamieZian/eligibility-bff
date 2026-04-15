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
# Install Poetry 1.8.3 if you don't have it
pipx install poetry==1.8.3  # or: pip install --user poetry==1.8.3

# Install all deps (including the vendored eligibility-common lib) into a managed venv
poetry install

# Configure
export $(cat .env | xargs)

# Run
PYTHONPATH=.:libs/python-common/src poetry run python -m app.main
```

## Test

```bash
PYTHONPATH=.:libs/python-common/src \
  DATABASE_URL=postgresql+psycopg://x@x/x \
  poetry run pytest tests -q
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

## Testing via curl

BFF serves GraphQL at **`POST http://localhost:4000/graphql`** and a REST file-upload at `POST /files/eligibility`. The frontend is optional — reviewers can exercise every feature from the command line.

```bash
BFF=http://localhost:4000
T=11111111-1111-1111-1111-111111111111
H=(-H "Content-Type: application/json" -H "X-Tenant-Id: $T")
```

**Queries**

```bash
# 1. Fuzzy search (hits OpenSearch + hydrates from pg)
curl -s -X POST $BFF/graphql "${H[@]}" -d '{
  "query":"{ searchEnrollments(filter:{q:\"sharma\"}, page:{limit:5}) { total items { memberName employerName planName status } } }"
}' | jq .

# 2. Exact filter + pagination (server-side status chip)
curl -s -X POST $BFF/graphql "${H[@]}" -d '{
  "query":"{ searchEnrollments(filter:{status:\"active\"}, page:{limit:10}) { total items { memberName } nextCursor } }"
}' | jq .

# 3. Plans / Employers / Groups admin
curl -s -X POST $BFF/graphql "${H[@]}" -d '{"query":"{ plans { id planCode name type } }"}' | jq .
curl -s -X POST $BFF/graphql "${H[@]}" -d '{"query":"{ groupAdmin { id name subgroups { name } visiblePlanIds } }"}' | jq .

# 4. Bitemporal timeline (plan names enriched)
MID=...  # from (1)
curl -s -X POST $BFF/graphql "${H[@]}" -d "{
  \"query\":\"{ enrollmentTimeline(memberId: \\\"$MID\\\") { planName status validFrom validTo isInForce } }\"
}" | jq .
```

**Mutations**

```bash
# Add member (orchestrated saga: POST /members → POST /commands)
EMP=...; PLAN=...
curl -s -X POST $BFF/graphql "${H[@]}" -d "{
  \"query\":\"mutation(\$in: AddMemberInput!) { addMember(input: \$in) { memberId memberName } }\",
  \"variables\":{\"in\":{
    \"firstName\":\"DEMO\",\"lastName\":\"USER\",\"dob\":\"2000-01-01\",
    \"employerId\":\"$EMP\",\"planId\":\"$PLAN\",
    \"relationship\":\"subscriber\",\"effectiveDate\":\"2026-05-01\"
  }}
}" | jq .

# Terminate
curl -s -X POST $BFF/graphql "${H[@]}" -d "{
  \"query\":\"mutation { terminateEnrollment(memberId: \\\"$MID\\\", planId: \\\"$PLAN\\\", validTo: \\\"2026-07-31\\\") }\"
}" | jq .

# Plan change saga (TERMINATE old + ADD new on the same effective date)
curl -s -X POST $BFF/graphql "${H[@]}" -d "{
  \"query\":\"mutation { changeEnrollmentPlan(memberId:\\\"$MID\\\", oldPlanId:\\\"$OLD\\\", newPlanId:\\\"$NEW\\\", employerId:\\\"$EMP\\\", newValidFrom:\\\"2026-07-01\\\") }\"
}" | jq .

# Demographics update (bumps version; triggers MemberUpserted → projector refresh)
curl -s -X POST $BFF/graphql "${H[@]}" -d "{
  \"query\":\"mutation { updateMemberDemographics(memberId:\\\"$MID\\\", lastName:\\\"SHARMA-PATEL\\\") }\"
}" | jq .
```

**File upload** (REST multipart — same endpoint the Upload UI uses)

```bash
curl -s -X POST $BFF/files/eligibility \
  -H "X-Tenant-Id: $T" -H "X-Correlation-Id: $(uuidgen)" \
  -F "file=@samples/834_demo.x12" | jq .
# → {"file_id": "...", "job_id": "...", "status": "UPLOADED"}
```

**GraphQL subscription** (WebSocket, `graphql-transport-ws`)

```bash
pip install websockets httpx
python3 - <<'PY'
import asyncio, json, websockets
async def main():
    async with websockets.connect("ws://localhost:4000/graphql",
                                   subprotocols=["graphql-transport-ws"]) as ws:
        await ws.send(json.dumps({"type":"connection_init","payload":{}}))
        print(await ws.recv())
        await ws.send(json.dumps({
            "id":"1","type":"subscribe",
            "payload":{"query":"subscription($m: ID!) { enrollmentUpdated(memberId: $m) { eventType occurredAt } }",
                       "variables":{"m":"REPLACE_WITH_REAL_MEMBER_ID"}}
        }))
        while True: print(await ws.recv())
asyncio.run(main())
PY
# Trigger any mutation on that member in another terminal → see the event stream in ~1s
```

**Error envelope** (GraphQL `extensions`)

```json
{
  "errors": [{
    "message": "Internal error",
    "extensions": {
      "code": "INTERNAL_ERROR",
      "retryable": false,
      "correlation_id": "test-1"
    }
  }]
}
```

**Depth-limit test** (schema rejects queries deeper than 8):

```bash
curl -s -X POST $BFF/graphql "${H[@]}" -d '{
  "query":"{ __schema { types { fields { type { fields { type { fields { type { fields { name } } } } } } } } } }"
}' | jq .
# → errors: "Query '<anonymous>' exceeds maximum depth of 8 (got 10)"
```

## Patterns used

- Hexagonal architecture (domain / application / infra / interfaces)
- Strawberry GraphQL with custom extensions: **error envelope**, **DataLoader**, **AST depth-limit validator**
- GraphQL subscriptions over WebSocket (`graphql-transport-ws` + `graphql-ws`)
- Structured JSON logs with correlation ID propagation
- OpenTelemetry traces (browser → BFF → service → DB)
- Circuit breakers + retry-with-jitter + deadline propagation on every downstream
- CORS origin allow-list (rejects `*` with credentialed requests)

## License

MIT.
