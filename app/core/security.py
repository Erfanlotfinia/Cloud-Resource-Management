from datetime import datetime, timedelta, timezone
from jose import JWTError, jwt
from passlib.context import CryptContext
from app.core.config import get_settings

pwd_context = CryptContext(schemes=['bcrypt'], deprecated='auto')

def hash_password(password: str) -> str:
    return pwd_context.hash(password)

def verify_password(password: str, hashed: str) -> bool:
    return pwd_context.verify(password, hashed)

def create_access_token(subject: str) -> str:
    s = get_settings(); expire = datetime.now(timezone.utc) + timedelta(minutes=s.access_token_expire_minutes)
    return jwt.encode({'sub': subject, 'exp': expire}, s.jwt_secret_key, algorithm=s.jwt_algorithm)

def decode_token(token: str) -> str | None:
    try: return jwt.decode(token, get_settings().jwt_secret_key, algorithms=[get_settings().jwt_algorithm]).get('sub')
    except JWTError: return None
