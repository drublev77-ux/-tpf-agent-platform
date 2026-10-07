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
    order_token_budget_accounts: int = 5_000_000   # an application with user roles is several times the code and the rounds of checks
    budget_warning_share: float = 0.8
    max_check_attempts: int = 6
    max_check_attempts_accounts: int = 8   # an application with user roles has more to get right: it gets more rounds
    # What a delivered application may ask of the platform (see api/apps.py)
    app_mail_per_day: int = 200        # e-mails one live application sends in a day
    app_mail_per_day_review: int = 30  # and its review version
    app_mail_per_recipient_day: int = 10
    app_ai_monthly_tokens: int = 2_000_000    # the model's tokens one application may use in a calendar month
    app_ai_max_output: int = 4000
    app_tick_minutes: int = 15         # how often applications with scheduled work are woken
    visual_check: bool = True          # QA looks at screenshots of the built pages
    visual_blocking_rounds: int = 2    # after this many rounds failed only on looks, findings stop blocking
    max_review_rounds: int = 2
    task_max_retries: int = 3
    # How long a task stays claimed without a sign of life from its worker. The worker renews the lease
    # every third of it while the task runs, so this is only how fast a dead worker's task is retried.
    task_lease_sec: int = 120

    # Commercial terms
    hourly_rate: float = 40.0          # fallback for a product type without its own rate
    # Rates per product type, in euros per hour. They were set in US dollars 20% below the market reference
    # for comparable small-business work (October 2026: landing ~$900, small web application ~$3,000, Telegram
    # bot with booking ~$2,500) and converted at 1 EUR = 1.117 USD when the studio began charging in euros.
    # Sites: lowered on 7 October 2026 by the owner's decision. Published Polish prices put a landing at
    # 800-3,000 PLN; 16 hours at 28 is 448 EUR (~1,960 PLN), 20% under the middle of that range.
    hourly_rate_site: float = 28.0
    hourly_rate_app: float = 63.0
    hourly_rate_bot: float = 45.0
    hourly_rate_shop: float = 33.0     # small shop ~$3,000 on the market; a shop takes about 65 hours

    # Orders in Russian are priced against the Russian-speaking market, which is cheaper: landing ~60 000 RUB,
    # simple bot ~65 000 RUB, bot with booking ~140 000 RUB, small application ~200 000 RUB (EUR at ~93 RUB).
    hourly_rate_ru_site: float = 28.0  # the same as everywhere: the owner set one site rate for all languages
    hourly_rate_ru_app: float = 50.0
    hourly_rate_ru_bot: float = 30.0
    hourly_rate_ru_shop: float = 26.0  # small shop ~200 000 RUB

    def rate_for(self, product_type: str, language: str = "") -> float:
        regional = getattr(self, f"hourly_rate_{language}_{product_type}", 0) if language else 0
        return float(regional or getattr(self, f"hourly_rate_{product_type}", 0) or self.hourly_rate)
    currency: str = "EUR"
    usd_per_eur: float = 1.117         # a client may ask to be invoiced in US dollars: the rates above times this

    def rate_in(self, currency: str, product_type: str, language: str = "") -> float:
        """The hourly rate in the currency of the order; rates are kept in the studio's own currency."""
        rate = self.rate_for(product_type, language)
        return float(round(rate * self.usd_per_eur)) if currency == "USD" and self.currency == "EUR" else rate
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
    # Days of the client's silence after which an order that waits for them gets a reminder; empty = no reminders
    client_reminder_days: str = "2,5"
    indexnow: bool = True      # the worker reports the studio site's changed pages to Yandex and Bing

    @property
    def reminder_days(self) -> list[float]:
        try:
            return sorted(float(d) for d in self.client_reminder_days.split(",") if d.strip())
        except ValueError:
            return []

    @property
    def email_enabled(self) -> bool:
        return self.email_provider in ("resend", "brevo") and bool(self.email_api_key and self.email_from)


@lru_cache
def get_settings() -> Settings:
    return Settings()
