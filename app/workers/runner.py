import asyncio
import json
from datetime import datetime, timezone

from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.infrastructure.database import SessionLocal
from app.infrastructure.rabbitmq import get_connection, publish_job
from app.infrastructure.redis import invalidate_pattern, publish_status
from app.models.job import Job, JobLog, JobStatus, LogLevel
from app.models.user import User

log = get_logger(__name__)


async def execute(payload: dict) -> dict:
    await asyncio.sleep(float(payload.get("duration_seconds", 1)))
    if payload.get("should_fail"):
        raise RuntimeError("simulated job failure")
    return {"ok": True, "task_type": payload.get("task_type", "demo")}


async def mark_queued_and_publish(job_id: int, *, failure_message: str) -> bool:
    """Publish a queued job and restore it to pending if RabbitMQ publish fails."""
    try:
        await publish_job(job_id)
        await publish_status(job_id, "queued")
        return True
    except Exception as exc:
        log.warning("job_publish_failed", job_id=job_id, error=str(exc))
        async with SessionLocal() as session:
            job = await session.scalar(select(Job).where(Job.id == job_id).with_for_update())
            if job is not None and job.status == JobStatus.queued:
                job.status = JobStatus.pending
                session.add(
                    JobLog(
                        job_id=job.id,
                        level=LogLevel.error,
                        message=f"{failure_message}: {exc}",
                    )
                )
                await session.commit()
                await invalidate_pattern("jobs:*")
                await publish_status(job.id, "pending")
        return False


async def trigger_next(owner_id: int) -> None:
    promoted_job_id: int | None = None
    async with SessionLocal() as session:
        await session.scalar(select(User.id).where(User.id == owner_id).with_for_update())
        running = await session.scalar(
            select(func.count())
            .select_from(Job)
            .where(Job.owner_id == owner_id, Job.status == JobStatus.running)
        )
        if (running or 0) >= get_settings().jobs_per_user_running_limit:
            return
        job = await session.scalar(
            select(Job)
            .where(Job.owner_id == owner_id, Job.status == JobStatus.pending)
            .order_by(Job.created_at, Job.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if job is None:
            return
        job.status = JobStatus.queued
        session.add(JobLog(job_id=job.id, message="queued job promoted for publishing"))
        await session.commit()
        await invalidate_pattern("jobs:*")
        promoted_job_id = job.id

    if promoted_job_id is not None:
        await mark_queued_and_publish(
            promoted_job_id,
            failure_message="RabbitMQ publish failed after pending job promotion",
        )


async def claim_job(job_id: int) -> tuple[int, dict] | None:
    async with SessionLocal() as session:
        job = await session.scalar(select(Job).where(Job.id == job_id).with_for_update())
        if job is None or job.status != JobStatus.queued:
            return None
        await session.scalar(select(User.id).where(User.id == job.owner_id).with_for_update())
        running = await session.scalar(
            select(func.count())
            .select_from(Job)
            .where(Job.owner_id == job.owner_id, Job.status == JobStatus.running)
        )
        if (running or 0) >= get_settings().jobs_per_user_running_limit:
            job.status = JobStatus.pending
            session.add(JobLog(job_id=job.id, level=LogLevel.warning, message="deferred by concurrency limit"))
            await session.commit()
            await invalidate_pattern("jobs:*")
            await publish_status(job.id, "pending")
            return None
        job.status = JobStatus.running
        job.started_at = datetime.now(timezone.utc)
        session.add(JobLog(job_id=job.id, message="job started"))
        await session.commit()
        await invalidate_pattern("jobs:*")
        await publish_status(job.id, "running")
        return job.owner_id, job.payload


async def process(job_id: int) -> None:
    claimed = await claim_job(job_id)
    if claimed is None:
        return
    owner_id, payload = claimed
    try:
        result = await execute(payload)
        async with SessionLocal() as session:
            job = await session.get(Job, job_id)
            if job and job.status == JobStatus.running:
                job.status = JobStatus.completed
                job.result = result
                job.completed_at = datetime.now(timezone.utc)
                session.add(JobLog(job_id=job.id, message="job completed"))
                await session.commit()
                await invalidate_pattern("jobs:*")
                await publish_status(job.id, "completed")
    except Exception as exc:
        async with SessionLocal() as session:
            job = await session.get(Job, job_id)
            if job is None or job.status == JobStatus.cancelled:
                return
            job.retry_count += 1
            job.error_message = str(exc)
            if job.retry_count <= job.max_retries:
                job.status = JobStatus.queued
                session.add(
                    JobLog(
                        job_id=job.id,
                        level=LogLevel.warning,
                        message=f"retry scheduled {job.retry_count}",
                    )
                )
                retry_job_id = job.id
                await session.commit()
                await invalidate_pattern("jobs:*")
                await mark_queued_and_publish(
                    retry_job_id,
                    failure_message="RabbitMQ publish failed after retry scheduling",
                )
                return
            job.status = JobStatus.failed
            job.completed_at = datetime.now(timezone.utc)
            session.add(JobLog(job_id=job.id, level=LogLevel.error, message="job failed"))
            await session.commit()
            await invalidate_pattern("jobs:*")
            await publish_status(job.id, "failed")
    await trigger_next(owner_id)


async def main() -> None:
    configure_logging()
    connection = await get_connection()
    channel = await connection.channel()
    await channel.set_qos(prefetch_count=5)
    queue = await channel.declare_queue(get_settings().jobs_queue_name, durable=True)
    async with queue.iterator() as iterator:
        async for message in iterator:
            async with message.process(requeue=False):
                body = json.loads(message.body.decode())
                await process(int(body["job_id"]))


if __name__ == "__main__":
    asyncio.run(main())
