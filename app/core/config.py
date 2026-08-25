"""
Application configuration.

Loads settings from environment variables (via a .env file in development).
Import the module-level `settings` singleton anywhere config values are needed —
never read os.environ directly elsewhere in the app.
"""

from functools import lru_cache
from typing import List

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- App ---
    app_name: str = "Recon"
    env: str = "development"
    debug: bool = True
    api_v1_prefix: str = "/api/v1"

    # --- CORS ---
    cors_origins: str = "http://localhost:3000"

    # --- Database ---
    database_url: str = Field(
        default="postgresql+psycopg2://recon:recon@localhost:5432/recon"
    )

    # --- Redis / background jobs ---
    redis_url: str = "redis://localhost:6379/0"
    # Explicit switch, same philosophy as storage_backend below: true (the
    # production default) queues document extraction/matching for a
    # separate `rq worker` process; false runs each job synchronously the
    # moment it's enqueued, in-process, no worker needed. Set to false for
    # local dev without a worker running, or leave it at its default in
    # tests where a real Redis is available but nothing is consuming the
    # queue asynchronously. See app/core/queue.py.
    background_jobs_enabled: bool = True

    # --- Auth (JWT) ---
    jwt_secret_key: str
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 60

    # --- Object storage ---
    s3_bucket_name: str = "recon-documents-dev"
    s3_region: str = "us-east-1"
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    s3_endpoint_url: str | None = None

    # Explicit switch rather than inferring from "are AWS creds present" —
    # ambient AWS credentials (an IAM role, CI-wide env vars, a local proxy)
    # are common and shouldn't silently flip storage backends. See
    # app/core/storage.py.
    storage_backend: str = "local"  # "local" | "s3"
    local_storage_dir: str = "./storage"

    # --- AI (dual-provider — see app/ai/matching.py's module docstring) ---
    # Explicit switch, same philosophy as storage_backend below: which
    # provider's API actually does extraction/classification/matching.
    # "gemini" (default) uses Google's free-tier-friendly Interactions API;
    # "anthropic" uses Claude's Messages API. Only the selected provider's
    # API key needs to be set — see the model_validator below.
    ai_provider: str = "gemini"  # "anthropic" | "gemini"
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-sonnet-4-5"
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-3.7-flash"

    # --- Inbound email ingestion ---
    inbound_email_domain: str = "inbound.reconapp.io"
    # HTTP Basic Auth credentials Postmark's inbound webhook is configured
    # to send (baked into the webhook URL you register with Postmark —
    # see app/api/email.py). Both must be set for that endpoint to accept
    # anything; unset means "refuse all requests", not "allow all requests".
    inbound_email_webhook_username: str | None = None
    inbound_email_webhook_password: str | None = None
    # Mailgun's HTTP webhook signing key (Sending -> Webhook signing key in
    # the Mailgun dashboard) — used to verify the timestamp/token/signature
    # triplet Mailgun attaches to every inbound webhook POST.
    mailgun_signing_key: str | None = None

    @field_validator("env")
    @classmethod
    def validate_env(cls, value: str) -> str:
        allowed = {"development", "staging", "production"}
        if value not in allowed:
            raise ValueError(f"env must be one of {allowed}, got {value!r}")
        return value

    @field_validator("storage_backend")
    @classmethod
    def validate_storage_backend(cls, value: str) -> str:
        allowed = {"local", "s3"}
        if value not in allowed:
            raise ValueError(f"storage_backend must be one of {allowed}, got {value!r}")
        return value

    @field_validator("ai_provider")
    @classmethod
    def validate_ai_provider(cls, value: str) -> str:
        allowed = {"anthropic", "gemini"}
        if value not in allowed:
            raise ValueError(f"ai_provider must be one of {allowed}, got {value!r}")
        return value

    @model_validator(mode="after")
    def validate_ai_provider_key_present(self) -> "Settings":
        # Cross-field check (needs both ai_provider and the key), so this
        # can't be a plain field_validator on either field alone.
        if self.ai_provider == "gemini" and not self.gemini_api_key:
            raise ValueError("GEMINI_API_KEY is required when AI_PROVIDER=gemini.")
        if self.ai_provider == "anthropic" and not self.anthropic_api_key:
            raise ValueError("ANTHROPIC_API_KEY is required when AI_PROVIDER=anthropic.")
        return self

    @property
    def cors_origins_list(self) -> List[str]:
        """CORS_ORIGINS as a parsed list, e.g. 'a,b' -> ['a', 'b']."""
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def is_production(self) -> bool:
        return self.env == "production"


@lru_cache
def get_settings() -> Settings:
    """
    Cached settings accessor. Use this in FastAPI dependencies so settings
    are parsed once per process, not on every request.
    """
    return Settings()


# Module-level singleton for straightforward imports: `from app.core.config import settings`
settings = get_settings()
