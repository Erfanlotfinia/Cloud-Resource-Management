import base64, json
from datetime import datetime, timezone
from sqlalchemy import and_, desc, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status
from app.core.config import get_settings
from app.core.exceptions import AppError
from app.infrastructure import redis as redisinfra
from app.infrastructure.rabbitmq import publish_job
from app.models.job import Job, JobLog, JobStatus, LogLevel
from app.models.user import User, UserRole

def enc(dt, id): return base64.urlsafe_b64encode(json.dumps({'created_at':dt.isoformat(),'id':id}).encode()).decode()
def dec(cur):
    try:
        d=json.loads(base64.urlsafe_b64decode(cur.encode()).decode()); return datetime.fromisoformat(d['created_at']), int(d['id'])
    except Exception: raise AppError('invalid_cursor','Invalid pagination cursor',400)
async def check_access(job:Job|None, user:User):
    if not job: raise AppError('job_not_found','Job not found',404)
    if user.role!=UserRole.admin and job.owner_id!=user.id: raise AppError('forbidden','Forbidden',403)
async def rate_limit(user_id:int):
    r=redisinfra.get_redis(); key=f'rl:create_job:{user_id}'
    try:
        val=await r.incr(key)
        if val==1: await r.expire(key,60)
        if val>get_settings().rate_limit_per_minute: raise AppError('rate_limited','Rate limit exceeded',429)
    except AppError: raise
    except Exception: return
async def create_job(data, user:User, session:AsyncSession, idem:str|None):
    await rate_limit(user.id)
    if idem:
        existing=await session.scalar(select(Job).where(Job.owner_id==user.id, Job.idempotency_key==idem))
        if existing: return existing
    await session.scalar(select(User.id).where(User.id==user.id).with_for_update())
    reserved=await session.scalar(select(func.count()).select_from(Job).where(Job.owner_id==user.id, Job.status.in_([JobStatus.queued, JobStatus.running])))
    should_publish = reserved < get_settings().jobs_per_user_running_limit
    job=Job(owner_id=user.id,status=JobStatus.queued if should_publish else JobStatus.pending,payload=data.payload,max_retries=data.max_retries,idempotency_key=idem)
    session.add(job); await session.flush(); session.add(JobLog(job_id=job.id, level=LogLevel.info, message='job created'))
    await session.commit(); await session.refresh(job); await redisinfra.invalidate_pattern(f'jobs:{user.role}:{user.id}:*')
    if should_publish:
        try: await publish_job(job.id); session.add(JobLog(job_id=job.id, message='job published')); await session.commit(); await redisinfra.publish_status(job.id, job.status.value)
        except Exception as e: job.status=JobStatus.pending; session.add(JobLog(job_id=job.id, level=LogLevel.error, message=f'RabbitMQ publish failed: {e}')); await session.commit(); raise AppError('rabbitmq_unavailable','Job saved but could not be queued for execution',503)
    return job
async def list_jobs(user:User, session:AsyncSession, cursor:str|None, limit:int):
    limit=min(max(limit,1),100); key=f'jobs:{user.role}:{user.id}:{cursor or "first"}:{limit}'; cached=await redisinfra.safe_get(key)
    if cached: return json.loads(cached)
    stmt=select(Job)
    if user.role!=UserRole.admin: stmt=stmt.where(Job.owner_id==user.id)
    if cursor:
        dt,jid=dec(cursor); stmt=stmt.where(or_(Job.created_at<dt, and_(Job.created_at==dt, Job.id<jid)))
    rows=list((await session.scalars(stmt.order_by(desc(Job.created_at), desc(Job.id)).limit(limit+1))).all()); has=len(rows)>limit; items=rows[:limit]
    data={'items':[job_to_dict(j) for j in items], 'next_cursor': enc(items[-1].created_at, items[-1].id) if has and items else None, 'has_more':has}
    await redisinfra.safe_setex(key, get_settings().jobs_cache_ttl_seconds, json.dumps(data, default=str)); return data
def job_to_dict(j): return {c:getattr(j,c) for c in ['id','owner_id','status','payload','result','error_message','retry_count','max_retries','idempotency_key','started_at','completed_at','cancelled_at','created_at','updated_at']}
async def get_job(job_id:int,user:User,session:AsyncSession): job=await session.get(Job,job_id); await check_access(job,user); return job
async def cancel_job(job_id:int,user:User,session:AsyncSession):
    job=await get_job(job_id,user,session)
    if job.status not in [JobStatus.completed, JobStatus.failed, JobStatus.cancelled]:
        job.status=JobStatus.cancelled; job.cancelled_at=datetime.now(timezone.utc); session.add(JobLog(job_id=job.id, message='job cancelled'))
        await session.commit(); await session.refresh(job); await redisinfra.invalidate_pattern('jobs:*'); await redisinfra.publish_status(job.id, job.status.value)
    return job
async def logs(job_id:int,user:User,session:AsyncSession): await get_job(job_id,user,session); return (await session.scalars(select(JobLog).where(JobLog.job_id==job_id).order_by(JobLog.created_at))).all()
