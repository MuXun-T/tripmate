from functools import lru_cache

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    log_level: str = "INFO"
    database_url: str = "postgresql+asyncpg://tripmate:tripmate@localhost:5432/tripmate"
    redis_url: str = "redis://localhost:6379/0"
    redis_prefix: str = "tm"
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-flash"
    llm_timeout_seconds: float = Field(default=30.0, gt=0, le=180)
    hold_ttl_seconds: int = Field(default=600, ge=60, le=3600)
    short_term_memory_ttl_seconds: int = Field(default=604800, ge=60, le=2592000)
    idempotency_ttl_seconds: int = Field(default=604800, ge=60)
    action_confirmation_ttl_seconds: int = Field(default=900, ge=30, le=3600)
    db_pool_min: int = Field(default=2, ge=1)
    db_pool_max: int = Field(default=10, ge=1)
    db_statement_timeout_ms: int = Field(default=5000, ge=100, le=120000)
    stream_batch_size: int = Field(default=100, ge=1, le=1000)
    max_event_attempts: int = Field(default=5, ge=1, le=20)
    pending_claim_idle_ms: int = Field(default=30000, ge=1000)
    stream_auto_trim: bool = False
    otel_service_name: str = "tripmate-api"
    otel_exporter_otlp_endpoint: str | None = None
    trace_sample_ratio: float = Field(default=0.1, ge=0, le=1)
    rag_index_alias: str = "tm:knowledge:v1:dev:active"
    rag_top_k: int = Field(default=5, ge=1, le=20)
    rag_min_score: float = Field(default=0.05, ge=-1, le=1)
    external_http_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    auth_mode: str = "signed_bearer"
    auth_signing_secret: SecretStr = SecretStr("local-tripmate-signing-secret-change-before-deploy")
    agent_run_timeout_seconds: int = Field(default=300, ge=30, le=1800)
    max_request_body_bytes: int = Field(default=65536, ge=1024, le=1048576)

    @field_validator("database_url")
    @classmethod
    def normalize_database_url(cls, value: str) -> str:
        if value.startswith("postgres://"):
            return value.replace("postgres://", "postgresql+asyncpg://", 1)
        if value.startswith("postgresql://"):
            return value.replace("postgresql://", "postgresql+asyncpg://", 1)
        return value

    @field_validator("otel_exporter_otlp_endpoint")
    @classmethod
    def blank_otlp_endpoint_is_disabled(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None

    @field_validator("db_pool_max")
    @classmethod
    def check_pool_bounds(cls, value: int, info: object) -> int:
        minimum = getattr(info, "data", {}).get("db_pool_min", 1)
        if value < minimum:
            raise ValueError("DB_POOL_MAX must be greater than or equal to DB_POOL_MIN")
        return value

    @model_validator(mode="after")
    def validate_authentication(self) -> "Settings":
        if self.auth_mode != "signed_bearer":
            raise ValueError("AUTH_MODE must be signed_bearer")
        if len(self.auth_signing_secret.get_secret_value().encode("utf-8")) < 32:
            raise ValueError("AUTH_SIGNING_SECRET must contain at least 32 bytes")
        default_secret = "local-tripmate-signing-secret-change-before-deploy"
        if self.app_env.casefold() not in {"development", "test"}:
            if self.auth_signing_secret.get_secret_value() == default_secret:
                raise ValueError("AUTH_SIGNING_SECRET must be changed outside development")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
