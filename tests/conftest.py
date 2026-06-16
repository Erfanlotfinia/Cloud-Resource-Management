import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.infrastructure import rabbitmq, redis as redisinfra
from app.infrastructure.database import Base, get_session
from app.main import app
from app.models.job import OutboxEvent
from app.workers import outbox_publisher, runner


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

    async def fake_publish(job_id: int, correlation_id: str | None = None, outbox_event_id: int | None = None):
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

    async def count_outbox():
        async with Session() as session:
            return await session.scalar(select(func.count()).select_from(OutboxEvent)) or 0

    get_settings.cache_clear()
    monkeypatch.setenv("ADMIN_SETUP_TOKEN", "setup-token")
    app.dependency_overrides[get_session] = override_session
    monkeypatch.setattr(rabbitmq, "publish_job", fake_publish)
    monkeypatch.setattr(redisinfra, "safe_get", none)
    monkeypatch.setattr(redisinfra, "safe_setex", noop)
    monkeypatch.setattr(redisinfra, "invalidate_pattern", noop)
    monkeypatch.setattr(redisinfra, "publish_status", noop)
    monkeypatch.setattr("app.workers.runner.invalidate_pattern", noop)
    monkeypatch.setattr("app.workers.runner.publish_status", noop)
    monkeypatch.setattr(redisinfra, "get_redis", lambda: FakeRedis())
    monkeypatch.setattr(runner, "SessionLocal", Session)
    monkeypatch.setattr(outbox_publisher, "SessionLocal", Session)
    app.state.published_jobs = published
    app.state.count_outbox = count_outbox
    app.state.session_factory = Session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def register_user(
    client,
    email: str,
    *,
    role: str = "user",
    password: str = "password123",
    admin_token: str | None = "setup-token",
):
    headers = {"X-Admin-Setup-Token": admin_token} if role == "admin" else {}
    return await client.post(
        "/auth/register",
        headers=headers,
        json={"email": email, "password": password, "role": role},
    )


async def login_user(client, email: str, password: str = "password123"):
    return await client.post("/auth/login", json={"email": email, "password": password})


async def token(client, email: str = "u@example.com", role: str = "user") -> str:
    await register_user(client, email, role=role)
    response = await login_user(client, email)
    return response.json()["access_token"]


async def create_job(client, user_token: str, **kwargs):
    headers = auth_headers(user_token)
    if "idempotency_key" in kwargs:
        headers["Idempotency-Key"] = kwargs.pop("idempotency_key")
    payload = kwargs.pop("payload", {})
    body = {"payload": payload, **kwargs}
    return await client.post("/jobs", headers=headers, json=body)
