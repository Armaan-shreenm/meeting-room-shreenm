"""Application settings.

Every tunable value in NM Meet lives in this module. No other file may contain a
magic number, a hardcoded time, a mail address or a connection string. Settings
are read from environment variables, falling back to the defaults declared here,
so nothing has to be edited to move between a laptop and Render.
"""

from __future__ import annotations

from datetime import time
from functools import lru_cache
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Scheme prefixes Render and psql hand out, and the one SQLAlchemy needs.
_SQLALCHEMY_SCHEME = "postgresql+psycopg2://"
_REWRITABLE_SCHEMES = ("postgres://", "postgresql://")


class Settings(BaseSettings):
    """Runtime configuration, populated from the environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---------------------------------------------------------------- runtime
    environment: Literal["development", "staging", "production"] = "development"
    port: int = 8000
    log_level: str = "INFO"
    sql_echo: bool = False

    # --------------------------------------------------------------- database
    database_url: str = (
        "postgresql+psycopg2://postgres:postgres@localhost:5432/nm_meet"
    )
    db_pool_size: int = 5
    db_max_overflow: int = 5
    # Render's managed Postgres closes idle connections; recycle before it does.
    db_pool_recycle_seconds: int = 1800

    # -------------------------------------------------------------- security
    # Signs the session cookie. MUST be set in production - a rotated value logs
    # everyone out, and a leaked one lets anybody forge a session. Render
    # generates this automatically via generateValue in render.yaml.
    secret_key: str = "dev-only-not-a-secret-change-me"
    session_max_age_seconds: int = 60 * 60 * 12   # one working day
    bcrypt_rounds: int = 12
    # Printed once by scripts/seed.py when it sets a password it generated.
    seed_password: str = ""

    # ---------------------------------------------------------- actor identity
    # Real authentication arrives in Phase 5. Until then the actor is resolved
    # from the X-User-Email request header, and this is who it falls back to
    # when the header is absent.
    #
    # The fallback applies ONLY when environment != "production". In production a
    # request with no header is a 401: an unauthenticated booking silently made
    # in somebody else's name is worse than a failed request. See app.core.auth.
    dev_user_email: str = "priya.nair@shreenm.com"

    # --------------------------------------------------------- branch, locale
    # Stored as timestamptz in UTC, displayed in this zone. Spec section 11.
    tz: str = "Asia/Kolkata"
    branch_name: str = "Mumbai"

    # ------------------------------------------- booking rules, spec sections 3, 4, 13
    open_time: time = time(9, 0)
    close_time: time = time(20, 0)
    slot_minutes: int = 30
    min_booking_minutes: int = 30
    max_booking_minutes: int = 240
    max_advance_days: int = 90
    # D-06: an unused room is released by reception after this long.
    no_show_release_minutes: int = 15
    # Monday=0 ... Sunday=6. D-03: Monday to Saturday working, Sunday closed.
    closed_weekdays: list[int] = Field(default_factory=lambda: [6])
    # Spec field 8: the title falls back to this when the user leaves it blank.
    default_meeting_title: str = "Meeting"

    # ---------------------------------------------------------- notifications
    # Base URL used to build the "view or cancel this booking" link in every
    # message (spec section 8). On Render set this to the service URL.
    public_base_url: str = "http://localhost:8000"
    notifications_enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = True
    smtp_from_email: str = "nm-meet@example.invalid"
    smtp_from_name: str = "NM Meet"
    # Spec section 8 recipients that are mailboxes rather than directory users.
    reception_email: str = "reception.mumbai@shreenm.com"
    mumbai_group_email: str = "mumbai.all@shreenm.com"
    # D-01: the branch list gets one summary at this local hour, not a mail per booking.
    daily_summary_hour: int = 8
    # D-02: an .ics invite is attached; no video-conference link is created.
    calendar_invite_enabled: bool = True

    # -------------------------------------------------------------------- cors
    # Unused in production: FastAPI serves the frontend and the API from one origin.
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])

    # ------------------------------------------------------------- validators
    @field_validator("database_url", mode="before")
    @classmethod
    def _normalise_database_url(cls, value: object) -> object:
        """Rewrite Render's ``postgres://`` scheme to the psycopg2 dialect.

        Render injects DATABASE_URL as ``postgres://user:pass@host/db``. SQLAlchemy
        2.0 does not recognise that scheme and the first deploy dies on it, so the
        rewrite happens here, once, before an engine is ever created.
        """
        if not isinstance(value, str):
            return value

        url = value.strip()
        if not url:
            return url

        for scheme in _REWRITABLE_SCHEMES:
            if url.startswith(scheme):
                return _SQLALCHEMY_SCHEME + url[len(scheme) :]
        return url

    @field_validator("tz")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        ZoneInfo(value)  # raises ZoneInfoNotFoundError on a bad name
        return value

    @model_validator(mode="after")
    def _require_postgresql(self) -> "Settings":
        """PostgreSQL is not swappable.

        The ``no_double_booking`` EXCLUDE constraint in spec section 7 is the only
        real guarantee against a double booking, and it exists in no other engine.
        Fail at import time rather than serving a system that cannot keep its
        central promise.
        """
        if not self.database_url.startswith("postgresql"):
            raise ValueError(
                "DATABASE_URL must point at PostgreSQL. NM Meet relies on the "
                "btree_gist EXCLUDE constraint to prevent double bookings and "
                "cannot run on any other database engine."
            )
        return self

    @model_validator(mode="after")
    def _require_real_secret_in_production(self) -> "Settings":
        """A production deploy signing sessions with the shipped default would
        let anyone who has read this repository forge a session for any user."""
        if self.is_production and self.secret_key == "dev-only-not-a-secret-change-me":
            raise ValueError(
                "SECRET_KEY must be set to a real random value in production. "
                "render.yaml generates one; set it in the dashboard otherwise."
            )
        return self

    @model_validator(mode="after")
    def _validate_booking_window(self) -> "Settings":
        if self.open_time >= self.close_time:
            raise ValueError("OPEN_TIME must be earlier than CLOSE_TIME.")
        if self.min_booking_minutes > self.max_booking_minutes:
            raise ValueError(
                "MIN_BOOKING_MINUTES cannot exceed MAX_BOOKING_MINUTES."
            )
        if self.max_booking_minutes % self.slot_minutes:
            raise ValueError(
                "MAX_BOOKING_MINUTES must be a whole number of SLOT_MINUTES."
            )
        return self

    # ----------------------------------------------------------- conveniences
    @property
    def timezone(self) -> ZoneInfo:
        """The branch display timezone."""
        return ZoneInfo(self.tz)

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def open_minutes(self) -> int:
        """Opening time as minutes from local midnight."""
        return self.open_time.hour * 60 + self.open_time.minute

    @property
    def close_minutes(self) -> int:
        """Closing time as minutes from local midnight."""
        return self.close_time.hour * 60 + self.close_time.minute

    @property
    def last_entry_minutes(self) -> int:
        """Latest local start time that still leaves room for a shortest booking."""
        return self.close_minutes - self.min_booking_minutes


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Settings are read once per process and cached."""
    return Settings()


settings = get_settings()
