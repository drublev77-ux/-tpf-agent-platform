"""Runtime configuration. Every tunable lives in the environment, never in code."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://tpf:tpf@localhost:5432/tpf"
    admin_token: str = "change-me"

    # LLM gateway
    anthropic_api_key: str = ""
    anthropic_base_url: str = "https://api.anthropic.com"
    model_strong: str = "claude-opus-5-5"
    model_code: str = "claude-sonnet-5"
    model_fast: str = "claude-haiku-4-5-20251001"
    model_reviewer: str = "claude-opus-5-5"
    price_input_per_mtok: float = 0.0
    price_output_per_mtok: float = 0.0
    llm_timeout_sec: float = 600.0

    # Budgets and limits
    order_token_budget: int = 2_000_000
    budget_warning_share: float = 0.8
    max_check_attempts: int = 3
    max_review_rounds: int = 2
    task_max_retries: int = 3
    task_lease_sec: int = 1800

    # Commercial terms
    hourly_rate: float = 40.0
    currency: str = "USD"
    payment_required: bool = False
    prepayment_share: float = 0.5

    # Sandbox
    sandbox: str = "docker"  # docker | local
    sandbox_image: str = "node:22-alpine"
    sandbox_timeout_sec: int = 900
    workspace_root: str = "/tmp/tpf-workspaces"

    # Delivery
    vercel_token: str = ""
    vercel_team_id: str = ""
    github_token: str = ""
    github_owner: str = ""

    # Notifications
    notifier: str = "log"  # log | webhook
    notify_webhook_url: str = ""
    public_base_url: str = "http://localhost:8000"


@lru_cache
def get_settings() -> Settings:
    return Settings()
