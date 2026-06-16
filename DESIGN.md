# Design Notes

## Production reliability model

The API uses PostgreSQL as the source of truth, Redis as an optional acceleration layer, RabbitMQ as an at-least-once work signal, and workers as horizontally scalable executors. All correctness decisions are made in PostgreSQL transactions so broker, cache, and worker failures cannot corrupt job state.

## Transactional outbox for RabbitMQ consistency

`POST /jobs` no longer publishes directly to RabbitMQ. Job creation and an `outbox_events` insert are committed in the same PostgreSQL transaction. A separate outbox publisher process reads pending events with `FOR UPDATE SKIP LOCKED`, publishes durable RabbitMQ messages, and marks events `sent`. If RabbitMQ is down, the outbox row remains pending and is retried with exponential backoff. This removes the crash window where a job is committed but never published.

Publishing is idempotent: RabbitMQ messages include the job id, correlation id, and outbox event id/message id, while the worker still must claim the job in PostgreSQL before execution. Duplicate broker deliveries are safe because the database lease transition is the only authority.

## Worker leasing and duplicate execution prevention

Workers never execute just because a RabbitMQ message arrived. They atomically claim a queued job with a PostgreSQL row lock and the condition `status = queued AND (locked_by IS NULL OR lock_expires_at < now())`. Claiming sets `status = running`, `locked_by = worker_id`, and `lock_expires_at = now + lease_timeout` in the same critical section.

During execution, workers renew the lease. Completion and failure updates require the same `locked_by` value, so a stale worker cannot overwrite a job reclaimed by another worker. RabbitMQ is acknowledged only after the processing callback returns and after the database state transition has committed.

## Atomic max-3 running jobs per user

The per-user running limit is enforced inside PostgreSQL transactions, not in application memory. Workers lock the owner row with `SELECT ... FOR UPDATE`, count current running jobs for that owner, and only then transition one job from queued to running. This serializes concurrent claims for the same user and prevents two workers from simultaneously observing capacity and exceeding the limit.

Pending jobs are promoted to queued only after capacity becomes available. Promotion writes a new outbox event instead of publishing directly.

## Crash recovery

A recovery loop scans for `running` jobs with expired leases. It increments `retry_count`, clears lease fields, and either requeues the job through the outbox or marks it failed once retries are exhausted. This prevents jobs from remaining stuck forever after a worker process, host, or network failure.

Execution failures are separated into retryable and non-retryable paths. Retryable failures schedule a delayed outbox event using exponential backoff; non-retryable failures go directly to `failed`. Retry counts are bounded by `max_retries`, so jobs eventually reach a terminal state.

## Redis degradation modes

Redis is not required for correctness:

- Rate limiting: if Redis is unavailable, the request is allowed and a structured warning `rate_limit_bypassed_due_to_redis_failure` is logged.
- Cache: reads fall back to PostgreSQL and cache writes/invalidations are skipped with warnings.
- SSE: Redis pub/sub failures degrade to polling snapshots over the existing SSE connection, so clients still observe progress without real-time push.

## Exactly-once vs at-least-once tradeoff

The system provides at-least-once message delivery and effectively-once job state transitions. RabbitMQ can redeliver, the outbox publisher can retry, and workers can crash after side effects. PostgreSQL leases prevent two active workers from owning the same job concurrently, but external side effects performed by job code must still be idempotent or guarded by their own idempotency keys for true end-to-end exactly-once behavior.

## Dead-letter handling

RabbitMQ topology declares a durable dead-letter exchange and queue (`jobs.dlx` / `jobs.dead`). The primary jobs queue is configured with dead-letter routing so poison broker messages have a defined destination after broker-side rejection policies or future queue retry policies are enabled. Application retries are primarily represented in PostgreSQL/outbox state for auditability.

## Scaling considerations

- API replicas remain stateless and can scale behind a load balancer.
- Workers scale horizontally because row locks and leases coordinate execution.
- PostgreSQL needs indexes on owner/status/created_at/idempotency/lease paths and may later need partitioning for very large job tables.
- Redis should be run with HA for performance, but its outage only degrades optional behavior.
- RabbitMQ should use durable queues, persistent messages, publisher confirms in stricter deployments, quorum queues, and queue-depth/DLQ alerts.

## Remaining trade-offs

- Running cancellation is cooperative; job implementations must check for cancellation before irreversible work.
- The system is at-least-once at the broker boundary; external side effects still need idempotency.
- Outbox rows marked `failed` require operational alerting/replay tooling.
