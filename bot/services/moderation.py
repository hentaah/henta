"""Temporary anti-spam bans and consecutive-message tracking."""
from __future__ import annotations

from aiogram.types import Message

from bot.db.pool import get_pool
from bot.services.redis_client import get_redis

SPAM_STREAK_LIMIT = 7
SPAM_BAN_SECONDS = 7 * 60
# Manual /mute accepts a positive whole-day duration, bounded to ten years so
# an accidental giant input cannot create an impractical or overflowing expiry.
MAX_MANUAL_MUTE_DAYS = 3_650
# A burst is considered consecutive only while it remains reasonably recent.
SPAM_STREAK_TTL_SECONDS = 10 * 60

# Dot aliases are player commands rather than ordinary punctuation. Keeping
# this small allowlist centralized makes anti-spam, membership, throttling, and
# response delay treat them exactly like their slash counterparts.
DOT_PLAYER_COMMANDS = frozenset({"casino", "harem", "trade", "gift", "henmode"})

# A reason is player-visible in the permanent Boom notice and owner-visible in
# /boomlist. Bound it so neither a maliciously huge message nor an accidental
# paste can make moderation output impractical.
MAX_BOOM_REASON_LENGTH = 500


def dot_player_command_name(text: str | None) -> str | None:
    """Return one exact requested dot-command name, otherwise ``None``."""
    stripped = (text or "").lstrip()
    if not stripped.startswith("."):
        return None
    token = stripped.split(maxsplit=1)[0][1:].casefold()
    return token if token in DOT_PLAYER_COMMANDS else None


def normalize_boom_reason(value: object) -> str | None:
    """Normalize one concise owner-supplied permanent-ban reason safely."""
    reason = " ".join(str(value or "").split())
    if not reason or len(reason) > MAX_BOOM_REASON_LENGTH:
        return None
    return reason


def is_ordinary_group_message(message: Message) -> bool:
    """Return whether a human message should advance the anti-spam streak.

    Slash commands, requested dot aliases, and inline-result messages are
    intentional bot actions, not ordinary conversation. Exclamation-prefixed
    text is ordinary chat now that owner controls use slash commands again.
    """
    if message.chat.type == "private" or not message.from_user or message.from_user.is_bot:
        return False
    # ``via_bot`` marks Telegram inline results. Some Bot API/aiogram
    # versions do not expose ``inline_query_id`` on Message, hence getattr.
    if message.via_bot is not None or getattr(message, "inline_query_id", None):
        return False
    text = (message.text or "").lstrip()
    return not (text.startswith("/") or dot_player_command_name(text) is not None)


async def apply_boom_ban(
    *,
    user_id: int,
    username: str | None,
    full_name: str | None,
    reason: object,
    boomed_by_user_id: int,
    boomed_by_name: str | None,
) -> dict:
    """Persist one owner-attributed permanent Boom with its visible reason."""
    if (
        not isinstance(user_id, int)
        or isinstance(user_id, bool)
        or user_id <= 0
        or not isinstance(boomed_by_user_id, int)
        or isinstance(boomed_by_user_id, bool)
        or boomed_by_user_id <= 0
    ):
        raise ValueError("invalid Boom user id")
    normalized_reason = normalize_boom_reason(reason)
    if normalized_reason is None:
        raise ValueError("invalid Boom reason")

    target_name = " ".join((full_name or "").split()) or f"User {user_id}"
    target_username = (username or "").strip() or None
    actor_name = " ".join((boomed_by_name or "").split()) or f"Owner {boomed_by_user_id}"
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO users (user_id, username, full_name, is_banned)
                VALUES ($1, $2, $3, TRUE)
                ON CONFLICT (user_id) DO UPDATE SET is_banned = TRUE
                """,
                user_id,
                target_username,
                target_name,
            )
            await conn.execute(
                """
                INSERT INTO boom_bans (
                    user_id, reason, boomed_by_user_id, boomed_by_name, boomed_at
                )
                VALUES ($1, $2, $3, $4, NOW())
                ON CONFLICT (user_id) DO UPDATE SET
                    reason = EXCLUDED.reason,
                    boomed_by_user_id = EXCLUDED.boomed_by_user_id,
                    boomed_by_name = EXCLUDED.boomed_by_name,
                    boomed_at = NOW()
                """,
                user_id,
                normalized_reason,
                boomed_by_user_id,
                actor_name,
            )
    return {
        "user_id": user_id,
        "reason": normalized_reason,
        "boomed_by_user_id": boomed_by_user_id,
        "boomed_by_name": actor_name,
    }


async def list_active_boom_bans() -> list[dict]:
    """Return every currently permanent-banned user with durable Boom context."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                player.user_id,
                COALESCE(NULLIF(BTRIM(boom.reason), ''), 'No reason recorded') AS reason,
                COALESCE(NULLIF(BTRIM(boom.boomed_by_name), ''), 'Unknown owner') AS boomed_by_name,
                boom.boomed_at
            FROM users AS player
            LEFT JOIN boom_bans AS boom ON boom.user_id = player.user_id
            WHERE player.is_banned = TRUE
            ORDER BY boom.boomed_at DESC NULLS LAST, player.user_id ASC
            """
        )
    return [dict(row) for row in rows]


async def record_consecutive_group_message(chat_id: int, user_id: int) -> int:
    """Atomically track a sender's uninterrupted message streak in one chat.

    Any message from another user resets the streak. Redis Lua keeps two
    simultaneous group messages from racing into an incorrect count.
    """
    redis = get_redis()
    result = await redis.eval(
        """
        local previous_sender = redis.call('GET', KEYS[1])
        local streak = 1
        if previous_sender == ARGV[1] then
            streak = redis.call('INCR', KEYS[2])
        else
            redis.call('SET', KEYS[1], ARGV[1])
            redis.call('SET', KEYS[2], 1)
        end

        redis.call('EXPIRE', KEYS[1], ARGV[2])
        redis.call('EXPIRE', KEYS[2], ARGV[2])

        if streak >= tonumber(ARGV[3]) then
            redis.call('DEL', KEYS[1])
            redis.call('DEL', KEYS[2])
        end
        return streak
        """,
        2,
        f"spam:{chat_id}:last_sender",
        f"spam:{chat_id}:streak",
        str(user_id),
        SPAM_STREAK_TTL_SECONDS,
        SPAM_STREAK_LIMIT,
    )
    return int(result)


async def apply_spam_ban(user_id: int) -> None:
    """Block a user's bot actions globally for exactly seven minutes."""
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (user_id, last_seen_at, spam_banned_until)
            VALUES ($1, NOW(), NOW() + ($2 * INTERVAL '1 second'))
            ON CONFLICT (user_id) DO UPDATE SET
              last_seen_at = NOW(),
              spam_banned_until = NOW() + ($2 * INTERVAL '1 second')
            """,
            user_id,
            SPAM_BAN_SECONDS,
        )


async def apply_manual_mute(
    *,
    user_id: int,
    username: str | None,
    full_name: str | None,
    days: int,
) -> dict:
    """Set an exact durable full-owner bot mute for one human user.

    The timestamp is deliberately separate from the seven-minute automatic
    anti-spam field. `BanMiddleware` treats this manual mute as a complete
    member-facing bot block (messages, callbacks, and inline queries) until an
    owner uses `/unmute` or the requested number of days elapses.
    """
    if (
        not isinstance(user_id, int)
        or isinstance(user_id, bool)
        or user_id <= 0
        or not isinstance(days, int)
        or isinstance(days, bool)
        or not 1 <= days <= MAX_MANUAL_MUTE_DAYS
    ):
        raise ValueError("invalid manual mute request")

    supplied_name = (full_name or "").strip()
    insert_name = supplied_name or f"User {user_id}"
    visible_username = (username or "").strip() or None
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO users (
                user_id, username, full_name, last_seen_at, manual_muted_until
            )
            VALUES ($1, $2, $3, NOW(), NOW() + ($4 * INTERVAL '1 day'))
            ON CONFLICT (user_id) DO UPDATE SET
                username = COALESCE(EXCLUDED.username, users.username),
                full_name = CASE
                    WHEN BTRIM($5) <> '' THEN $5
                    ELSE users.full_name
                END,
                last_seen_at = NOW(),
                manual_muted_until = EXCLUDED.manual_muted_until
            RETURNING manual_muted_until, is_banned
            """,
            user_id,
            visible_username,
            insert_name,
            days,
            supplied_name,
        )
    if not row:
        raise RuntimeError("manual mute user row was not saved")
    return {
        "expires_at": row["manual_muted_until"],
        "permanently_banned": bool(row["is_banned"]),
    }


async def get_spam_ban_remaining(user_id: int) -> int:
    """Return active temporary-ban time in seconds and clean up expired rows."""
    pool = get_pool()
    async with pool.acquire() as conn:
        remaining = await conn.fetchval(
            """
            SELECT CASE
                WHEN spam_banned_until > NOW()
                THEN CEIL(EXTRACT(EPOCH FROM (spam_banned_until - NOW())))::INT
                ELSE 0
            END
            FROM users
            WHERE user_id = $1
            """,
            user_id,
        )
        if remaining is None:
            return 0
        if remaining <= 0:
            await conn.execute(
                """
                UPDATE users
                SET spam_banned_until = NULL
                WHERE user_id = $1 AND spam_banned_until <= NOW()
                """,
                user_id,
            )
            return 0
        return int(remaining)


def format_remaining_time(seconds: int) -> str:
    """Produce an English duration such as '5 minutes and 56 seconds'."""
    seconds = max(0, int(seconds))
    minutes, seconds = divmod(seconds, 60)
    parts: list[str] = []
    if minutes:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    if seconds or not parts:
        parts.append(f"{seconds} second{'s' if seconds != 1 else ''}")
    return " and ".join(parts)
