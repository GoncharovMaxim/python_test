from functools import lru_cache
from urllib.parse import urlsplit

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration. Every field can be set via an env var of the same name."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://support:support@localhost:5432/support"
    db_pool_size: int = 10

    # --- LLM -------------------------------------------------------------------------
    # Empty key => "no-key mode": the real request is still sent to the provider, but its
    # result is replaced by a static response after a 5-30 s delay.
    anthropic_api_key: str = ""
    anthropic_base_url: str | None = None
    llm_model: str = "claude-opus-5-5"
    llm_effort: str = "low"
    llm_max_tokens: int = 8000
    llm_timeout_seconds: float = 90.0
    llm_server_fallbacks: bool = True
    llm_rate_limit_per_minute: int = 60
    llm_stub_min_delay_seconds: float = 5.0
    llm_stub_max_delay_seconds: float = 30.0

    # --- Knowledge base --------------------------------------------------------------
    kb_dir: str = "kb"
    kb_public_base_url: str = "https://help.zvithub.example/articles"
    kb_top_k: int = 3

    # --- Worker ----------------------------------------------------------------------
    generation_concurrency: int = 30  # 60 req/min * up to 30 s per call
    send_concurrency: int = 10
    worker_poll_interval_seconds: float = 1.0
    job_lease_seconds: int = 300
    generation_max_attempts: int = 6
    generation_backoff_base_seconds: float = 5.0
    generation_backoff_cap_seconds: float = 120.0
    send_max_attempts: int = 12
    send_backoff_base_seconds: float = 2.0
    send_backoff_cap_seconds: float = 300.0

    # --- Helpdesk --------------------------------------------------------------------
    # Total time the webhook may spend storing an event, including waiting for a pooled
    # DB connection. The helpdesk gives up after 2 s; past this budget we answer 503 so
    # it retries (a retry of an event that did get stored is answered "duplicate").
    webhook_timeout_seconds: float = 1.5
    helpdesk_timeout_seconds: float = 10.0
    # Comma-separated host allowlist for `reply_url` (SSRF guard). Empty = allow any host.
    reply_url_allowed_hosts: str = ""

    @property
    def allowed_reply_hosts(self) -> frozenset[str]:
        return frozenset(
            h.strip().lower() for h in self.reply_url_allowed_hosts.split(",") if h.strip()
        )

    def is_reply_url_allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return False
        allowed = self.allowed_reply_hosts
        return not allowed or parts.hostname.lower() in allowed

    @property
    def llm_stub_mode(self) -> bool:
        return not self.anthropic_api_key.strip()


@lru_cache
def get_settings() -> Settings:
    return Settings()
