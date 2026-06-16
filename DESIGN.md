# Design Notes

## 1. Scaling: 100x request growth

### Bottlenecks

- PostgreSQL write volume for users, jobs, and logs.
- Hot per-user concurrency checks and idempotency lookups.
- Redis connection pressure for cache, rate limiting, and pub/sub.
- RabbitMQ queue depth and broker I/O.
- Worker throughput for long-running jobs.

### API scaling

The API is stateless except for external dependencies, so it can run behind a load balancer with many replicas. Keep JWT validation local, tune DB/Redis/RabbitMQ pools, and preserve thin route handlers.

### Worker scaling

Workers can be scaled horizontally. Each worker claims jobs with database row locks and state checks, so duplicate messages do not produce duplicate execution. Tune RabbitMQ prefetch to match job duration and downstream capacity.

### PostgreSQL scaling

Use stronger instances, connection pooling, read replicas for list/admin reads, partitioning for very large job tables, and careful indexes on owner/status/created_at/idempotency paths.

### Redis scaling

Use Redis Sentinel or Cluster, bounded TTLs, and separate logical databases or key prefixes for cache/rate-limit/pub-sub. Monitor evictions and latency.

### RabbitMQ scaling

Use durable queues, persistent messages, quorum queues for higher availability, broker clustering, queue-length alerts, and dead-letter queues for poison messages.

## 2. RabbitMQ failure

### What happens now?

`POST /jobs` writes the job to PostgreSQL, then publishes to RabbitMQ. If publish fails, the job is moved back to `pending`, an error log is recorded, caches are invalidated, and the API returns `503`.

### Risks

The DB write and broker publish are not atomic. A crash between commit and publish can strand a job. A publish success followed by API failure can also confuse clients unless they use idempotency keys.

### Recommended improvements

Add a transactional outbox table. Job creation and outbox insertion should happen in one database transaction; a relay publishes outbox rows to RabbitMQ and marks them delivered. Add retry/backoff, dead-lettering, and operational dashboards.

## 3. Worker failure

### Current behavior

Messages are acknowledged only after the processing callback returns. The worker marks a queued job as `running`, executes it, then marks it `completed`, `failed`, or `queued` for retry.

### Risks

If a worker crashes after marking a job `running`, RabbitMQ can redeliver the message, but the next worker will skip the job because it is already `running`. That prevents duplicates but can leave stale running jobs.

### Improvements

Add leases and heartbeats (`locked_by`, `lock_expires_at`, `heartbeat_at`). A recovery process should detect stale running jobs, increment retry count when appropriate, and requeue them. Long-running tasks should periodically check cancellation and lease validity.

## 4. Duplicate processing

Multiple workers may receive duplicate messages, but processing is guarded by the database:

- The worker selects the job with `FOR UPDATE`.
- Only `queued` jobs are claimable.
- The worker locks the owner row before checking running-job capacity.
- If capacity exists, the job transitions to `running` in the same critical section.
- Terminal states are idempotent; completed/failed/cancelled/running jobs are skipped.

This prevents two workers from successfully claiming the same queued job. External side effects should still be idempotent in production.

## 5. Redis failure

### Affected features

- `GET /jobs` response caching.
- `POST /jobs` rate limiting.
- Cache invalidation.
- SSE real-time status delivery through pub/sub.

### Degraded behavior

The API falls back to PostgreSQL for normal reads/writes where possible. Rate limiting is skipped when Redis is down to preserve availability, and warnings are logged. SSE cannot reliably deliver events without pub/sub.

### Recommended improvements

Use Redis HA, circuit breakers, metrics, local emergency rate limits, and optional durable event storage for replayable status streams.

## 6. Large-scale database: 100M+ jobs

### Indexing strategy

Keep unique `users.email`, `jobs.owner_id`, `jobs.status`, `jobs.created_at`, `(jobs.owner_id, jobs.created_at)`, partial unique `(owner_id, idempotency_key) WHERE idempotency_key IS NOT NULL`, and `job_logs.job_id`. Consider `(owner_id, created_at DESC, id DESC)` and `(status, created_at)` indexes for common paths.

### Partitioning

Partition jobs and logs by month/quarter or by hash of `owner_id` depending on access patterns. Time partitioning simplifies archival; hash partitioning spreads tenant hot spots.

### Archiving

Move old terminal jobs and logs to cold storage after retention windows. Keep summaries in hot tables if users need historical lists.

### Query optimization

Use cursor pagination on `(created_at DESC, id DESC)` instead of offsets. Avoid unbounded admin scans, select only response fields when needed, and cache stable pages briefly.

### Read replicas

Serve admin/reporting/list views from replicas when read-after-write consistency is not required. Keep writes and state transitions on the primary.

### Cursor pagination impact

Cursor pagination remains efficient at high cardinality because it uses indexed tuple comparisons and does not scan skipped rows like offset pagination.

## Current trade-offs

- Running cancellation is practical but cooperative: the API marks running jobs as `cancelled`, while real task code should periodically check cancellation before doing irreversible work.
- RabbitMQ publish is not transactionally atomic with PostgreSQL yet; the documented production answer is a transactional outbox.
- Stale running job recovery is documented as the next production hardening step.
