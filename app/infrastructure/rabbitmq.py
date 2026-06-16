import json
import aio_pika
from app.core.config import get_settings

def jobs_queue_arguments() -> dict[str, str]:
    s = get_settings()
    return {
        "x-dead-letter-exchange": s.jobs_dead_letter_exchange,
        "x-dead-letter-routing-key": s.jobs_dead_letter_routing_key,
    }

async def configure_topology(channel) -> None:
    s = get_settings()
    dlx = await channel.declare_exchange(s.jobs_dead_letter_exchange, aio_pika.ExchangeType.DIRECT, durable=True)
    await channel.declare_queue(s.jobs_dead_letter_queue, durable=True)
    await (await channel.get_queue(s.jobs_dead_letter_queue)).bind(dlx, routing_key=s.jobs_dead_letter_routing_key)
    await channel.declare_queue(
        s.jobs_queue_name,
        durable=True,
        arguments=jobs_queue_arguments(),
    )

async def publish_job(job_id:int, correlation_id: str | None = None, outbox_event_id: int | None = None) -> None:
    s=get_settings(); conn=await aio_pika.connect_robust(s.rabbitmq_url)
    async with conn:
        ch=await conn.channel(); await configure_topology(ch)
        body={'job_id':job_id, 'correlation_id': correlation_id, 'outbox_event_id': outbox_event_id}
        await ch.default_exchange.publish(
            aio_pika.Message(
                json.dumps(body).encode(),
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                correlation_id=correlation_id,
                message_id=f"job:{job_id}:outbox:{outbox_event_id}" if outbox_event_id else f"job:{job_id}",
            ),
            routing_key=s.jobs_queue_name,
        )
async def get_connection():
    return await aio_pika.connect_robust(get_settings().rabbitmq_url)
