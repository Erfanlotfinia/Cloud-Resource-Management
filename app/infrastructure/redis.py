import json
from redis.asyncio import Redis
from app.core.config import get_settings
_client: Redis|None=None
def get_redis() -> Redis:
    global _client
    if _client is None: _client=Redis.from_url(get_settings().redis_url, decode_responses=True)
    return _client
async def safe_get(key: str):
    try: return await get_redis().get(key)
    except Exception: return None
async def safe_setex(key: str, ttl: int, value: str):
    try: await get_redis().setex(key, ttl, value)
    except Exception: pass
async def invalidate_pattern(pattern: str):
    try:
        async for key in get_redis().scan_iter(match=pattern): await get_redis().delete(key)
    except Exception: pass
async def publish_status(job_id:int, status:str):
    try: await get_redis().publish(f'job:{job_id}:events', json.dumps({'job_id':job_id,'status':status}))
    except Exception: pass
