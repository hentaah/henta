"""Authenticated FastAPI surface for the HentaCatcherBot Mini App.

Run locally after installing FastAPI/Uvicorn:
    uvicorn api:app --host 0.0.0.0 --port 8000

On Railway, use this Start Command:
    uvicorn api:app --host 0.0.0.0 --port $PORT

The lifespan hooks intentionally use ``bot.db.pool.init_pool`` / ``close_pool``
and each endpoint obtains its connection through ``bot.db.pool.get_pool``. No
new asyncpg pool is created in this file.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
import hashlib
import logging
import hmac
import json
import os
from pathlib import Path as FileSystemPath
import time
from typing import AsyncIterator
from urllib.parse import parse_qsl, quote

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Path
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from bot.config import settings
from bot.db.pool import close_pool, get_pool, init_pool
from bot.services.game import (
    DAILY_REWARD_INTERVAL_SECONDS,
    burn_harem_copy,
    claim_daily_reward,
)
from bot.services.market import (
    create_player_market_listing,
    list_market_listings,
    parse_market_price,
    purchase_market_listing,
    remove_player_market_listing,
)
from bot.services.love import (
    LOVE_LEVEL_TARGETS,
    LOVE_MAX_LEVEL,
    create_love_market_listing,
    get_love_overview,
    perform_love_action,
    purchase_love_card,
    select_active_love,
)
from bot.services.neko import (
    DAILY_NEKO_REWARD_INTERVAL_SECONDS,
    claim_daily_neko_reward,
)
from bot.services.permissions import is_bot_owner

log = logging.getLogger("HentaCatcher.api")
MINIAPP_DIRECTORY = FileSystemPath(__file__).resolve().parent / "miniapp"

# Telegram Web App init data is intentionally short-lived. A page reload obtains
# fresh signed data from Telegram, while a captured old header cannot be reused
# indefinitely against the API.
TELEGRAM_INIT_DATA_MAX_AGE_SECONDS = 24 * 60 * 60
TELEGRAM_INIT_DATA_FUTURE_SKEW_SECONDS = 5 * 60
TELEGRAM_IMAGE_TIMEOUT_SECONDS = 20.0
LEADERBOARD_AVATAR_CACHE_SECONDS = 15 * 60
LEADERBOARD_AVATAR_CACHE_MAX_ENTRIES = 32

# A small per-process cache prevents the ten visible leaderboard avatars from
# repeatedly asking Telegram for the same thumbnails. Image bytes never leave
# this server except through authenticated image responses.
_leaderboard_avatar_cache: dict[int, tuple[float, bytes, str]] = {}


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Open and close the project's existing asyncpg-pool singleton."""
    await init_pool()
    try:
        yield
    finally:
        await close_pool()


app = FastAPI(
    title="HentaCatcherBot API",
    version="1.13.0",
    description=(
        "Authenticated Telegram Mini App data: profiles, Daily Hub claims, Harem cards, "
        "global /topusers ranking, escrowed one-copy Neko market listings, and token-safe catalog-media proxying."
    ),
    lifespan=lifespan,
)

# The Mini App is hosted separately from this API. Wildcard origins are kept as
# requested; authorization is based on Telegram's signed initData header, not
# browser cookies or permissive origin checks.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health", include_in_schema=False)
async def health_check() -> dict[str, str]:
    """Identify the deployed Mini App API without exposing configuration."""
    return {
        "service": "HentaCatcher API",
        "status": "ok",
        "version": app.version,
    }


@dataclass(frozen=True)
class TelegramWebAppUser:
    """The Telegram identity authenticated from signed Mini App initData."""

    user_id: int
    username: str | None
    full_name: str


class UserStatsResponse(BaseModel):
    user_id: int
    username: str | None
    full_name: str
    total_claims: int
    harem_count: int
    unique_characters: int
    # Position uses the same stable total-claims ordering as /topusers.
    global_rank: int
    highest_rarity: str | None
    # Preserve an exact NUMERIC wallet value rather than silently converting a
    # large Decimal balance through a lossy JSON floating-point number.
    neko_balance: str
    created_at: datetime
    last_seen_at: datetime


class HaremCharacterResponse(BaseModel):
    harem_id: int
    character_id: int
    # A legacy Harem can retain a valid owned copy after its catalog row was
    # removed. Returning it as a placeholder is better than hiding the whole
    # collection behind an inner JOIN.
    catalog_available: bool
    name: str
    anime: str
    rarity: str
    rarity_score: int
    aliases: str
    event: str
    # Historic field name: this stores the Telegram file ID for either a card
    # photo or a per-character video. The Mini App resolves it through the
    # authenticated /image proxy endpoint using the separate media type.
    image_url: str
    media_type: str = "photo"
    is_favorite: bool
    is_ranked: bool
    # Older production Harem rows may predate this timestamp; the Mini App
    # still renders the card instead of failing the whole collection response.
    acquired_at: datetime | None
    # Love metadata is optional so legacy/non-Love cards keep the exact
    # previous response shape while Love cards render their current level art.
    love_code: str | None = None
    love_level: int | None = None
    love_level_points: int | None = None
    love_next_target: int | None = None
    love_is_active: bool = False
    love_base_price: str | None = None


class HaremCompletionResponse(BaseModel):
    """Current owned unique-catalog progress for the signed user's Harem."""

    owned_unique: int
    catalog_total: int
    percentage: int


class HaremResponse(BaseModel):
    user_id: int
    characters: list[HaremCharacterResponse]
    completion: HaremCompletionResponse


class LeaderboardEntryResponse(BaseModel):
    rank: int
    user_id: int
    username: str | None
    full_name: str
    total_claims: int
    highest_rarity: str | None


class GlobalLeaderboardResponse(BaseModel):
    """Matches the rows and total-claims ranking basis of the bot's /topusers."""

    leaderboard: list[LeaderboardEntryResponse]


class LeaderboardResponse(BaseModel):
    """The retained per-chat leaderboard response for /leaderboard/{chat_id}."""

    chat_id: int
    leaderboard: list[LeaderboardEntryResponse]


class MarketListingResponse(BaseModel):
    listing_id: int
    character_id: int
    name: str
    anime: str
    rarity: str
    rarity_score: int
    aliases: str
    event: str
    image_url: str
    media_type: str = "photo"
    price: str
    created_at: datetime
    updated_at: datetime
    # The actual seller identity remains private; these server-calculated flags
    # are sufficient for the Mini App to disable/allow the correct controls.
    listing_kind: str = "catalog"
    is_own_listing: bool = False
    can_buy: bool = True
    can_remove: bool = False
    love_code: str | None = None
    love_base_price: str | None = None


class MarketResponse(BaseModel):
    listings: list[MarketListingResponse]


class MarketPurchaseResponse(BaseModel):
    listing_id: int
    price: str
    balance_after: str
    total_claims: int
    harem_character: HaremCharacterResponse


class PlayerMarketListingRequest(BaseModel):
    """A browser can request a price, never claim an arbitrary character."""

    harem_id: int
    price: str


class PlayerMarketListingResponse(BaseModel):
    listing: MarketListingResponse


class MarketRemoveResponse(BaseModel):
    listing_id: int
    returned_to_seller: bool


class LoveCardResponse(BaseModel):
    character_id: int
    love_code: str
    name: str
    anime: str
    base_price: str
    image_url: str
    media_type: str = "photo"
    level: int | None = None
    level_points: int | None = None
    next_target: int | None = None
    is_max: bool = False
    is_active: bool = False


class LoveOverviewResponse(BaseModel):
    active: LoveCardResponse | None = None
    owned: list[LoveCardResponse]
    catalog: list[LoveCardResponse]


class LoveActionRequest(BaseModel):
    action: str


class LoveActionResponse(BaseModel):
    action: str
    state: LoveCardResponse
    points_awarded: int
    date_cost: str
    reward: str
    balance_after: str
    leveled_up: bool
    became_max: bool
    cooldown_until: datetime


class LoveSellResponse(BaseModel):
    listing_id: int
    love_code: str
    name: str
    price: str


class BurnHaremCopyResponse(BaseModel):
    harem_id: int
    character_id: int
    name: str
    rarity: str
    reward: str
    balance_after: str
    remaining_copies: int
    total_claims: int


class DailyCooldownResponse(BaseModel):
    available: bool
    remaining_seconds: int


class DailyHubResponse(BaseModel):
    """The two independent daily claims exposed in the Mini App."""

    daily_card: DailyCooldownResponse
    daily_neko: DailyCooldownResponse


class DailyCardRewardResponse(BaseModel):
    character_id: int
    name: str
    anime: str
    rarity: str
    rarity_score: int
    event: str
    image_url: str
    media_type: str = "photo"


class DailyCardClaimResponse(BaseModel):
    reward: DailyCardRewardResponse


class DailyNekoClaimResponse(BaseModel):
    reward: str
    balance: str


def _unauthorized_init_data(detail: str = "Invalid Telegram Mini App authorization") -> None:
    raise HTTPException(status_code=401, detail=detail)


async def require_telegram_web_app_user(
    x_telegram_init_data: str | None = Header(
        default=None,
        alias="X-Telegram-Init-Data",
    ),
) -> TelegramWebAppUser:
    """Verify Telegram Web App initData and return its signed user identity.

    ``initDataUnsafe`` is useful for immediate client rendering but can be
    modified in a browser. Every API request instead verifies the original
    ``Telegram.WebApp.initData`` HMAC with the bot token.
    """
    if not x_telegram_init_data:
        _unauthorized_init_data("Missing Telegram Mini App authorization")

    pairs = parse_qsl(x_telegram_init_data, keep_blank_values=True)
    if not pairs:
        _unauthorized_init_data()

    values: dict[str, str] = {}
    for key, value in pairs:
        # A duplicate key could make the browser and server interpret different
        # canonical values, so it is rejected instead of silently overwriting.
        if key in values:
            _unauthorized_init_data()
        values[key] = value

    supplied_hash = values.pop("hash", "")
    if not supplied_hash:
        _unauthorized_init_data()

    auth_date_raw = values.get("auth_date")
    try:
        auth_date = int(auth_date_raw) if auth_date_raw is not None else 0
    except (TypeError, ValueError):
        _unauthorized_init_data()

    now = int(time.time())
    if (
        auth_date <= 0
        or now - auth_date > TELEGRAM_INIT_DATA_MAX_AGE_SECONDS
        or auth_date - now > TELEGRAM_INIT_DATA_FUTURE_SKEW_SECONDS
    ):
        _unauthorized_init_data("Expired Telegram Mini App authorization")

    data_check_string = "\n".join(
        f"{key}={value}" for key, value in sorted(values.items())
    )
    secret_key = hmac.new(
        b"WebAppData",
        settings.bot_token.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    calculated_hash = hmac.new(
        secret_key,
        data_check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(calculated_hash, supplied_hash):
        _unauthorized_init_data()

    try:
        user_payload = json.loads(values["user"])
        if not isinstance(user_payload, dict):
            raise ValueError("user payload is not an object")
        raw_user_id = user_payload["id"]
        if isinstance(raw_user_id, bool):
            raise ValueError("boolean is not a Telegram user id")
        user_id = int(raw_user_id)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        _unauthorized_init_data()

    if user_id <= 0:
        _unauthorized_init_data()

    raw_username = user_payload.get("username")
    username = str(raw_username).strip() if isinstance(raw_username, str) else ""
    name_parts = [
        str(user_payload.get(key) or "").strip()
        for key in ("first_name", "last_name")
    ]
    full_name = " ".join(part for part in name_parts if part) or username or f"User {user_id}"
    return TelegramWebAppUser(
        user_id=user_id,
        username=username or None,
        full_name=full_name,
    )


async def _ensure_authenticated_user(viewer: TelegramWebAppUser) -> None:
    """Create/refresh the signed user's profile for private Mini App launches."""
    pool = get_pool()
    async with pool.acquire() as connection:
        await connection.execute(
            """
            INSERT INTO users (user_id, username, full_name, last_seen_at)
            VALUES ($1, $2, $3, NOW())
            ON CONFLICT (user_id) DO UPDATE SET
                username = COALESCE(EXCLUDED.username, users.username),
                full_name = CASE
                    WHEN BTRIM($3) <> '' THEN $3
                    ELSE users.full_name
                END,
                last_seen_at = NOW()
            """,
            viewer.user_id,
            viewer.username,
            viewer.full_name,
        )



async def _require_miniapp_action_allowed(viewer: TelegramWebAppUser) -> None:
    """Apply the bot-wide ban/manual-mute rules to state-changing Mini App calls."""
    await _ensure_authenticated_user(viewer)
    pool = get_pool()
    async with pool.acquire() as connection:
        state = await connection.fetchrow(
            """
            SELECT
                is_banned,
                manual_muted_until,
                manual_muted_until IS NOT NULL AND manual_muted_until > NOW() AS is_muted
            FROM users
            WHERE user_id = $1
            """,
            viewer.user_id,
        )
    if not state:
        raise HTTPException(status_code=403, detail="Your Mini App account is unavailable.")
    if bool(state["is_banned"]):
        raise HTTPException(status_code=403, detail="This account cannot use game actions.")
    if bool(state["is_muted"]):
        raise HTTPException(status_code=403, detail="You are temporarily muted from bot actions.")


async def _daily_hub_cooldowns(user_id: int) -> tuple[DailyCooldownResponse, DailyCooldownResponse]:
    """Read both independent rolling 24-hour timers from one safe user row."""
    pool = get_pool()
    async with pool.acquire() as connection:
        row = await connection.fetchrow(
            """
            SELECT
                CASE
                    WHEN daily_claimed_at IS NOT NULL
                     AND daily_claimed_at + ($2::BIGINT * INTERVAL '1 second') > NOW()
                    THEN CEIL(EXTRACT(EPOCH FROM (
                        daily_claimed_at + ($2::BIGINT * INTERVAL '1 second') - NOW()
                    )))::BIGINT
                    ELSE 0
                END AS daily_card_remaining_seconds,
                CASE
                    WHEN daily_neko_claimed_at IS NOT NULL
                     AND daily_neko_claimed_at + ($3::BIGINT * INTERVAL '1 second') > NOW()
                    THEN CEIL(EXTRACT(EPOCH FROM (
                        daily_neko_claimed_at + ($3::BIGINT * INTERVAL '1 second') - NOW()
                    )))::BIGINT
                    ELSE 0
                END AS daily_neko_remaining_seconds
            FROM users
            WHERE user_id = $1
            """,
            user_id,
            DAILY_REWARD_INTERVAL_SECONDS,
            DAILY_NEKO_REWARD_INTERVAL_SECONDS,
        )
    card_remaining = max(0, int(row["daily_card_remaining_seconds"] or 0)) if row else 0
    neko_remaining = max(0, int(row["daily_neko_remaining_seconds"] or 0)) if row else 0
    return (
        DailyCooldownResponse(available=card_remaining == 0, remaining_seconds=card_remaining),
        DailyCooldownResponse(available=neko_remaining == 0, remaining_seconds=neko_remaining),
    )


async def _daily_hub_for_viewer(viewer: TelegramWebAppUser) -> DailyHubResponse:
    """Compose the signed user's two standalone daily-claim cooldowns."""
    await _ensure_authenticated_user(viewer)
    daily_card, daily_neko = await _daily_hub_cooldowns(viewer.user_id)
    return DailyHubResponse(daily_card=daily_card, daily_neko=daily_neko)


def _require_current_user(requested_user_id: int, viewer: TelegramWebAppUser) -> None:
    """Prevent a Mini App visitor from reading another user's private Harem."""
    if requested_user_id != viewer.user_id:
        raise HTTPException(status_code=403, detail="You can only access your own user data")


async def _require_visible_image(user_id: int, file_id: str) -> str:
    """Return the allowed catalog medium kind for a signed viewer/file ID.

    The historic route name remains ``/image`` for compatibility, but it now
    safely serves either an owned/listed photo or an owned/listed video. The
    kind comes from PostgreSQL, never from a browser-controlled query string.
    """
    pool = get_pool()
    async with pool.acquire() as connection:
        row = await connection.fetchrow(
            """
            SELECT media_type
            FROM (
                SELECT COALESCE(c.media_type, 'photo') AS media_type, 1 AS precedence
                FROM harem AS h
                JOIN characters AS c ON c.id = h.character_id
                WHERE h.user_id = $1 AND c.image_url = $2

                UNION ALL

                -- Current owned Love-level art is private to its owner. The
                -- selected state determines which of Levels 1–5 is visible.
                SELECT COALESCE(level_media.media_type, 'photo') AS media_type, 1 AS precedence
                FROM love_player_states AS love_state
                JOIN love_card_levels AS level_media
                  ON level_media.character_id = love_state.character_id
                 AND level_media.level = love_state.level
                WHERE love_state.user_id = $1
                  AND love_state.is_escrowed = FALSE
                  AND level_media.image_url = $2

                UNION ALL

                SELECT COALESCE(c.media_type, 'photo') AS media_type, 2 AS precedence
                FROM market_listings AS listing
                JOIN characters AS c ON c.id = listing.character_id
                WHERE c.image_url = $2

                UNION ALL

                -- Lovelist is public catalog art, but only Level 1 is shown
                -- before ownership; higher level images remain previewed by
                -- the bot's intentional /check L-code level command.
                SELECT COALESCE(level_one.media_type, 'photo') AS media_type, 3 AS precedence
                FROM love_card_levels AS level_one
                WHERE level_one.level = 1 AND level_one.image_url = $2
            ) AS visible_catalog_media
            ORDER BY precedence ASC
            LIMIT 1
            """,
            user_id,
            file_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Media not found")
    return "video" if str(row["media_type"] or "").strip().casefold() == "video" else "photo"

def _image_media_type(content_type: str | None, content: bytes) -> str | None:
    """Accept Telegram's normal image header or safely infer common image bytes.

    Telegram file downloads occasionally use ``application/octet-stream`` even
    for a photo. Checking recognizable magic bytes keeps legitimate catalog art
    visible while still refusing an HTML/JSON error body as an image.
    """
    normalized = (content_type or "").split(";", 1)[0].strip().lower()
    if normalized.startswith("image/"):
        return normalized
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(content) >= 12 and content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "image/webp"
    if content.startswith(b"BM"):
        return "image/bmp"
    return None


def _video_media_type(
    content_type: str | None,
    content: bytes,
    file_path: str | None = None,
) -> str | None:
    """Verify a Telegram video response without accepting arbitrary documents."""
    normalized = (content_type or "").split(";", 1)[0].strip().lower()
    if normalized.startswith("video/"):
        return normalized
    # ISO Base Media / MP4 stores its `ftyp` box at byte offset four; WebM/MKV
    # begins with EBML. These fallbacks cover Telegram's octet-stream responses.
    if len(content) >= 12 and content[4:8] == b"ftyp":
        return "video/mp4"
    if content.startswith(b"\x1aE\xdf\xa3"):
        return "video/webm"
    suffix = (file_path or "").rsplit(".", 1)[-1].casefold()
    if suffix in {"mp4", "m4v", "mov"}:
        return "video/mp4"
    if suffix == "webm":
        return "video/webm"
    return None


async def _download_telegram_image(
    file_id: str,
    *,
    expected_kind: str = "photo",
) -> tuple[bytes, str]:
    """Download a verified catalog photo/video without exposing the bot token."""
    telegram_get_file_url = f"https://api.telegram.org/bot{settings.bot_token}/getFile"
    try:
        async with httpx.AsyncClient(timeout=TELEGRAM_IMAGE_TIMEOUT_SECONDS) as client:
            metadata_response = await client.get(
                telegram_get_file_url,
                params={"file_id": file_id},
            )
            if metadata_response.status_code in {400, 404}:
                raise HTTPException(status_code=404, detail="Media not found")
            metadata_response.raise_for_status()
            metadata = metadata_response.json()
            result = (
                metadata.get("result")
                if isinstance(metadata, dict) and metadata.get("ok") is True
                else None
            )
            file_path = result.get("file_path") if isinstance(result, dict) else None
            if not isinstance(file_path, str) or not file_path:
                raise HTTPException(status_code=404, detail="Media not found")

            # This URL is used only server-side. It is never returned to a
            # browser, so the bot token cannot leak through an image URL.
            telegram_download_url = (
                f"https://api.telegram.org/file/bot{settings.bot_token}/"
                f"{quote(file_path, safe='/')}"
            )
            image_response = await client.get(telegram_download_url)
            image_response.raise_for_status()
    except httpx.HTTPError as error:
        raise HTTPException(
            status_code=502,
            detail="Telegram media request failed",
        ) from error
    except ValueError as error:
        raise HTTPException(
            status_code=502,
            detail="Telegram returned an invalid media response",
        ) from error

    image_bytes = image_response.content
    if expected_kind == "video":
        media_type = _video_media_type(
            image_response.headers.get("content-type"),
            image_bytes,
            str(file_path),
        )
        if not media_type:
            raise HTTPException(status_code=502, detail="Telegram file is not a video")
    else:
        media_type = _image_media_type(image_response.headers.get("content-type"), image_bytes)
        if not media_type:
            raise HTTPException(status_code=502, detail="Telegram file is not an image")
    return image_bytes, media_type


async def _is_visible_global_leader(user_id: int) -> bool:
    """Limit leaderboard-photo proxying to users already visible in /leaderboard."""
    pool = get_pool()
    async with pool.acquire() as connection:
        return bool(
            await connection.fetchval(
                """
                SELECT 1
                FROM (
                    SELECT user_id
                    FROM users
                    ORDER BY total_claims DESC, user_id ASC
                    LIMIT 10
                ) AS visible_leaders
                WHERE user_id = $1
                """,
                user_id,
            )
        )


def _cached_leaderboard_avatar(user_id: int) -> tuple[bytes, str] | None:
    now = time.monotonic()
    cached = _leaderboard_avatar_cache.get(user_id)
    if not cached:
        return None
    expires_at, image_bytes, media_type = cached
    if expires_at > now:
        return image_bytes, media_type
    _leaderboard_avatar_cache.pop(user_id, None)
    return None


def _cache_leaderboard_avatar(user_id: int, image_bytes: bytes, media_type: str) -> None:
    now = time.monotonic()
    expired_ids = [
        cached_user_id
        for cached_user_id, (expires_at, _bytes, _type) in _leaderboard_avatar_cache.items()
        if expires_at <= now
    ]
    for cached_user_id in expired_ids:
        _leaderboard_avatar_cache.pop(cached_user_id, None)
    if len(_leaderboard_avatar_cache) >= LEADERBOARD_AVATAR_CACHE_MAX_ENTRIES:
        oldest_user_id = min(
            _leaderboard_avatar_cache,
            key=lambda cached_user_id: _leaderboard_avatar_cache[cached_user_id][0],
        )
        _leaderboard_avatar_cache.pop(oldest_user_id, None)
    _leaderboard_avatar_cache[user_id] = (
        now + LEADERBOARD_AVATAR_CACHE_SECONDS,
        image_bytes,
        media_type,
    )


async def _download_leaderboard_avatar(user_id: int) -> tuple[bytes, str]:
    """Fetch the smallest available current Telegram profile photo for one user."""
    cached = _cached_leaderboard_avatar(user_id)
    if cached:
        return cached

    telegram_profile_photos_url = (
        f"https://api.telegram.org/bot{settings.bot_token}/getUserProfilePhotos"
    )
    try:
        async with httpx.AsyncClient(timeout=TELEGRAM_IMAGE_TIMEOUT_SECONDS) as client:
            response = await client.get(
                telegram_profile_photos_url,
                params={"user_id": user_id, "limit": 1},
            )
            if response.status_code in {400, 404}:
                raise HTTPException(status_code=404, detail="Profile photo unavailable")
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPError as error:
        raise HTTPException(
            status_code=502,
            detail="Telegram profile photo request failed",
        ) from error
    except ValueError as error:
        raise HTTPException(
            status_code=502,
            detail="Telegram returned an invalid profile-photo response",
        ) from error

    result = payload.get("result") if isinstance(payload, dict) and payload.get("ok") is True else None
    photos = result.get("photos") if isinstance(result, dict) else None
    first_photo_sizes = photos[0] if isinstance(photos, list) and photos else None
    if not isinstance(first_photo_sizes, list) or not first_photo_sizes:
        raise HTTPException(status_code=404, detail="Profile photo unavailable")

    valid_sizes = [size for size in first_photo_sizes if isinstance(size, dict)]
    if not valid_sizes:
        raise HTTPException(status_code=404, detail="Profile photo unavailable")
    # A leaderboard row only needs a thumbnail, not the full-resolution image.
    smallest_size = min(
        valid_sizes,
        key=lambda size: int(size.get("file_size") or size.get("width") or 0),
    )
    file_id = smallest_size.get("file_id")
    if not isinstance(file_id, str) or not file_id:
        raise HTTPException(status_code=404, detail="Profile photo unavailable")

    image_bytes, media_type = await _download_telegram_image(file_id)
    _cache_leaderboard_avatar(user_id, image_bytes, media_type)
    return image_bytes, media_type


@app.get("/user/{user_id}", response_model=UserStatsResponse)
async def get_user(
    user_id: int = Path(..., ge=1),
    viewer: TelegramWebAppUser = Depends(require_telegram_web_app_user),
) -> UserStatsResponse:
    """Return global collection and wallet statistics for the signed-in user."""
    _require_current_user(user_id, viewer)
    await _ensure_authenticated_user(viewer)
    pool = get_pool()
    async with pool.acquire() as connection:
        row = await connection.fetchrow(
            """
            WITH global_positions AS (
                SELECT
                    user_id,
                    ROW_NUMBER() OVER (
                        ORDER BY total_claims DESC, user_id ASC
                    ) AS global_rank
                FROM users
            )
            SELECT
                u.user_id,
                u.username,
                u.full_name,
                u.total_claims,
                u.highest_rarity,
                u.neko_balance,
                u.created_at,
                u.last_seen_at,
                positions.global_rank,
                COUNT(h.id) FILTER (
                    WHERE LOWER(BTRIM(COALESCE(owned_character.rarity, ''))) <> 'love'
                ) AS harem_count,
                COUNT(DISTINCT CASE
                    WHEN LOWER(BTRIM(COALESCE(owned_character.rarity, ''))) <> 'love'
                    THEN h.character_id
                END) AS unique_characters
            FROM users AS u
            JOIN global_positions AS positions ON positions.user_id = u.user_id
            LEFT JOIN harem AS h ON h.user_id = u.user_id
            LEFT JOIN characters AS owned_character ON owned_character.id = h.character_id
            WHERE u.user_id = $1
            GROUP BY
                u.user_id,
                u.username,
                u.full_name,
                u.total_claims,
                u.highest_rarity,
                u.neko_balance,
                u.created_at,
                u.last_seen_at,
                positions.global_rank
            """,
            user_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail="User not found")

    return UserStatsResponse(
        user_id=int(row["user_id"]),
        username=str(row["username"]) if row["username"] else None,
        full_name=str(row["full_name"] or ""),
        total_claims=int(row["total_claims"] or 0),
        harem_count=int(row["harem_count"] or 0),
        unique_characters=int(row["unique_characters"] or 0),
        global_rank=int(row["global_rank"] or 1),
        highest_rarity=str(row["highest_rarity"]) if row["highest_rarity"] else None,
        neko_balance=str(row["neko_balance"]),
        created_at=row["created_at"],
        last_seen_at=row["last_seen_at"],
    )


@app.get("/harem/{user_id}", response_model=HaremResponse)
async def get_harem(
    user_id: int = Path(..., ge=1),
    viewer: TelegramWebAppUser = Depends(require_telegram_web_app_user),
) -> HaremResponse:
    """Return every globally owned Harem copy, including duplicate characters."""
    _require_current_user(user_id, viewer)
    await _ensure_authenticated_user(viewer)
    pool = get_pool()
    async with pool.acquire() as connection:
        rows = await connection.fetch(
            """
            SELECT
                h.id AS harem_id,
                -- Read optional newer Harem fields through the row JSON so a
                -- pre-migration production collection cannot make the entire
                -- Mini App Harem endpoint fail. Current-schema values remain
                -- unchanged; absent legacy fields receive their safe defaults.
                COALESCE((to_jsonb(h) ->> 'is_favorite')::BOOLEAN, FALSE) AS is_favorite,
                COALESCE((to_jsonb(h) ->> 'is_ranked')::BOOLEAN, TRUE) AS is_ranked,
                NULLIF(to_jsonb(h) ->> 'acquired_at', '')::TIMESTAMPTZ AS acquired_at,
                h.character_id AS character_id,
                (c.id IS NOT NULL) AS catalog_available,
                COALESCE(NULLIF(BTRIM(c.name), ''), CONCAT('Unknown character #', h.character_id::TEXT)) AS name,
                COALESCE(NULLIF(BTRIM(c.anime), ''), 'Catalog entry unavailable') AS anime,
                COALESCE(NULLIF(BTRIM(c.rarity), ''), 'Common') AS rarity,
                COALESCE(c.rarity_score, 0) AS rarity_score,
                COALESCE(c.aliases, '') AS aliases,
                COALESCE(c.event, '') AS event,
                COALESCE(level_media.image_url, c.image_url, '') AS image_url,
                COALESCE(level_media.media_type, c.media_type, 'photo') AS media_type,
                love_card.love_code AS love_code_number,
                love_card.base_price AS love_base_price,
                love_state.level AS love_level,
                love_state.level_points AS love_level_points,
                COALESCE(love_state.is_active, FALSE) AS love_is_active
            FROM harem AS h
            LEFT JOIN characters AS c ON c.id = h.character_id
            LEFT JOIN love_cards AS love_card ON love_card.character_id = h.character_id
            LEFT JOIN love_player_states AS love_state
              ON love_state.character_id = h.character_id
             AND love_state.user_id = h.user_id
             AND love_state.is_escrowed = FALSE
            LEFT JOIN love_card_levels AS level_media
              ON level_media.character_id = h.character_id
             AND level_media.level = love_state.level
            WHERE h.user_id = $1
            ORDER BY
                NULLIF(to_jsonb(h) ->> 'acquired_at', '')::TIMESTAMPTZ DESC NULLS LAST,
                h.id DESC
            """,
            user_id,
        )
        completion_row = await connection.fetchrow(
            """
            SELECT
                COUNT(*)::BIGINT AS catalog_total,
                COUNT(DISTINCT owned.character_id)::BIGINT AS owned_unique
            FROM characters AS catalog
            LEFT JOIN harem AS owned
              ON owned.character_id = catalog.id AND owned.user_id = $1
            WHERE BTRIM(COALESCE(catalog.name, '')) <> ''
              AND LOWER(BTRIM(COALESCE(catalog.rarity, ''))) NOT IN ('epic', 'love')
            """,
            user_id,
        )

    missing_catalog_count = sum(not bool(row["catalog_available"]) for row in rows)
    log.info(
        "Mini App Harem for user %s: %s card(s), %s missing catalog record(s)",
        user_id,
        len(rows),
        missing_catalog_count,
    )
    catalog_total = max(0, int(completion_row["catalog_total"] or 0)) if completion_row else 0
    owned_unique = max(0, int(completion_row["owned_unique"] or 0)) if completion_row else 0
    completion = HaremCompletionResponse(
        owned_unique=min(owned_unique, catalog_total) if catalog_total else 0,
        catalog_total=catalog_total,
        percentage=(round((min(owned_unique, catalog_total) / catalog_total) * 100) if catalog_total else 0),
    )
    return HaremResponse(
        user_id=user_id,
        completion=completion,
        characters=[
            HaremCharacterResponse(
                harem_id=int(row["harem_id"]),
                character_id=int(row["character_id"]),
                catalog_available=bool(row["catalog_available"]),
                name=str(row["name"]),
                anime=str(row["anime"]),
                rarity=str(row["rarity"]),
                rarity_score=int(row["rarity_score"] or 0),
                aliases=str(row["aliases"] or ""),
                event=str(row["event"] or ""),
                image_url=str(row["image_url"]),
                media_type=str(row["media_type"] or "photo"),
                is_favorite=bool(row["is_favorite"]),
                is_ranked=bool(row["is_ranked"]),
                acquired_at=row["acquired_at"],
                love_code=(f"L{int(row['love_code_number'])}" if row["love_code_number"] is not None else None),
                love_level=(int(row["love_level"]) if row["love_level"] is not None else None),
                love_level_points=(int(row["love_level_points"]) if row["love_level_points"] is not None else None),
                love_next_target=(
                    int(LOVE_LEVEL_TARGETS.get(int(row["love_level"]), 0))
                    if row["love_level"] is not None else None
                ),
                love_is_active=bool(row["love_is_active"]),
                love_base_price=(str(row["love_base_price"]) if row["love_base_price"] is not None else None),
            )
            for row in rows
        ],
    )


@app.get("/leaderboard", response_model=GlobalLeaderboardResponse)
async def get_global_leaderboard(
    viewer: TelegramWebAppUser = Depends(require_telegram_web_app_user),
) -> GlobalLeaderboardResponse:
    """Return the exact global top-ten basis used by the bot's /topusers."""
    await _ensure_authenticated_user(viewer)
    pool = get_pool()
    async with pool.acquire() as connection:
        rows = await connection.fetch(
            """
            SELECT
                u.user_id,
                u.username,
                u.full_name,
                u.total_claims,
                u.highest_rarity
            FROM users AS u
            ORDER BY u.total_claims DESC, u.user_id ASC
            LIMIT 10
            """,
        )

    return GlobalLeaderboardResponse(
        leaderboard=[
            LeaderboardEntryResponse(
                rank=index,
                user_id=int(row["user_id"]),
                username=str(row["username"]) if row["username"] else None,
                full_name=str(row["full_name"] or ""),
                total_claims=int(row["total_claims"] or 0),
                highest_rarity=(
                    str(row["highest_rarity"])
                    if row["highest_rarity"]
                    else None
                ),
            )
            for index, row in enumerate(rows, start=1)
        ],
    )


@app.get("/leaderboard/avatar/{user_id}")
async def get_global_leaderboard_avatar(
    user_id: int = Path(..., ge=1),
    viewer: TelegramWebAppUser = Depends(require_telegram_web_app_user),
) -> Response:
    """Return one visible global leader's Telegram avatar as a safe byte proxy."""
    if not await _is_visible_global_leader(user_id):
        raise HTTPException(status_code=404, detail="Leaderboard profile photo not found")
    image_bytes, media_type = await _download_leaderboard_avatar(user_id)
    return Response(
        content=image_bytes,
        media_type=media_type,
        headers={
            "Cache-Control": "private, max-age=900",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.get("/leaderboard/{chat_id}", response_model=LeaderboardResponse)
async def get_chat_leaderboard(
    chat_id: int,
    viewer: TelegramWebAppUser = Depends(require_telegram_web_app_user),
) -> LeaderboardResponse:
    """Retain a signed visitor's per-chat top ten by in-chat claim count."""
    await _ensure_authenticated_user(viewer)
    pool = get_pool()
    async with pool.acquire() as connection:
        group_exists = await connection.fetchval(
            "SELECT 1 FROM groups WHERE chat_id = $1",
            chat_id,
        )
        if not group_exists:
            raise HTTPException(status_code=404, detail="Chat not found")
        rows = await connection.fetch(
            """
            SELECT
                u.user_id,
                u.username,
                u.full_name,
                u.highest_rarity,
                stats.claims AS total_claims
            FROM user_group_stats AS stats
            JOIN users AS u ON u.user_id = stats.user_id
            WHERE stats.chat_id = $1
            ORDER BY stats.claims DESC, u.user_id ASC
            LIMIT 10
            """,
            chat_id,
        )

    return LeaderboardResponse(
        chat_id=chat_id,
        leaderboard=[
            LeaderboardEntryResponse(
                rank=index,
                user_id=int(row["user_id"]),
                username=str(row["username"]) if row["username"] else None,
                full_name=str(row["full_name"] or ""),
                total_claims=int(row["total_claims"] or 0),
                highest_rarity=(
                    str(row["highest_rarity"])
                    if row["highest_rarity"]
                    else None
                ),
            )
            for index, row in enumerate(rows, start=1)
        ],
    )


def _market_listing_response(
    listing: dict[str, object],
    *,
    viewer_id: int,
    viewer_is_full_owner: bool,
) -> MarketListingResponse:
    """Expose market controls as server-calculated flags, never seller IDs."""
    kind = str(listing.get("listing_kind") or "catalog")
    seller_raw = listing.get("seller_user_id")
    seller_id = int(seller_raw) if isinstance(seller_raw, int) else None
    is_own_listing = kind == "player" and seller_id == viewer_id
    is_love_listing = str(listing.get("rarity") or "").strip().casefold() == "love"
    return MarketListingResponse(
        listing_id=int(listing["listing_id"]),
        character_id=int(listing["character_id"]),
        name=str(listing["name"]),
        anime=str(listing["anime"]),
        rarity=str(listing["rarity"]),
        rarity_score=int(listing["rarity_score"]),
        aliases=str(listing["aliases"]),
        event=str(listing["event"]),
        image_url=str(listing["image_url"]),
        media_type=str(listing.get("media_type") or "photo"),
        price=str(listing["price"]),
        created_at=listing["created_at"],
        updated_at=listing["updated_at"],
        listing_kind=kind,
        is_own_listing=is_own_listing,
        # Members cannot buy their own escrow; regular cards retain the owner
        # exception. A Love self-purchase is deliberately blocked so escrowed
        # level/points can only return through cancellation, never reset.
        can_buy=((not is_own_listing) or viewer_is_full_owner) and not (is_love_listing and is_own_listing),
        can_remove=is_own_listing or viewer_is_full_owner,
        love_code=(str(listing["love_code"]) if listing.get("love_code") else None),
        love_base_price=(str(listing["love_base_price"]) if listing.get("love_base_price") is not None else None),
    )


@app.get("/daily-hub", response_model=DailyHubResponse)
async def get_daily_hub(
    viewer: TelegramWebAppUser = Depends(require_telegram_web_app_user),
) -> DailyHubResponse:
    """Return the signed user's real standalone daily-claim cooldowns."""
    return await _daily_hub_for_viewer(viewer)


@app.post("/daily/claim", response_model=DailyCardClaimResponse)
async def claim_miniapp_daily_card(
    viewer: TelegramWebAppUser = Depends(require_telegram_web_app_user),
) -> DailyCardClaimResponse:
    """Claim the existing rolling card daily through the signed Mini App."""
    await _require_miniapp_action_allowed(viewer)
    result = await claim_daily_reward(
        user_id=viewer.user_id,
        username=viewer.username or "",
        full_name=viewer.full_name,
        # A Mini App reward is global and must not fabricate group statistics.
        chat_id=0,
    )
    if not result.success:
        if result.reason == "cooldown":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "daily_cooldown",
                    "message": "Your daily card is not ready yet.",
                    "remaining_seconds": result.remaining_seconds,
                },
            )
        if result.reason == "catalog_unavailable":
            raise HTTPException(
                status_code=503,
                detail="No eligible daily card is available right now. Your cooldown was not used.",
            )
        raise HTTPException(status_code=400, detail="Your daily card could not be claimed.")
    return DailyCardClaimResponse(
        reward=DailyCardRewardResponse(
            character_id=result.character_id,
            name=result.name,
            anime=result.anime,
            rarity=result.rarity,
            rarity_score=result.rarity_score,
            event=result.event,
            image_url=result.image_url,
            media_type=result.media_type,
        )
    )


@app.post("/dailyneko/claim", response_model=DailyNekoClaimResponse)
async def claim_miniapp_daily_neko(
    viewer: TelegramWebAppUser = Depends(require_telegram_web_app_user),
) -> DailyNekoClaimResponse:
    """Claim the existing independent 1–49 Neko daily through the Mini App."""
    await _require_miniapp_action_allowed(viewer)
    result = await claim_daily_neko_reward(
        user_id=viewer.user_id,
        username=viewer.username,
        full_name=viewer.full_name,
    )
    if not result.get("success"):
        reason = str(result.get("reason") or "")
        if reason == "cooldown":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "daily_neko_cooldown",
                    "message": "Your Daily Neko reward is not ready yet.",
                    "remaining_seconds": int(result.get("remaining_seconds") or 0),
                },
            )
        if reason == "balance_limit":
            raise HTTPException(
                status_code=409,
                detail="Your Neko wallet is at its safe balance limit.",
            )
        raise HTTPException(status_code=400, detail="Your Daily Neko reward could not be claimed.")
    return DailyNekoClaimResponse(
        reward=str(result["reward"]),
        balance=str(result["balance"]),
    )


@app.post("/harem/{harem_id}/burn", response_model=BurnHaremCopyResponse)
async def burn_miniapp_harem_copy(
    harem_id: int = Path(..., ge=1),
    viewer: TelegramWebAppUser = Depends(require_telegram_web_app_user),
) -> BurnHaremCopyResponse:
    """Burn exactly one signed user's non-Eternal copy for its fixed Neko value."""
    await _require_miniapp_action_allowed(viewer)
    result = await burn_harem_copy(user_id=viewer.user_id, harem_id=harem_id)
    if not result.success:
        reason = result.reason
        if reason == "harem_copy_not_found":
            raise HTTPException(status_code=404, detail="That Harem copy is no longer available.")
        if reason == "eternal_protected":
            raise HTTPException(
                status_code=403,
                detail="Eternal cards cannot be sold, gifted, traded, or burned.",
            )
        if reason == "overlord_protected":
            raise HTTPException(status_code=403, detail="OverLord cards cannot be burned.")
        if reason == "love_protected":
            raise HTTPException(status_code=403, detail="Love cards cannot be burned; list your active Love card through the Love panel instead.")
        if reason == "balance_limit":
            raise HTTPException(status_code=409, detail="Your Neko wallet is at its safe balance limit.")
        raise HTTPException(status_code=400, detail="This card cannot be burned.")
    return BurnHaremCopyResponse(
        harem_id=int(result.harem_id or harem_id),
        character_id=int(result.character_id or 0),
        name=result.character_name,
        rarity=result.rarity,
        reward=str(result.reward),
        balance_after=str(result.balance_after),
        remaining_copies=result.remaining_copies,
        total_claims=result.total_claims,
    )



def _love_card_response(raw: dict) -> LoveCardResponse:
    """Convert internal catalog/state dictionaries into a safe Mini App card."""
    levels = raw.get("levels") if isinstance(raw.get("levels"), dict) else {}
    level_one = levels.get(1) if isinstance(levels, dict) else None
    image_url = raw.get("image_url") or (level_one or {}).get("image_url") or raw.get("catalog_image_url") or ""
    media_type = raw.get("media_type") or (level_one or {}).get("media_type") or raw.get("catalog_media_type") or "photo"
    level_value = raw.get("level")
    level = int(level_value) if level_value is not None else None
    return LoveCardResponse(
        character_id=int(raw["character_id"]),
        love_code=str(raw["love_code"]),
        name=str(raw.get("name") or ""),
        anime=str(raw.get("anime") or ""),
        base_price=str(raw.get("base_price") or 0),
        image_url=str(image_url),
        media_type=str(media_type or "photo"),
        level=level,
        level_points=(int(raw["level_points"]) if raw.get("level_points") is not None else None),
        next_target=(int(raw["next_target"]) if raw.get("next_target") is not None else None),
        is_max=bool(raw.get("is_max")) if level is not None else False,
        is_active=bool(raw.get("is_active")),
    )


def _raise_love_action_error(result: dict) -> None:
    """Map durable Love service reasons to useful authenticated API failures."""
    reason = str(result.get("reason") or "")
    if reason in {"no_active_love", "not_owned"}:
        raise HTTPException(status_code=409, detail="Choose or buy an active Love card first.")
    if reason == "already_has_love":
        raise HTTPException(status_code=409, detail="You already own a Love card. Sell it before buying another one.")
    if reason == "love_not_found":
        raise HTTPException(status_code=404, detail="That Love card was not found.")
    if reason == "insufficient_neko":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "not_enough_neko",
                "message": "Not enough Neko for this Love action.",
                "price": str(result.get("price") or 0),
                "balance": str(result.get("balance") or 0),
            },
        )
    if reason == "cooldown":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "love_cooldown",
                "message": "This Love action is cooling down.",
                "remaining_seconds": int(result.get("remaining_seconds") or 0),
            },
        )
    if reason == "not_max":
        raise HTTPException(status_code=409, detail="Romance is available only at Love Level 5 MAX.")
    if reason == "max_no_points":
        raise HTTPException(status_code=409, detail="Dates no longer add points at Love Level 5 MAX.")
    if reason == "balance_limit":
        raise HTTPException(status_code=409, detail="Your Neko wallet is at its safe balance limit.")
    if reason == "ownership_lost":
        raise HTTPException(status_code=409, detail="That Love card is no longer available in your Harem.")
    raise HTTPException(status_code=400, detail="This Love action could not be completed.")


@app.get("/love", response_model=LoveOverviewResponse)
async def get_love(
    viewer: TelegramWebAppUser = Depends(require_telegram_web_app_user),
) -> LoveOverviewResponse:
    """Return public Lovelist cards plus the signed player's current Love state."""
    await _ensure_authenticated_user(viewer)
    overview = await get_love_overview(user_id=viewer.user_id)
    active_raw = overview.get("active")
    return LoveOverviewResponse(
        active=_love_card_response(active_raw) if isinstance(active_raw, dict) else None,
        owned=[_love_card_response(card) for card in overview.get("owned", [])],
        catalog=[_love_card_response(card) for card in overview.get("catalog", [])],
    )


@app.post("/love/buy/{love_code}", response_model=LoveCardResponse)
async def buy_love(
    love_code: str = Path(..., min_length=2, max_length=20),
    viewer: TelegramWebAppUser = Depends(require_telegram_web_app_user),
) -> LoveCardResponse:
    """Buy one owner-priced Lovelist card and initialize it at Level 1/0 XP."""
    await _require_miniapp_action_allowed(viewer)
    result = await purchase_love_card(
        user_id=viewer.user_id,
        love_code=love_code,
        username=viewer.username,
        full_name=viewer.full_name,
        chat_id=0,
    )
    if not result.get("success"):
        _raise_love_action_error(result)
    return _love_card_response(result["card"])


@app.post("/love/select/{love_code}", response_model=LoveCardResponse)
async def select_love(
    love_code: str = Path(..., min_length=2, max_length=20),
    viewer: TelegramWebAppUser = Depends(require_telegram_web_app_user),
) -> LoveCardResponse:
    """Select an owner-granted/owned Love card as the Mini App action target."""
    await _require_miniapp_action_allowed(viewer)
    result = await select_active_love(user_id=viewer.user_id, love_code=love_code)
    if not result.get("success"):
        _raise_love_action_error(result)
    return _love_card_response(result["state"])


@app.post("/love/action", response_model=LoveActionResponse)
async def run_love_action(
    request: LoveActionRequest,
    viewer: TelegramWebAppUser = Depends(require_telegram_web_app_user),
) -> LoveActionResponse:
    """Perform a safe Love interaction using the signed user's active card."""
    await _require_miniapp_action_allowed(viewer)
    requested_action = str(request.action or "").strip().casefold()
    # Mini App panel labels stay human-readable while the domain layer keeps
    # its explicit shared-date action names.
    action = {"restaurant": "date_restaurant", "cinema": "date_cinema"}.get(
        requested_action,
        requested_action,
    )
    result = await perform_love_action(
        user_id=viewer.user_id,
        action=action,
        username=viewer.username,
        full_name=viewer.full_name,
    )
    if not result.get("success"):
        _raise_love_action_error(result)
    return LoveActionResponse(
        action=str(result["action"]),
        state=_love_card_response(result["state"]),
        points_awarded=int(result["points_awarded"]),
        date_cost=str(result["date_cost"]),
        reward=str(result["reward"]),
        balance_after=str(result["balance_after"]),
        leveled_up=bool(result["leveled_up"]),
        became_max=bool(result["became_max"]),
        cooldown_until=result["cooldown_until"],
    )


@app.post("/love/sell", response_model=LoveSellResponse)
async def sell_love(
    viewer: TelegramWebAppUser = Depends(require_telegram_web_app_user),
) -> LoveSellResponse:
    """List the active Love card at its catalog owner's immutable original price."""
    await _require_miniapp_action_allowed(viewer)
    result = await create_love_market_listing(user_id=viewer.user_id)
    if not result.get("success"):
        _raise_love_action_error(result)
    return LoveSellResponse(
        listing_id=int(result["listing_id"]),
        love_code=str(result["love_code"]),
        name=str(result["name"]),
        price=str(result["price"]),
    )


@app.get("/market", response_model=MarketResponse)
async def get_market(
    viewer: TelegramWebAppUser = Depends(require_telegram_web_app_user),
) -> MarketResponse:
    """Return catalog-stock and player-copy listings with safe viewer controls."""
    await _ensure_authenticated_user(viewer)
    viewer_is_full_owner = await is_bot_owner(viewer.user_id)
    listings = await list_market_listings()
    return MarketResponse(
        listings=[
            _market_listing_response(
                listing,
                viewer_id=viewer.user_id,
                viewer_is_full_owner=viewer_is_full_owner,
            )
            for listing in listings
        ]
    )


@app.post("/market/list", response_model=PlayerMarketListingResponse)
async def create_miniapp_market_listing(
    request: PlayerMarketListingRequest,
    viewer: TelegramWebAppUser = Depends(require_telegram_web_app_user),
) -> PlayerMarketListingResponse:
    """Move exactly one signed user's Harem copy into player-sale escrow."""
    await _require_miniapp_action_allowed(viewer)
    price = parse_market_price(request.price)
    if price is None:
        raise HTTPException(
            status_code=422,
            detail="Enter a positive Neko price with at most two decimal places.",
        )
    result = await create_player_market_listing(
        harem_id=request.harem_id,
        seller_id=viewer.user_id,
        price=price,
    )
    if not result.get("success"):
        reason = str(result.get("reason") or "")
        if reason == "harem_copy_not_found":
            raise HTTPException(status_code=404, detail="That Harem copy is no longer available.")
        if reason == "eternal_protected":
            raise HTTPException(
                status_code=403,
                detail="Eternal cards cannot be sold, gifted, traded, or burned.",
            )
        if reason == "overlord_protected":
            raise HTTPException(
                status_code=403,
                detail="OverLord cards cannot be sold, gifted, or traded.",
            )
        if reason == "love_fixed_price_required":
            raise HTTPException(status_code=403, detail="Love cards must use their fixed original price through the Love panel.")
        if reason == "already_listed":
            raise HTTPException(status_code=409, detail="This Harem copy is already listed.")
        raise HTTPException(status_code=400, detail="This card could not be listed.")
    viewer_is_full_owner = await is_bot_owner(viewer.user_id)
    return PlayerMarketListingResponse(
        listing=_market_listing_response(
            result["listing"],
            viewer_id=viewer.user_id,
            viewer_is_full_owner=viewer_is_full_owner,
        )
    )


@app.delete("/market/{listing_id}", response_model=MarketRemoveResponse)
async def remove_miniapp_market_listing(
    listing_id: int = Path(..., ge=1),
    viewer: TelegramWebAppUser = Depends(require_telegram_web_app_user),
) -> MarketRemoveResponse:
    """Let a seller cancel their own escrow or a Full Owner moderate any listing."""
    await _require_miniapp_action_allowed(viewer)
    viewer_is_full_owner = await is_bot_owner(viewer.user_id)
    result = await remove_player_market_listing(
        listing_id=listing_id,
        actor_id=viewer.user_id,
        actor_is_full_owner=viewer_is_full_owner,
    )
    if not result.get("success"):
        reason = str(result.get("reason") or "")
        if reason == "listing_not_found":
            raise HTTPException(status_code=404, detail="This market listing is no longer available.")
        if reason == "not_allowed":
            raise HTTPException(status_code=403, detail="You cannot remove this market listing.")
        raise HTTPException(status_code=400, detail="This market listing could not be removed.")
    listing = result["listing"]
    return MarketRemoveResponse(
        listing_id=int(listing["listing_id"]),
        returned_to_seller=bool(result.get("returned_to_seller")),
    )


@app.post("/market/{listing_id}/buy", response_model=MarketPurchaseResponse)
async def buy_market_listing(
    listing_id: int = Path(..., ge=1),
    viewer: TelegramWebAppUser = Depends(require_telegram_web_app_user),
) -> MarketPurchaseResponse:
    """Buy one market listing with a safe buyer-to-seller wallet transfer."""
    await _require_miniapp_action_allowed(viewer)
    viewer_is_full_owner = await is_bot_owner(viewer.user_id)
    result = await purchase_market_listing(
        listing_id=listing_id,
        buyer_id=viewer.user_id,
        username=viewer.username,
        full_name=viewer.full_name,
        allow_self_purchase=viewer_is_full_owner,
    )
    if not result.get("success"):
        reason = str(result.get("reason") or "")
        if reason == "insufficient_neko":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "not_enough_neko",
                    "message": "Not enough Neko to buy this card.",
                    "price": str(result["price"]),
                    "balance": str(result["balance"]),
                },
            )
        if reason == "listing_unavailable":
            raise HTTPException(status_code=404, detail="This market listing is no longer available.")
        if reason == "self_purchase_forbidden":
            raise HTTPException(status_code=403, detail="You cannot buy your own listed card.")
        if reason == "seller_balance_limit":
            raise HTTPException(
                status_code=409,
                detail="The seller's Neko wallet cannot receive this sale right now.",
            )
        if reason == "eternal_protected":
            raise HTTPException(
                status_code=403,
                detail="Eternal cards cannot be sold, gifted, traded, or burned.",
            )
        if reason == "overlord_protected":
            raise HTTPException(
                status_code=403,
                detail="OverLord cards cannot be sold, gifted, or traded.",
            )
        if reason == "love_already_owned":
            raise HTTPException(status_code=409, detail="You already own a Love card and cannot buy another one.")
        if reason == "love_list_only":
            raise HTTPException(status_code=403, detail="Love cards are bought through Lovelist or approved fixed-price player sales only.")
        raise HTTPException(status_code=400, detail="Invalid market purchase request.")

    character = result["harem_character"]
    return MarketPurchaseResponse(
        listing_id=int(result["listing_id"]),
        price=str(result["price"]),
        balance_after=str(result["balance_after"]),
        total_claims=int(result["total_claims"]),
        harem_character=HaremCharacterResponse(
            harem_id=int(character["harem_id"]),
            character_id=int(character["character_id"]),
            catalog_available=True,
            name=str(character["name"]),
            anime=str(character["anime"]),
            rarity=str(character["rarity"]),
            rarity_score=int(character["rarity_score"]),
            aliases=str(character["aliases"]),
            event=str(character["event"]),
            image_url=str(character["image_url"]),
            media_type=str(character.get("media_type") or "photo"),
            is_favorite=bool(character["is_favorite"]),
            is_ranked=bool(character["is_ranked"]),
            acquired_at=character["acquired_at"],
        ),
    )


@app.get("/image/{file_id}")
async def get_harem_or_market_image(
    file_id: str = Path(..., min_length=1, max_length=1024),
    viewer: TelegramWebAppUser = Depends(require_telegram_web_app_user),
) -> Response:
    """Proxy visible Harem/Market media without exposing the bot token."""
    expected_kind = await _require_visible_image(viewer.user_id, file_id)
    image_bytes, media_type = await _download_telegram_image(
        file_id,
        expected_kind=expected_kind,
    )
    return Response(
        content=image_bytes,
        media_type=media_type,
        headers={
            "Cache-Control": "private, max-age=3600",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.get("/", include_in_schema=False)
@app.get("/index.html", include_in_schema=False)
async def get_miniapp_index() -> FileResponse:
    """Always revalidate the Mini App shell after a Railway deployment."""
    return FileResponse(
        MINIAPP_DIRECTORY / "index.html",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


# Serve the packaged Mini App from the same Railway service as the API. This
# mount is deliberately registered after all API routes, so /user, /harem,
# /market, /leaderboard, /image, and /docs continue to resolve before the
# static root.
app.mount(
    "/",
    StaticFiles(directory=MINIAPP_DIRECTORY, html=True),
    name="miniapp",
)


if __name__ == "__main__":
    import uvicorn

    # Railway supplies PORT dynamically. Local starts keep the familiar 8000.
    uvicorn.run(
        "api:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
        reload=False,
    )
