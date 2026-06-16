from datetime import datetime, timedelta, timezone

from app.main import app
from app.models.job import Job
from tests.conftest import auth_headers, create_job, token


async def test_job_not_found(client):
    user_token = await token(client, "notfound@example.com")
    response = await client.get("/jobs/9999", headers=auth_headers(user_token))
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "job_not_found"


async def test_invalid_cursor(client):
    user_token = await token(client, "badcursor@example.com")
    await create_job(client, user_token, payload={})
    response = await client.get("/jobs?cursor=not-valid", headers=auth_headers(user_token))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_cursor"


async def test_idempotency_isolated_per_user(client):
    user1 = await token(client, "idem1@example.com")
    user2 = await token(client, "idem2@example.com")
    job1 = await create_job(client, user1, payload={"n": 1}, idempotency_key="shared-key")
    job2 = await create_job(client, user2, payload={"n": 2}, idempotency_key="shared-key")
    assert job1.status_code == 201
    assert job2.status_code == 201
    assert job1.json()["id"] != job2.json()["id"]


async def test_pagination_cursor_and_has_more(client):
    user_token = await token(client, "pages@example.com")
    headers = auth_headers(user_token)
    job_ids = []
    for i in range(3):
        job_ids.append((await client.post("/jobs", headers=headers, json={"payload": {"i": i}})).json()["id"])

    base = datetime.now(timezone.utc)
    async with app.state.session_factory() as session:
        for offset, job_id in enumerate(job_ids):
            job = await session.get(Job, job_id)
            job.created_at = base - timedelta(minutes=offset)
        await session.commit()

    first = (await client.get("/jobs?limit=2", headers=headers)).json()
    assert first["has_more"] is True
    assert first["next_cursor"]
    first_ids = {item["id"] for item in first["items"]}

    second = (await client.get(f"/jobs?limit=2&cursor={first['next_cursor']}", headers=headers)).json()
    second_ids = {item["id"] for item in second["items"]}
    assert first_ids.isdisjoint(second_ids)
    assert len(second["items"]) == 1
    assert second["has_more"] is False


async def test_admin_sees_all_users_jobs(client):
    user1 = await token(client, "adminview1@example.com")
    user2 = await token(client, "adminview2@example.com")
    admin = await token(client, "adminview-admin@example.com", role="admin")
    await create_job(client, user1, payload={"owner": 1})
    await create_job(client, user2, payload={"owner": 2})

    admin_list = (await client.get("/jobs?limit=10", headers=auth_headers(admin))).json()
    owner_ids = {item["owner_id"] for item in admin_list["items"]}
    assert len(admin_list["items"]) >= 2
    assert len(owner_ids) >= 2


async def test_cancel_completed_job_is_idempotent(client):
    user_token = await token(client, "canceldone@example.com")
    headers = auth_headers(user_token)
    created = await client.post("/jobs", headers=headers, json={"payload": {"duration_seconds": 0}})
    job_id = created.json()["id"]

    from app.workers import runner

    await runner.process(job_id)
    assert (await client.get(f"/jobs/{job_id}", headers=headers)).json()["status"] == "completed"

    cancelled = (await client.post(f"/jobs/{job_id}/cancel", headers=headers)).json()
    assert cancelled["status"] == "completed"


async def test_cancel_already_cancelled_is_idempotent(client):
    user_token = await token(client, "canceltwice@example.com")
    headers = auth_headers(user_token)
    job_id = (await create_job(client, user_token, payload={})).json()["id"]

    first = (await client.post(f"/jobs/{job_id}/cancel", headers=headers)).json()
    second = (await client.post(f"/jobs/{job_id}/cancel", headers=headers)).json()
    assert first["status"] == "cancelled"
    assert second["status"] == "cancelled"


async def test_pending_job_has_no_outbox_event(client):
    user_token = await token(client, "pending-no-outbox@example.com")
    headers = auth_headers(user_token)
    created = []
    for _ in range(4):
        created.append((await client.post("/jobs", headers=headers, json={"payload": {}})).json())
    pending = next(job for job in created if job["status"] == "pending")
    assert pending["status"] == "pending"
    assert await app.state.count_outbox() == 3


async def test_job_includes_correlation_id_and_logs(client):
    user_token = await token(client, "meta@example.com")
    headers = auth_headers(user_token)
    job = (await create_job(client, user_token, payload={"task_type": "demo"})).json()
    assert job["correlation_id"]

    logs = (await client.get(f"/jobs/{job['id']}/logs", headers=headers)).json()
    assert any(log["message"] == "job created" for log in logs)


async def test_rate_limit_is_per_user(client):
    user_a = await token(client, "rl-a@example.com")
    user_b = await token(client, "rl-b@example.com")
    headers_a = auth_headers(user_a)

    for _ in range(10):
        assert (await client.post("/jobs", headers=headers_a, json={"payload": {}})).status_code == 201
    assert (await client.post("/jobs", headers=headers_a, json={"payload": {}})).status_code == 429

    assert (await client.post("/jobs", headers=auth_headers(user_b), json={"payload": {}})).status_code == 201


async def test_create_job_validation_rejects_invalid_max_retries(client):
    user_token = await token(client, "validation@example.com")
    response = await client.post(
        "/jobs",
        headers=auth_headers(user_token),
        json={"payload": {}, "max_retries": 99},
    )
    assert response.status_code == 422
