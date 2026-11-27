"""Runtime configuration, read from environment variables.

Every time-dependent rule reads `recon_as_of`, never the wall clock, so the
demo produces identical results no matter when it is opened.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


@dataclass(frozen=True)
class Settings:
    mode: str = field(default_factory=lambda: _env("MODE", "offline"))
    database_url: str = field(
        default_factory=lambda: _env("DATABASE_URL", f"sqlite:///{ROOT / 'data' / 'finrecon.db'}")
    )
    recon_as_of: date = field(
        default_factory=lambda: date.fromisoformat(_env("RECON_AS_OF", "2026-11-30"))
    )
    # Run 1 of the offline demo uses an earlier as-of date so that the second
    # run can demonstrate incremental loads, stale data and incomplete extracts.
    first_run_as_of: date = field(
        default_factory=lambda: date.fromisoformat(_env("FIRST_RUN_AS_OF", "2026-11-25"))
    )
    window_start: date = field(
        default_factory=lambda: date.fromisoformat(_env("WINDOW_START", "2026-07-01"))
    )
    sync_delay_days: int = field(default_factory=lambda: int(_env("SYNC_DELAY_DAYS", "2")))
    candidate_date_tolerance_days: int = field(
        default_factory=lambda: int(_env("CANDIDATE_DATE_TOLERANCE_DAYS", "3"))
    )
    stale_after_days: int = field(default_factory=lambda: int(_env("STALE_AFTER_DAYS", "2")))
    incremental_lookback_days: int = field(
        default_factory=lambda: int(_env("INCREMENTAL_LOOKBACK_DAYS", "3"))
    )
    fixture_dir: Path = field(
        default_factory=lambda: Path(_env("FIXTURE_DIR", str(ROOT / "fixtures" / "offline")))
    )
    raw_dir: Path = field(default_factory=lambda: Path(_env("RAW_DIR", str(ROOT / "data" / "raw"))))
    mock_ad_api_url: str = field(default_factory=lambda: _env("MOCK_AD_API_URL", ""))
    github_url: str = field(
        default_factory=lambda: _env("GITHUB_URL", "https://github.com/jjeong4335/finrecon")
    )
    # Live-mode credentials (never committed; see .env.example)
    qbo_client_id: str = field(default_factory=lambda: _env("QBO_CLIENT_ID", ""))
    qbo_client_secret: str = field(default_factory=lambda: _env("QBO_CLIENT_SECRET", ""))
    qbo_realm_id: str = field(default_factory=lambda: _env("QBO_REALM_ID", ""))
    qbo_token_file: Path = field(
        default_factory=lambda: Path(_env("QBO_TOKEN_FILE", str(ROOT / ".secrets" / "qbo_tokens.json")))
    )
    qbo_base_url: str = field(
        default_factory=lambda: _env("QBO_BASE_URL", "https://sandbox-quickbooks.api.intuit.com")
    )
    qbo_minor_version: str = field(default_factory=lambda: _env("QBO_MINOR_VERSION", "75"))
    stripe_secret_key: str = field(default_factory=lambda: _env("STRIPE_SECRET_KEY", ""))

    def __post_init__(self) -> None:
        if self.mode not in ("offline", "live"):
            raise ValueError("MODE must be 'offline' or 'live'")
        if self.stripe_secret_key and not self.stripe_secret_key.startswith(("sk_test_", "rk_test_")):
            # Safety rail: this project must never touch a live Stripe account.
            raise ValueError("STRIPE_SECRET_KEY must be a test/sandbox key (sk_test_...)")


def get_settings(**overrides) -> Settings:
    settings = Settings()
    if overrides:
        from dataclasses import replace

        settings = replace(settings, **overrides)
    return settings
