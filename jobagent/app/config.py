"""Central configuration. Everything comes from one .env (see .env.example)."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PACKAGE_ROOT = Path(__file__).resolve().parent.parent  # jobagent/


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(PACKAGE_ROOT / ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    # --- secrets -----------------------------------------------------------
    anthropic_api_key: str = ""
    mail_webhook_secret: str = "change-me"
    proxy_url: str | None = None
    encryption_key: str = ""  # Fernet key; generate with scripts/gen_key.py

    # --- infrastructure ----------------------------------------------------
    database_url: str = f"sqlite:///{PACKAGE_ROOT / 'data' / 'jobagent.db'}"
    redis_url: str | None = None  # unset -> DB-backed queue
    data_dir: Path = PACKAGE_ROOT / "data"
    apply_domain: str = "apply.example.com"
    operator_email: str = "operator@example.com"
    admin_user: str = "admin"
    admin_password: str = "admin"
    base_url: str = "http://localhost:8000"

    # --- LLM ---------------------------------------------------------------
    llm_mode: str = "auto"  # live | mock | auto (live when key present, else mock)
    haiku_model: str = "claude-haiku-4-5-20251001"
    sonnet_model: str = "claude-sonnet-5"

    # --- outbound mail -----------------------------------------------------
    mail_provider: str = "log"  # log | mailgun | smtp
    mailgun_domain: str = ""
    mailgun_api_key: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    mail_from: str = "JobAgent <noreply@apply.example.com>"

    # --- matching / pacing knobs ------------------------------------------
    match_threshold: int = 65
    max_job_age_days: int = 14
    company_cooldown_days: int = 90
    cross_client_stagger_hours: int = 3
    daily_cap_per_client: int = 40
    submit_window_start_hour: int = 8
    submit_window_end_hour: int = 20
    jitter_min_minutes: int = 3
    jitter_max_minutes: int = 12
    per_ats_concurrency: int = 3
    adapter_health_min_success_rate: float = 0.70
    discovery_interval_hours: int = 2
    discovery_concurrency: int = 10
    otp_wait_seconds: int = 120
    otp_session_ttl_seconds: int = 180
    headless: bool = True

    @property
    def effective_llm_mode(self) -> str:
        if self.llm_mode == "auto":
            return "live" if self.anthropic_api_key else "mock"
        return self.llm_mode

    @property
    def pdf_dir(self) -> Path:
        return self.data_dir / "pdfs"

    @property
    def screenshot_dir(self) -> Path:
        return self.data_dir / "screenshots"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    for d in (s.data_dir, s.pdf_dir, s.screenshot_dir, s.backup_dir):
        Path(d).mkdir(parents=True, exist_ok=True)
    return s


def reset_settings_cache() -> None:
    get_settings.cache_clear()
