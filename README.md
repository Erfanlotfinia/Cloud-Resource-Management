# Cloud Resource Management API

FastAPI service for authenticated asynchronous job management with PostgreSQL persistence, Redis cache/rate-limit/SSE pub-sub, RabbitMQ dispatch, Alembic migrations, and a worker.

## Run

```bash
docker compose up --build
```
The API is at `http://localhost:8000`, RabbitMQ UI at `http://localhost:15672` (`guest`/`guest`). The API container runs `alembic upgrade head` on startup. Manual migrations: `docker compose run --rm api alembic upgrade head`.

## Admin user
Register with role admin in trusted local/dev environments:
```bash
curl -X POST localhost:8000/auth/register -H 'Content-Type: application/json' -d '{"email":"admin@example.com","password":"password123","role":"admin"}'
```

## Examples
```bash
curl -X POST localhost:8000/auth/register -H 'Content-Type: application/json' -d '{"email":"u@example.com","password":"password123"}'
TOKEN=$(curl -s -X POST localhost:8000/auth/login -H 'Content-Type: application/json' -d '{"email":"u@example.com","password":"password123"}' | python -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
curl -X POST localhost:8000/jobs -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -H 'Idempotency-Key: demo-1' -d '{"payload":{"task_type":"demo","duration_seconds":2,"should_fail":false}}'
curl -H "Authorization: Bearer $TOKEN" localhost:8000/jobs
curl -N -H "Authorization: Bearer $TOKEN" localhost:8000/jobs/1/events
curl -H "Authorization: Bearer $TOKEN" localhost:8000/jobs/1/logs
curl -X POST -H "Authorization: Bearer $TOKEN" localhost:8000/jobs/1/cancel
```

## Idempotency, rate limits, pagination
Repeat `POST /jobs` with the same `Idempotency-Key` to receive the original job. `POST /jobs` is limited to 10 requests/minute/user when Redis is available; if Redis is down requests are allowed and documented as degraded. Use `GET /jobs?limit=10&cursor=<next_cursor>` for stable cursor pagination.

## Environment
`DATABASE_URL`, `REDIS_URL`, `RABBITMQ_URL`, `JWT_SECRET_KEY`, `ACCESS_TOKEN_EXPIRE_MINUTES`, `JOBS_QUEUE_NAME`, `RATE_LIMIT_PER_MINUTE`.

## Testing and troubleshooting
Install dev deps: `pip install -e '.[test]'`; run `pytest`. Check container logs with `docker compose logs api worker`. If RabbitMQ is down, job creation persists but returns 503 because dispatch failed. If Redis is down, cache, rate limiting, and SSE pub/sub degrade while PostgreSQL-backed APIs continue.
