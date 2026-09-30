"""Centralized configuration. Loads from environment / .env file."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


# The requested flagship-group cadence is intentionally fixed in code rather
# than inferred from a mutable title or username. Every other group starts at
# the generic default until a Full Owner uses /changemessage in that group.
HENTACATCHER_GP_CHAT_ID = -1003769071399
HENTACATCHER_GP_SPAWN_INTERVAL = 100
DEFAULT_GROUP_SPAWN_INTERVAL = 125


def default_spawn_interval_for_group(chat_id: int) -> int:
    """Return the correct initial/fallback automatic-spawn cadence for a chat."""
    return (
        HENTACATCHER_GP_SPAWN_INTERVAL
        if int(chat_id) == HENTACATCHER_GP_CHAT_ID
        else DEFAULT_GROUP_SPAWN_INTERVAL
    )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Core
    bot_token: str
    bot_username: str = "HentaCatcherBot"

    # Postgres
    database_url: str
    pg_pool_min: int = 10
    pg_pool_max: int = 30

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Webhook vs polling
    use_webhook: bool = False
    webhook_url: str = ""
    webhook_path: str = "/bot"
    webhook_host: str = "0.0.0.0"
    webhook_port: int = 8443

    # Game tuning
    # Group spawn cadence is intentionally not environment-overridable: the
    # requested policy is 100 messages only for HentaCatcher GP and 125 for
    # every other group. See default_spawn_interval_for_group().
    spawn_claim_window: int = 90
    guess_cooldown: int = 3
    trade_window: int = 60

    # Admin — no default. Fix: previously defaulted to 0, which meant a
    # missing OWNER_ID silently locked everyone out of /admin instead of
    # failing loudly. Now it's required.
    owner_id: int

    # Catalog staging: admins can reply `/addcharacter` to a photo/video in
    # this broadcast channel, then continue the protected flow through a
    # private deep link. A numeric ID, when configured, is preferred because a
    # public username can be renamed; the supplied staging username remains a
    # safe package default until that ID is available.
    catalog_staging_channel_id: int | None = None
    catalog_staging_channel_username: str = "Hentai_Catcther_D_B1"

    # Logging
    log_level: str = "INFO"

    @property
    def webhook_secret(self) -> str:
        """Stable webhook secret derived from bot token."""
        import hashlib
        return hashlib.sha256(self.bot_token.encode()).hexdigest()[:32]


try:
    settings = Settings()  # type: ignore[call-arg]
except Exception as e:
    raise SystemExit(
        "❌ Missing or invalid configuration. Make sure your .env file sets "
        "BOT_TOKEN, DATABASE_URL, and OWNER_ID (your numeric Telegram user ID).\n"
        f"Details: {e}"
    )
