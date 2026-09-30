"""Public inline-catalog launchers, including the curated /eventsearch panel.

Each launcher sends a primary switch-inline button. Telegram inserts this bot's
inline username into the current chat when the user taps it. Rarity and event
control prefixes are consumed by ``inline_query.py`` rather than treated as
ordinary search text.
"""
from __future__ import annotations

from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.services.game import (
    get_catalog_event_for_search,
    list_catalog_events_for_search,
)

router = Router()


# The value is intentionally a human-readable inline prefix. It remains in the
# input field after Telegram opens inline mode, so users can immediately
# continue typing a character name, anime, or alias after it.
RARITY_LAUNCHERS: dict[str, tuple[str, str, str]] = {
    "csearch": ("Common", "✧", "Browse Common"),
    "rsearch": ("Rare", "◈", "Browse Rare"),
    "lsearch": ("Legendary", "✪", "Browse Legendary"),
    "msearch": ("Mythical", "✵", "Browse Mythical"),
    "ssearch": ("Sovereign", "♛", "Browse Sovereign"),
    "osearch": ("OP", "亗", "Browse OP"),
    "esearch": ("Eternal", "𖣘", "Browse Eternal"),
    "susearch": ("Suspicious", "ᯓ", "Browse Suspicious"),
    "ovsearch": ("OverLord", "☬", "Browse OverLord"),
}

# Event panels deliberately keep each choice on its own row for easy tapping.
# Pages prevent a very large catalog from exceeding Telegram's practical inline
# keyboard limits, while still exposing every reusable event.
EVENTS_PER_PAGE = 8
# Compact callback actions (`e:o` / `e:p`) keep max-bigint owner IDs, event
# IDs, and even unusually large page numbers under Telegram's 64-byte limit.
EVENT_CALLBACK_PREFIX = "e:"
MAX_BIGINT = 9_223_372_036_854_775_807


def _launcher_keyboard(label: str, inline_query: str) -> InlineKeyboardMarkup:
    """Return a primary button that opens this bot's inline mode in-place."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=f"🔎 {label}",
            switch_inline_query_current_chat=inline_query,
            style="primary",
        ),
    ]])


async def _send_launcher(
    message: Message,
    *,
    title: str,
    body: str,
    button_label: str,
    inline_query: str,
) -> None:
    await message.reply(
        f"🔍 <b>{title}</b>\n"
        "────────────────────────\n\n"
        f"{body}\n\n"
        "<i>Tap the button to open the inline catalog in this chat.</i>",
        parse_mode="HTML",
        reply_markup=_launcher_keyboard(button_label, inline_query),
    )


def _normalized_event_label(value: object) -> str:
    """Keep user-visible labels compact, safe for HTML, and keyboard-friendly."""
    label = " ".join(str(value or "").split())
    return label or "Unnamed Event"


def _truncate_label(label: str, limit: int) -> str:
    return label if len(label) <= limit else f"{label[: max(1, limit - 1)].rstrip()}…"


def _positive_int(value: str) -> int | None:
    """Parse a callback numeric token without accepting malformed IDs/pages."""
    if not value.isascii() or not value.isdigit():
        return None
    try:
        number = int(value)
    except ValueError:
        return None
    return number if 0 < number <= MAX_BIGINT else None


def _event_page(events: list[dict], requested_page: int) -> tuple[list[dict], int, int]:
    """Return a bounded 1-based event page and its total page count."""
    total_pages = max(1, (len(events) + EVENTS_PER_PAGE - 1) // EVENTS_PER_PAGE)
    page = min(max(1, requested_page), total_pages)
    start = (page - 1) * EVENTS_PER_PAGE
    return events[start:start + EVENTS_PER_PAGE], page, total_pages


def _event_selection_keyboard(
    events: list[dict],
    *,
    owner_id: int,
    requested_page: int,
) -> tuple[InlineKeyboardMarkup, int, int]:
    """Build an owner-bound, fully primary event selector with safe paging."""
    page_events, page, total_pages = _event_page(events, requested_page)
    rows: list[list[InlineKeyboardButton]] = []
    for event in page_events:
        event_id = int(event["id"])
        label = _truncate_label(_normalized_event_label(event.get("label")), 36)
        card_count = max(0, int(event.get("character_count") or 0))
        rows.append([InlineKeyboardButton(
            text=f"🎊 {label} · {card_count}",
            callback_data=f"{EVENT_CALLBACK_PREFIX}o:{owner_id}:{event_id}:{page}",
            style="primary",
        )])

    if total_pages > 1:
        navigation: list[InlineKeyboardButton] = []
        if page > 1:
            navigation.append(InlineKeyboardButton(
                text="⬅️ Previous",
                callback_data=f"{EVENT_CALLBACK_PREFIX}p:{owner_id}:{page - 1}",
                style="primary",
            ))
        if page < total_pages:
            navigation.append(InlineKeyboardButton(
                text="Next ➡️",
                callback_data=f"{EVENT_CALLBACK_PREFIX}p:{owner_id}:{page + 1}",
                style="primary",
            ))
        if navigation:
            rows.append(navigation)

    return InlineKeyboardMarkup(inline_keyboard=rows), page, total_pages


def _event_selection_text(event_count: int, page: int, total_pages: int) -> str:
    return (
        "🎊 <b>Event Search</b>\n"
        "────────────────────────\n\n"
        "Choose an event to browse only its character art cards. "
        "After selecting one, you can keep typing a name, anime, or alias to narrow it.\n\n"
        f"<i>{event_count} event{'s' if event_count != 1 else ''} · Page {page}/{total_pages}</i>"
    )


def _event_launch_keyboard(event: dict, *, owner_id: int, page: int) -> InlineKeyboardMarkup:
    """Build the second, primary inline launcher for exactly one event."""
    label = _truncate_label(_normalized_event_label(event.get("label")), 48)
    event_id = int(event["id"])
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"🔎 Search {label}",
            switch_inline_query_current_chat=f"event:{event_id} ",
            style="primary",
        )],
        [InlineKeyboardButton(
            text="⬅️ All Events",
            callback_data=f"{EVENT_CALLBACK_PREFIX}p:{owner_id}:{page}",
            style="primary",
        )],
    ])


async def _replace_event_panel(
    callback: CallbackQuery,
    text: str,
    keyboard: InlineKeyboardMarkup,
) -> None:
    """Edit the original glass panel, with a message fallback if it is stale."""
    if callback.message is None:
        return
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    except Exception:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=keyboard)


async def _render_event_selection(
    callback: CallbackQuery,
    *,
    owner_id: int,
    requested_page: int,
) -> None:
    events = await list_catalog_events_for_search()
    if not events:
        await _replace_event_panel(
            callback,
            "🎊 <b>Event Search</b>\n────────────────────────\n\n"
            "No reusable events are available right now.",
            InlineKeyboardMarkup(inline_keyboard=[]),
        )
        return
    keyboard, page, total_pages = _event_selection_keyboard(
        events,
        owner_id=owner_id,
        requested_page=requested_page,
    )
    await _replace_event_panel(
        callback,
        _event_selection_text(len(events), page, total_pages),
        keyboard,
    )


@router.message(Command("search"))
async def cmd_search(message: Message) -> None:
    """Open the full inline catalog; an empty inline query browses all art."""
    await _send_launcher(
        message,
        title="Character Search",
        body=(
            "Browse every character card by scrolling the inline results, or "
            "type a character name, anime, or alias after the inline username "
            "to search."
        ),
        button_label="Search Characters",
        inline_query="",
    )


@router.message(Command("eventsearch"))
async def cmd_eventsearch(message: Message) -> None:
    """Show every reusable event, then launch an inline search for one choice."""
    if message.from_user is None:
        return

    events = await list_catalog_events_for_search()
    if not events:
        await message.reply(
            "🎊 <b>Event Search</b>\n────────────────────────\n\n"
            "No reusable events are available right now.",
            parse_mode="HTML",
        )
        return

    keyboard, page, total_pages = _event_selection_keyboard(
        events,
        owner_id=message.from_user.id,
        requested_page=1,
    )
    await message.reply(
        _event_selection_text(len(events), page, total_pages),
        parse_mode="HTML",
        reply_markup=keyboard,
    )


@router.message(Command("csearch"))
async def cmd_csearch(message: Message) -> None:
    await _send_rarity_launcher(message, "csearch")


@router.message(Command("rsearch"))
async def cmd_rsearch(message: Message) -> None:
    await _send_rarity_launcher(message, "rsearch")


@router.message(Command("lsearch"))
async def cmd_lsearch(message: Message) -> None:
    await _send_rarity_launcher(message, "lsearch")


@router.message(Command("msearch"))
async def cmd_msearch(message: Message) -> None:
    await _send_rarity_launcher(message, "msearch")


@router.message(Command("ssearch"))
async def cmd_ssearch(message: Message) -> None:
    await _send_rarity_launcher(message, "ssearch")


@router.message(Command("osearch"))
async def cmd_osearch(message: Message) -> None:
    await _send_rarity_launcher(message, "osearch")


@router.message(Command("esearch"))
async def cmd_esearch(message: Message) -> None:
    await _send_rarity_launcher(message, "esearch")


@router.message(Command("susearch"))
async def cmd_susearch(message: Message) -> None:
    """Open inline catalog results filtered to the Suspicious tier."""
    await _send_rarity_launcher(message, "susearch")


@router.message(Command("ovsearch"))
async def cmd_ovsearch(message: Message) -> None:
    """Open inline catalog results filtered to the OverLord tier."""
    await _send_rarity_launcher(message, "ovsearch")


async def _send_rarity_launcher(message: Message, command: str) -> None:
    """Send the filtered inline-catalog launcher for one supported tier."""
    rarity, symbol, label = RARITY_LAUNCHERS[command]
    await _send_launcher(
        message,
        title=f"{symbol} {rarity} Character Search",
        body=(
            f"Browse only <b>{rarity}</b> character cards. After it opens, "
            "keep typing a character name, anime, or alias to narrow the results."
        ),
        button_label=label,
        inline_query=f"rarity:{rarity} ",
    )


@router.callback_query(F.data.startswith(EVENT_CALLBACK_PREFIX))
async def cb_eventsearch(callback: CallbackQuery) -> None:
    """Handle private event-panel navigation and selected-event launchers."""
    parts = (callback.data or "").split(":")
    if len(parts) < 4 or parts[0] != "e":
        await callback.answer("Invalid event search action.", show_alert=True)
        return

    action = parts[1]
    owner_id = _positive_int(parts[2])
    if owner_id is None or callback.from_user.id != owner_id:
        await callback.answer(
            "This event search panel belongs to the player who opened it.",
            show_alert=True,
        )
        return
    if callback.message is None:
        await callback.answer("This event search panel is no longer available.", show_alert=True)
        return

    if action == "p" and len(parts) == 4:
        page = _positive_int(parts[3])
        if page is None:
            await callback.answer("Invalid event page.", show_alert=True)
            return
        await _render_event_selection(callback, owner_id=owner_id, requested_page=page)
        await callback.answer()
        return

    if action != "o" or len(parts) != 5:
        await callback.answer("Invalid event search action.", show_alert=True)
        return
    event_id = _positive_int(parts[3])
    source_page = _positive_int(parts[4])
    if event_id is None or source_page is None:
        await callback.answer("Invalid event choice.", show_alert=True)
        return

    event = await get_catalog_event_for_search(event_id)
    if event is None:
        await _render_event_selection(callback, owner_id=owner_id, requested_page=source_page)
        await callback.answer("That event is no longer available. The list was refreshed.", show_alert=True)
        return

    label = _normalized_event_label(event.get("label"))
    card_count = max(0, int(event.get("character_count") or 0))
    await _replace_event_panel(
        callback,
        (
            f"🎊 <b>{escape(label)}</b>\n"
            "────────────────────────\n\n"
            f"This event currently has <b>{card_count}</b> character "
            f"{'card' if card_count == 1 else 'cards'}.\n\n"
            "<i>Tap the button below to place this event search into the inline input "
            "in the current chat.</i>"
        ),
        _event_launch_keyboard(event, owner_id=owner_id, page=source_page),
    )
    await callback.answer("Event selected")
