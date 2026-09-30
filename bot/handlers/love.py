"""Player-facing Love ❤️ commands and user-bound interaction panels."""
from __future__ import annotations

from html import escape
from math import ceil

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.services.catalog_media import reply_catalog_media
from bot.services.love import (
    CINEMA_DATE_POINTS,
    CINEMA_DATE_PRICE,
    LOVE_MAX_LEVEL,
    RESTAURANT_DATE_POINTS,
    RESTAURANT_DATE_PRICE,
    create_love_market_listing,
    get_love_overview,
    list_love_cards,
    parse_love_code,
    perform_love_action,
    purchase_love_card,
    select_active_love,
)
from bot.services.neko import format_neko_amount

router = Router()
LOVE_LIST_PAGE_SIZE = 6


def _player_name(message_or_callback) -> str:
    user = getattr(message_or_callback, "from_user", None)
    return escape(str(getattr(user, "full_name", None) or getattr(user, "username", None) or "Player"))


def _cooldown_text(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds >= 3600:
        return f"{seconds // 3600}h {ceil((seconds % 3600) / 60)}m"
    return f"{max(1, ceil(seconds / 60))}m"


def _progress_text(state: dict) -> str:
    level = int(state.get("level") or 1)
    if level >= LOVE_MAX_LEVEL:
        return f"❤️ {state.get('love_code', 'Love')} • Level {LOVE_MAX_LEVEL} MAX"
    return (
        f"❤️ {state.get('love_code', 'Love')} • Level {level} — "
        f"{int(state.get('level_points') or 0)}/{int(state.get('next_target') or 0)} points"
    )


def _lovelist_keyboard(cards: list[dict], *, user_id: int, page: int) -> InlineKeyboardMarkup:
    total_pages = max(1, ceil(len(cards) / LOVE_LIST_PAGE_SIZE))
    page = min(max(1, page), total_pages)
    start = (page - 1) * LOVE_LIST_PAGE_SIZE
    current = cards[start:start + LOVE_LIST_PAGE_SIZE]
    rows = [
        [InlineKeyboardButton(
            text=f"❤️ Buy {card['love_code']} • {format_neko_amount(card['base_price'])} Neko",
            callback_data=f"lovebuy:{user_id}:{int(card['love_code_number'])}",
            style="primary",
        )]
        for card in current
    ]
    nav: list[InlineKeyboardButton] = []
    if page > 1:
        nav.append(InlineKeyboardButton(
            text="⬅️ Prev", callback_data=f"lovelist:{user_id}:{page - 1}", style="primary"
        ))
    nav.append(InlineKeyboardButton(text=f"{page}/{total_pages}", callback_data=f"lovelist:{user_id}:{page}"))
    if page < total_pages:
        nav.append(InlineKeyboardButton(
            text="Next ➡️", callback_data=f"lovelist:{user_id}:{page + 1}", style="primary"
        ))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _lovelist_text(cards: list[dict], *, page: int) -> str:
    if not cards:
        return (
            "❤️ <b>Love List</b>\n\n"
            "No Love cards have been published yet. Love cards are never random spawns."
        )
    total_pages = max(1, ceil(len(cards) / LOVE_LIST_PAGE_SIZE))
    page = min(max(1, page), total_pages)
    start = (page - 1) * LOVE_LIST_PAGE_SIZE
    current = cards[start:start + LOVE_LIST_PAGE_SIZE]
    lines = [
        f"❤️ <b>Love List</b> — page {page}/{total_pages}",
        "",
        "Buy one Love card with Neko. A normal player may own one at a time.",
        "Owner gifts can add more; use /loveselect L-code to choose your active card.",
        "",
    ]
    for card in current:
        lines.extend((
            f"<b>{escape(str(card['love_code']))}</b> • {escape(str(card['name']))}",
            f"🎬 {escape(str(card['anime']))}",
            f"💎 {format_neko_amount(card['base_price'])} Neko • 5 visual levels",
            f"🖼️ Preview: <code>/check {escape(str(card['love_code']))} 1</code>",
            "",
        ))
    return "\n".join(lines).rstrip()


async def _send_lovelist(message: Message, *, user_id: int, page: int, edit: bool = False) -> None:
    cards = await list_love_cards()
    text = _lovelist_text(cards, page=page)
    keyboard = _lovelist_keyboard(cards, user_id=user_id, page=page) if cards else None
    if edit:
        try:
            await message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
            return
        except Exception:
            try:
                await message.edit_caption(caption=text, parse_mode="HTML", reply_markup=keyboard)
                return
            except Exception:
                pass
    await message.reply(text, parse_mode="HTML", reply_markup=keyboard)


@router.message(Command("lovelist"))
async def cmd_lovelist(message: Message) -> None:
    if not message.from_user:
        return
    await _send_lovelist(message, user_id=message.from_user.id, page=1)


@router.callback_query(F.data.startswith("lovelist:"))
async def cb_lovelist(callback: CallbackQuery) -> None:
    parts = (callback.data or "").split(":")
    if (
        len(parts) != 3
        or not all(part.isascii() and part.isdigit() for part in parts[1:])
        or not callback.from_user
        or not callback.message
    ):
        await callback.answer("Invalid Love List page.", show_alert=True)
        return
    owner_id, page = int(parts[1]), int(parts[2])
    if callback.from_user.id != owner_id or page <= 0:
        await callback.answer("This Love List is not for you.", show_alert=True)
        return
    await _send_lovelist(callback.message, user_id=owner_id, page=page, edit=True)
    await callback.answer()


@router.callback_query(F.data.startswith("lovebuy:"))
async def cb_love_buy(callback: CallbackQuery) -> None:
    parts = (callback.data or "").split(":")
    if (
        len(parts) != 3
        or not all(part.isascii() and part.isdigit() for part in parts[1:])
        or not callback.from_user
        or not callback.message
    ):
        await callback.answer("Invalid Love purchase.", show_alert=True)
        return
    owner_id, code_number = int(parts[1]), int(parts[2])
    if callback.from_user.id != owner_id:
        await callback.answer("This Love List is not for you.", show_alert=True)
        return
    result = await purchase_love_card(
        user_id=owner_id,
        love_code=code_number,
        username=callback.from_user.username,
        full_name=callback.from_user.full_name,
        chat_id=callback.message.chat.id,
    )
    if not result.get("success"):
        reason = result.get("reason")
        if reason == "already_has_love":
            text = "❤️ You already own a Love card. Sell it first, or use /loveselect L-code if an owner gave you several."
        elif reason == "insufficient_neko":
            text = (
                f"💔 You need <b>{format_neko_amount(result['price'])} Neko</b> for this Love card. "
                f"Your balance is <b>{format_neko_amount(result['balance'])} Neko</b>."
            )
        elif reason == "love_not_found":
            text = "❌ This Love card is no longer available. Refresh /lovelist."
        else:
            text = "❌ This Love purchase could not be completed."
        await callback.answer("Purchase unavailable", show_alert=True)
        await callback.message.reply(text, parse_mode="HTML")
        return

    card = result["card"]
    caption = (
        "❤️ <b>Love card acquired!</b>\n\n"
        f"{_player_name(callback)}, you chose <b>{escape(str(card['name']))}</b>.\n"
        f"🏷️ {escape(str(card['love_code']))} • Level 1\n"
        f"💎 Paid: <b>{format_neko_amount(card['base_price'])} Neko</b>\n"
        f"🐾 Balance: <b>{format_neko_amount(card['balance_after'])} Neko</b>\n\n"
        "Use /kiss, /hug, or /date to build Love Points."
    )
    level_one = card["levels"][1]
    character = {
        "id": card["character_id"], "name": card["name"], "anime": card["anime"],
        "rarity": "Love", "image_url": level_one["image_url"],
        "media_type": level_one.get("media_type", "photo"), "event": card.get("event", ""),
    }
    try:
        await reply_catalog_media(callback.message, character=character, caption=caption)
    except Exception:
        await callback.message.reply(caption, parse_mode="HTML")
    await callback.answer("Love card acquired")


@router.message(Command("loveselect"))
async def cmd_loveselect(message: Message) -> None:
    if not message.from_user:
        return
    parts = (message.text or "").split(maxsplit=1)
    code = parts[1].strip() if len(parts) > 1 else ""
    if parse_love_code(code) is None:
        await message.reply("❤️ Usage: <code>/loveselect L1</code>", parse_mode="HTML")
        return
    result = await select_active_love(user_id=message.from_user.id, love_code=code)
    if not result.get("success"):
        await message.reply("❌ You do not own that available Love card.")
        return
    state = result["state"]
    await message.reply(
        f"❤️ { _player_name(message) }, <b>{escape(str(state['name']))}</b> "
        f"({escape(str(state['love_code']))}) is now your active Love card.\n"
        f"{_progress_text(state)}",
        parse_mode="HTML",
    )


def _date_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"🍽 Restaurant • {format_neko_amount(RESTAURANT_DATE_PRICE)} Neko • +{RESTAURANT_DATE_POINTS}",
            callback_data=f"lovedate:{user_id}:restaurant", style="primary",
        )],
        [InlineKeyboardButton(
            text=f"🎬 Cinema • {format_neko_amount(CINEMA_DATE_PRICE)} Neko • +{CINEMA_DATE_POINTS}",
            callback_data=f"lovedate:{user_id}:cinema", style="primary",
        )],
    ])


@router.message(Command("date"))
async def cmd_date(message: Message) -> None:
    if not message.from_user:
        return
    overview = await get_love_overview(user_id=message.from_user.id)
    active = overview.get("active")
    if not active:
        await message.reply("❤️ You need an active Love card first. Browse /lovelist.")
        return
    if int(active.get("level") or 1) >= LOVE_MAX_LEVEL:
        await message.reply(
            f"❤️ {_player_name(message)}, <b>{escape(str(active['name']))}</b> is already Level 5 MAX. "
            "Dates no longer add points; use /kiss, /hug, or /romance.",
            parse_mode="HTML",
        )
        return
    await message.reply(
        "💕 <b>Private Love Date Panel</b>\n\n"
        f"{_player_name(message)}, choose a safe date with <b>{escape(str(active['name']))}</b>.\n"
        f"{_progress_text(active)}\n\n"
        "🍽 Restaurant: 1,000 Neko → +5 points\n"
        "🎬 Cinema: 3,000 Neko → +8 points\n"
        "⏳ Shared date cooldown: 3 hours",
        parse_mode="HTML",
        reply_markup=_date_keyboard(message.from_user.id),
    )


async def _run_love_action(message: Message, *, user, action: str) -> None:
    result = await perform_love_action(
        user_id=user.id,
        action=action,
        username=user.username,
        full_name=user.full_name,
    )
    player = escape(str(user.full_name or user.username or "Player"))
    if not result.get("success"):
        state = result.get("state") or {}
        character_name = escape(str(state.get("name") or "your Love card"))
        reason = result.get("reason")
        if reason == "no_active_love":
            text = "❤️ You need an active Love card first. Browse /lovelist or use /loveselect L-code."
        elif reason == "not_max":
            text = f"❤️ {player}, <b>{character_name}</b> needs Level 5 MAX before /romance."
        elif reason == "max_no_points":
            text = f"❤️ {player}, <b>{character_name}</b> is already Level 5 MAX. This date cannot add more points."
        elif reason == "cooldown":
            text = f"⏳ {player}, wait <b>{_cooldown_text(result.get('remaining_seconds', 0))}</b> before interacting with <b>{character_name}</b> again."
        elif reason == "insufficient_neko":
            text = (
                f"💔 {player}, you need <b>{format_neko_amount(result['price'])} Neko</b> for this date with "
                f"<b>{character_name}</b>. You have <b>{format_neko_amount(result['balance'])} Neko</b>."
            )
        elif reason == "balance_limit":
            text = f"💰 {player}, your Neko wallet is already at its reward limit."
        else:
            text = "❌ This Love action could not be completed."
        await message.reply(text, parse_mode="HTML")
        return

    state = result["state"]
    character_name = escape(str(state["name"]))
    action_lines = {
        "kiss": f"💋 {player} shared a kiss with <b>{character_name}</b>.",
        "hug": f"🤗 {player} shared a hug with <b>{character_name}</b>.",
        "date_restaurant": f"🍽️ {player} went to a restaurant with <b>{character_name}</b>.",
        "date_cinema": f"🎬 {player} went to the cinema with <b>{character_name}</b>.",
        "romance": f"💞 {player} shared a warm, safe romantic moment with <b>{character_name}</b>.",
    }
    lines = [action_lines[action]]
    if result["date_cost"]:
        lines.append(f"💎 Spent: <b>{format_neko_amount(result['date_cost'])} Neko</b>")
    if result["points_awarded"]:
        lines.append(f"❤️ Love Points: <b>+{int(result['points_awarded'])}</b>")
    if result["reward"]:
        lines.append(f"🎁 Neko reward: <b>+{format_neko_amount(result['reward'])} Neko</b>")
    if result["leveled_up"]:
        lines.append(f"✨ <b>Level up!</b> {character_name} reached <b>Level {int(state['level'])}</b>{' MAX' if result['became_max'] else ''}.")
    lines.append(_progress_text(state))
    if result["reward"] or result["date_cost"]:
        lines.append(f"🐾 Balance: <b>{format_neko_amount(result['balance_after'])} Neko</b>")
    await message.reply("\n".join(lines), parse_mode="HTML")


@router.message(Command("kiss"))
async def cmd_kiss(message: Message) -> None:
    if message.from_user:
        await _run_love_action(message, user=message.from_user, action="kiss")


@router.message(Command("hug"))
async def cmd_hug(message: Message) -> None:
    if message.from_user:
        await _run_love_action(message, user=message.from_user, action="hug")


@router.message(Command("romance"))
async def cmd_romance(message: Message) -> None:
    if message.from_user:
        await _run_love_action(message, user=message.from_user, action="romance")


@router.callback_query(F.data.startswith("lovedate:"))
async def cb_love_date(callback: CallbackQuery) -> None:
    parts = (callback.data or "").split(":")
    if (
        len(parts) != 3
        or not parts[1].isascii()
        or not parts[1].isdigit()
        or parts[2] not in {"restaurant", "cinema"}
        or not callback.from_user
        or not callback.message
    ):
        await callback.answer("Invalid Love date panel.", show_alert=True)
        return
    if callback.from_user.id != int(parts[1]):
        await callback.answer("This private Love date panel is not for you.", show_alert=True)
        return
    await _run_love_action(
        callback.message,
        user=callback.from_user,
        action=f"date_{parts[2]}",
    )
    await callback.answer()


@router.message(Command("sell"))
async def cmd_sell_love(message: Message) -> None:
    """List the currently active Love card at its original owner-set price."""
    if not message.from_user:
        return
    overview = await get_love_overview(user_id=message.from_user.id)
    active = overview.get("active")
    if not active:
        await message.reply("❤️ You do not have an active Love card to sell.")
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="❤️ Confirm fixed-price sale",
            callback_data=f"lovesell:{message.from_user.id}:confirm",
            style="danger",
        ),
        InlineKeyboardButton(
            text="Cancel",
            callback_data=f"lovesell:{message.from_user.id}:cancel",
        ),
    ]])
    await message.reply(
        "🏪 <b>Sell active Love card?</b>\n\n"
        f"{_player_name(message)}, list <b>{escape(str(active['name']))}</b> "
        f"({escape(str(active['love_code']))}) for its fixed original price: "
        f"<b>{format_neko_amount(active['base_price'])} Neko</b>.\n\n"
        "Your current Love level and points stay safely escrowed while listed. "
        "After another player buys it, their card begins at Level 1 with 0 points.",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


@router.callback_query(F.data.startswith("lovesell:"))
async def cb_love_sell(callback: CallbackQuery) -> None:
    parts = (callback.data or "").split(":")
    if (
        len(parts) != 3
        or not parts[1].isascii()
        or not parts[1].isdigit()
        or parts[2] not in {"confirm", "cancel"}
        or not callback.from_user
        or not callback.message
    ):
        await callback.answer("Invalid Love sale.", show_alert=True)
        return
    if callback.from_user.id != int(parts[1]):
        await callback.answer("This Love sale confirmation is not for you.", show_alert=True)
        return
    if parts[2] == "cancel":
        await callback.message.reply("❌ Love sale cancelled.")
        await callback.answer("Cancelled")
        return
    result = await create_love_market_listing(user_id=callback.from_user.id)
    if not result.get("success"):
        reason = result.get("reason")
        if reason == "no_active_love":
            text = "❤️ You no longer have an active Love card to sell."
        elif reason == "ownership_lost":
            text = "❌ That Love card is no longer in your Harem."
        elif reason == "already_listed":
            text = "🏪 Your active Love card is already listed."
        else:
            text = "❌ Your Love sale could not be created."
        await callback.message.reply(text)
        await callback.answer("Sale unavailable", show_alert=True)
        return
    await callback.message.reply(
        "🏪 <b>Love card listed!</b>\n\n"
        f"{_player_name(callback)}, <b>{escape(str(result['name']))}</b> "
        f"({escape(str(result['love_code']))}) is now in Market for "
        f"<b>{format_neko_amount(result['price'])} Neko</b>.\n"
        "If the listing is cancelled, your saved level and points return to you.",
        parse_mode="HTML",
    )
    await callback.answer("Love card listed")
