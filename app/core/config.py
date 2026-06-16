from functools import lru_cache
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', extra='ignore')
    app_name: str = 'Cloud Resource Management API'
    database_url: str = 'postgresql+asyncpg://crm:crm@postgres:5432/crm'
    redis_url: str = 'redis://redis:6379/0'
    rabbitmq_url: str = 'amqp://guest:guest@rabbitmq:5672/'
    jwt_secret_key: str = Field('change-me-in-production', min_length=16)
    jwt_algorithm: str = 'HS256'
    access_token_expire_minutes: int = 60
    jobs_queue_name: str = 'jobs.execute'
    jobs_per_user_running_limit: int = 3
    jobs_cache_ttl_seconds: int = 30
    rate_limit_per_minute: int = 10

@lru_cache
def get_settings() -> Settings:
    return Settings()
