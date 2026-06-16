import asyncio, json
from datetime import datetime, timezone
import aio_pika
from sqlalchemy import select
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.infrastructure.database import SessionLocal
from app.infrastructure.rabbitmq import get_connection, publish_job
from app.infrastructure.redis import invalidate_pattern, publish_status
from app.models.job import Job, JobLog, JobStatus, LogLevel
log=get_logger(__name__)
async def execute(payload:dict)->dict:
    await asyncio.sleep(float(payload.get('duration_seconds',1)))
    if payload.get('should_fail'): raise RuntimeError('simulated job failure')
    return {'ok': True, 'task_type': payload.get('task_type','demo')}
async def trigger_next(owner_id:int):
    async with SessionLocal() as s:
        running=await s.scalar(select(__import__('sqlalchemy').func.count()).select_from(Job).where(Job.owner_id==owner_id, Job.status==JobStatus.running))
        if running>=get_settings().jobs_per_user_running_limit: return
        job=await s.scalar(select(Job).where(Job.owner_id==owner_id, Job.status==JobStatus.queued).order_by(Job.created_at).with_for_update(skip_locked=True).limit(1))
        if job: await publish_job(job.id); s.add(JobLog(job_id=job.id, message='queued job published')); await s.commit()
async def process(job_id:int):
    async with SessionLocal() as s:
        job=await s.scalar(select(Job).where(Job.id==job_id).with_for_update())
        if not job or job.status in [JobStatus.cancelled,JobStatus.completed,JobStatus.failed,JobStatus.running]: return
        job.status=JobStatus.running; job.started_at=datetime.now(timezone.utc); s.add(JobLog(job_id=job.id, message='job started')); await s.commit(); await publish_status(job.id,'running')
        owner=job.owner_id
    try:
        result=await execute(job.payload)
        async with SessionLocal() as s:
            job=await s.get(Job, job_id); job.status=JobStatus.completed; job.result=result; job.completed_at=datetime.now(timezone.utc); s.add(JobLog(job_id=job.id, message='job completed')); await s.commit(); await invalidate_pattern('jobs:*'); await publish_status(job.id,'completed')
    except Exception as e:
        async with SessionLocal() as s:
            job=await s.get(Job, job_id); job.retry_count += 1; job.error_message=str(e)
            if job.retry_count <= job.max_retries:
                job.status=JobStatus.queued; s.add(JobLog(job_id=job.id, level=LogLevel.warning, message=f'retry scheduled {job.retry_count}')); await s.commit(); await invalidate_pattern('jobs:*'); await publish_status(job.id,'queued'); await publish_job(job.id); return
            job.status=JobStatus.failed; job.completed_at=datetime.now(timezone.utc); s.add(JobLog(job_id=job.id, level=LogLevel.error, message='job failed')); await s.commit(); await invalidate_pattern('jobs:*'); await publish_status(job.id,'failed')
    await trigger_next(owner)
async def main():
    configure_logging(); conn=await get_connection(); ch=await conn.channel(); await ch.set_qos(prefetch_count=5); q=await ch.declare_queue(get_settings().jobs_queue_name, durable=True)
    async with q.iterator() as it:
        async for msg in it:
            async with msg.process(requeue=False):
                body=json.loads(msg.body.decode()); await process(int(body['job_id']))
if __name__=='__main__': asyncio.run(main())
