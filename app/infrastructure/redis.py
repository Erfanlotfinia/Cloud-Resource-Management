import json
from redis.asyncio import Redis
from app.core.config import get_settings
from app.core.logging import get_logger


log = get_logger(__name__)
_client: Redis|None=None


def get_redis() -> Redis:
    global _client
    if _client is None: _client=Redis.from_url(get_settings().redis_url, decode_responses=True)
    return _client


async def safe_get(key: str):
    try: 
        return await get_redis().get(key)
    except Exception as exc:
        log.warning('redis_cache_fallback_to_postgres', event_type='redis_cache_failure', error=str(exc)); return None


async def safe_setex(key: str, ttl: int, value: str):
    try: 
        await get_redis().setex(key, ttl, value)
    except Exception as exc: 
        log.warning('redis_cache_write_skipped', event_type='redis_cache_failure', error=str(exc))


async def invalidate_pattern(pattern: str):
    try:
        async for key in get_redis().scan_iter(match=pattern): 
            await get_redis().delete(key)
    except Exception as exc: 
        log.warning('redis_cache_invalidation_skipped', event_type='redis_cache_failure', error=str(exc))


async def publish_status(job_id:int, status:str, user_id:int|None=None, correlation_id:str|None=None, worker_id:str|None=None):
    payload={'job_id':job_id,'user_id':user_id,'status':status,'event_type':'job_status','correlation_id':correlation_id,'worker_id':worker_id}
    try: 
        await get_redis().publish(f'job:{job_id}:events', json.dumps(payload))
    except Exception as exc: 
        log.warning('sse_realtime_push_disabled_due_to_redis_failure', job_id=job_id, user_id=user_id, event_type='sse_degraded', correlation_id=correlation_id, worker_id=worker_id, error=str(exc))
