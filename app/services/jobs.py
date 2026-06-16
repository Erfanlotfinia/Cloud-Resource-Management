import base64
import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.infrastructure import redis as redisinfra
from app.models.job import Job, JobLog, JobStatus, LogLevel, OutboxEvent
from app.models.user import User, UserRole

log = get_logger(__name__)


def encode_cursor(created_at: datetime, job_id: int) -> str:
    payload = {"created_at": created_at.isoformat(), "id": job_id}
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime, int]:
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
        return datetime.fromisoformat(data["created_at"]), int(data["id"])
    except Exception as exc:
        raise AppError("invalid_cursor", "Invalid pagination cursor", 400) from exc


async def check_access(job: Job | None, user: User) -> None:
    if job is None:
        raise AppError("job_not_found", "Job not found", 404)
    if user.role != UserRole.admin and job.owner_id != user.id:
        raise AppError("forbidden", "Forbidden", 403)


async def rate_limit(user_id: int) -> None:
    key = f"rl:create_job:{user_id}"
    try:
        redis = redisinfra.get_redis()
        value = await redis.incr(key)
        if value == 1:
            await redis.expire(key, 60)
        if value > get_settings().rate_limit_per_minute:
            raise AppError("rate_limited", "Rate limit exceeded", 429)
    except AppError:
        raise
    except Exception as exc:
        log.warning("rate_limit_bypassed_due_to_redis_failure", user_id=user_id, event_type="rate_limit_bypassed", error=str(exc))


async def create_job(data, user: User, session: AsyncSession, idem: str | None) -> Job:
    await rate_limit(user.id)
    await session.scalar(select(User.id).where(User.id == user.id).with_for_update())
    if idem:
        existing = await session.scalar(select(Job).where(Job.owner_id == user.id, Job.idempotency_key == idem))
        if existing is not None:
            return existing

    reserved = await session.scalar(select(func.count()).select_from(Job).where(Job.owner_id == user.id, Job.status.in_([JobStatus.queued, JobStatus.running])))
    should_queue = (reserved or 0) < get_settings().jobs_per_user_running_limit
    correlation_id = str(uuid.uuid4())
    job = Job(owner_id=user.id, status=JobStatus.queued if should_queue else JobStatus.pending, payload=data.payload, max_retries=data.max_retries, idempotency_key=idem, correlation_id=correlation_id)
    session.add(job)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        if idem:
            existing = await session.scalar(select(Job).where(Job.owner_id == user.id, Job.idempotency_key == idem))
            if existing is not None:
                return existing
        raise
    session.add(JobLog(job_id=job.id, level=LogLevel.info, message="job created"))
    if should_queue:
        session.add(OutboxEvent(event_type="job.queued", payload={"job_id": job.id, "user_id": user.id, "correlation_id": correlation_id}))
    await session.commit()
    await session.refresh(job)
    await redisinfra.invalidate_pattern("jobs:*")
    await redisinfra.publish_status(job.id, job.status.value, user_id=user.id, correlation_id=correlation_id)
    log.info("job_created", job_id=job.id, user_id=user.id, event_type="job_created", correlation_id=correlation_id)
    return job


def job_to_dict(job: Job) -> dict:
    return {field: getattr(job, field) for field in ["id", "owner_id", "status", "payload", "result", "error_message", "retry_count", "max_retries", "idempotency_key", "correlation_id", "locked_by", "lock_expires_at", "started_at", "completed_at", "cancelled_at", "created_at", "updated_at"]}


async def list_jobs(user: User, session: AsyncSession, cursor: str | None, limit: int) -> dict:
    limit = min(max(limit, 1), 100)
    scope = "admin" if user.role == UserRole.admin else f"user:{user.id}"
    key = f"jobs:{scope}:{cursor or 'first'}:{limit}"
    cached = await redisinfra.safe_get(key)
    if cached:
        log.info("jobs_cache_hit", key=key, event_type="cache_hit")
        return json.loads(cached)
    log.info("jobs_cache_miss", key=key, event_type="cache_miss")
    stmt = select(Job)
    if user.role != UserRole.admin:
        stmt = stmt.where(Job.owner_id == user.id)
    if cursor:
        created_at, job_id = decode_cursor(cursor)
        stmt = stmt.where(or_(Job.created_at < created_at, and_(Job.created_at == created_at, Job.id < job_id)))
    rows = list((await session.scalars(stmt.order_by(desc(Job.created_at), desc(Job.id)).limit(limit + 1))).all())
    has_more = len(rows) > limit
    items = rows[:limit]
    data = {"items": [job_to_dict(job) for job in items], "next_cursor": encode_cursor(items[-1].created_at, items[-1].id) if has_more and items else None, "has_more": has_more}
    await redisinfra.safe_setex(key, get_settings().jobs_cache_ttl_seconds, json.dumps(data, default=str))
    return data

async def get_job(job_id: int, user: User, session: AsyncSession) -> Job:
    job = await session.get(Job, job_id)
    await check_access(job, user)
    return job

async def cancel_job(job_id: int, user: User, session: AsyncSession) -> Job:
    job = await get_job(job_id, user, session)
    if job.status in [JobStatus.completed, JobStatus.failed, JobStatus.cancelled]:
        return job
    job.status = JobStatus.cancelled
    job.cancelled_at = datetime.now(timezone.utc)
    job.locked_by = None
    job.lock_expires_at = None
    session.add(JobLog(job_id=job.id, message="job cancelled"))
    await session.commit()
    await session.refresh(job)
    await redisinfra.invalidate_pattern("jobs:*")
    await redisinfra.publish_status(job.id, job.status.value, user_id=job.owner_id, correlation_id=job.correlation_id)
    return job

async def logs(job_id: int, user: User, session: AsyncSession) -> list[JobLog]:
    await get_job(job_id, user, session)
    return (await session.scalars(select(JobLog).where(JobLog.job_id == job_id).order_by(JobLog.created_at, JobLog.id))).all()
