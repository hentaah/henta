"""Permanent bans plus automatic-spam and owner-set bot-mute access control."""
from __future__ import annotations

import logging
from html import escape
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, InlineQuery, Message, TelegramObject

from bot.config import settings
from bot.db.pool import get_pool
from bot.services.moderation import (
    dot_player_command_name,
    is_ordinary_group_message,
    record_consecutive_group_message,
)

log = logging.getLogger(__name__)


def _is_profile_command(message: Message) -> bool:
    """Allow precisely /profile or /profile@BotName during a Boom/spam ban."""
    if not message.text:
        return False
    first_token = message.text.strip().split(maxsplit=1)[0].lower()
    return first_token.split("@", 1)[0] == "/profile"


def _is_boom_blocked_command(message: Message) -> bool:
    """Recognize command attempts that should receive the Boom notice."""
    text = (message.text or "").lstrip()
    return text.startswith("/") or dot_player_command_name(text) is not None


def _boom_notice(status) -> str:
    """Render the requested owner-attributed permanent Boom response safely."""
    reason = str(status["boom_reason"] or "").strip() or "No reason recorded"
    boomed_by = str(status["boomed_by_name"] or "").strip() or "Unknown owner"
    return (
        "🚫 BOOM!\n"
        f"📝 Reason: {escape(reason)}\n"
        f"👮Boomed by: {escape(boomed_by)}"
    )


async def _record_blocked_group_message(message: Message) -> None:
    """Track a blocked ordinary message without allowing bot activity.

    Commands and inline results are deliberately excluded from anti-spam, even
    while the sender is blocked; ordinary messages still participate in the
    consecutive-message rule.
    """
    if not is_ordinary_group_message(message):
        return
    try:
        await record_consecutive_group_message(message.chat.id, message.from_user.id)
    except Exception as exc:
        log.warning(
            "could not reset spam streak for blocked message in %s: %s",
            message.chat.id,
            exc,
        )


class BanMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = None
        if isinstance(event, (Message, CallbackQuery, InlineQuery)):
            user = event.from_user

        if not user:
            return await handler(event, data)

        pool = get_pool()
        async with pool.acquire() as conn:
            status = await conn.fetchrow(
                """
                SELECT
                    player.is_banned,
                    boom.reason AS boom_reason,
                    boom.boomed_by_name AS boomed_by_name,
                    CASE
                        WHEN spam_banned_until > NOW()
                        THEN CEIL(EXTRACT(EPOCH FROM (spam_banned_until - NOW())))::INT
                        ELSE 0
                    END AS spam_remaining,
                    CASE
                        WHEN manual_muted_until > NOW()
                        THEN CEIL(EXTRACT(EPOCH FROM (manual_muted_until - NOW())))::INT
                        ELSE 0
                    END AS manual_mute_remaining
                FROM users AS player
                LEFT JOIN boom_bans AS boom ON boom.user_id = player.user_id
                WHERE player.user_id = $1
                """,
                user.id,
            )
            if status:
                permanent_ban = bool(status["is_banned"])
                spam_remaining = int(status["spam_remaining"] or 0)
                manual_mute_remaining = int(status["manual_mute_remaining"] or 0)
                if not spam_remaining:
                    # Keep the DB clean after an automatic anti-spam pause ends.
                    await conn.execute(
                        """
                        UPDATE users
                        SET spam_banned_until = NULL
                        WHERE user_id = $1
                          AND spam_banned_until IS NOT NULL
                          AND spam_banned_until <= NOW()
                        """,
                        user.id,
                    )
                if not manual_mute_remaining:
                    await conn.execute(
                        """
                        UPDATE users
                        SET manual_muted_until = NULL
                        WHERE user_id = $1
                          AND manual_muted_until IS NOT NULL
                          AND manual_muted_until <= NOW()
                        """,
                        user.id,
                    )

                # Root and /upowner full owners bypass every bot-level ban.
                # Catalog-only admins remain ordinary members for temporary
                # mute purposes, as they are not full owners.
                full_owner = user.id == settings.owner_id
                if (
                    permanent_ban
                    or spam_remaining > 0
                    or manual_mute_remaining > 0
                ) and not full_owner:
                    full_owner = bool(
                        await conn.fetchval(
                            "SELECT 1 FROM bot_owners WHERE user_id = $1",
                            user.id,
                        )
                    )

                # A manual owner mute intentionally blocks every member-facing
                # bot surface, including /profile, callbacks, and inline mode.
                if manual_mute_remaining > 0 and not full_owner:
                    if isinstance(event, Message):
                        await _record_blocked_group_message(event)
                    return

                if permanent_ban and not full_owner:
                    if isinstance(event, Message) and _is_profile_command(event):
                        # A permanent-Boomed player may view their own profile,
                        # but ActivityMiddleware must not let that exception
                        # advance group counters or spam tracking.
                        data["boom_ban_active"] = True
                    else:
                        if isinstance(event, Message):
                            if _is_boom_blocked_command(event):
                                try:
                                    await event.answer(_boom_notice(status), parse_mode="HTML")
                                except Exception:
                                    pass
                            await _record_blocked_group_message(event)
                        return

                if spam_remaining > 0 and not full_owner:
                    if isinstance(event, Message) and _is_profile_command(event):
                        # ActivityMiddleware consumes this flag so /profile
                        # cannot advance a streak, activity counter, or spawn.
                        data["spam_ban_active"] = True
                        data["spam_ban_remaining"] = spam_remaining
                    else:
                        # Every callback (trade/gift acceptance included),
                        # inline request, and non-profile command is blocked.
                        if isinstance(event, Message):
                            await _record_blocked_group_message(event)
                        return
                elif (spam_remaining > 0 or manual_mute_remaining > 0) and full_owner:
                    # Remove a stale temporary state that may have existed
                    # before an owner exemption was introduced.
                    await conn.execute(
                        "UPDATE users SET spam_banned_until = NULL, manual_muted_until = NULL WHERE user_id = $1",
                        user.id,
                    )

        # Deliberately call the downstream handler after releasing the DB
        # connection: /profile itself performs fresh profile/countdown queries.
        return await handler(event, data)
