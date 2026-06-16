from fastapi import APIRouter, Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession
from app.infrastructure.database import get_session
from app.schemas.auth import RegisterRequest, LoginRequest, TokenResponse, UserResponse
from app.services.auth import register, login


router=APIRouter(prefix='/auth', tags=['auth'])

@router.post('/register', response_model=UserResponse, status_code=201)
async def register_route(data:RegisterRequest, x_admin_setup_token:str|None=Header(default=None, alias="X-Admin-Setup-Token"), session:AsyncSession=Depends(get_session)):
    return await register(data, session, x_admin_setup_token)


@router.post('/login', response_model=TokenResponse)
async def login_route(data:LoginRequest, session:AsyncSession=Depends(get_session)):
    return TokenResponse(access_token=await login(data, session))
