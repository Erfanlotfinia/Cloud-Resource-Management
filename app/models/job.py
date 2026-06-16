import enum
from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, String, Text, func, text, JSON
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.infrastructure.database import Base
class JobStatus(str, enum.Enum): pending='pending'; queued='queued'; running='running'; completed='completed'; failed='failed'; cancelled='cancelled'
class LogLevel(str, enum.Enum): info='info'; warning='warning'; error='error'
JSONType = JSON().with_variant(JSONB, 'postgresql')
class Job(Base):
    __tablename__='jobs'
    id: Mapped[int]=mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int]=mapped_column(ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    status: Mapped[JobStatus]=mapped_column(Enum(JobStatus, name='job_status'), nullable=False, index=True)
    payload: Mapped[dict]=mapped_column(JSONType, nullable=False)
    result: Mapped[dict|None]=mapped_column(JSONType, nullable=True)
    error_message: Mapped[str|None]=mapped_column(Text)
    retry_count: Mapped[int]=mapped_column(Integer, default=0, nullable=False)
    max_retries: Mapped[int]=mapped_column(Integer, default=3, nullable=False)
    idempotency_key: Mapped[str|None]=mapped_column(String(255))
    started_at=mapped_column(DateTime(timezone=True)); completed_at=mapped_column(DateTime(timezone=True)); cancelled_at=mapped_column(DateTime(timezone=True))
    created_at=mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    updated_at=mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    owner=relationship('User', back_populates='jobs'); logs=relationship('JobLog', back_populates='job', cascade='all, delete-orphan')
    __table_args__=(Index('ix_jobs_owner_created','owner_id','created_at'), Index('uq_jobs_owner_idempotency_key','owner_id','idempotency_key', unique=True, postgresql_where=text('idempotency_key IS NOT NULL')),)
class JobLog(Base):
    __tablename__='job_logs'
    id: Mapped[int]=mapped_column(Integer, primary_key=True)
    job_id: Mapped[int]=mapped_column(ForeignKey('jobs.id', ondelete='CASCADE'), nullable=False, index=True)
    level: Mapped[LogLevel]=mapped_column(Enum(LogLevel, name='log_level'), default=LogLevel.info, nullable=False)
    message: Mapped[str]=mapped_column(Text, nullable=False)
    created_at=mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    job=relationship('Job', back_populates='logs')
