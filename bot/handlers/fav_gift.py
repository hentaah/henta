"""/fav — confirm a global favorite.
/gift — give a globally owned character away.
Callbacks: fav:* from the claim/favorite confirmation keyboards.
"""
from __future__ import annotations

from html import escape

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.services.game import (
    ETERNAL_TRANSFER_WARNING,
    OVERLORD_TRANSFER_WARNING,
    accept_gift,
    cancel_gift,
    create_gift,
    get_character_by_id,
    get_characters_by_ids,
    get_harem_gift_summary,
    set_favorite,
    user_owns_character,
)
from bot.services.rich_renderer import (
    heading, paragraph, text_bold, text_plain, text_italic,
    blocks_to_html, event_badge, rarity_symbol,
)
from bot.handlers.common import dot_player_command_filter
from bot.services.catalog_media import answer_catalog_media

router = Router()


def _favorite_keyboard(user_id: int, character_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="✅ Yes",
            callback_data=f"fav:confirm:{user_id}:{character_id}",
            style="success",
        ),
        InlineKeyboardButton(
            text="❌ No",
            callback_data=f"fav:cancel:{user_id}",
            style="danger",
        ),
    ]])


def _favorite_caption(character: dict) -> str:
    return (
        "⭐ <b>Set Favorite?</b>\n\n"
        f"Set {rarity_symbol(character['rarity'])} "
        f"<b>{escape(character['name'])}</b> as your favorite?\n\n"
        "Your current favorite will stay unchanged unless you choose Yes."
    )


async def _send_favorite_confirmation(
    message: Message, user_id: int, character: dict,
) -> None:
    """Send a photo confirmation card, falling back to text if needed."""
    caption = _favorite_caption(character)
    keyboard = _favorite_keyboard(user_id, character["id"])
    try:
        await answer_catalog_media(
            message,
            character=character,
            caption=caption,
            reply_markup=keyboard,
        )
        return
    except Exception:
        await message.answer(caption, parse_mode="HTML", reply_markup=keyboard)


async def _replace_favorite_card(callback: CallbackQuery, text: str) -> None:
    if not callback.message:
        return
    try:
        await callback.message.edit_caption(
            caption=text, parse_mode="HTML", reply_markup=None,
        )
    except Exception:
        try:
            await callback.message.edit_text(
                text, parse_mode="HTML", reply_markup=None,
            )
        except Exception:
            pass


# ─── /fav command ──────────────────────────────────────────────────────────

@router.message(Command("fav"))
async def cmd_fav(message: Message):
    user = message.from_user
    if not user:
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        blocks = [
            heading("⭐ Set Favorite"),
            paragraph(
                text_plain("Usage: "),
                text_bold("/fav <character_id>"),
                text_plain("\n\nFind your character IDs with "),
                text_italic("/harem"),
                text_plain("."),
            ),
        ]
        await message.answer(blocks_to_html(blocks), parse_mode="HTML")
        return

    character_id = int(parts[1].strip())
    if not await user_owns_character(user.id, character_id):
        await message.answer("❌ You don't own that character.", parse_mode="HTML")
        return

    character = await get_character_by_id(character_id)
    if not character:
        await message.answer("❌ That character no longer exists.", parse_mode="HTML")
        return
    await _send_favorite_confirmation(message, user.id, character)


# ─── Favorite callbacks ─────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("fav:"))
async def cb_fav(callback: CallbackQuery):
    data = (callback.data or "").split(":")
    if not callback.message:
        await callback.answer("This action is no longer available.", show_alert=True)
        return

    # The ⭐ Favorite button on a successful claim sends a confirmation card.
    if len(data) == 2 and data[1].isdigit():
        character_id = int(data[1])
        if not await user_owns_character(callback.from_user.id, character_id):
            await callback.answer("❌ You don't own this character.", show_alert=True)
            return
        character = await get_character_by_id(character_id)
        if not character:
            await callback.answer("Character not found.", show_alert=True)
            return
        await _send_favorite_confirmation(callback.message, callback.from_user.id, character)
        await callback.answer("Choose Yes or No.")
        return

    action = data[1] if len(data) > 1 else ""
    request_user_id = data[2] if len(data) > 2 else ""
    if not request_user_id.isdigit() or callback.from_user.id != int(request_user_id):
        await callback.answer("⛔ This favorite prompt is not for you.", show_alert=True)
        return

    if action == "cancel" and len(data) == 3:
        await _replace_favorite_card(callback, "❌ <b>Favorite unchanged.</b>")
        await callback.answer("Cancelled")
        return

    if action != "confirm" or len(data) != 4 or not data[3].isdigit():
        await callback.answer("Invalid favorite action.", show_alert=True)
        return

    character_id = int(data[3])
    if await set_favorite(callback.from_user.id, character_id):
        await _replace_favorite_card(
            callback,
            "⭐ <b>Favorite updated!</b> This character is now your showcase.",
        )
        await callback.answer("Favorite updated")
    else:
        await _replace_favorite_card(
            callback,
            "❌ <b>Favorite was not changed.</b> You no longer own this character.",
        )
        await callback.answer("Not changed", show_alert=True)


# ─── /gift command ──────────────────────────────────────────────────────────

MAX_GIFT_CHARACTERS = 20


def _gift_keyboard(gift_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="✅ Accept",
            callback_data=f"gift:accept:{gift_id}",
            style="success",
        ),
        InlineKeyboardButton(
            text="❌ Cancel",
            callback_data=f"gift:cancel:{gift_id}",
            style="danger",
        ),
    ]])


def _gift_character_block(character: dict) -> str:
    """The compact themed card used in multi-character gift confirmation."""
    badge = event_badge(character.get("event"))
    event_suffix = f" {badge}" if badge else ""
    return (
        f"╭── ✦ 𝗚𝗜𝗙𝗧 𝗖𝗔𝗥𝗗 ✦\n"
        f"├ 🆔 ID : <code>{character['id']}</code>\n"
        f"├ 📺 Anime : {escape(character['anime'])}\n"
        f"├ 🎴 Character : {escape(character['name'])}{event_suffix}\n"
        f"╰ 🔱 Rarity : {rarity_symbol(character['rarity'])} {escape(character['rarity'])}"
    )


def _single_gift_caption(target_name: str, character: dict) -> str:
    return (
        "🎁 <b>Confirm Gift</b>\n\n"
        f"Send this character to <b>{escape(target_name)}</b>?\n\n"
        + _gift_character_block(character)
    )


def _bulk_gift_caption(target_name: str, characters: list[dict]) -> str:
    lines = [
        "🎁 <b>Confirm Multi-Character Gift</b>",
        "",
        f"Send <b>{len(characters)}</b> character(s) to <b>{escape(target_name)}</b>?",
        "",
    ]
    for index, character in enumerate(characters):
        lines.append(_gift_character_block(character))
        if index != len(characters) - 1:
            lines.append("")
    return "\n".join(lines)


def _all_gift_caption(target_name: str, copy_count: int, unique_count: int) -> str:
    """A compact confirmation for /gift all that cannot exceed caption limits."""
    return (
        "🎁 <b>Confirm Full Harem Gift</b>\n\n"
        f"Send all <b>{copy_count}</b> harem copy/copies "
        f"across <b>{unique_count}</b> character(s) to <b>{escape(target_name)}</b>?\n\n"
        "This includes Favorites and duplicates. Any transferred Favorite will "
        "be cleared from your showcase.\n\n"
        "<i>This action cannot be undone.</i>"
    )


async def _replace_gift_card(callback: CallbackQuery, text: str) -> None:
    if not callback.message:
        return
    try:
        await callback.message.edit_caption(
            caption=text, parse_mode="HTML", reply_markup=None,
        )
    except Exception:
        try:
            await callback.message.edit_text(
                text, parse_mode="HTML", reply_markup=None,
            )
        except Exception:
            pass


@router.message(Command("gift"), F.chat.type != "private")
@router.message(dot_player_command_filter("gift", accepts_arguments=True), F.chat.type != "private")
async def cmd_gift(message: Message):
    """Reply /gift <id> [id ...] or /gift all to confirm a global transfer."""
    user = message.from_user
    if not user:
        return
    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.answer(
            "🎁 Usage: reply to a user with <code>/gift &lt;character_id&gt; [more IDs]</code>\n"
            "or <code>/gift all</code>.",
            parse_mode="HTML",
        )
        return

    tokens = (message.text or "").split()[1:]
    if not tokens:
        await message.answer(
            "🎁 Usage: reply to a user with <code>/gift &lt;character_id&gt; [more IDs]</code>\n"
            "or <code>/gift all</code>.",
            parse_mode="HTML",
        )
        return

    target = message.reply_to_message.from_user
    if target.is_bot:
        await message.answer("🚫 Can't gift to a bot.", parse_mode="HTML")
        return
    if target.id == user.id:
        await message.answer("🚫 You can't gift to yourself.", parse_mode="HTML")
        return

    is_all = tokens[0].casefold() == "all"
    if is_all:
        if len(tokens) != 1:
            await message.answer(
                "❌ Use <code>/gift all</code> by itself, while replying to the recipient.",
                parse_mode="HTML",
            )
            return
        copy_count, unique_count = await get_harem_gift_summary(user.id)
        if copy_count <= 0:
            await message.answer("❌ Your harem is empty.", parse_mode="HTML")
            return
        gift_id, reason = await create_gift(
            chat_id=message.chat.id,
            from_uid=user.id,
            to_uid=target.id,
            all_harem=True,
        )
        if gift_id is None:
            if reason == "eternal_protected":
                text = f"🚫 {ETERNAL_TRANSFER_WARNING}"
            elif reason == "overlord_protected":
                text = f"🚫 {OVERLORD_TRANSFER_WARNING}"
            else:
                text = "❌ Your harem changed before this gift could be prepared. Try again."
            await message.answer(text, parse_mode="HTML")
            return
        await message.answer(
            _all_gift_caption(target.full_name, copy_count, unique_count),
            parse_mode="HTML",
            reply_markup=_gift_keyboard(gift_id),
        )
        return

    if len(tokens) > MAX_GIFT_CHARACTERS:
        await message.answer(
            f"❌ You can gift at most {MAX_GIFT_CHARACTERS} characters at once.",
            parse_mode="HTML",
        )
        return
    if not all(token.isdigit() and int(token) > 0 for token in tokens):
        await message.answer(
            "❌ Every character ID must be a positive number, or use <code>/gift all</code>.",
            parse_mode="HTML",
        )
        return

    character_ids = [int(token) for token in tokens]
    characters_by_id = await get_characters_by_ids(character_ids)
    missing = [str(character_id) for character_id in dict.fromkeys(character_ids)
               if character_id not in characters_by_id]
    if missing:
        await message.answer(
            "❌ No character was found for ID: <code>" + ", ".join(missing) + "</code>.",
            parse_mode="HTML",
        )
        return

    gift_id, reason = await create_gift(
        chat_id=message.chat.id,
        from_uid=user.id,
        to_uid=target.id,
        character_ids=character_ids,
    )
    if gift_id is None:
        if reason == "eternal_protected":
            text = f"🚫 {ETERNAL_TRANSFER_WARNING}"
        elif reason == "overlord_protected":
            text = f"🚫 {OVERLORD_TRANSFER_WARNING}"
        elif reason == "sender_does_not_own":
            text = "❌ You don't own every requested character copy."
        else:
            text = "❌ Couldn't prepare this gift."
        await message.answer(text, parse_mode="HTML")
        return

    characters = [characters_by_id[character_id] for character_id in character_ids]
    keyboard = _gift_keyboard(gift_id)
    if len(characters) == 1:
        character = characters[0]
        caption = _single_gift_caption(target.full_name, character)
        try:
            await answer_catalog_media(
                message,
                character=character,
                caption=caption,
                reply_markup=keyboard,
            )
            return
        except Exception:
            await message.answer(caption, parse_mode="HTML", reply_markup=keyboard)
            return

    await message.answer(
        _bulk_gift_caption(target.full_name, characters),
        parse_mode="HTML",
        reply_markup=keyboard,
    )

@router.callback_query(F.data.startswith("gift:"))
async def cb_gift(callback: CallbackQuery):
    """Only the sender may confirm or cancel a pending single/bulk gift."""
    parts = (callback.data or "").split(":")
    if len(parts) != 3 or parts[1] not in {"accept", "cancel"} or not parts[2].isdigit():
        await callback.answer("Invalid gift action.", show_alert=True)
        return

    action = parts[1]
    gift_id = int(parts[2])
    if action == "cancel":
        cancelled, reason = await cancel_gift(gift_id, callback.from_user.id)
        if not cancelled:
            await callback.answer("🚫 This gift is not pending for you.", show_alert=True)
            return
        await _replace_gift_card(callback, "❌ <b>Gift cancelled.</b>")
        await callback.answer("Cancelled")
        return

    accepted, reason, character_ids = await accept_gift(gift_id, callback.from_user.id)
    if not accepted:
        if reason == "not_your_gift":
            await callback.answer("🚫 Not your gift to confirm.", show_alert=True)
            return
        if reason == "eternal_protected":
            await _replace_gift_card(callback, f"🚫 <b>{ETERNAL_TRANSFER_WARNING}</b>")
        elif reason == "overlord_protected":
            await _replace_gift_card(callback, f"🚫 <b>{OVERLORD_TRANSFER_WARNING}</b>")
        elif reason == "ownership_lost":
            await _replace_gift_card(
                callback,
                "❌ <b>Gift failed.</b> One or more characters are no longer available.",
            )
        else:
            await _replace_gift_card(callback, "❌ <b>Gift not found or already resolved.</b>")
        await callback.answer("Failed", show_alert=True)
        return

    if len(character_ids) == 1:
        character = await get_character_by_id(character_ids[0])
        character_name = escape(character["name"]) if character else f"Character #{character_ids[0]}"
        result = f"✅ <b>Gift complete!</b> {character_name} was transferred."
    else:
        result = f"✅ <b>Gift complete!</b> Transferred <b>{len(character_ids)}</b> characters."
    await _replace_gift_card(callback, result)
    await callback.answer("Gifted")
