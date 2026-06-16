from datetime import datetime
from pydantic import BaseModel, Field
from app.models.job import JobStatus, LogLevel

class JobCreate(BaseModel):
    payload: dict = Field(default_factory=dict)
    max_retries: int = Field(default=3, ge=0, le=10)

class JobResponse(BaseModel):
    id: int
    owner_id: int
    status: JobStatus
    payload: dict
    result: dict | None = None
    error_message: str | None = None
    retry_count: int
    max_retries: int
    idempotency_key: str | None = None
    correlation_id: str
    locked_by: str | None = None
    lock_expires_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    cancelled_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    model_config = {'from_attributes': True}

class JobListResponse(BaseModel):
    items: list[JobResponse]
    next_cursor: str | None
    has_more: bool

class JobLogResponse(BaseModel):
    id: int
    job_id: int
    level: LogLevel
    message: str
    created_at: datetime
    model_config = {'from_attributes': True}
