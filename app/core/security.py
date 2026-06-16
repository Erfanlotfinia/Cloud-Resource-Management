from datetime import datetime, timedelta, timezone

import bcrypt
from jose import JWTError, jwt

from app.core.config import get_settings


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


def create_access_token(subject: str) -> str:
    settings = get_settings()
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_expire_minutes)
    return jwt.encode({'sub': subject, 'exp': expire}, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> str | None:
    try:
        return jwt.decode(token, get_settings().jwt_secret_key, algorithms=[get_settings().jwt_algorithm]).get('sub')
    except JWTError:
        return None
