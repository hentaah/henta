"""Group activity tracking, spawning, and consecutive-message anti-spam bans."""
from __future__ import annotations

import logging

from aiogram import BaseMiddleware, Bot
from aiogram.types import Message, TelegramObject

from bot.config import default_spawn_interval_for_group
from bot.db.pool import get_pool
from bot.services.moderation import (
    SPAM_STREAK_LIMIT,
    apply_spam_ban,
    is_ordinary_group_message,
    record_consecutive_group_message,
)
from bot.services.spawn_engine import maybe_spawn

log = logging.getLogger(__name__)


def _is_profile_command(message: Message) -> bool:
    """Recognize /profile and /profile@BotName, but not similarly named text."""
    text = (message.text or "").strip()
    if not text:
        return False
    first_token = text.split(maxsplit=1)[0].lower()
    return first_token.split("@", 1)[0] == "/profile"


def _is_spawn_control_command(message: Message) -> bool:
    """Keep slash spawn/leave controls effective before their own spawn check."""
    text = (message.text or "").strip()
    if not text:
        return False
    first_token = text.split(maxsplit=1)[0].casefold().split("@", 1)[0]
    return first_token in {"/stopspawn", "/restorespawn", "/stoprarityspawn", "/spawnstart", "/left"}


class ActivityMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data):
        if not isinstance(event, Message):
            return await handler(event, data)

        # BanMiddleware permits /profile during a temporary spam ban or a
        # permanent Boom and annotates the update. Do not let either narrow
        # status-command exception affect spawn counters or spam tracking.
        if data.get("spam_ban_active") or data.get("boom_ban_active"):
            return await handler(event, data)

        chat = event.chat
        if not chat or chat.type == "private":
            return await handler(event, data)

        chat_id = chat.id
        title = chat.title or chat.username or "Private"
        pool = get_pool()

        # Register/update the group before any group-specific work.
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO groups (
                    chat_id, title, username, spawn_interval,
                    spawn_interval_custom, updated_at
                )
                VALUES ($1, $2, $3, $4, FALSE, NOW())
                ON CONFLICT (chat_id) DO UPDATE SET
                  title = EXCLUDED.title,
                  username = EXCLUDED.username,
                  updated_at = NOW()
                """,
                chat_id, title, chat.username,
                default_spawn_interval_for_group(chat_id),
            )

        user = event.from_user
        if user:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO users (user_id, username, full_name, language_code, last_seen_at)
                    VALUES ($1, $2, $3, $4, NOW())
                    ON CONFLICT (user_id) DO UPDATE SET
                      username = EXCLUDED.username,
                      full_name = EXCLUDED.full_name,
                      language_code = COALESCE(EXCLUDED.language_code, users.language_code),
                      last_seen_at = NOW()
                    """,
                    user.id, user.username, user.full_name, user.language_code,
                )

            # Only ordinary member conversation advances the anti-spam streak.
            # Commands and inline-result cards are ignored; normal media/text
            # messages still count. Full owners are never temporarily banned.
            if is_ordinary_group_message(event):
                try:
                    streak = await record_consecutive_group_message(chat_id, user.id)
                    if streak >= SPAM_STREAK_LIMIT:
                        from bot.services.permissions import is_bot_owner
                        if not await is_bot_owner(user.id):
                            await apply_spam_ban(user.id)
                            data["spam_ban_active"] = True
                            # The one permitted command can show the new countdown
                            # immediately if it happened to be the seventh message.
                            if _is_profile_command(event):
                                return await handler(event, data)
                            try:
                                await event.answer(
                                    "⛔ <b>Temporary anti-spam ban applied.</b>\n"
                                    "Only <code>/profile</code> is available for the next 7 minutes.",
                                    parse_mode="HTML",
                                )
                            except Exception:
                                pass
                            return None
                except Exception as exc:
                    # A Redis issue should not break ordinary group messages.
                    log.warning("spam tracking failed in %s for %s: %s", chat_id, user.id, exc)

        # These slash owner controls must take effect before their own command
        # can trigger a threshold spawn of the event/rarity being changed.
        if _is_spawn_control_command(event):
            return await handler(event, data)

        # Spawn evaluation happens only after a non-banned, non-spam message.
        bot: Bot = data.get("bot") or data.get("event_bot")
        if bot:
            try:
                await maybe_spawn(bot, chat_id, title)
            except Exception as exc:
                # Don't break the user's message if spawn fails.
                log.warning("spawn failed in %s: %s", chat_id, exc)

        return await handler(event, data)
