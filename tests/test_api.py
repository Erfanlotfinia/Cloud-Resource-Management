import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from app.infrastructure.database import Base, get_session
from app.main import app
from app.infrastructure import rabbitmq, redis as redisinfra

@pytest.fixture
async def client(monkeypatch):
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async def override_session():
        async with Session() as s:
            yield s
    async def fake_publish(job_id:int): return None
    async def noop(*a, **k): return None
    async def none(*a, **k): return None
    app.dependency_overrides[get_session] = override_session
    monkeypatch.setattr(rabbitmq, 'publish_job', fake_publish)
    monkeypatch.setattr('app.services.jobs.publish_job', fake_publish)
    monkeypatch.setattr(redisinfra, 'safe_get', none)
    monkeypatch.setattr(redisinfra, 'safe_setex', noop)
    monkeypatch.setattr(redisinfra, 'invalidate_pattern', noop)
    monkeypatch.setattr(redisinfra, 'publish_status', noop)
    class R:
        n=0
        async def incr(self,k): self.n += 1; return self.n
        async def expire(self,k,t): return None
    monkeypatch.setattr(redisinfra, 'get_redis', lambda: R())
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as ac:
        yield ac
    app.dependency_overrides.clear()

async def token(client, email='u@example.com', role='user'):
    await client.post('/auth/register', json={'email':email,'password':'password123','role':role})
    r=await client.post('/auth/login', json={'email':email,'password':'password123'})
    return r.json()['access_token']

@pytest.mark.asyncio
async def test_register_login_create_idempotent_cancel_logs_and_pagination(client):
    t=await token(client)
    h={'Authorization':f'Bearer {t}'}
    r=await client.post('/jobs', headers={**h,'Idempotency-Key':'k1'}, json={'payload':{'duration_seconds':0}})
    assert r.status_code==201
    job_id=r.json()['id']
    r2=await client.post('/jobs', headers={**h,'Idempotency-Key':'k1'}, json={'payload':{'duration_seconds':0}})
    assert r2.json()['id']==job_id
    assert (await client.get('/jobs?limit=1', headers=h)).json()['items']
    assert (await client.post(f'/jobs/{job_id}/cancel', headers=h)).json()['status']=='cancelled'
    assert (await client.get(f'/jobs/{job_id}/logs', headers=h)).status_code==200

@pytest.mark.asyncio
async def test_user_forbidden_admin_allowed(client):
    t1=await token(client,'a@example.com'); t2=await token(client,'b@example.com'); admin=await token(client,'admin@example.com','admin')
    r=await client.post('/jobs', headers={'Authorization':f'Bearer {t1}'}, json={'payload':{}}); jid=r.json()['id']
    assert (await client.get(f'/jobs/{jid}', headers={'Authorization':f'Bearer {t2}'})).status_code==403
    assert (await client.get(f'/jobs/{jid}', headers={'Authorization':f'Bearer {admin}'})).status_code==200
