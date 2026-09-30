"""Full-owner Mini App market listing command."""
from __future__ import annotations

from decimal import Decimal
from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.services.market import (
    add_market_listing,
    list_market_listings,
    parse_market_price,
    remove_market_listing,
    remove_player_market_listing,
)
from bot.services.neko import format_neko_amount
from bot.services.permissions import is_bot_owner
from bot.services.catalog_media import answer_catalog_media, edit_catalog_media

router = Router()


def _market_arguments(message: Message) -> tuple[int, Decimal] | None:
    """Parse exactly ``/addmarket <character_id> <price>neko``."""
    parts = (message.text or "").strip().split()
    if len(parts) != 3:
        return None
    try:
        character_id = int(parts[1])
    except ValueError:
        return None
    if character_id <= 0:
        return None
    price = parse_market_price(parts[2])
    if price is None:
        return None
    return character_id, price


@router.message(Command("addmarket"))
async def cmd_add_market(message: Message) -> None:
    """Let a Full Owner publish one global Mini App listing by catalog ID."""
    actor = message.from_user
    if not actor or not await is_bot_owner(actor.id):
        await message.reply("🚫 Only Full Owners can add a Mini App market listing.")
        return

    parsed = _market_arguments(message)
    if parsed is None:
        await message.reply(
            "❌ Usage: <code>/addmarket &lt;character_id&gt; &lt;price&gt;neko</code>\n"
            "Example: <code>/addmarket 452 4000neko</code>",
            parse_mode="HTML",
        )
        return

    character_id, price = parsed
    try:
        result = await add_market_listing(
            character_id=character_id,
            price=price,
            listed_by=actor.id,
        )
    except Exception:
        await message.reply(
            "⚠️ I could not update the Mini App market right now. Please try again."
        )
        return

    if not result.get("success"):
        if result.get("reason") == "character_not_found":
            await message.reply(f"❌ Character <code>#{character_id}</code> was not found.", parse_mode="HTML")
        elif result.get("reason") == "eternal_protected":
            await message.reply("🚫 You can't sell, gift, or trade an Eternal rank card.")
        elif result.get("reason") == "overlord_protected":
            await message.reply("🚫 You can't sell, gift, or trade an OverLord rank card.")
        elif result.get("reason") == "love_list_only":
            await message.reply("❤️ Love cards are sold only through /lovelist and their fixed player /sell path.")
        else:
            await message.reply("❌ Invalid market listing arguments.")
        return

    listing = result["listing"]
    formatted_price = format_neko_amount(listing["price"])
    await message.reply(
        "🏪 <b>Market listing saved.</b>\n\n"
        f"🪪 <b>{escape(str(listing['name']))}</b>\n"
        f"🆔 Character: <code>#{int(listing['character_id'])}</code>\n"
        f"💎 Price: <b>{formatted_price} Neko</b>\n\n"
        "Only one copy is available. When a user buys it, the listing is removed "
        "until a Full Owner adds it again.",
        parse_mode="HTML",
    )


_OWNER_MARKET_CALLBACK_PREFIX = "ownermarket"
_OWNER_MARKET_REMOVE_CALLBACK_PREFIX = "ownermarketremove"
_MAX_OWNER_MARKET_PAGE = 100_000


def _single_market_code_argument(message: Message) -> int | None:
    """Parse exactly one positive ASCII catalog code after a market command."""
    parts = (message.text or "").strip().split()
    if len(parts) != 2 or not parts[1].isascii() or not parts[1].isdigit():
        return None
    character_id = int(parts[1])
    return character_id if character_id > 0 else None


def _owner_market_callback_data(owner_id: int, page: int) -> str:
    return f"{_OWNER_MARKET_CALLBACK_PREFIX}:{owner_id}:{page}"


def _parse_owner_market_callback(data: str | None) -> tuple[int, int] | None:
    """Accept only a bounded owner-bound market-review page callback."""
    parts = (data or "").split(":")
    if (
        len(parts) != 3
        or parts[0] != _OWNER_MARKET_CALLBACK_PREFIX
        or not all(part.isascii() and part.isdigit() for part in parts[1:])
    ):
        return None
    owner_id, page = int(parts[1]), int(parts[2])
    if owner_id <= 0 or not 1 <= page <= _MAX_OWNER_MARKET_PAGE:
        return None
    return owner_id, page


def _owner_market_remove_callback_data(owner_id: int, listing_id: int, page: int) -> str:
    return f"{_OWNER_MARKET_REMOVE_CALLBACK_PREFIX}:{owner_id}:{listing_id}:{page}"


def _parse_owner_market_remove_callback(data: str | None) -> tuple[int, int, int] | None:
    """Accept an exact listing-removal request bound to its Full Owner panel."""
    parts = (data or "").split(":")
    if (
        len(parts) != 4
        or parts[0] != _OWNER_MARKET_REMOVE_CALLBACK_PREFIX
        or not all(part.isascii() and part.isdigit() for part in parts[1:])
    ):
        return None
    owner_id, listing_id, page = (int(part) for part in parts[1:])
    if owner_id <= 0 or listing_id <= 0 or not 1 <= page <= _MAX_OWNER_MARKET_PAGE:
        return None
    return owner_id, listing_id, page


def _owner_market_keyboard(
    owner_id: int,
    page: int,
    total_pages: int,
    listing_id: int,
) -> InlineKeyboardMarkup:
    """Keep navigation bound to the Full Owner who opened the market review."""
    buttons: list[InlineKeyboardButton] = []
    if page > 1:
        buttons.append(
            InlineKeyboardButton(
                text="⬅️ Prev",
                callback_data=_owner_market_callback_data(owner_id, page - 1),
                style="primary",
            )
        )
    buttons.append(
        InlineKeyboardButton(
            text=f"{page}/{total_pages}",
            callback_data=_owner_market_callback_data(owner_id, page),
        )
    )
    if page < total_pages:
        buttons.append(
            InlineKeyboardButton(
                text="Next ➡️",
                callback_data=_owner_market_callback_data(owner_id, page + 1),
                style="primary",
            )
        )
    return InlineKeyboardMarkup(inline_keyboard=[
        buttons,
        [
            InlineKeyboardButton(
                text="🗑 Remove this listing",
                callback_data=_owner_market_remove_callback_data(owner_id, listing_id, page),
                style="danger",
            )
        ],
    ])


def _owner_market_caption(listing: dict[str, object], page: int, total_pages: int) -> str:
    """Render one owner-only listing card without interpolating unescaped catalog text."""
    name = escape(str(listing.get("name") or "Unknown"))
    anime = escape(str(listing.get("anime") or "—"))
    rarity = escape(str(listing.get("rarity") or "—"))
    event = str(listing.get("event") or "").strip()
    listing_id = int(listing["listing_id"])
    character_id = int(listing["character_id"])
    price = format_neko_amount(listing["price"])
    kind = str(listing.get("listing_kind") or "catalog")
    lines = [
        f"🏪 <b>Market card {page}/{total_pages}</b>",
        "",
        f"🪪 <b>{name}</b>",
        f"🧾 Listing: <code>#{listing_id}</code>",
        f"🆔 Code: <code>#{character_id}</code>",
        f"🎬 Anime: {anime}",
        f"✨ Rarity: {rarity}",
        f"💎 Price: <b>{price} Neko</b>",
    ]
    if kind == "player":
        seller_id = listing.get("seller_user_id")
        lines.append(f"👤 Player escrow: <code>{int(seller_id) if seller_id is not None else 'unknown'}</code>")
    else:
        lines.append("🏷 Official catalog stock")
    if event:
        lines.append(f"🎉 Event: {escape(event)}")
    lines.extend([
        "",
        "Use the red button below to remove this exact listing.",
    ])
    return "\n".join(lines)


async def _send_owner_market_text(
    message: Message,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    *,
    edit: bool,
) -> None:
    """Edit either a text/photo review safely, otherwise send a replacement."""
    if edit:
        try:
            await message.edit_text(text, parse_mode="HTML", reply_markup=reply_markup)
            return
        except Exception:
            try:
                await message.edit_caption(caption=text, parse_mode="HTML", reply_markup=reply_markup)
                return
            except Exception:
                pass
    await message.answer(text, parse_mode="HTML", reply_markup=reply_markup)


async def _send_owner_market_page(
    message: Message,
    owner_id: int,
    page: int,
    *,
    edit: bool,
) -> None:
    """Show exactly one active market card and owner-bound Prev/Next controls."""
    listings = await list_market_listings()
    if not listings:
        await _send_owner_market_text(
            message,
            "🏪 <b>Market is empty.</b>\n\nThere are no active card listings.",
            edit=edit,
        )
        return

    total_pages = len(listings)
    page = min(max(1, page), total_pages)
    listing = listings[page - 1]
    caption = _owner_market_caption(listing, page, total_pages)
    keyboard = _owner_market_keyboard(
        owner_id,
        page,
        total_pages,
        int(listing["listing_id"]),
    )
    if edit:
        try:
            if await edit_catalog_media(
                message,
                character=listing,
                caption=caption,
                reply_markup=keyboard,
            ):
                return
        except Exception:
            # A stale/missing Telegram file ID should not leave a broken owner
            # panel. Replace it with a fresh correctly typed media/text message.
            try:
                await message.delete()
            except Exception:
                pass
            edit = False

    if not edit:
        try:
            await answer_catalog_media(
                message,
                character=listing,
                caption=caption,
                reply_markup=keyboard,
            )
            return
        except Exception:
            # The listing remains visible and removable even if Telegram no
            # longer serves its historic photo/video file ID.
            pass

    await _send_owner_market_text(message, caption, keyboard, edit=edit)


@router.message(Command("ownermarketcheck"))
async def cmd_owner_market_check(message: Message) -> None:
    """Full Owner-only carousel of the cards currently offered in the Mini App."""
    actor = message.from_user
    if not actor or not await is_bot_owner(actor.id):
        await message.reply("🚫 Only Full Owners can view Mini App market listings.")
        return
    if len((message.text or "").strip().split()) != 1:
        await message.reply("❌ Usage: <code>/ownermarketcheck</code>", parse_mode="HTML")
        return
    try:
        await _send_owner_market_page(message, actor.id, page=1, edit=False)
    except Exception:
        await message.reply("⚠️ I could not load the Mini App market right now. Please try again.")


@router.callback_query(F.data.startswith(f"{_OWNER_MARKET_REMOVE_CALLBACK_PREFIX}:"))
async def cb_owner_market_remove(callback: CallbackQuery) -> None:
    """Let a Full Owner remove one exact catalog/player Market listing safely."""
    parsed = _parse_owner_market_remove_callback(callback.data)
    if parsed is None:
        await callback.answer("Invalid market removal.", show_alert=True)
        return
    owner_id, listing_id, page = parsed
    if callback.from_user.id != owner_id:
        await callback.answer("⛔ This market panel is not for you.", show_alert=True)
        return
    if not await is_bot_owner(callback.from_user.id):
        await callback.answer("⛔ Full Owner only.", show_alert=True)
        return
    if not callback.message:
        await callback.answer("This market panel is unavailable.", show_alert=True)
        return
    try:
        result = await remove_player_market_listing(
            listing_id=listing_id,
            actor_id=callback.from_user.id,
            actor_is_full_owner=True,
        )
    except Exception:
        await callback.answer("The listing could not be removed.", show_alert=True)
        return
    if not result.get("success"):
        await callback.answer("This listing is no longer available.", show_alert=True)
        return
    try:
        await _send_owner_market_page(callback.message, owner_id, page=page, edit=True)
    except Exception:
        await callback.answer("The market could not be refreshed.", show_alert=True)
        return
    if result.get("returned_to_seller"):
        await callback.answer("Listing removed; the card was returned to its seller.")
    else:
        await callback.answer("Catalog listing removed.")


@router.callback_query(F.data.startswith(f"{_OWNER_MARKET_CALLBACK_PREFIX}:"))
async def cb_owner_market_check(callback: CallbackQuery) -> None:
    """Page through a market review only as its original Full Owner."""
    parsed = _parse_owner_market_callback(callback.data)
    if parsed is None:
        await callback.answer("Invalid market page.", show_alert=True)
        return
    owner_id, page = parsed
    if callback.from_user.id != owner_id:
        await callback.answer("⛔ This market panel is not for you.", show_alert=True)
        return
    if not await is_bot_owner(callback.from_user.id):
        await callback.answer("⛔ Full Owner only.", show_alert=True)
        return
    if not callback.message:
        await callback.answer("This market panel is unavailable.", show_alert=True)
        return
    try:
        await _send_owner_market_page(callback.message, owner_id, page=page, edit=True)
    except Exception:
        await callback.answer("The market could not be refreshed.", show_alert=True)
        return
    await callback.answer()


@router.message(Command("deletemarket"))
async def cmd_delete_market(message: Message) -> None:
    """Full Owner-only removal of the active listing for exactly one catalog code."""
    actor = message.from_user
    if not actor or not await is_bot_owner(actor.id):
        await message.reply("🚫 Only Full Owners can remove a Mini App market listing.")
        return

    character_id = _single_market_code_argument(message)
    if character_id is None:
        await message.reply(
            "❌ Usage: <code>/deletemarket &lt;character_id&gt;</code>\n"
            "Example: <code>/deletemarket 452</code>",
            parse_mode="HTML",
        )
        return

    try:
        result = await remove_market_listing(character_id=character_id)
    except Exception:
        await message.reply("⚠️ I could not remove this market listing right now. Please try again.")
        return

    if not result.get("success"):
        await message.reply(
            f"ℹ️ Character <code>#{character_id}</code> has no active market listing.",
            parse_mode="HTML",
        )
        return

    listing = result["listing"]
    await message.reply(
        "🗑 <b>Market listing removed.</b>\n\n"
        f"🪪 <b>{escape(str(listing['name']))}</b>\n"
        f"🆔 Code: <code>#{int(listing['character_id'])}</code>\n"
        f"💎 Previous price: <b>{format_neko_amount(listing['price'])} Neko</b>\n\n"
        "The catalog card and all player Harem copies were left unchanged.",
        parse_mode="HTML",
    )
