from sqlalchemy import select

from app.main import app
from app.models.job import OutboxEvent, OutboxStatus
from app.workers import outbox_publisher, runner
from tests.conftest import auth_headers, token


async def test_register_login_create_idempotent_cancel_logs_and_pagination(client):
    user_token = await token(client)
    headers = auth_headers(user_token)
    response = await client.post(
        "/jobs",
        headers={**headers, "Idempotency-Key": "k1"},
        json={"payload": {"duration_seconds": 0}},
    )
    assert response.status_code == 201
    job_id = response.json()["id"]
    duplicate = await client.post(
        "/jobs",
        headers={**headers, "Idempotency-Key": "k1"},
        json={"payload": {"duration_seconds": 0}},
    )
    assert duplicate.json()["id"] == job_id
    assert (await client.get("/jobs?limit=1", headers=headers)).json()["items"]
    assert (await client.post(f"/jobs/{job_id}/cancel", headers=headers)).json()["status"] == "cancelled"
    assert (await client.get(f"/jobs/{job_id}/logs", headers=headers)).status_code == 200


async def test_authz_admin_and_public_admin_registration_blocked(client):
    user1 = await token(client, "a@example.com")
    user2 = await token(client, "b@example.com")
    blocked_admin = await client.post(
        "/auth/register", json={"email": "bad-admin@example.com", "password": "password123", "role": "admin"}
    )
    assert blocked_admin.status_code == 403
    admin = await token(client, "admin@example.com", "admin")
    created = await client.post("/jobs", headers=auth_headers(user1), json={"payload": {}})
    job_id = created.json()["id"]
    assert (await client.get(f"/jobs/{job_id}", headers=auth_headers(user2))).status_code == 403
    assert (await client.get(f"/jobs/{job_id}", headers=auth_headers(admin))).status_code == 200


async def test_create_job_reserves_dispatch_slots_and_rate_limits(client):
    user_token = await token(client, "slots@example.com")
    headers = auth_headers(user_token)
    statuses = []
    for _ in range(4):
        response = await client.post("/jobs", headers=headers, json={"payload": {}})
        assert response.status_code == 201
        statuses.append(response.json()["status"])
    assert statuses.count("queued") == 3
    assert statuses.count("pending") == 1
    assert await app.state.count_outbox() == 3
    for _ in range(6):
        assert (await client.post("/jobs", headers=headers, json={"payload": {}})).status_code == 201
    assert (await client.post("/jobs", headers=headers, json={"payload": {}})).status_code == 429


async def test_worker_success_and_retry_failure_paths(client):
    user_token = await token(client, "worker@example.com")
    headers = auth_headers(user_token)
    ok = await client.post("/jobs", headers=headers, json={"payload": {"duration_seconds": 0}})
    await runner.process(ok.json()["id"])
    assert (await client.get(f"/jobs/{ok.json()['id']}", headers=headers)).json()["status"] == "completed"

    failing = await client.post(
        "/jobs", headers=headers, json={"payload": {"duration_seconds": 0, "should_fail": True}, "max_retries": 1}
    )
    fail_id = failing.json()["id"]
    await runner.process(fail_id)
    await runner.process(fail_id)
    details = (await client.get(f"/jobs/{fail_id}", headers=headers)).json()
    assert details["status"] == "failed"
    assert details["retry_count"] == 2


async def test_trigger_next_promotes_pending_via_outbox(client):
    user_token = await token(client, "promotion@example.com")
    headers = auth_headers(user_token)
    created = []
    for _ in range(4):
        response = await client.post("/jobs", headers=headers, json={"payload": {}})
        assert response.status_code == 201
        created.append(response.json())
    pending_job = next(job for job in created if job["status"] == "pending")

    await runner.trigger_next(pending_job["owner_id"])

    details = (await client.get(f"/jobs/{pending_job['id']}", headers=headers)).json()
    assert details["status"] == "queued"
    assert await app.state.count_outbox() == 4
    logs = (await client.get(f"/jobs/{pending_job['id']}/logs", headers=headers)).json()
    assert any("queued job promoted via outbox" in log["message"] for log in logs)


async def test_outbox_publish_failure_keeps_event_pending(client, monkeypatch):
    user_token = await token(client, "outbox-failure@example.com")
    headers = auth_headers(user_token)
    response = await client.post("/jobs", headers=headers, json={"payload": {}})
    assert response.status_code == 201

    async def failing_publish(*args, **kwargs):
        raise RuntimeError("broker down")

    monkeypatch.setattr(outbox_publisher, "publish_job", failing_publish)

    sent = await outbox_publisher.publish_pending_once()
    assert sent == 0

    async with runner.SessionLocal() as session:
        event = await session.scalar(select(OutboxEvent))
        assert event is not None
        assert event.status == OutboxStatus.pending
        assert event.retry_count == 1
