from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status

from app.core.config import get_settings
from app.core.exceptions import AppError
from app.core.security import create_access_token, hash_password, verify_password
from app.models.user import User, UserRole
from app.schemas.auth import LoginRequest, RegisterRequest


async def register(
    data: RegisterRequest, session: AsyncSession, admin_setup_token: str | None = None
) -> User:
    if await session.scalar(select(User).where(User.email == data.email)):
        raise AppError('email_exists', 'Email already registered', 400)

    role = UserRole.user
    if data.role == UserRole.admin:
        expected = get_settings().admin_setup_token
        if not expected or admin_setup_token != expected:
            raise AppError(
                'admin_setup_required',
                'Admin registration requires X-Admin-Setup-Token',
                403,
            )
        role = UserRole.admin

    user = User(email=data.email, hashed_password=hash_password(data.password), role=role)
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def login(data: LoginRequest, session: AsyncSession) -> str:
    user = await session.scalar(select(User).where(User.email == data.email))
    if not user or not verify_password(data.password, user.hashed_password):
        raise AppError('invalid_credentials', 'Invalid credentials', status.HTTP_401_UNAUTHORIZED)
    return create_access_token(str(user.id))
