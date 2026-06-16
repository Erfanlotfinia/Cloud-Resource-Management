import asyncio
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.infrastructure.database import SessionLocal
from app.infrastructure.rabbitmq import publish_job
from app.models.job import OutboxEvent, OutboxStatus

log = get_logger(__name__)

def backoff_seconds(retry_count: int) -> int:
    return min(300, get_settings().job_retry_base_delay_seconds * (2 ** max(retry_count - 1, 0)))

async def publish_pending_once(limit: int = 50) -> int:
    now = datetime.now(timezone.utc)
    async with SessionLocal() as session:
        events = list((await session.scalars(select(OutboxEvent).where(OutboxEvent.status == OutboxStatus.pending, OutboxEvent.next_attempt_at <= now).order_by(OutboxEvent.created_at, OutboxEvent.id).with_for_update(skip_locked=True).limit(limit))).all())
        count = 0
        for event in events:
            try:
                await publish_job(int(event.payload['job_id']), event.payload.get('correlation_id'), event.id)
                event.status = OutboxStatus.sent
                event.sent_at = datetime.now(timezone.utc)
                event.last_error = None
                count += 1
                log.info('outbox_event_sent', job_id=event.payload.get('job_id'), user_id=event.payload.get('user_id'), event_type=event.event_type, correlation_id=event.payload.get('correlation_id'), outbox_event_id=event.id)
            except Exception as exc:
                event.retry_count += 1
                event.last_error = str(exc)
                if event.retry_count >= get_settings().outbox_max_retries:
                    event.status = OutboxStatus.failed
                else:
                    event.next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=backoff_seconds(event.retry_count))
                log.warning('outbox_event_publish_failed', job_id=event.payload.get('job_id'), user_id=event.payload.get('user_id'), event_type=event.event_type, correlation_id=event.payload.get('correlation_id'), outbox_event_id=event.id, error=str(exc), retry_count=event.retry_count)
        await session.commit()
        return count

async def main() -> None:
    configure_logging()
    while True:
        await publish_pending_once()
        await asyncio.sleep(get_settings().outbox_poll_interval_seconds)

if __name__ == '__main__':
    asyncio.run(main())
