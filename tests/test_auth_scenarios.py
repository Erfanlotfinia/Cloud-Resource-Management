from app.main import app
from tests.conftest import auth_headers, login_user, register_user, token


async def test_health(client):
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_register_duplicate_email(client):
    await register_user(client, "dup@example.com")
    response = await register_user(client, "dup@example.com")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "email_exists"


async def test_register_short_password_rejected(client):
    response = await register_user(client, "short@example.com", password="abc")
    assert response.status_code == 422


async def test_login_invalid_password(client):
    await register_user(client, "login@example.com")
    response = await login_user(client, "login@example.com", password="wrong-password")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"


async def test_login_unknown_email(client):
    response = await login_user(client, "missing@example.com")
    assert response.status_code == 401


async def test_unauthenticated_request_rejected(client):
    response = await client.get("/jobs")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


async def test_invalid_token_rejected(client):
    response = await client.get("/jobs", headers=auth_headers("not-a-valid-token"))
    assert response.status_code == 401


async def test_admin_registration_wrong_setup_token(client):
    response = await register_user(
        client,
        "wrong-token-admin@example.com",
        role="admin",
        admin_token="wrong-token",
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "admin_setup_required"


async def test_inactive_user_rejected(client):
    user_token = await token(client, "inactive@example.com")
    async with app.state.session_factory() as session:
        from app.models.user import User

        user = await session.get(User, 1)
        user.is_active = False
        await session.commit()

    response = await client.get("/jobs", headers=auth_headers(user_token))
    assert response.status_code == 401


async def test_login_returns_bearer_token(client):
    await register_user(client, "token-shape@example.com")
    response = await login_user(client, "token-shape@example.com")
    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert isinstance(body["access_token"], str)
    assert len(body["access_token"]) > 20
