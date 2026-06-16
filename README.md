# Cloud Resource Management API

Production-minded FastAPI service for authenticated asynchronous cloud-resource jobs. It includes PostgreSQL persistence, Alembic migrations, Redis caching/rate limiting/pub-sub, RabbitMQ dispatch, a separate worker process, JWT authentication, user/admin authorization, cursor pagination, job logs, idempotent job creation, and SSE status updates.

## Architecture

```text
app/
├── api/              # Thin FastAPI route handlers and dependencies
├── core/             # Configuration, logging, security, error handling
├── infrastructure/   # PostgreSQL, Redis, RabbitMQ integrations
├── models/           # SQLAlchemy ORM models
├── schemas/          # Pydantic request/response schemas
├── services/         # Business rules and transaction orchestration
└── workers/          # RabbitMQ consumer and job execution loop
```

Routes delegate business logic to services. Services own authorization, idempotency, rate limits, cache invalidation, and state transitions. Infrastructure modules isolate external systems. The worker is independent from the HTTP API.

## Tech stack

- FastAPI + Pydantic
- SQLAlchemy asyncio + PostgreSQL + Alembic
- Redis for list caching, per-user rate limiting, and SSE pub/sub
- RabbitMQ for durable asynchronous job dispatch
- JWT bearer authentication with bcrypt password hashing
- Docker Compose for local full-system execution

## Run with Docker Compose

```bash
docker compose up --build
```

Services:

- API: <http://localhost:8000>
- OpenAPI docs: <http://localhost:8000/docs>
- RabbitMQ management UI: <http://localhost:15672> (`guest` / `guest`)
- PostgreSQL: `localhost:5432`
- Redis: `localhost:6379`

The API container runs migrations at startup:

```bash
alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Manual migration command:

```bash
docker compose run --rm api alembic upgrade head
```

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+asyncpg://crm:crm@postgres:5432/crm` | PostgreSQL connection |
| `REDIS_URL` | `redis://redis:6379/0` | Redis connection |
| `RABBITMQ_URL` | `amqp://guest:guest@rabbitmq:5672/` | RabbitMQ connection |
| `JWT_SECRET_KEY` | dev placeholder | JWT signing secret; replace outside local dev |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `60` | Token lifetime |
| `JOBS_QUEUE_NAME` | `jobs.execute` | RabbitMQ queue name |
| `JOBS_PER_USER_RUNNING_LIMIT` | `3` | Maximum running jobs per user |
| `JOBS_CACHE_TTL_SECONDS` | `30` | Redis list cache TTL |
| `RATE_LIMIT_PER_MINUTE` | `10` | `POST /jobs` limit per authenticated user |
| `ADMIN_SETUP_TOKEN` | unset | Required header token for admin registration |

## Authentication examples

Register a normal user:

```bash
curl -X POST http://localhost:8000/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"email":"user@example.com","password":"password123"}'
```

Create an admin user in local/dev by using the configured setup token:

```bash
curl -X POST http://localhost:8000/auth/register \
  -H 'Content-Type: application/json' \
  -H 'X-Admin-Setup-Token: local-admin-setup-token' \
  -d '{"email":"admin@example.com","password":"password123","role":"admin"}'
```

Login:

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"user@example.com","password":"password123"}' \
  | python -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')
```

## Job API examples

Create a demo job:

```bash
curl -X POST http://localhost:8000/jobs \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"payload":{"task_type":"demo","duration_seconds":5,"should_fail":false}}'
```

Idempotency test:

```bash
curl -X POST http://localhost:8000/jobs \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: abc123' \
  -d '{"payload":{"task_type":"demo","duration_seconds":1,"should_fail":false}}'
```

Repeat the exact command with the same user/key; the original job is returned and no duplicate row is created. Different users may reuse the same key independently.

List with cursor pagination:

```bash
curl -H "Authorization: Bearer $TOKEN" 'http://localhost:8000/jobs?limit=10'
curl -H "Authorization: Bearer $TOKEN" 'http://localhost:8000/jobs?limit=10&cursor=<next_cursor>'
```

Get details, logs, cancel, and SSE events:

```bash
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/jobs/1
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/jobs/1/logs
curl -X POST -H "Authorization: Bearer $TOKEN" http://localhost:8000/jobs/1/cancel
curl -N -H "Authorization: Bearer $TOKEN" http://localhost:8000/jobs/1/events
```

Rate limit check (`11th` create request inside one minute returns `429`):

```bash
for i in $(seq 1 11); do
  curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:8000/jobs \
    -H "Authorization: Bearer $TOKEN" \
    -H 'Content-Type: application/json' \
    -d '{"payload":{"task_type":"demo","duration_seconds":1}}'
done
```

## Testing

Local checks:

```bash
python -m compileall app
pytest
```

Full-system smoke test:

1. `docker compose up --build`
2. Register and login a user.
3. Create a job with `duration_seconds: 5`.
4. Watch `docker compose logs -f worker`.
5. Poll `GET /jobs/{id}` until `completed`.
6. Check `GET /jobs/{id}/logs`.
7. Repeat `POST /jobs` with `Idempotency-Key: abc123`.
8. Run the rate-limit loop.
9. Create a queued/pending job and call cancel.
10. Open the SSE curl command before creating or processing a job.
11. Login as admin and verify access to all jobs.

## Troubleshooting

- If RabbitMQ is unavailable, job creation may persist a job but returns `503` when dispatch fails; the design notes recommend a transactional outbox for production hardening.
- If Redis is unavailable, PostgreSQL-backed APIs continue; caching, rate limiting, and SSE delivery degrade and warnings are logged.
- If a worker crashes during a job, RabbitMQ may redeliver but a job already marked `running` needs stale-running recovery in production.
- For local Docker startup issues, inspect `docker compose logs api worker postgres redis rabbitmq` and verify health checks.
