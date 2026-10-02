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
    # Payments: off | stripe | manual (the operator confirms bank transfers in /admin)
    payment_provider: str = "off"
    prepayment_share: float = 0.5  # 0.5 = 50% before work + 50% before release; 1 = all upfront; 0 = all at the end
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""

    @property
    def payment_required(self) -> bool:
        """A prepayment is due after the estimate is approved."""
        return self.payment_provider != "off" and self.prepayment_share > 0

    @property
    def final_payment_required(self) -> bool:
        """A final payment is due after acceptance, before the production release."""
        return self.payment_provider != "off" and self.prepayment_share < 1

    # Sandbox
    sandbox: str = "docker"  # docker | vercel (build checks run as Vercel previews) | local (dev only)
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
    # E-mail over an HTTPS API (Railway blocks outbound SMTP): off | resend | brevo
    email_provider: str = "off"
    email_api_key: str = ""
    email_from: str = ""       # "TPF Technology <orders@your-domain>" — the domain must be verified
    email_reply_to: str = ""
    intake_hourly_limit: int = 20  # new orders per hour across all channels
    operator_email: str = ""   # escalations and new orders; empty = log only

    @property
    def email_enabled(self) -> bool:
        return self.email_provider in ("resend", "brevo") and bool(self.email_api_key and self.email_from)


@lru_cache
def get_settings() -> Settings:
    return Settings()
