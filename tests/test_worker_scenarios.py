from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.config import get_settings
from app.main import app
from app.models.job import Job, JobStatus, OutboxEvent, OutboxStatus
from app.workers import outbox_publisher, runner
from tests.conftest import auth_headers, create_job, token


async def test_non_retryable_failure_fails_immediately(client):
    user_token = await token(client, "nonretry@example.com")
    headers = auth_headers(user_token)
    created = await client.post(
        "/jobs",
        headers=headers,
        json={"payload": {"duration_seconds": 0, "non_retryable_fail": True}, "max_retries": 3},
    )
    job_id = created.json()["id"]

    await runner.process(job_id)

    details = (await client.get(f"/jobs/{job_id}", headers=headers)).json()
    assert details["status"] == "failed"
    assert details["retry_count"] == 1
    assert "non retryable" in details["error_message"]


async def test_worker_process_missing_job_is_noop(client):
    await runner.process(99999)


async def test_worker_defers_job_when_concurrency_limit_reached(client):
    user_token = await token(client, "defer@example.com")
    headers = auth_headers(user_token)
    created = []
    for _ in range(4):
        created.append((await client.post("/jobs", headers=headers, json={"payload": {}})).json())

    queued_ids = [job["id"] for job in created if job["status"] == "queued"]
    pending_id = next(job["id"] for job in created if job["status"] == "pending")

    async with runner.SessionLocal() as session:
        for job_id in queued_ids:
            job = await session.get(Job, job_id)
            job.status = JobStatus.running
            job.locked_by = "other-worker"
            job.lock_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        pending = await session.get(Job, pending_id)
        pending.status = JobStatus.queued
        await session.commit()

    await runner.process(pending_id)

    details = (await client.get(f"/jobs/{pending_id}", headers=headers)).json()
    assert details["status"] == "pending"
    logs = (await client.get(f"/jobs/{pending_id}/logs", headers=headers)).json()
    assert any("deferred by concurrency limit" in log["message"] for log in logs)


async def test_expired_lease_recovery_requeues_job(client):
    user_token = await token(client, "lease@example.com")
    headers = auth_headers(user_token)
    job_id = (await create_job(client, user_token, payload={}, max_retries=2)).json()["id"]

    async with runner.SessionLocal() as session:
        job = await session.get(Job, job_id)
        job.status = JobStatus.running
        job.locked_by = "stale-worker"
        job.lock_expires_at = datetime.now(timezone.utc) - timedelta(seconds=30)
        await session.commit()

    recovered = await runner.recover_expired_leases_once()
    assert recovered == 1

    details = (await client.get(f"/jobs/{job_id}", headers=headers)).json()
    assert details["status"] == "queued"
    assert details["retry_count"] == 1


async def test_completion_promotes_next_pending_job(client):
    user_token = await token(client, "promote-on-complete@example.com")
    headers = auth_headers(user_token)
    created = []
    for _ in range(4):
        created.append((await client.post("/jobs", headers=headers, json={"payload": {"duration_seconds": 0}})).json())

    first_queued = next(job for job in created if job["status"] == "queued")
    pending = next(job for job in created if job["status"] == "pending")

    await runner.process(first_queued["id"])

    pending_details = (await client.get(f"/jobs/{pending['id']}", headers=headers)).json()
    assert pending_details["status"] == "queued"


async def test_outbox_publish_success_marks_event_sent(client, monkeypatch):
    user_token = await token(client, "outbox-ok@example.com")
    await create_job(client, user_token, payload={})

    published = []

    async def ok_publish(*args, **kwargs):
        published.append(args)

    monkeypatch.setattr(outbox_publisher, "publish_job", ok_publish)

    sent = await outbox_publisher.publish_pending_once()
    assert sent == 1
    assert len(published) == 1

    async with runner.SessionLocal() as session:
        event = await session.scalar(select(OutboxEvent))
        assert event.status == OutboxStatus.sent
        assert event.sent_at is not None


async def test_outbox_max_retries_marks_event_failed(client, monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("OUTBOX_MAX_RETRIES", "2")

    user_token = await token(client, "outbox-max@example.com")
    await create_job(client, user_token, payload={})

    async def failing_publish(*args, **kwargs):
        raise RuntimeError("broker down")

    monkeypatch.setattr(outbox_publisher, "publish_job", failing_publish)

    await outbox_publisher.publish_pending_once()

    async with runner.SessionLocal() as session:
        event = await session.scalar(select(OutboxEvent))
        event.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await session.commit()

    await outbox_publisher.publish_pending_once()

    async with runner.SessionLocal() as session:
        event = await session.scalar(select(OutboxEvent))
        assert event.status == OutboxStatus.failed
        assert event.retry_count == 2

    get_settings.cache_clear()
