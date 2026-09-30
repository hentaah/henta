"""Inline mode — @BotUsername <query> in any chat."""
from __future__ import annotations

import re

from aiogram import Router
from aiogram.types import (
    InlineQuery,
    InlineQueryResultCachedPhoto,
    InlineQueryResultCachedVideo,
    InlineQueryResultArticle,
    InputTextMessageContent,
)

from bot.services.game import (
    get_anime_totals,
    get_user_anime_counts,
    get_user_harem,
    search_characters,
)
from bot.db.pool import get_pool
from bot.services.rich_renderer import (
    anime_emoji, event_badge, rarity_label, rarity_label_bold, rarity_symbol,
)
from bot.services.catalog_media import character_is_video
router = Router()

HAREM_ID_QUERY_RE = re.compile(r"^\s*harem\.(\d+)\s*$", re.IGNORECASE)
HAREM_SELF_QUERY_RE = re.compile(r"^\s*harem\s*$", re.IGNORECASE)
# A bare `harem.<user_id>` remains the private collection launcher. If someone
# leaves that old prefix before a name, treat the trailing words as the normal
# catalog query so `@BotUsername harem.123 Alya` works like `@BotUsername Alya`.
LEGACY_HAREM_CATALOG_SEARCH_RE = re.compile(
    r"^\s*harem\.\d+\s+(?P<search>.+?)\s*$",
    re.IGNORECASE,
)

# /csearch, /rsearch, etc. open inline mode with a compact control prefix
# such as ``rarity:Common ``.  The prefix is intentionally consumed here so
# the remainder stays a normal name/anime/alias search term.
RARITY_INLINE_QUERY_RE = re.compile(
    r"^\s*rarity\s*:\s*"
    r"(?P<rarity>common|rare|legendary|mythical|sovereign|op|eternal|suspicious|overlord)"
    r"(?:\s+(?P<search>.*?))?\s*$",
    re.IGNORECASE,
)
RARITY_CANONICAL = {
    "common": "Common",
    "rare": "Rare",
    "legendary": "Legendary",
    "mythical": "Mythical",
    "sovereign": "Sovereign",
    "op": "OP",
    "eternal": "Eternal",
    "suspicious": "Suspicious",
    "overlord": "OverLord",
}

# /eventsearch opens inline mode with `event:<curated-event-id> `. The numeric
# token is stable and server-resolved, while users can keep typing after it to
# narrow the chosen event by character name, anime, or alias.
EVENT_INLINE_QUERY_RE = re.compile(
    r"^\s*event\s*:\s*(?P<event_id>[1-9]\d*)"
    r"(?:\s+(?P<search>.*?))?\s*$",
    re.IGNORECASE,
)

SEP = "────────────────────────"
# Telegram's Bot API permits at most 50 inline results in one answer. Fetch one
# extra catalog/harem row so `next_offset` reliably exposes every later page.
INLINE_PAGE_SIZE = 50
INLINE_FETCH_SIZE = INLINE_PAGE_SIZE + 1
MAX_INLINE_OFFSET = 100_000
MAX_CATALOG_EVENT_ID = 9_223_372_036_854_775_807


def parse_inline_offset(offset: str | None) -> int:
    """Return a safe numeric cursor supplied by Telegram, or the first page."""
    raw = (offset or "").strip()
    if not raw.isascii() or not raw.isdigit():
        return 0
    try:
        value = int(raw)
    except ValueError:
        return 0
    return value if 0 <= value <= MAX_INLINE_OFFSET else 0


def split_inline_page(items: list[dict], offset: int) -> tuple[list[dict], str]:
    """Trim an extra fetched record and produce Telegram's next cursor."""
    page = items[:INLINE_PAGE_SIZE]
    next_offset = str(offset + len(page)) if len(items) > INLINE_PAGE_SIZE else ""
    return page, next_offset


def _event_name(character: dict) -> str:
    """Append the compact emoji-only event marker to a card name."""
    badge = event_badge(character.get("event"))
    return f"{character['name']} {badge}" if badge else str(character["name"])


def normalize_catalog_inline_query(query_text: str) -> str:
    """Make name-only catalog lookups work with or without an obsolete harem ID.

    Exact ``harem`` and ``harem.<user_id>`` requests are resolved before this
    helper, so they retain their existing harem behavior. Only a harem ID plus
    trailing words is normalized into the same ordinary catalog search text a
    player would get from typing ``@BotUsername Alya`` directly.
    """
    match = LEGACY_HAREM_CATALOG_SEARCH_RE.match(query_text)
    return (match.group("search") or "").strip() if match else query_text


def parse_rarity_inline_query(query_text: str) -> tuple[str, str | None]:
    """Split a rarity-launcher query into ``(free_text, canonical_rarity)``.

    Normal inline searches and malformed control prefixes remain ordinary
    full-catalog text searches, so typing a real character title is never
    unexpectedly filtered.
    """
    match = RARITY_INLINE_QUERY_RE.match(query_text)
    if not match:
        return query_text, None
    rarity = RARITY_CANONICAL[match.group("rarity").casefold()]
    return (match.group("search") or "").strip(), rarity


def parse_event_inline_query(query_text: str) -> tuple[str, int | None]:
    """Split a valid /eventsearch control into free text and curated event ID.

    Invalid or incomplete event controls deliberately remain ordinary catalog
    text searches. This matches the non-surprising fallback used by rarity
    search and keeps normal inline search behavior intact.
    """
    match = EVENT_INLINE_QUERY_RE.match(query_text)
    if not match:
        return query_text, None
    try:
        event_id = int(match.group("event_id"))
    except ValueError:
        return query_text, None
    if not 0 < event_id <= MAX_CATALOG_EVENT_ID:
        return query_text, None
    return (match.group("search") or "").strip(), event_id


def _search_result(
    char: dict,
    idx: int,
) -> InlineQueryResultCachedPhoto | InlineQueryResultCachedVideo | InlineQueryResultArticle:
    """✦ ─── 𝗖𝗛𝗔𝗥𝗔𝗖𝗧𝗘𝗥 𝗗𝗥𝗢𝗣 ─── ✦  — general search/browse card."""
    sym = rarity_symbol(char["rarity"])
    rar = rarity_label_bold(char["rarity"])
    ani_em = anime_emoji(char["anime"])

    # bold unicode digits for id
    bold_id = _bold_num(str(char["id"]))
    display_name = _event_name(char)

    caption = (
        f"✦ ─── 𝗖𝗛𝗔𝗥𝗔𝗖𝗧𝗘𝗥 𝗗𝗥𝗢𝗣 ─── ✦\n"
        f"🔮 {char['anime']}\n"
        f"🆔 {bold_id} ╏ {display_name}\n"
        f"⟨ {sym} 𝗥𝗔𝗥𝗜𝗧𝗬: {rar} ⟩\n"
        f"{SEP}"
    )
    result_id = f"search:{idx}:{char['id']}"

    if char.get("image_url"):
        if character_is_video(char):
            return InlineQueryResultCachedVideo(
                id=result_id,
                video_file_id=char["image_url"],
                title=display_name,
                description=f"{char['anime']} • {char['rarity']}",
                caption=caption,
                parse_mode="HTML",
            )
        return InlineQueryResultCachedPhoto(
            id=result_id,
            photo_file_id=char["image_url"],
            title=display_name,
            description=f"{char['anime']} • {char['rarity']}",
            caption=caption,
            parse_mode="HTML",
        )

    return InlineQueryResultArticle(
        id=result_id,
        title=f"{display_name} (no image yet)",
        description=f"{char['anime']} • {char['rarity']}",
        input_message_content=InputTextMessageContent(
            message_text=caption, parse_mode="HTML",
        ),
    )


def _harem_result(
    char: dict,
    idx: int,
    owner_name: str,
) -> InlineQueryResultCachedPhoto | InlineQueryResultCachedVideo | InlineQueryResultArticle:
    """─── 𝙾𝙿'𝚜 𝙲𝙷𝙰𝚁𝙰𝙲𝚃𝙴𝚁 ─── — card shown from a user's harem."""
    sym = rarity_symbol(char["rarity"])
    rar = rarity_label(char["rarity"])
    qty = char.get("qty", 1)
    anime_count = char.get("anime_count", "?")
    anime_total = char.get("anime_total", "?")
    bold_id = _bold_num(str(char["id"]))
    display_name = _event_name(char)

    caption = (
        f"─── {owner_name}'𝚜 𝙲𝙷𝙰𝚁𝙰𝙲𝚃𝙴𝚁 ───\n"
        f"📁 {char['anime']} ❨{anime_count}/{anime_total}❩\n"
        f"🆔 {bold_id} ╏ {display_name} ❨𝚡{qty}❩\n"
        f"⟨ 𝚁𝙰𝚁𝙸𝚃𝚈: {sym} {rar} ⟩\n"
        f"{SEP}"
    )
    result_id = f"harem:{idx}:{char['id']}"

    if char.get("image_url"):
        if character_is_video(char):
            return InlineQueryResultCachedVideo(
                id=result_id,
                video_file_id=char["image_url"],
                title=display_name,
                description=f"{char['anime']} • x{qty}",
                caption=caption,
                parse_mode="HTML",
            )
        return InlineQueryResultCachedPhoto(
            id=result_id,
            photo_file_id=char["image_url"],
            title=display_name,
            description=f"{char['anime']} • x{qty}",
            caption=caption,
            parse_mode="HTML",
        )

    return InlineQueryResultArticle(
        id=result_id,
        title=f"{display_name} (no image yet)",
        description=f"{char['anime']} • x{qty}",
        input_message_content=InputTextMessageContent(
            message_text=caption, parse_mode="HTML",
        ),
    )


def _bold_num(s: str) -> str:
    """Convert ASCII digits to bold unicode digits."""
    bold_digits = "𝟬𝟭𝟮𝟯𝟰𝟱𝟲𝟳𝟴𝟵"
    return "".join(bold_digits[int(c)] if c.isdigit() else c for c in s)


def _owner_rarity_label(harem: list[dict]) -> str:
    """Pick the highest rarity in the harem and return its label."""
    order = ["OverLord", "Suspicious", "Eternal", "OP", "Sovereign", "Mythical", "Legendary", "Rare", "Common"]
    rarities = {c["rarity"] for c in harem}
    for r in order:
        if r in rarities:
            return rarity_label(r)
    return "𝙲𝙾𝙼𝙼𝙾𝙽"


@router.inline_query()
async def handle_inline_query(inline_query: InlineQuery):
    """Answer one Telegram-sized page and expose a cursor for all later cards."""
    query_text = inline_query.query or ""
    offset = parse_inline_offset(inline_query.offset)

    m = HAREM_ID_QUERY_RE.match(query_text)
    target_user_id = int(m.group(1)) if m else (
        inline_query.from_user.id if HAREM_SELF_QUERY_RE.match(query_text) else None
    )

    if target_user_id is not None:
        harem_rows = await get_user_harem(
            target_user_id,
            limit=INLINE_FETCH_SIZE,
            offset=offset,
        )
        harem, next_offset = split_inline_page(harem_rows, offset)

        if not harem:
            # Telegram should not normally request an offset after we returned
            # an empty next_offset. Keep an out-of-range page empty instead of
            # falsely saying a player with earlier pages has no harem.
            if offset:
                await inline_query.answer(
                    results=[],
                    cache_time=0,
                    is_personal=True,
                    next_offset="",
                )
                return
            await inline_query.answer(
                results=[InlineQueryResultArticle(
                    id="empty",
                    title="No characters found",
                    description=f"User {target_user_id} has no harem yet.",
                    input_message_content=InputTextMessageContent(
                        message_text=f"📦 User <code>{target_user_id}</code> has an empty harem.",
                        parse_mode="HTML",
                    ),
                )],
                cache_time=10,
                is_personal=True,
                next_offset="",
            )
            return

        # Count globally, not only inside this 50-result inline page, so an
        # anime's x/y label stays accurate when a harem spans several pages.
        animes = list(dict.fromkeys(str(card["anime"]) for card in harem))
        anime_totals = await get_anime_totals(animes)
        anime_owned_counts = await get_user_anime_counts(target_user_id, animes)
        for card in harem:
            card["anime_count"] = anime_owned_counts.get(card["anime"], 0)
            card["anime_total"] = anime_totals.get(card["anime"], 0)
        if target_user_id == inline_query.from_user.id:
            owner_name = inline_query.from_user.full_name
        else:
            pool = get_pool()
            async with pool.acquire() as conn:
                owner_name = await conn.fetchval(
                    "SELECT full_name FROM users WHERE user_id = $1", target_user_id
                ) or f"User {target_user_id}"
        results = [_harem_result(card, offset + i, owner_name) for i, card in enumerate(harem)]
        await inline_query.answer(
            results=results,
            cache_time=0,
            is_personal=True,
            next_offset=next_offset,
        )
        return

    # Free-text search / browse-all. A player can simply type
    # `@BotUsername Alya`; an obsolete `harem.<id> Alya` prefix is normalized to
    # that same catalog name query. A launcher may contribute either a rarity
    # control or an event control; only the text after that control is searched.
    # Fetching 51 records lets us return Telegram's legal maximum 50 and still
    # know whether there is another page to scroll to.
    catalog_query_text = normalize_catalog_inline_query(query_text)
    search_text, rarity_filter = parse_rarity_inline_query(catalog_query_text)
    event_filter_id = None
    if rarity_filter is None:
        search_text, event_filter_id = parse_event_inline_query(catalog_query_text)

    match_rows = await search_characters(
        search_text,
        limit=INLINE_FETCH_SIZE,
        rarity=rarity_filter,
        offset=offset,
        event_id=event_filter_id,
    )
    matches, next_offset = split_inline_page(match_rows, offset)

    if not matches:
        filter_phrase = (
            f" {rarity_filter}"
            if rarity_filter
            else (" event" if event_filter_id is not None else "")
        )
        if offset:
            await inline_query.answer(results=[], cache_time=0, next_offset="")
            return
        await inline_query.answer(
            results=[InlineQueryResultArticle(
                id="none",
                title=f"No{filter_phrase} characters match",
                description=(
                    "Try a different name, or leave the search text blank to browse "
                    f"all{filter_phrase} cards."
                ),
                input_message_content=InputTextMessageContent(
                    message_text=f"🔍 No{filter_phrase} characters found for that search.",
                ),
            )],
            cache_time=10,
            next_offset="",
        )
        return

    results = [_search_result(card, offset + i) for i, card in enumerate(matches)]
    await inline_query.answer(results=results, cache_time=0, next_offset=next_offset)
