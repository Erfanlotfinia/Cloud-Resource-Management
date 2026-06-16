import asyncio, json, os, uuid
from datetime import datetime, timedelta, timezone
from sqlalchemy import func, select
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.infrastructure.database import SessionLocal
from app.infrastructure.rabbitmq import configure_topology, get_connection
from app.infrastructure.redis import invalidate_pattern, publish_status
from app.models.job import Job, JobLog, JobStatus, LogLevel, OutboxEvent
from app.models.user import User

WORKER_ID = os.getenv('WORKER_ID', f"worker-{uuid.uuid4()}")
log = get_logger(__name__)

class RetryableExecutionError(Exception): pass
class NonRetryableExecutionError(Exception): pass

async def execute(payload: dict) -> dict:
    await asyncio.sleep(float(payload.get('duration_seconds', 1)))
    if payload.get('non_retryable_fail'): raise NonRetryableExecutionError('non retryable job failure')
    if payload.get('should_fail'): raise RetryableExecutionError('simulated job failure')
    return {'ok': True, 'task_type': payload.get('task_type', 'demo')}

def lease_deadline(): return datetime.now(timezone.utc) + timedelta(seconds=get_settings().job_lease_seconds)
def retry_delay(retry_count:int): return min(300, get_settings().job_retry_base_delay_seconds * (2 ** max(retry_count-1, 0)))

async def enqueue_outbox(session, job: Job, event_type='job.queued'):
    session.add(OutboxEvent(event_type=event_type, payload={'job_id': job.id, 'user_id': job.owner_id, 'correlation_id': job.correlation_id}))

async def trigger_next(owner_id: int) -> None:
    async with SessionLocal() as session:
        await session.scalar(select(User.id).where(User.id == owner_id).with_for_update())
        running = await session.scalar(select(func.count()).select_from(Job).where(Job.owner_id == owner_id, Job.status == JobStatus.running))
        if (running or 0) >= get_settings().jobs_per_user_running_limit: return
        job = await session.scalar(select(Job).where(Job.owner_id == owner_id, Job.status == JobStatus.pending).order_by(Job.created_at, Job.id).with_for_update(skip_locked=True).limit(1))
        if job is None: return
        job.status = JobStatus.queued
        session.add(JobLog(job_id=job.id, message='queued job promoted via outbox'))
        await enqueue_outbox(session, job)
        await session.commit(); await invalidate_pattern('jobs:*')
        await publish_status(job.id, 'queued', user_id=job.owner_id, correlation_id=job.correlation_id)

async def claim_job(job_id: int) -> tuple[int, dict, str] | None:
    now = datetime.now(timezone.utc)
    async with SessionLocal() as session:
        job = await session.scalar(select(Job).where(Job.id == job_id, Job.status == JobStatus.queued, ((Job.locked_by.is_(None)) | (Job.lock_expires_at < now))).with_for_update(skip_locked=True))
        if job is None: return None
        await session.scalar(select(User.id).where(User.id == job.owner_id).with_for_update())
        running = await session.scalar(select(func.count()).select_from(Job).where(Job.owner_id == job.owner_id, Job.status == JobStatus.running))
        if (running or 0) >= get_settings().jobs_per_user_running_limit:
            job.status = JobStatus.pending; job.locked_by = None; job.lock_expires_at = None
            session.add(JobLog(job_id=job.id, level=LogLevel.warning, message='deferred by concurrency limit'))
            await session.commit(); await invalidate_pattern('jobs:*')
            await publish_status(job.id, 'pending', user_id=job.owner_id, correlation_id=job.correlation_id, worker_id=WORKER_ID)
            return None
        job.status = JobStatus.running; job.locked_by = WORKER_ID; job.lock_expires_at = lease_deadline(); job.started_at = now
        session.add(JobLog(job_id=job.id, message=f'job lease acquired by {WORKER_ID}'))
        await session.commit(); await invalidate_pattern('jobs:*')
        await publish_status(job.id, 'running', user_id=job.owner_id, correlation_id=job.correlation_id, worker_id=WORKER_ID)
        log.info('job_claimed', job_id=job.id, user_id=job.owner_id, event_type='job_claimed', worker_id=WORKER_ID, correlation_id=job.correlation_id)
        return job.owner_id, job.payload, job.correlation_id

async def renew_lease(job_id:int, stop:asyncio.Event):
    while not stop.is_set():
        await asyncio.sleep(max(1, get_settings().job_lease_seconds // 3))
        async with SessionLocal() as session:
            job = await session.scalar(select(Job).where(Job.id == job_id, Job.status == JobStatus.running, Job.locked_by == WORKER_ID).with_for_update())
            if job is None: return
            job.lock_expires_at = lease_deadline(); await session.commit()

async def process(job_id: int) -> None:
    claimed = await claim_job(job_id)
    if claimed is None: return
    owner_id, payload, correlation_id = claimed
    stop = asyncio.Event(); renew_task = asyncio.create_task(renew_lease(job_id, stop))
    try:
        result = await execute(payload)
        async with SessionLocal() as session:
            job = await session.scalar(select(Job).where(Job.id == job_id, Job.status == JobStatus.running, Job.locked_by == WORKER_ID).with_for_update())
            if not job: return
            job.status = JobStatus.completed; job.result = result; job.completed_at = datetime.now(timezone.utc); job.locked_by = None; job.lock_expires_at = None
            session.add(JobLog(job_id=job.id, message='job completed'))
            await session.commit(); await invalidate_pattern('jobs:*')
            await publish_status(job.id, 'completed', user_id=owner_id, correlation_id=correlation_id, worker_id=WORKER_ID)
            log.info('job_completed', job_id=job.id, user_id=owner_id, event_type='job_completed', worker_id=WORKER_ID, correlation_id=correlation_id)
    except NonRetryableExecutionError as exc:
        await fail_job(job_id, owner_id, correlation_id, exc, retryable=False)
    except Exception as exc:
        await fail_job(job_id, owner_id, correlation_id, exc, retryable=True)
    finally:
        stop.set(); await renew_task; await trigger_next(owner_id)

async def fail_job(job_id:int, owner_id:int, correlation_id:str, exc:Exception, retryable:bool):
    async with SessionLocal() as session:
        job = await session.scalar(select(Job).where(Job.id == job_id, Job.locked_by == WORKER_ID).with_for_update())
        if job is None or job.status == JobStatus.cancelled: return
        job.retry_count += 1; job.error_message = str(exc); job.locked_by = None; job.lock_expires_at = None
        if retryable and job.retry_count <= job.max_retries:
            job.status = JobStatus.queued
            session.add(JobLog(job_id=job.id, level=LogLevel.warning, message=f'retry scheduled {job.retry_count} after {retry_delay(job.retry_count)}s'))
            event = OutboxEvent(event_type='job.retry_queued', payload={'job_id': job.id, 'user_id': owner_id, 'correlation_id': correlation_id})
            event.next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=retry_delay(job.retry_count))
            session.add(event)
            status = 'queued'
        else:
            job.status = JobStatus.failed; job.completed_at = datetime.now(timezone.utc)
            session.add(JobLog(job_id=job.id, level=LogLevel.error, message=f'job failed: {type(exc).__name__}'))
            status = 'failed'
        await session.commit(); await invalidate_pattern('jobs:*')
        await publish_status(job.id, status, user_id=owner_id, correlation_id=correlation_id, worker_id=WORKER_ID)
        log.warning('job_execution_failed', job_id=job.id, user_id=owner_id, event_type='job_execution_failed', worker_id=WORKER_ID, correlation_id=correlation_id, retryable=retryable, retry_count=job.retry_count, error=str(exc))

async def recover_expired_leases_once() -> int:
    now = datetime.now(timezone.utc)
    async with SessionLocal() as session:
        jobs = list((await session.scalars(select(Job).where(Job.status == JobStatus.running, Job.lock_expires_at < now).with_for_update(skip_locked=True).limit(100))).all())
        for job in jobs:
            job.retry_count += 1; job.status = JobStatus.queued if job.retry_count <= job.max_retries else JobStatus.failed; job.locked_by = None; job.lock_expires_at = None
            session.add(JobLog(job_id=job.id, level=LogLevel.warning, message='expired lease recovered'))
            if job.status == JobStatus.queued: await enqueue_outbox(session, job, 'job.lease_recovered')
            else: job.completed_at = now
            log.warning('job_lease_recovered', job_id=job.id, user_id=job.owner_id, event_type='job_lease_recovered', worker_id=WORKER_ID, correlation_id=job.correlation_id)
        await session.commit()
        return len(jobs)

async def recovery_loop():
    while True:
        await recover_expired_leases_once(); await asyncio.sleep(max(5, get_settings().job_lease_seconds // 2))

async def main() -> None:
    configure_logging(); asyncio.create_task(recovery_loop())
    connection = await get_connection(); channel = await connection.channel(); await channel.set_qos(prefetch_count=5); await configure_topology(channel)
    queue = await channel.declare_queue(get_settings().jobs_queue_name, durable=True)
    async with queue.iterator() as iterator:
        async for message in iterator:
            async with message.process(requeue=False):
                body = json.loads(message.body.decode())
                await process(int(body['job_id']))

if __name__ == '__main__': asyncio.run(main())
