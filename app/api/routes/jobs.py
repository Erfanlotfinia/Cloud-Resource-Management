import asyncio, json
from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from app.api.dependencies import current_user
from app.infrastructure.database import get_session
from app.infrastructure.redis import get_redis
from app.models.user import User
from app.schemas.job import JobCreate, JobResponse, JobListResponse, JobLogResponse
from app.services import jobs as svc


router=APIRouter(prefix='/jobs', tags=['jobs'])

@router.post('', response_model=JobResponse, status_code=201)
async def create(data:JobCreate, idempotency_key:str|None=Header(default=None, alias='Idempotency-Key'), user:User=Depends(current_user), session:AsyncSession=Depends(get_session)): 
    return await svc.create_job(data,user,session,idempotency_key)


@router.get('', response_model=JobListResponse)
async def list_(cursor:str|None=None, limit:int=20, user:User=Depends(current_user), session:AsyncSession=Depends(get_session)): 
    return await svc.list_jobs(user,session,cursor,limit)


@router.get('/{job_id}', response_model=JobResponse)
async def get(job_id:int,user:User=Depends(current_user),session:AsyncSession=Depends(get_session)):
    return await svc.get_job(job_id,user,session)


@router.post('/{job_id}/cancel', response_model=JobResponse)
async def cancel(job_id:int,user:User=Depends(current_user),session:AsyncSession=Depends(get_session)): 
    return await svc.cancel_job(job_id,user,session)


@router.get('/{job_id}/logs', response_model=list[JobLogResponse])
async def logs(job_id:int,user:User=Depends(current_user),session:AsyncSession=Depends(get_session)):
    return await svc.logs(job_id,user,session)


@router.get('/{job_id}/events')
async def events(job_id:int, request:Request, user:User=Depends(current_user), session:AsyncSession=Depends(get_session)):
    job = await svc.get_job(job_id,user,session)
    async def gen():
        pubsub = None
        try:
            pubsub=get_redis().pubsub(); await pubsub.subscribe(f'job:{job_id}:events')
            yield f"event: snapshot\ndata: {json.dumps({'job_id': job.id, 'user_id': job.owner_id, 'status': job.status.value, 'event_type': 'job_snapshot', 'correlation_id': job.correlation_id})}\n\n"
            while not await request.is_disconnected():
                msg=await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if msg: yield f"data: {msg['data']}\n\n"
                else: yield ': heartbeat\n\n'
        except Exception:
            while not await request.is_disconnected():
                fresh = await svc.get_job(job_id, user, session)
                yield f"event: poll\ndata: {json.dumps({'job_id': fresh.id, 'user_id': fresh.owner_id, 'status': fresh.status.value, 'event_type': 'job_status_poll', 'correlation_id': fresh.correlation_id})}\n\n"
                await asyncio.sleep(5)
        finally:
            if pubsub: await pubsub.unsubscribe(f'job:{job_id}:events'); await pubsub.close()
    return StreamingResponse(gen(), media_type='text/event-stream')
