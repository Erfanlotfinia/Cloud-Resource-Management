# Design Notes

## 1. Scaling
At 100x traffic, bottlenecks are DB write throughput, hot per-user job counters, Redis connections, RabbitMQ queue depth, and worker CPU/I/O. Scale API replicas statelessly, workers horizontally with prefetch tuning, PostgreSQL with larger instances/read replicas/partitioning, Redis with Sentinel or Cluster, and RabbitMQ with quorum queues and clustered nodes.

## 2. RabbitMQ Failure
Current behavior persists the job then attempts publish; if publish fails the API logs the error and returns 503. This can leave a queued job not delivered. Recommended improvement is a transactional outbox table written in the same DB transaction, with a relay publishing to RabbitMQ and marking outbox rows delivered.

## 3. Worker Failure
Messages are acked only after processing code returns. If a worker crashes mid-execution RabbitMQ can redeliver, but the DB job may remain `running`, so the next worker skips it. Production should add job leases, heartbeats, stale-running timeout recovery, and idempotent external side effects.

## 4. Duplicate Processing
Workers select the job with `FOR UPDATE`, check terminal/running states, and transition only claimable jobs to `running`. Queued follow-up selection uses `FOR UPDATE SKIP LOCKED`. These state checks make processing idempotent at the DB layer.

## 5. Redis Failure
Affected features: list caching, create-job rate limiting, cache invalidation, and SSE updates. Current degradation allows PostgreSQL APIs to continue, skips rate limiting, and loses real-time delivery. Improve with local emergency limits, Redis HA, and database-backed notifications.

## 6. Large Scale Database
For 100M+ jobs, keep indexes on owner/status/created_at and partial idempotency uniqueness, partition jobs by time or hash(owner_id), archive old terminal jobs to cold storage, optimize queries to covered cursor access paths, use read replicas for admin/list reads, and preserve cursor pagination on `(created_at DESC, id DESC)` to avoid large offsets.

## Trade-offs
Running cancellation is simplified: the API marks a running job cancelled, but the demo executor may complete unless cancellation checks are added inside task code. Concurrency limit is enforced when publishing and when queued jobs are triggered; for strict admission under extreme concurrency, add per-user advisory locks or a counters table updated transactionally.
