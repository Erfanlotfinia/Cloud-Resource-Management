import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.infrastructure import rabbitmq, redis as redisinfra
from app.infrastructure.database import Base, get_session
from app.main import app
from app.models.job import Job, JobStatus
from app.models.user import User
from app.workers import runner


@pytest.fixture
async def client(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async def override_session():
        async with Session() as session:
            yield session

    published = []

    async def fake_publish(job_id: int):
        published.append(job_id)

    async def noop(*args, **kwargs):
        return None

    async def none(*args, **kwargs):
        return None

    class FakeRedis:
        counts = {}

        async def incr(self, key):
            self.counts[key] = self.counts.get(key, 0) + 1
            return self.counts[key]

        async def expire(self, key, ttl):
            return None

    get_settings.cache_clear()
    monkeypatch.setenv("ADMIN_SETUP_TOKEN", "setup-token")
    app.dependency_overrides[get_session] = override_session
    monkeypatch.setattr(rabbitmq, "publish_job", fake_publish)
    monkeypatch.setattr("app.services.jobs.publish_job", fake_publish)
    monkeypatch.setattr("app.workers.runner.publish_job", fake_publish)
    monkeypatch.setattr(redisinfra, "safe_get", none)
    monkeypatch.setattr(redisinfra, "safe_setex", noop)
    monkeypatch.setattr(redisinfra, "invalidate_pattern", noop)
    monkeypatch.setattr(redisinfra, "publish_status", noop)
    monkeypatch.setattr("app.workers.runner.invalidate_pattern", noop)
    monkeypatch.setattr("app.workers.runner.publish_status", noop)
    monkeypatch.setattr(redisinfra, "get_redis", lambda: FakeRedis())
    monkeypatch.setattr(runner, "SessionLocal", Session)
    app.state.published_jobs = published
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()
    get_settings.cache_clear()


async def token(client, email="u@example.com", role="user"):
    headers = {"X-Admin-Setup-Token": "setup-token"} if role == "admin" else {}
    await client.post(
        "/auth/register",
        headers=headers,
        json={"email": email, "password": "password123", "role": role},
    )
    response = await client.post("/auth/login", json={"email": email, "password": "password123"})
    return response.json()["access_token"]


@pytest.mark.asyncio
async def test_register_login_create_idempotent_cancel_logs_and_pagination(client):
    user_token = await token(client)
    headers = {"Authorization": f"Bearer {user_token}"}
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


@pytest.mark.asyncio
async def test_authz_admin_and_public_admin_registration_blocked(client):
    user1 = await token(client, "a@example.com")
    user2 = await token(client, "b@example.com")
    blocked_admin = await client.post(
        "/auth/register", json={"email": "bad-admin@example.com", "password": "password123", "role": "admin"}
    )
    assert blocked_admin.status_code == 403
    admin = await token(client, "admin@example.com", "admin")
    created = await client.post("/jobs", headers={"Authorization": f"Bearer {user1}"}, json={"payload": {}})
    job_id = created.json()["id"]
    assert (await client.get(f"/jobs/{job_id}", headers={"Authorization": f"Bearer {user2}"})).status_code == 403
    assert (await client.get(f"/jobs/{job_id}", headers={"Authorization": f"Bearer {admin}"})).status_code == 200


@pytest.mark.asyncio
async def test_create_job_reserves_dispatch_slots_and_rate_limits(client):
    user_token = await token(client, "slots@example.com")
    headers = {"Authorization": f"Bearer {user_token}"}
    statuses = []
    for _ in range(4):
        response = await client.post("/jobs", headers=headers, json={"payload": {}})
        assert response.status_code == 201
        statuses.append(response.json()["status"])
    assert statuses.count("queued") == 3
    assert statuses.count("pending") == 1
    assert len(app.state.published_jobs) == 3
    for _ in range(6):
        assert (await client.post("/jobs", headers=headers, json={"payload": {}})).status_code == 201
    assert (await client.post("/jobs", headers=headers, json={"payload": {}})).status_code == 429


@pytest.mark.asyncio
async def test_worker_success_and_retry_failure_paths(client):
    user_token = await token(client, "worker@example.com")
    headers = {"Authorization": f"Bearer {user_token}"}
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
