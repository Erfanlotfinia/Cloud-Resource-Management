import enum
from sqlalchemy import Boolean, DateTime, Enum, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.infrastructure.database import Base


class UserRole(str, enum.Enum):
    user='user'
    admin='admin'
    
    
class User(Base):
    __tablename__='users'
    id: Mapped[int]=mapped_column(Integer, primary_key=True)
    email: Mapped[str]=mapped_column(String(320), unique=True, index=True, nullable=False)
    hashed_password: Mapped[str]=mapped_column(String(255), nullable=False)
    role: Mapped[UserRole]=mapped_column(Enum(UserRole, name='user_role'), default=UserRole.user, nullable=False)
    is_active: Mapped[bool]=mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[object]=mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[object]=mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    jobs=relationship('Job', back_populates='owner')
