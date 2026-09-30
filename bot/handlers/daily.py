"""/daily — one rolling-24-hour Common-to-Mythical collection reward."""
from __future__ import annotations

import logging
from html import escape

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from bot.services.game import (
    DAILY_REWARD_INTERVAL_SECONDS,
    DailyRewardResult,
    claim_daily_reward,
)
from bot.services.rich_renderer import event_badge, rarity_symbol
from bot.services.catalog_media import answer_catalog_media

router = Router()
log = logging.getLogger(__name__)
DAILY_REWARD_HOURS = DAILY_REWARD_INTERVAL_SECONDS // 60 // 60


def _format_daily_remaining(seconds: int) -> str:
    """Render a compact English duration for the personal daily cooldown."""
    remaining = max(0, int(seconds))
    hours, remaining = divmod(remaining, 60 * 60)
    minutes, seconds = divmod(remaining, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    if seconds and not hours:
        parts.append(f"{seconds} second{'s' if seconds != 1 else ''}")
    return " and ".join(parts) or "less than a minute"


def _daily_reward_caption(result: DailyRewardResult) -> str:
    """Use the same compact Event-badge policy as harem/gift/inline cards."""
    badge = event_badge(result.event)
    event_suffix = f" {badge}" if badge else ""
    return (
        "🎁 <b>Daily Reward Claimed!</b>\n"
        "────────────────────────\n\n"
        f"🪪 <b>{escape(result.name)}</b>{event_suffix}\n"
        f"📺 {escape(result.anime)}\n"
        f"🆔 <code>#{result.character_id}</code>\n"
        f"🔱 Rarity: {rarity_symbol(result.rarity)} <b>{escape(result.rarity)}</b>\n\n"
        "<i>This card was added to your global collection. Your next personal "
        f"daily reward is available in {DAILY_REWARD_HOURS} hours.</i>"
    )


@router.message(Command("daily"))
async def cmd_daily(message: Message) -> None:
    """Give one independent, race-safe daily reward in any chat type."""
    user = message.from_user
    if not user or not message.chat:
        return

    try:
        result = await claim_daily_reward(
            user_id=user.id,
            username=user.username or "",
            full_name=user.full_name or user.username or f"User {user.id}",
            chat_id=message.chat.id,
        )
    except Exception:
        # A database/query outage must never make /daily appear to do nothing.
        # Do not claim the cooldown was unused: a network failure can happen
        # after a database transaction committed but before its result arrived.
        log.exception("could not process /daily for user %s", user.id)
        await message.reply(
            "⚠️ I could not confirm your daily reward right now. Please try "
            "<code>/daily</code> again; if it was granted, your cooldown will be shown.",
            parse_mode="HTML",
        )
        return

    if not result.success:
        if result.reason == "cooldown":
            await message.reply(
                "⏳ <b>Daily reward not ready.</b>\n\n"
                f"Your personal {DAILY_REWARD_HOURS}-hour cooldown ends in "
                f"<b>{_format_daily_remaining(result.remaining_seconds)}</b>.",
                parse_mode="HTML",
            )
        elif result.reason == "catalog_unavailable":
            await message.reply(
                "⚠️ No eligible Common-to-Mythical daily card is available right now. "
                "Try again later; your daily cooldown was not used.",
                parse_mode="HTML",
            )
        else:
            await message.reply(
                "⚠️ Your daily reward could not be prepared. Please try again.",
                parse_mode="HTML",
            )
        return

    caption = _daily_reward_caption(result)
    if result.image_url:
        try:
            await answer_catalog_media(
                message,
                character={
                    "image_url": result.image_url,
                    "media_type": result.media_type,
                },
                caption=caption,
            )
            return
        except Exception:
            # A stale Telegram file ID must not hide a successfully granted card.
            pass
    await message.reply(caption, parse_mode="HTML")

