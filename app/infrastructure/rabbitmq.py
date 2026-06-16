import json, aio_pika
from app.core.config import get_settings
async def publish_job(job_id:int) -> None:
    s=get_settings(); conn=await aio_pika.connect_robust(s.rabbitmq_url)
    async with conn:
        ch=await conn.channel(); q=await ch.declare_queue(s.jobs_queue_name, durable=True)
        await ch.default_exchange.publish(aio_pika.Message(json.dumps({'job_id':job_id}).encode(), delivery_mode=aio_pika.DeliveryMode.PERSISTENT), routing_key=q.name)
async def get_connection(): return await aio_pika.connect_robust(get_settings().rabbitmq_url)
