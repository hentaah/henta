"""Durable player-facing Love-card collection, progression, and fixed-price sale rules.

Love is a catalog-only relationship tier.  It never enters automatic spawning;
players acquire a normal Love card through ``/lovelist`` or an owner grant, then
progress one selected active Love card through safe, non-explicit interactions.
"""
from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from math import ceil
from typing import Any

from bot.db.pool import get_pool
from bot.services.neko import (
    MAX_NEKO_BALANCE,
    NEKO_QUANTUM,
    ZERO_NEKO,
    _record_wallet_ledger,
)

LOVE_RARITY = "Love"
LOVE_EMOJI = "❤️"
LOVE_MIN_LEVEL = 1
LOVE_MAX_LEVEL = 5
# A Love card starts at Level 1. Each successful transition resets the next
# level's point bar to zero; excess points are intentionally not carried over.
LOVE_LEVEL_TARGETS: dict[int, int] = {
    1: 50,
    2: 100,
    3: 150,
    4: 200,
}
KISS_COOLDOWN_SECONDS = 15 * 60
HUG_COOLDOWN_SECONDS = 15 * 60
DATE_COOLDOWN_SECONDS = 3 * 60 * 60
ROMANCE_COOLDOWN_SECONDS = 24 * 60 * 60
RESTAURANT_DATE_PRICE = Decimal("1000.00")
CINEMA_DATE_PRICE = Decimal("3000.00")
RESTAURANT_DATE_POINTS = 5
CINEMA_DATE_POINTS = 8
KISS_POINTS = 2
HUG_POINTS = 5
MAX_INTERACTION_REWARD_MIN = 1
MAX_INTERACTION_REWARD_MAX = 20
ROMANCE_REWARD_MIN = 1
ROMANCE_REWARD_MAX = 49

_LOVE_CODE_RE = re.compile(r"^[Ll]([1-9][0-9]{0,18})$")


def is_love(value: object) -> bool:
    """Return whether a catalog rarity is the non-spawnable Love tier."""
    return str(value or "").strip().casefold() == LOVE_RARITY.casefold()


def love_code_label(number: int) -> str:
    return f"L{int(number)}"


def parse_love_code(value: object) -> int | None:
    """Parse only canonical positive Love codes such as ``L1`` or ``l42``."""
    if not isinstance(value, str):
        return None
    match = _LOVE_CODE_RE.fullmatch(value.strip())
    if not match:
        return None
    try:
        number = int(match.group(1))
    except ValueError:
        return None
    return number if 0 < number <= 9_223_372_036_854_775_807 else None


def _decimal(value: object) -> Decimal:
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise RuntimeError("invalid Love Neko amount") from error
    if not parsed.is_finite():
        raise RuntimeError("invalid Love Neko amount")
    return parsed.quantize(NEKO_QUANTUM, rounding=ROUND_HALF_UP)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _remaining_seconds(until: datetime | None, now: datetime) -> int:
    expiry = _utc(until)
    return max(0, ceil((expiry - now).total_seconds())) if expiry and expiry > now else 0


def _current_target(level: int) -> int:
    return int(LOVE_LEVEL_TARGETS.get(int(level), 0))


def _advance_points(level: int, points: int, gained: int) -> tuple[int, int, bool, bool]:
    """Return ``(level, points, leveled_up, became_max)`` after one action."""
    safe_level = min(LOVE_MAX_LEVEL, max(LOVE_MIN_LEVEL, int(level)))
    safe_points = max(0, int(points))
    if safe_level >= LOVE_MAX_LEVEL:
        return safe_level, safe_points, False, False
    target = _current_target(safe_level)
    if safe_points + max(0, int(gained)) < target:
        return safe_level, safe_points + max(0, int(gained)), False, False
    next_level = min(LOVE_MAX_LEVEL, safe_level + 1)
    # User-requested behavior: every level starts from zero rather than carrying
    # a small action's excess into the next threshold.
    return next_level, 0, True, next_level == LOVE_MAX_LEVEL


async def _ensure_love_user(
    conn,
    *,
    user_id: int,
    username: str | None = None,
    full_name: str | None = None,
) -> None:
    clean_name = (full_name or "").strip() or f"User {user_id}"
    clean_username = (username or "").strip() or None
    await conn.execute(
        """
        INSERT INTO users (user_id, username, full_name, last_seen_at)
        VALUES ($1, $2, $3, NOW())
        ON CONFLICT (user_id) DO UPDATE SET
            username = COALESCE(EXCLUDED.username, users.username),
            full_name = CASE WHEN BTRIM($3) <> '' THEN $3 ELSE users.full_name END,
            last_seen_at = NOW()
        """,
        user_id,
        clean_username,
        clean_name,
    )


async def create_love_card_in_transaction(
    conn,
    *,
    character_id: int,
    created_by: int,
    base_price: Decimal,
    level_media: dict[int, tuple[str, str]],
) -> int:
    """Attach one owner-created Love definition to its new catalog character.

    The caller owns the same catalog-ID advisory lock/transaction used by
    `/addcharacter`, so the catalog row and its five level assets either commit
    together or not at all.
    """
    if not isinstance(character_id, int) or character_id <= 0:
        raise ValueError("invalid Love character id")
    if not isinstance(created_by, int) or created_by <= 0:
        raise ValueError("invalid Love creator")
    price = _decimal(base_price)
    if price <= ZERO_NEKO:
        raise ValueError("invalid Love price")
    if set(level_media) != set(range(LOVE_MIN_LEVEL, LOVE_MAX_LEVEL + 1)):
        raise ValueError("Love cards require exactly five levels")
    normalized_media: list[tuple[int, str, str]] = []
    for level in range(LOVE_MIN_LEVEL, LOVE_MAX_LEVEL + 1):
        file_id, media_type = level_media[level]
        if not isinstance(file_id, str) or not file_id.strip():
            raise ValueError("invalid Love level media")
        # The user requested photos. Keeping every level photo-only makes every
        # owner-selected image safe for `/check L# <level>` and Mini App swaps.
        if str(media_type or "").strip().casefold() != "photo":
            raise ValueError("Love level media must be photos")
        normalized_media.append((level, file_id.strip(), "photo"))

    code_number = int(await conn.fetchval("SELECT nextval('love_card_code_sequence')"))
    await conn.execute(
        """
        INSERT INTO love_cards (character_id, love_code, base_price, created_by)
        VALUES ($1, $2, $3, $4)
        """,
        character_id,
        code_number,
        price,
        created_by,
    )
    await conn.executemany(
        """
        INSERT INTO love_card_levels (character_id, level, image_url, media_type)
        VALUES ($1, $2, $3, $4)
        """,
        [
            (character_id, level, file_id, media_type)
            for level, file_id, media_type in normalized_media
        ],
    )
    return code_number


async def _love_card_by_code(conn, code_number: int, *, lock: bool = False):
    query = """
        SELECT
            card.character_id,
            card.love_code,
            card.base_price,
            card.created_by,
            card.created_at,
            character.name,
            character.anime,
            character.rarity,
            character.rarity_score,
            character.aliases,
            COALESCE(character.event, '') AS event,
            character.image_url AS catalog_image_url,
            COALESCE(character.media_type, 'photo') AS catalog_media_type
        FROM love_cards AS card
        JOIN characters AS character ON character.id = card.character_id
        WHERE card.love_code = $1
    """
    if lock:
        query += " FOR KEY SHARE OF card, character"
    return await conn.fetchrow(query, code_number)


async def _love_card_by_character(conn, character_id: int, *, lock: bool = False):
    query = """
        SELECT
            card.character_id,
            card.love_code,
            card.base_price,
            card.created_by,
            card.created_at,
            character.name,
            character.anime,
            character.rarity,
            character.rarity_score,
            character.aliases,
            COALESCE(character.event, '') AS event,
            character.image_url AS catalog_image_url,
            COALESCE(character.media_type, 'photo') AS catalog_media_type
        FROM love_cards AS card
        JOIN characters AS character ON character.id = card.character_id
        WHERE card.character_id = $1
    """
    if lock:
        query += " FOR KEY SHARE OF card, character"
    return await conn.fetchrow(query, character_id)


async def _levels_for_character(conn, character_id: int) -> dict[int, dict[str, str]]:
    rows = await conn.fetch(
        """
        SELECT level, image_url, media_type
        FROM love_card_levels
        WHERE character_id = $1
        ORDER BY level ASC
        """,
        character_id,
    )
    return {
        int(row["level"]): {
            "image_url": str(row["image_url"]),
            "media_type": str(row["media_type"] or "photo"),
        }
        for row in rows
    }


def _catalog_card_payload(row, levels: dict[int, dict[str, str]]) -> dict[str, Any]:
    return {
        "character_id": int(row["character_id"]),
        "love_code_number": int(row["love_code"]),
        "love_code": love_code_label(int(row["love_code"])),
        "base_price": _decimal(row["base_price"]),
        "name": str(row["name"] or ""),
        "anime": str(row["anime"] or ""),
        "rarity": str(row["rarity"] or LOVE_RARITY),
        "rarity_score": int(row["rarity_score"] or 0),
        "aliases": str(row["aliases"] or ""),
        "event": str(row["event"] or ""),
        "catalog_image_url": str(row["catalog_image_url"] or ""),
        "catalog_media_type": str(row["catalog_media_type"] or "photo"),
        "levels": levels,
    }


async def get_love_card_by_code(code: str | int) -> dict[str, Any] | None:
    """Return public Love catalog metadata and all five static level images."""
    code_number = int(code) if isinstance(code, int) else parse_love_code(code)
    if code_number is None or code_number <= 0:
        return None
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await _love_card_by_code(conn, code_number)
        if not row:
            return None
        levels = await _levels_for_character(conn, int(row["character_id"]))
    if set(levels) != set(range(LOVE_MIN_LEVEL, LOVE_MAX_LEVEL + 1)):
        return None
    return _catalog_card_payload(row, levels)


async def get_love_card_by_character_id(character_id: int) -> dict[str, Any] | None:
    """Return a Love definition by ordinary numeric catalog character ID."""
    if not isinstance(character_id, int) or character_id <= 0:
        return None
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await _love_card_by_character(conn, character_id)
        if not row:
            return None
        levels = await _levels_for_character(conn, character_id)
    if set(levels) != set(range(LOVE_MIN_LEVEL, LOVE_MAX_LEVEL + 1)):
        return None
    return _catalog_card_payload(row, levels)


async def list_love_cards() -> list[dict[str, Any]]:
    """Return public Lovelist cards in their stable L-code order."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                card.character_id,
                card.love_code,
                card.base_price,
                character.name,
                character.anime,
                character.rarity,
                character.rarity_score,
                character.aliases,
                COALESCE(character.event, '') AS event,
                level_one.image_url,
                COALESCE(level_one.media_type, 'photo') AS media_type
            FROM love_cards AS card
            JOIN characters AS character ON character.id = card.character_id
            JOIN love_card_levels AS level_one
              ON level_one.character_id = card.character_id AND level_one.level = 1
            ORDER BY card.love_code ASC
            """
        )
    return [
        {
            "character_id": int(row["character_id"]),
            "love_code_number": int(row["love_code"]),
            "love_code": love_code_label(int(row["love_code"])),
            "base_price": _decimal(row["base_price"]),
            "name": str(row["name"] or ""),
            "anime": str(row["anime"] or ""),
            "rarity": str(row["rarity"] or LOVE_RARITY),
            "rarity_score": int(row["rarity_score"] or 0),
            "aliases": str(row["aliases"] or ""),
            "event": str(row["event"] or ""),
            "image_url": str(row["image_url"] or ""),
            "media_type": str(row["media_type"] or "photo"),
        }
        for row in rows
    ]


async def purchase_love_card(
    *,
    user_id: int,
    love_code: str | int,
    username: str | None = None,
    full_name: str | None = None,
    chat_id: int = 0,
) -> dict[str, Any]:
    """Buy exactly one Love card at the owner-set immutable Lovelist price."""
    code_number = int(love_code) if isinstance(love_code, int) else parse_love_code(love_code)
    if (
        not isinstance(user_id, int)
        or isinstance(user_id, bool)
        or user_id <= 0
        or code_number is None
        or code_number <= 0
    ):
        return {"success": False, "reason": "invalid_arguments"}
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1)", user_id)
            await _ensure_love_user(
                conn,
                user_id=user_id,
                username=username,
                full_name=full_name,
            )
            existing = await conn.fetchval(
                "SELECT 1 FROM love_player_states WHERE user_id = $1 LIMIT 1 FOR UPDATE",
                user_id,
            )
            if existing:
                return {"success": False, "reason": "already_has_love"}
            card = await _love_card_by_code(conn, code_number, lock=True)
            if not card:
                return {"success": False, "reason": "love_not_found"}
            levels = await _levels_for_character(conn, int(card["character_id"]))
            if set(levels) != set(range(LOVE_MIN_LEVEL, LOVE_MAX_LEVEL + 1)):
                return {"success": False, "reason": "love_unavailable"}
            wallet = await conn.fetchrow(
                "SELECT neko_balance FROM users WHERE user_id = $1 FOR UPDATE",
                user_id,
            )
            if not wallet:
                raise RuntimeError("Love wallet row was not created")
            balance_before = _decimal(wallet["neko_balance"])
            price = _decimal(card["base_price"])
            if balance_before < price:
                return {
                    "success": False,
                    "reason": "insufficient_neko",
                    "price": price,
                    "balance": balance_before,
                }
            balance_after = (balance_before - price).quantize(NEKO_QUANTUM)
            harem = await conn.fetchrow(
                """
                INSERT INTO harem (user_id, chat_id, character_id, is_ranked)
                VALUES ($1, $2, $3, FALSE)
                RETURNING id, acquired_at
                """,
                user_id,
                int(chat_id),
                int(card["character_id"]),
            )
            await conn.execute("UPDATE love_player_states SET is_active = FALSE WHERE user_id = $1", user_id)
            await conn.execute(
                """
                INSERT INTO love_player_states (
                    user_id, character_id, level, level_points, is_active, is_escrowed
                )
                VALUES ($1, $2, 1, 0, TRUE, FALSE)
                """,
                user_id,
                int(card["character_id"]),
            )
            await conn.execute(
                "UPDATE users SET neko_balance = $2, last_seen_at = NOW() WHERE user_id = $1",
                user_id,
                balance_after,
            )
            await _record_wallet_ledger(
                conn,
                user_id=user_id,
                delta=-price,
                balance_after=balance_after,
                reason="love_purchase",
                reference_id=int(card["character_id"]),
            )
    payload = _catalog_card_payload(card, levels)
    payload.update({
        "harem_id": int(harem["id"]),
        "acquired_at": harem["acquired_at"],
        "level": 1,
        "level_points": 0,
        "next_target": LOVE_LEVEL_TARGETS[1],
        "is_active": True,
        "balance_after": balance_after,
    })
    return {"success": True, "card": payload}


async def _active_state_for_update(conn, *, user_id: int):
    return await conn.fetchrow(
        """
        SELECT
            state.user_id,
            state.character_id,
            state.level,
            state.level_points,
            state.is_active,
            state.is_escrowed,
            state.kiss_cooldown_until,
            state.hug_cooldown_until,
            state.date_cooldown_until,
            state.romance_cooldown_until,
            card.love_code,
            card.base_price,
            character.name,
            character.anime,
            character.rarity,
            character.rarity_score,
            level_media.image_url,
            COALESCE(level_media.media_type, 'photo') AS media_type
        FROM love_player_states AS state
        JOIN love_cards AS card ON card.character_id = state.character_id
        JOIN characters AS character ON character.id = state.character_id
        JOIN love_card_levels AS level_media
          ON level_media.character_id = state.character_id AND level_media.level = state.level
        WHERE state.user_id = $1
          AND state.is_active = TRUE
          AND state.is_escrowed = FALSE
        FOR UPDATE OF state
        """,
        user_id,
    )


def _state_payload(row) -> dict[str, Any]:
    level = min(LOVE_MAX_LEVEL, max(LOVE_MIN_LEVEL, int(row["level"] or 1)))
    return {
        "character_id": int(row["character_id"]),
        "love_code_number": int(row["love_code"]),
        "love_code": love_code_label(int(row["love_code"])),
        "name": str(row["name"] or ""),
        "anime": str(row["anime"] or ""),
        "rarity": str(row["rarity"] or LOVE_RARITY),
        "rarity_score": int(row["rarity_score"] or 0),
        "base_price": _decimal(row["base_price"]),
        "level": level,
        "level_points": max(0, int(row["level_points"] or 0)),
        "next_target": _current_target(level),
        "is_max": level >= LOVE_MAX_LEVEL,
        "is_active": bool(row["is_active"]),
        "image_url": str(row["image_url"] or ""),
        "media_type": str(row["media_type"] or "photo"),
    }


async def get_love_overview(*, user_id: int) -> dict[str, Any]:
    """Return a player's active/owner-granted Love cards plus public Lovelist."""
    if not isinstance(user_id, int) or user_id <= 0:
        return {"active": None, "owned": [], "catalog": []}
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                state.user_id,
                state.character_id,
                state.level,
                state.level_points,
                state.is_active,
                state.is_escrowed,
                state.kiss_cooldown_until,
                state.hug_cooldown_until,
                state.date_cooldown_until,
                state.romance_cooldown_until,
                card.love_code,
                card.base_price,
                character.name,
                character.anime,
                character.rarity,
                character.rarity_score,
                level_media.image_url,
                COALESCE(level_media.media_type, 'photo') AS media_type
            FROM love_player_states AS state
            JOIN love_cards AS card ON card.character_id = state.character_id
            JOIN characters AS character ON character.id = state.character_id
            JOIN love_card_levels AS level_media
              ON level_media.character_id = state.character_id AND level_media.level = state.level
            WHERE state.user_id = $1 AND state.is_escrowed = FALSE
            ORDER BY state.is_active DESC, card.love_code ASC
            """,
            user_id,
        )
    owned = [_state_payload(row) for row in rows]
    active = next((card for card in owned if card["is_active"]), None)
    return {"active": active, "owned": owned, "catalog": await list_love_cards()}


async def select_active_love(*, user_id: int, love_code: str | int) -> dict[str, Any]:
    """Select one owner-granted/owned Love card as the no-argument command target."""
    code_number = int(love_code) if isinstance(love_code, int) else parse_love_code(love_code)
    if not isinstance(user_id, int) or user_id <= 0 or code_number is None:
        return {"success": False, "reason": "invalid_arguments"}
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1)", user_id)
            row = await conn.fetchrow(
                """
                SELECT state.character_id
                FROM love_player_states AS state
                JOIN love_cards AS card ON card.character_id = state.character_id
                WHERE state.user_id = $1
                  AND state.is_escrowed = FALSE
                  AND card.love_code = $2
                FOR UPDATE OF state
                """,
                user_id,
                code_number,
            )
            if not row:
                return {"success": False, "reason": "not_owned"}
            await conn.execute("UPDATE love_player_states SET is_active = FALSE WHERE user_id = $1", user_id)
            await conn.execute(
                """
                UPDATE love_player_states
                SET is_active = TRUE, updated_at = NOW()
                WHERE user_id = $1 AND character_id = $2
                """,
                user_id,
                int(row["character_id"]),
            )
            selected = await _active_state_for_update(conn, user_id=user_id)
    return {"success": True, "state": _state_payload(selected)} if selected else {"success": False, "reason": "not_owned"}


async def perform_love_action(
    *,
    user_id: int,
    action: str,
    username: str | None = None,
    full_name: str | None = None,
    now: datetime | None = None,
    reward_override: int | None = None,
) -> dict[str, Any]:
    """Run a safe Love action against the player's currently active Love card.

    Valid actions are ``kiss``, ``hug``, ``date_restaurant``, ``date_cinema``,
    and non-explicit ``romance``. Wallet, cooldown, point, image-level, and
    progression changes share one transaction.
    """
    actions = {
        "kiss": ("kiss_cooldown_until", KISS_COOLDOWN_SECONDS, KISS_POINTS, ZERO_NEKO),
        "hug": ("hug_cooldown_until", HUG_COOLDOWN_SECONDS, HUG_POINTS, ZERO_NEKO),
        "date_restaurant": ("date_cooldown_until", DATE_COOLDOWN_SECONDS, RESTAURANT_DATE_POINTS, RESTAURANT_DATE_PRICE),
        "date_cinema": ("date_cooldown_until", DATE_COOLDOWN_SECONDS, CINEMA_DATE_POINTS, CINEMA_DATE_PRICE),
        "romance": ("romance_cooldown_until", ROMANCE_COOLDOWN_SECONDS, 0, ZERO_NEKO),
    }
    if (
        not isinstance(user_id, int)
        or isinstance(user_id, bool)
        or user_id <= 0
        or action not in actions
    ):
        return {"success": False, "reason": "invalid_arguments"}
    current_time = _utc(now) or datetime.now(timezone.utc)
    if reward_override is not None and not (
        isinstance(reward_override, int)
        and not isinstance(reward_override, bool)
        and reward_override > 0
    ):
        raise ValueError("invalid Love reward override")

    cooldown_column, cooldown_seconds, gained_points, date_cost = actions[action]
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1)", user_id)
            await _ensure_love_user(
                conn,
                user_id=user_id,
                username=username,
                full_name=full_name,
            )
            wallet = await conn.fetchrow(
                "SELECT neko_balance FROM users WHERE user_id = $1 FOR UPDATE",
                user_id,
            )
            if not wallet:
                raise RuntimeError("Love wallet row was not created")
            state = await _active_state_for_update(conn, user_id=user_id)
            if not state:
                return {"success": False, "reason": "no_active_love"}
            remaining = _remaining_seconds(state[cooldown_column], current_time)
            if remaining:
                return {
                    "success": False,
                    "reason": "cooldown",
                    "remaining_seconds": remaining,
                    "state": _state_payload(state),
                }

            current_level = min(LOVE_MAX_LEVEL, max(LOVE_MIN_LEVEL, int(state["level"] or 1)))
            current_points = max(0, int(state["level_points"] or 0))
            balance_before = _decimal(wallet["neko_balance"])
            if action == "romance" and current_level < LOVE_MAX_LEVEL:
                return {"success": False, "reason": "not_max", "state": _state_payload(state)}
            if action.startswith("date_") and current_level >= LOVE_MAX_LEVEL:
                return {"success": False, "reason": "max_no_points", "state": _state_payload(state)}
            if date_cost > ZERO_NEKO and balance_before < date_cost:
                return {
                    "success": False,
                    "reason": "insufficient_neko",
                    "balance": balance_before,
                    "price": date_cost,
                    "state": _state_payload(state),
                }

            reward = ZERO_NEKO
            points_awarded = gained_points
            if action == "romance":
                reward_value = reward_override if reward_override is not None else (
                    secrets.randbelow(ROMANCE_REWARD_MAX - ROMANCE_REWARD_MIN + 1) + ROMANCE_REWARD_MIN
                )
                reward = Decimal(reward_value).quantize(NEKO_QUANTUM)
                points_awarded = 0
            elif current_level >= LOVE_MAX_LEVEL and action in {"kiss", "hug"}:
                reward_value = reward_override if reward_override is not None else (
                    secrets.randbelow(MAX_INTERACTION_REWARD_MAX - MAX_INTERACTION_REWARD_MIN + 1)
                    + MAX_INTERACTION_REWARD_MIN
                )
                reward = Decimal(reward_value).quantize(NEKO_QUANTUM)
                points_awarded = 0

            if balance_before > MAX_NEKO_BALANCE - reward:
                return {"success": False, "reason": "balance_limit", "state": _state_payload(state)}
            balance_after = (balance_before - date_cost + reward).quantize(NEKO_QUANTUM)
            new_level, new_points, leveled_up, became_max = _advance_points(
                current_level,
                current_points,
                points_awarded,
            )
            cooldown_until = current_time + timedelta(seconds=cooldown_seconds)
            await conn.execute(
                f"""
                UPDATE love_player_states
                SET level = $3,
                    level_points = $4,
                    {cooldown_column} = $5,
                    updated_at = $2
                WHERE user_id = $1 AND character_id = $6
                """,
                user_id,
                current_time,
                new_level,
                new_points,
                cooldown_until,
                int(state["character_id"]),
            )
            if balance_after != balance_before:
                await conn.execute(
                    "UPDATE users SET neko_balance = $2, last_seen_at = NOW() WHERE user_id = $1",
                    user_id,
                    balance_after,
                )
                if date_cost > ZERO_NEKO:
                    await _record_wallet_ledger(
                        conn,
                        user_id=user_id,
                        delta=-date_cost,
                        balance_after=(balance_before - date_cost).quantize(NEKO_QUANTUM),
                        reason=f"love_{action}",
                        reference_id=int(state["character_id"]),
                    )
                if reward > ZERO_NEKO:
                    await _record_wallet_ledger(
                        conn,
                        user_id=user_id,
                        delta=reward,
                        balance_after=balance_after,
                        reason=f"love_{action}_reward",
                        reference_id=int(state["character_id"]),
                    )
            updated = await _active_state_for_update(conn, user_id=user_id)
    if not updated:
        raise RuntimeError("Love state disappeared during action")
    return {
        "success": True,
        "action": action,
        "points_awarded": points_awarded,
        "date_cost": date_cost,
        "reward": reward,
        "balance_after": balance_after,
        "leveled_up": leveled_up,
        "became_max": became_max,
        "cooldown_until": cooldown_until,
        "state": _state_payload(updated),
    }


async def create_love_market_listing(*, user_id: int) -> dict[str, Any]:
    """Escrow the active Love card at its immutable owner-set base price."""
    if not isinstance(user_id, int) or user_id <= 0:
        return {"success": False, "reason": "invalid_arguments"}
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1)", user_id)
            state = await _active_state_for_update(conn, user_id=user_id)
            if not state:
                return {"success": False, "reason": "no_active_love"}
            harem = await conn.fetchrow(
                """
                SELECT id, chat_id, is_favorite, acquired_at
                FROM harem
                WHERE user_id = $1 AND character_id = $2
                ORDER BY acquired_at ASC, id ASC
                LIMIT 1
                FOR UPDATE
                """,
                user_id,
                int(state["character_id"]),
            )
            if not harem:
                return {"success": False, "reason": "ownership_lost"}
            duplicate_listing = await conn.fetchval(
                "SELECT 1 FROM market_listings WHERE source_harem_id = $1",
                int(harem["id"]),
            )
            if duplicate_listing:
                return {"success": False, "reason": "already_listed"}
            removed = await conn.fetchval(
                "DELETE FROM harem WHERE id = $1 AND user_id = $2 RETURNING id",
                int(harem["id"]),
                user_id,
            )
            if removed is None:
                return {"success": False, "reason": "ownership_lost"}
            await conn.execute(
                """
                UPDATE love_player_states
                SET is_active = FALSE, is_escrowed = TRUE, updated_at = NOW()
                WHERE user_id = $1 AND character_id = $2
                """,
                user_id,
                int(state["character_id"]),
            )
            listing = await conn.fetchrow(
                """
                INSERT INTO market_listings (
                    character_id, price, listed_by, listing_kind, seller_user_id,
                    source_harem_id, source_chat_id, source_is_favorite,
                    source_is_ranked, source_acquired_at
                )
                VALUES ($1, $2, $3, 'player', $3, $4, $5, $6, FALSE, $7)
                RETURNING id AS listing_id, created_at, updated_at
                """,
                int(state["character_id"]),
                _decimal(state["base_price"]),
                user_id,
                int(harem["id"]),
                int(harem["chat_id"]),
                bool(harem["is_favorite"]),
                harem["acquired_at"],
            )
            if not listing:
                raise RuntimeError("Love market listing was not created")
    return {
        "success": True,
        "listing_id": int(listing["listing_id"]),
        "price": _decimal(state["base_price"]),
        "name": str(state["name"]),
        "love_code": love_code_label(int(state["love_code"])),
        "created_at": listing["created_at"],
    }


async def restore_love_state_after_market_cancel(
    conn,
    *,
    seller_id: int,
    character_id: int,
) -> bool:
    """Return a cancelled Love escrow to its seller with its saved progress."""
    state = await conn.fetchrow(
        """
        SELECT character_id
        FROM love_player_states
        WHERE user_id = $1 AND character_id = $2 AND is_escrowed = TRUE
        FOR UPDATE
        """,
        seller_id,
        character_id,
    )
    if not state:
        return False
    await conn.execute("UPDATE love_player_states SET is_active = FALSE WHERE user_id = $1", seller_id)
    await conn.execute(
        """
        UPDATE love_player_states
        SET is_escrowed = FALSE, is_active = TRUE, updated_at = NOW()
        WHERE user_id = $1 AND character_id = $2
        """,
        seller_id,
        character_id,
    )
    return True


async def transfer_love_state_after_market_purchase(
    conn,
    *,
    seller_id: int,
    buyer_id: int,
    character_id: int,
) -> tuple[bool, str]:
    """Erase seller progression and create a Level-1 Love for the buyer."""
    # An escrowed card still belongs to its seller until the listing completes.
    # Count it here too: otherwise listing a Love, buying another, then
    # cancelling the first listing could evade the one-purchasable-Love rule.
    buyer_existing = await conn.fetchval(
        """
        SELECT 1 FROM love_player_states
        WHERE user_id = $1
        LIMIT 1 FOR UPDATE
        """,
        buyer_id,
    )
    if buyer_existing:
        return False, "love_already_owned"
    seller_state = await conn.fetchrow(
        """
        SELECT character_id
        FROM love_player_states
        WHERE user_id = $1 AND character_id = $2 AND is_escrowed = TRUE
        FOR UPDATE
        """,
        seller_id,
        character_id,
    )
    if not seller_state:
        return False, "love_state_missing"
    await conn.execute(
        "DELETE FROM love_player_states WHERE user_id = $1 AND character_id = $2",
        seller_id,
        character_id,
    )
    await conn.execute("UPDATE love_player_states SET is_active = FALSE WHERE user_id = $1", buyer_id)
    await conn.execute(
        """
        INSERT INTO love_player_states (
            user_id, character_id, level, level_points, is_active, is_escrowed
        )
        VALUES ($1, $2, 1, 0, TRUE, FALSE)
        """,
        buyer_id,
        character_id,
    )
    return True, "ok"


async def grant_love_state_in_transaction(
    conn,
    *,
    user_id: int,
    character_id: int,
) -> tuple[bool, str]:
    """Create a fresh active Level-1 state for an owner-granted Love card."""
    card = await _love_card_by_character(conn, character_id, lock=True)
    if not card:
        return False, "not_love"
    existing = await conn.fetchval(
        """
        SELECT 1 FROM love_player_states
        WHERE user_id = $1 AND character_id = $2
        LIMIT 1 FOR UPDATE
        """,
        user_id,
        character_id,
    )
    if existing:
        return False, "already_owned"
    await conn.execute("UPDATE love_player_states SET is_active = FALSE WHERE user_id = $1", user_id)
    await conn.execute(
        """
        INSERT INTO love_player_states (
            user_id, character_id, level, level_points, is_active, is_escrowed
        )
        VALUES ($1, $2, 1, 0, TRUE, FALSE)
        """,
        user_id,
        character_id,
    )
    return True, "ok"
