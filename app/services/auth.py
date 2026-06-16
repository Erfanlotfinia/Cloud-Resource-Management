from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status
from app.core.exceptions import AppError
from app.core.security import create_access_token, hash_password, verify_password
from app.models.user import User
from app.schemas.auth import RegisterRequest, LoginRequest
async def register(data:RegisterRequest, session:AsyncSession)->User:
    if (await session.scalar(select(User).where(User.email==data.email))): raise AppError('email_exists','Email already registered',400)
    user=User(email=data.email, hashed_password=hash_password(data.password), role=data.role); session.add(user); await session.commit(); await session.refresh(user); return user
async def login(data:LoginRequest, session:AsyncSession)->str:
    user=await session.scalar(select(User).where(User.email==data.email))
    if not user or not verify_password(data.password, user.hashed_password): raise AppError('invalid_credentials','Invalid credentials', status.HTTP_401_UNAUTHORIZED)
    return create_access_token(str(user.id))
