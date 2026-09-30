"""/trade — propose a character trade with another user."""
from __future__ import annotations

import asyncio
import time

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from bot.config import settings
from bot.services.game import (
    ETERNAL_TRANSFER_WARNING,
    OVERLORD_TRANSFER_WARNING,
    create_trade,
    accept_trade,
    decline_trade,
    get_character_by_id,
)
from bot.services.rich_renderer import rarity_symbol
from bot.handlers.common import dot_player_command_filter
from bot.services.catalog_media import answer_catalog_media

router = Router()


@router.message(Command("trade"), F.chat.type != "private")
@router.message(dot_player_command_filter("trade", accepts_arguments=True), F.chat.type != "private")
async def cmd_trade(message: Message):
    """Usage: reply to user's message with /trade <your_char_id> <their_char_id>"""
    user = message.from_user
    if not user or not message.reply_to_message:
        await message.answer(
            "🔁 Usage: reply to a user's message with "
            "<code>/trade &lt;your_char_id&gt; &lt;their_char_id&gt;</code>",
            parse_mode="HTML",
        )
        return

    parts = message.text.split()
    if len(parts) < 3:
        await message.answer("❌ Provide both character IDs.", parse_mode="HTML")
        return

    try:
        your_char = int(parts[1])
        their_char = int(parts[2])
    except ValueError:
        await message.answer("❌ Character IDs must be numbers.", parse_mode="HTML")
        return

    target = message.reply_to_message.from_user
    if not target or target.is_bot:
        await message.answer("🚫 Can't trade with a bot.", parse_mode="HTML")
        return
    if target.id == user.id:
        await message.answer("🚫 You can't trade with yourself.", parse_mode="HTML")
        return

    trade_id, reason = await create_trade(
        chat_id=message.chat.id,
        from_uid=user.id,
        to_uid=target.id,
        from_char=your_char,
        to_char=their_char,
    )

    if trade_id is None:
        if reason == "eternal_protected":
            await message.answer(f"🚫 {ETERNAL_TRANSFER_WARNING}", parse_mode="HTML")
        elif reason == "overlord_protected":
            await message.answer(f"🚫 {OVERLORD_TRANSFER_WARNING}", parse_mode="HTML")
        elif reason == "sender_does_not_own":
            await message.answer(
                "❌ You don't own that character (or it's marked as favorite).",
                parse_mode="HTML",
            )
        else:
            await message.answer(
                f"❌ <b>{target.full_name}</b> doesn't own that character "
                "(or it's marked as favorite).",
                parse_mode="HTML",
            )
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Accept",  callback_data=f"trade:accept:{trade_id}", style="success"),
        InlineKeyboardButton(text="❌ Decline", callback_data=f"trade:decline:{trade_id}", style="danger"),
    ]])

    your_char = await get_character_by_id(your_char)
    their_char_info = await get_character_by_id(their_char)
    caption = (
        f"🔁 <b>{user.full_name}</b> wants to trade "
        f"{rarity_symbol(your_char['rarity']) if your_char else ''} "
        f"<b>{your_char['name'] if your_char else ('character #' + str(your_char))}</b> "
        f"for <b>{target.full_name}</b>'s "
        f"{rarity_symbol(their_char_info['rarity']) if their_char_info else ''} "
        f"<b>{their_char_info['name'] if their_char_info else ('character #' + str(their_char))}</b>.\n\n"
        f"<i>{target.mention_html()}, you have {settings.trade_window}s to respond.</i>"
    )

    delivered = False
    if your_char:
        try:
            await answer_catalog_media(
                message,
                character=your_char,
                caption=caption,
                reply_markup=keyboard,
            )
            # This also creates a clean text card when the catalog has no file ID.
            delivered = True
        except Exception:
            pass
    if not delivered:
        await message.answer(caption, parse_mode="HTML", reply_markup=keyboard)

    # Auto-expire after window regardless of photo/video/text presentation.
    asyncio.create_task(_expire_trade(trade_id, settings.trade_window))


@router.callback_query(F.data.startswith("trade:accept:"))
async def cb_trade_accept(callback: CallbackQuery):
    trade_id = int(callback.data.split(":")[2])
    # Fix: pass accepting_user_id so only the recipient can accept
    ok, reason = await accept_trade(trade_id, accepting_user_id=callback.from_user.id)
    if ok:
        await _resolve_message(callback.message, "✅ Trade complete! Both characters swapped.")
    elif reason == "not_your_trade":
        await callback.answer("🚫 This trade isn't for you.", show_alert=True)
        return
    elif reason == "eternal_protected":
        await _resolve_message(callback.message, f"🚫 {ETERNAL_TRANSFER_WARNING}")
    elif reason == "overlord_protected":
        await _resolve_message(callback.message, f"🚫 {OVERLORD_TRANSFER_WARNING}")
    elif reason == "ownership_lost":
        await _resolve_message(
            callback.message,
            "❌ Trade failed — one of you no longer owns the offered character.",
        )
    else:
        await _resolve_message(callback.message, "❌ Trade not found or already resolved.")
    await callback.answer()


@router.callback_query(F.data.startswith("trade:decline:"))
async def cb_trade_decline(callback: CallbackQuery):
    trade_id = int(callback.data.split(":")[2])
    ok, reason = await decline_trade(trade_id, declining_user_id=callback.from_user.id)
    if reason == "not_your_trade":
        await callback.answer("🚫 This trade isn't for you.", show_alert=True)
        return
    await _resolve_message(callback.message, "🚫 Trade declined.")
    await callback.answer()


async def _resolve_message(message: Message, text: str):
    """Trade proposals are now sent as a photo (the offered character's
    image), so edit_text alone would raise — try caption first, fall back
    to text for the rare case the original send had no photo.
    """
    try:
        await message.edit_caption(caption=text, parse_mode="HTML", reply_markup=None)
    except Exception:
        try:
            await message.edit_text(text, parse_mode="HTML", reply_markup=None)
        except Exception:
            pass


async def _expire_trade(trade_id: int, after: int):
    await asyncio.sleep(after)
    from bot.db.pool import get_pool
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE trades SET status='expired', resolved_at=NOW() "
            "WHERE id=$1 AND status='pending'",
            trade_id,
        )
