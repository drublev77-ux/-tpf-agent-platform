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
    max_check_attempts: int = 6
    max_review_rounds: int = 2
    task_max_retries: int = 3
    # How long a task stays claimed without a sign of life from its worker. The worker renews the lease
    # every third of it while the task runs, so this is only how fast a dead worker's task is retried.
    task_lease_sec: int = 120

    # Commercial terms
    hourly_rate: float = 40.0          # fallback for a product type without its own rate
    # Rates per product type, set 20% below the market reference for comparable small-business work
    # (October 2026): landing ~$900, small web application ~$3,000, Telegram bot with booking ~$2,500.
    hourly_rate_site: float = 45.0
    hourly_rate_app: float = 70.0
    hourly_rate_bot: float = 50.0
    hourly_rate_shop: float = 37.0     # small shop ~$3,000 on the market; a shop takes about 65 hours

    # Orders in Russian are priced against the Russian-speaking market, which is cheaper: landing ~60 000 RUB,
    # simple bot ~65 000 RUB, bot with booking ~140 000 RUB, small application ~200 000 RUB (USD at ~83 RUB).
    hourly_rate_ru_site: float = 36.0
    hourly_rate_ru_app: float = 56.0
    hourly_rate_ru_bot: float = 34.0
    hourly_rate_ru_shop: float = 29.0  # small shop ~200 000 RUB

    def rate_for(self, product_type: str, language: str = "") -> float:
        regional = getattr(self, f"hourly_rate_{language}_{product_type}", 0) if language else 0
        return float(regional or getattr(self, f"hourly_rate_{product_type}", 0) or self.hourly_rate)
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

    # Web applications: a separate Postgres where each order gets its own schema and role. Empty = sites only.
    tenant_database_url: str = ""

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
