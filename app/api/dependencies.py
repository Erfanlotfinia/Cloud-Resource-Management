from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status
from app.core.exceptions import AppError
from app.core.security import decode_token
from app.infrastructure.database import get_session
from app.models.user import User


bearer=HTTPBearer(auto_error=False)

async def current_user(creds: HTTPAuthorizationCredentials|None=Depends(bearer), session:AsyncSession=Depends(get_session)) -> User:
    if not creds: 
        raise AppError('unauthorized','Authentication required', status.HTTP_401_UNAUTHORIZED)
    
    sub=decode_token(creds.credentials)
    if not sub:
        raise AppError('unauthorized','Invalid token', status.HTTP_401_UNAUTHORIZED)
    
    user=await session.get(User, int(sub))
    if not user or not user.is_active:
        raise AppError('unauthorized','Inactive or missing user', status.HTTP_401_UNAUTHORIZED)
    
    return user
