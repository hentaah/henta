"""Required-channel membership gate for selected player commands.

The gate is deliberately command-scoped: ordinary conversation and automatic
spawn counting remain unaffected, while /check, /gift, /harem, /catch, and
/casino are available only to members of both official channels. The bot must
be an administrator in those channels for get_chat_member to be authoritative.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, TelegramObject

from bot.services.moderation import dot_player_command_name

log = logging.getLogger(__name__)

PROTECTED_COMMANDS = frozenset({"check", "gift", "harem", "catch", "casino"})
# label, public URL, and canonical chat reference for Bot API membership reads.
REQUIRED_CHANNELS = (
    ("📢 HentaCatcher GP", "https://t.me/HentaCatcherGP", "@HentaCatcherGP"),
    ("📣 HentaCatcher Main", "https://t.me/HentaCatcherMain", "@HentaCatcherMain"),
)


def protected_command_name(message: Message) -> str | None:
    """Return an exact protected slash or requested dot-command name."""
    text = (message.text or "").lstrip()
    if text.startswith("/"):
        token = text.split(maxsplit=1)[0][1:]
        command = token.split("@", 1)[0].casefold()
        return command if command in PROTECTED_COMMANDS else None
    dot_command = dot_player_command_name(text)
    return dot_command if dot_command in PROTECTED_COMMANDS else None


def _member_is_active(member: object) -> bool:
    """Accept all normal channel member/admin/owner statuses defensively."""
    raw_status = getattr(member, "status", "")
    status = str(getattr(raw_status, "value", raw_status)).casefold()
    if status in {"creator", "owner", "administrator", "member"}:
        return True
    # Restricted members can still be valid members when Telegram reports
    # is_member=True; left/kicked users are never allowed through.
    return status == "restricted" and bool(getattr(member, "is_member", False))


async def check_required_channel_membership(bot: Bot, user_id: int) -> tuple[bool, bool]:
    """Return (joined_everything, verification_available) for one player."""
    for _label, _url, chat_ref in REQUIRED_CHANNELS:
        try:
            member = await bot.get_chat_member(chat_id=chat_ref, user_id=user_id)
        except Exception as exc:
            log.warning(
                "could not verify required channel %s for user %s: %s",
                chat_ref,
                user_id,
                exc,
            )
            return False, False
        if not _member_is_active(member):
            return False, True
    return True, True


def required_channels_keyboard() -> InlineKeyboardMarkup:
    """Exactly two primary URL buttons, one for each required channel."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=label, url=url, style="primary")
        for label, url, _chat_ref in REQUIRED_CHANNELS
    ]])


async def _send_join_required(message: Message, command: str, verification_available: bool) -> None:
    # This outer gate responds before normal command middleware runs, so retain
    # the global two-second visible-command response cadence here as well.
    await asyncio.sleep(2)
    if verification_available:
        text = (
            "🔒 <b>Join required</b>\n\n"
            f"To use <code>/{command}</code>, join both official channels below, "
            "then send the command again."
        )
    else:
        text = (
            "⚠️ <b>Membership verification is temporarily unavailable.</b>\n\n"
            "Make sure you joined both official channels below, then try the command again."
        )
    await message.reply(
        text,
        parse_mode="HTML",
        reply_markup=required_channels_keyboard(),
        disable_web_page_preview=True,
    )


async def ensure_required_channel_membership(
    *,
    bot: Bot,
    user_id: int,
    message: Message,
    command: str,
) -> bool:
    """Verify required channels, or send the shared English join prompt.

    The command middleware uses this at `/casino` and `.casino` entry.
    Casino's reply and callback stages also reuse it before they can reserve a
    new stake, so a
    stale menu cannot bypass the entry check after a player leaves a channel.
    Already-funded Mine/Dice outcomes remain resolvable and therefore cannot
    strand a previously reserved stake.
    """
    joined, verification_available = await check_required_channel_membership(bot, user_id)
    if joined:
        return True
    await _send_join_required(message, command, verification_available)
    return False


class RequiredChannelMembershipMiddleware(BaseMiddleware):
    """Block selected slash/dot player commands until both channels are joined."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message) or not event.from_user:
            return await handler(event, data)

        command = protected_command_name(event)
        if command is None:
            return await handler(event, data)

        bot = data.get("bot") or data.get("event_bot")
        if bot is None:
            log.warning("no bot instance was available for required-membership check")
            await _send_join_required(event, command, verification_available=False)
            return None

        if await ensure_required_channel_membership(
            bot=bot,
            user_id=event.from_user.id,
            message=event,
            command=command,
        ):
            return await handler(event, data)
        return None
