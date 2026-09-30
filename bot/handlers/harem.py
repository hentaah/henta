"""/harem — show the user's global favorite image when set, otherwise a
random owned image, with a paginated collection caption grouped by anime.

The collection and favorite are global: characters caught in any group show
up in /harem regardless of the chat where the command is used.
"""
from __future__ import annotations
from bot.services.rich_renderer import event_badge, rarity_label, rarity_symbol
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery, Message, InlineKeyboardButton, InlineKeyboardMarkup,
)

from bot.services.game import (
    get_user_harem, get_user_harem_by_rarity,
    count_user_harem, count_user_harem_unique, count_user_harem_unique_by_rarity,
    get_harem_display_preference,
    get_favorite_owned_media, get_random_owned_media,
    get_anime_totals, get_user_anime_counts, get_user_display_name,
)
from bot.services.permissions import is_bot_owner
from bot.handlers.common import dot_player_command_filter, see_collection_keyboard
from bot.services.catalog_media import answer_catalog_media

router = Router()

PAGE_SIZE = 10
DIVIDER = "⚋⚋⚋⚋⚋⚋⚋⚋"
EMPTY_TEXT = "You Have Not Get Characters Yet."
MAX_TELEGRAM_ID = 9_223_372_036_854_775_807
MAX_HAREM_NAV_PAGE = 100_000


def _parse_own_harem_navigation(data: str | None) -> tuple[int, int] | None:
    """Parse only the owner-bound ``harem:<user_id>:<page>`` callback shape."""
    parts = (data or "").split(":")
    if (
        len(parts) != 3
        or parts[0] != "harem"
        or not all(part.isascii() and part.isdigit() for part in parts[1:])
    ):
        return None
    try:
        owner_id, page = (int(parts[1]), int(parts[2]))
    except ValueError:
        return None
    if not 0 < owner_id <= MAX_TELEGRAM_ID or not 1 <= page <= MAX_HAREM_NAV_PAGE:
        return None
    return owner_id, page


@router.message(Command("harem"))
@router.message(dot_player_command_filter("harem"))
async def cmd_harem(message: Message):
    user = message.from_user
    if not user:
        return
    await _send_harem(message, user.id, user.full_name, page=1)


@router.message(Command("haremownercheck"))
async def cmd_haremownercheck(message: Message):
    """Owner-only: render the replied user's global harem in normal layout."""
    actor = message.from_user
    if not actor or not await is_bot_owner(actor.id):
        return

    reply = message.reply_to_message
    target = reply.from_user if reply else None
    if not target:
        await message.reply("❌ Reply to a user with <code>/haremownercheck</code>.", parse_mode="HTML")
        return
    if target.is_bot:
        await message.reply("🚫 A bot does not have a harem.")
        return

    # The target's own /henmode preference, favorite, and global inventory are
    # used by _send_harem, while the special callback prefix keeps pagination
    # bound to the owner who opened this view.
    callback_prefix = f"oharem:{actor.id}:{target.id}"
    await _send_harem(
        message,
        target.id,
        target.full_name,
        page=1,
        navigation_prefix=callback_prefix,
    )


@router.callback_query(F.data.startswith("oharem:"))
async def cb_haremownercheck(callback: CallbackQuery):
    """Owner-bound pagination for a collection opened through /haremownercheck."""
    parts = (callback.data or "").split(":")
    if len(parts) != 4 or not all(part.isdigit() for part in parts[1:]):
        await callback.answer("Invalid owner harem page.", show_alert=True)
        return

    owner_id, target_user_id, page = (int(parts[1]), int(parts[2]), int(parts[3]))
    if callback.from_user.id != owner_id:
        await callback.answer("⛔ Only the owner who opened this harem can browse it.", show_alert=True)
        return
    if not await is_bot_owner(callback.from_user.id):
        await callback.answer("⛔ Owner only.", show_alert=True)
        return
    if not callback.message:
        await callback.answer("This harem view is unavailable.", show_alert=True)
        return

    display_name = await get_user_display_name(target_user_id)
    await _send_harem(
        callback.message,
        target_user_id,
        display_name,
        page=page,
        edit=True,
        navigation_prefix=f"oharem:{owner_id}:{target_user_id}",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("harem:"))
async def cb_harem(callback: CallbackQuery):
    """Allow only the harem owner to use that panel's Prev/Next buttons."""
    parsed = _parse_own_harem_navigation(callback.data)
    if parsed is None:
        await callback.answer("Invalid harem page.", show_alert=True)
        return

    owner_id, page = parsed
    if callback.from_user.id != owner_id:
        await callback.answer("⛔ This harem panel is not for you.", show_alert=True)
        return
    if not callback.message:
        await callback.answer("This harem panel is no longer available.", show_alert=True)
        return

    user = callback.from_user
    await _send_harem(
        callback.message,
        owner_id,
        user.full_name,
        page=page,
        edit=True,
        navigation_prefix=f"harem:{owner_id}",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("openharem:"))
async def cb_open_harem(callback: CallbackQuery):
    """'📦 Harem' button from the claim keyboard — opens /harem as a brand
    new message instead of editing whatever message the button was on
    (e.g. the spawn-reveal card, which must stay intact).
    """
    user = callback.from_user
    await _send_harem(callback.message, user.id, user.full_name, page=1, edit=False)
    await callback.answer()


async def _build_caption(user_id: int, display_name: str, page: int):
    """Render /harem using the persistent Anime/Rarity choice from /henmode."""
    page = max(1, page)
    offset = (page - 1) * PAGE_SIZE
    total_copies = await count_user_harem(user_id)
    display_mode, selected_rarity = await get_harem_display_preference(user_id)

    if display_mode == "rarity" and selected_rarity:
        total_unique = await count_user_harem_unique_by_rarity(user_id, selected_rarity)
        harem = await get_user_harem_by_rarity(
            user_id,
            selected_rarity,
            limit=PAGE_SIZE,
            offset=offset,
        )
    else:
        selected_rarity = None
        total_unique = await count_user_harem_unique(user_id)
        harem = await get_user_harem(user_id, limit=PAGE_SIZE, offset=offset)

    total_pages = max(1, (total_unique + PAGE_SIZE - 1) // PAGE_SIZE)
    if not harem:
        empty_text = (
            f"You Have Not Get {rarity_label(selected_rarity)} Characters Yet."
            if selected_rarity else EMPTY_TEXT
        )
        return None, total_copies, total_pages, empty_text

    animes = list(dict.fromkeys(c["anime"] for c in harem))
    anime_totals = await get_anime_totals(animes)
    # The familiar x/y anime progress is the player's complete global progress,
    # even when the visible card list is filtered to one rarity tier.
    owned_counts = await get_user_anime_counts(user_id, animes)

    SEP = "────────────────────────"
    title = (
        f"✦ {display_name}'s {rarity_label(selected_rarity)} Characters ✦"
        if selected_rarity else f"✦ {display_name}'s Recent Characters ✦"
    )
    lines = [title, ""]
    current_anime = None
    for c in harem:
        if c["anime"] != current_anime:
            current_anime = c["anime"]
            owned = owned_counts.get(current_anime, 0)
            total = anime_totals.get(current_anime, 0)
            lines.append(SEP)
            lines.append(f"📁 {current_anime} ⟨{owned}/{total}⟩")
        sym = rarity_symbol(c["rarity"])
        qty = c.get("qty", 1)
        badge = event_badge(c.get("event"))
        event_suffix = f" {badge}" if badge else ""
        love_suffix = ""
        if str(c.get("rarity") or "").casefold() == "love":
            code = str(c.get("love_code") or "Love")
            level = int(c.get("love_level") or 1)
            points = int(c.get("love_level_points") or 0)
            target = int(c.get("love_next_target") or 0)
            progress = "MAX" if level >= 5 else f"{points}/{target}"
            active = " • Active" if c.get("love_is_active") else ""
            love_suffix = f" ❤️ {code} Lv{level} {progress}{active}"
        lines.append(f"› [ {c['id']} ] {sym} {c['name']}{event_suffix}{love_suffix} ❨x{qty}❩")

    return "\n".join(lines).rstrip(), total_copies, total_pages, EMPTY_TEXT


def _nav_keyboard(
    page: int,
    total_pages: int,
    navigation_prefix: str = "harem",
) -> InlineKeyboardMarkup | None:
    """Build normal or owner-bound pagination without exposing target data."""
    buttons = []
    if page > 1:
        buttons.append(InlineKeyboardButton(
            text="⬅️ Prev",
            callback_data=f"{navigation_prefix}:{page - 1}",
            style="primary",
        ))
    if page < total_pages:
        buttons.append(InlineKeyboardButton(
            text="Next ➡️",
            callback_data=f"{navigation_prefix}:{page + 1}",
            style="primary",
        ))
    return InlineKeyboardMarkup(inline_keyboard=[buttons]) if buttons else None


async def _send_harem(
    message: Message,
    user_id: int,
    display_name: str,
    page: int,
    edit: bool = False,
    navigation_prefix: str | None = None,
):
    # Normal /harem pages encode their owner in the navigation callback. The
    # special /haremownercheck path passes its already owner-bound prefix explicitly.
    effective_navigation_prefix = navigation_prefix or f"harem:{user_id}"
    caption, total_copies, total_pages, empty_text = await _build_caption(user_id, display_name, page)

    if caption is None:
        if edit:
            try:
                await message.edit_caption(caption=empty_text, reply_markup=None)
            except Exception:
                try:
                    await message.edit_text(empty_text, reply_markup=None)
                except Exception:
                    pass
        else:
            await message.answer(empty_text)
        return

    nav = _nav_keyboard(page, total_pages, effective_navigation_prefix)
    # This glass button intentionally remains a normal inline collection view;
    # it is independent from, and must not be blocked by, page navigation.
    collection_btn = see_collection_keyboard(user_id, total_copies)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=(nav.inline_keyboard if nav else []) + collection_btn.inline_keyboard
    )

    if edit:
        try:
            await message.edit_caption(caption=caption, parse_mode="HTML", reply_markup=keyboard)
            return
        except Exception:
            pass  # fall through to a fresh photo below if editing failed

    header_media = await get_favorite_owned_media(user_id)
    if header_media is None:
        header_media = await get_random_owned_media(user_id)
    try:
        if header_media:
            await answer_catalog_media(
                message,
                character=header_media,
                caption=caption,
                reply_markup=keyboard,
            )
        else:
            await message.answer(caption, parse_mode="HTML", reply_markup=keyboard)
    except Exception:
        await message.answer(caption, parse_mode="HTML", reply_markup=keyboard)
