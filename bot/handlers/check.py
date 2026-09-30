"""/check — catalog ART VIEW cards and global collector details."""
from __future__ import annotations

from html import escape
from urllib.parse import quote

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import InputMediaPhoto, Message

from bot.services.catalog_media import (
    answer_catalog_media,
    character_is_video,
    reply_catalog_media,
)
from bot.services.game import (
    get_character_by_id,
    get_character_global_catch_count,
    get_character_top_catchers,
    get_characters_by_ids,
)
from bot.services.rich_renderer import rarity_label, rarity_symbol
from bot.services.love import (
    LOVE_MAX_LEVEL, LOVE_RARITY, get_love_card_by_character_id, get_love_card_by_code, parse_love_code,
)

router = Router()
MAX_CHECK_CHARACTERS = 3
MAX_CHECK_CAPTION_LENGTH = 1_000
SEP = "────────────────────────"

# Display-only collector-value bands requested for ART VIEW. They intentionally
# do not change Neko balances, burn rewards, Market listings, or game odds.
RARITY_VALUE_BANDS = {
    "common": "80 ~ 170 neko",
    "rare": "180 ~ 270 neko",
    "legendary": "280 ~ 370 neko",
    "mythical": "380 ~ 500 neko",
    "sovereign": "510 ~ 740 neko",
    "op": "750 ~ 900 neko",
    "eternal": "1k ~ 2k neko",
    "suspicious": "3k ~ 5k neko",
    "overlord": "8k ~ 20k neko",
}


def rarity_value_band(rarity: object) -> str:
    """Return the requested display value for a valid ordinary catalog rarity."""
    return RARITY_VALUE_BANDS.get(str(rarity or "").strip().casefold(), "—")


def _character_value(character: dict) -> str:
    """Keep Love's owner-set fixed price display-only like other /check values."""
    if str(character.get("rarity") or "").strip().casefold() == LOVE_RARITY.casefold():
        price = character.get("love_base_price") or character.get("base_price")
        return f"Fixed {escape(str(price))} Neko" if price is not None else "Fixed Love price"
    return rarity_value_band(character.get("rarity"))


def _append_caption_line(lines: list[str], line: str) -> bool:
    """Append a complete safe HTML line only while a Telegram caption fits."""
    candidate = "\n".join([*lines, line])
    if len(candidate) > MAX_CHECK_CAPTION_LENGTH:
        return False
    lines.append(line)
    return True


def _collector_line(index: int, total: int, catcher: dict) -> str:
    """Render one escaped top-collector line without trusting profile text."""
    prefix = "└" if index == total - 1 else "├"
    quantity = max(0, int(catcher.get("qty") or 0))
    username = str(catcher.get("username") or "").strip()
    display = str(catcher.get("full_name") or username or f"User {catcher.get('user_id')}")
    display = " ".join(display.split()) or f"User {catcher.get('user_id')}"
    # Long display names can otherwise make a valid ten-row caption exceed
    # Telegram's caption limit. Keep the link/text itself complete and escaped.
    if len(display) > 72:
        display = display[:71].rstrip() + "…"
    if username:
        url_username = quote(username.lstrip("@"), safe="_")
        label = f'<a href="https://t.me/{url_username}">{escape(display)}</a>'
    else:
        # A user can have a usable Telegram display name without a public
        # username. Keep that name in ART VIEW rather than replacing it with a
        # bare numeric ID, matching the requested collector list style.
        label = escape(display)
    return f"{prefix} {label} ━ [{quantity}]"


async def _build_art_view_caption(character: dict) -> str:
    """Build the requested detailed ART VIEW for one catalog character."""
    character_id = int(character.get("id") or 0)
    global_count = await get_character_global_catch_count(character_id)
    top_catchers = await get_character_top_catchers(character_id, limit=10)
    rarity = str(character.get("rarity") or "Common")
    lines = [
        "⚡ ─── ART VIEW ─── ⚡",
        "",
        f"⛩️ Anime : {escape(str(character.get('anime') or ''))}",
        "",
        f"🌸 Name : {escape(str(character.get('name') or ''))}",
        "",
        f"🏷️ ID : {escape(str(character.get('id') or character_id))}",
        "",
        f"🔱 Rarity : {rarity_symbol(rarity)}  {escape(rarity_label(rarity).upper())}",
        "",
        f"🐾 Value : {_character_value(character)}",
    ]
    event = str(character.get("event") or "").strip()
    if event:
        _append_caption_line(lines, "")
        _append_caption_line(lines, f"🎊 Event : {escape(event)}")
    if str(rarity).casefold() == LOVE_RARITY.casefold() and character.get("love_code"):
        _append_caption_line(lines, "")
        _append_caption_line(lines, f"❤️ Love Code : <code>{escape(str(character['love_code']))}</code>")
        _append_caption_line(lines, "")
        _append_caption_line(lines, "🖼️ Base art: Love Level 1 of 5")
    _append_caption_line(lines, "")
    _append_caption_line(lines, SEP)
    _append_caption_line(lines, "")
    _append_caption_line(lines, f"📊 Caught Globally : {max(0, int(global_count or 0))}")
    _append_caption_line(lines, "")
    _append_caption_line(lines, "🏆 Top Collectors :")
    _append_caption_line(lines, "")
    if not top_catchers:
        _append_caption_line(lines, "└ Nobody has caught this character yet.")
    else:
        for index, catcher in enumerate(top_catchers):
            if not _append_caption_line(lines, _collector_line(index, len(top_catchers), catcher)):
                _append_caption_line(lines, "└ … more collectors are not shown here.")
                break
    return "\n".join(lines)


async def _send_full_check_card(
    message: Message,
    character: dict,
    *,
    reply_to_command: bool,
) -> None:
    """Send one rich ART VIEW while retaining photo/video-specific media paths."""
    caption = await _build_art_view_caption(character)
    try:
        if reply_to_command:
            await reply_catalog_media(message, character=character, caption=caption)
        else:
            await answer_catalog_media(message, character=character, caption=caption)
        return
    except Exception:
        # A stale Telegram photo/video must not prevent the requested collector
        # details from being viewed as text.
        if reply_to_command:
            await message.reply(caption, parse_mode="HTML", disable_web_page_preview=True)
        else:
            await message.answer(caption, parse_mode="HTML", disable_web_page_preview=True)


async def _send_original_single_check(message: Message, character_id: int) -> None:
    """Fetch then send the full ART VIEW including value and collector detail."""
    char = await get_character_by_id(character_id)
    if not char:
        await message.reply(
            f"❌ No character with ID <code>{character_id}</code>.",
            parse_mode="HTML",
        )
        return
    # Numeric check intentionally remains the normal detailed ART VIEW. A Love
    # card's regular catalog image is Level 1, with its L-code/price supplied
    # as extra information rather than replacing the historic numeric layout.
    if str(char.get("rarity") or "").casefold() == LOVE_RARITY.casefold():
        love_card = await get_love_card_by_character_id(character_id)
        if love_card:
            char["love_code"] = love_card["love_code"]
            char["love_base_price"] = love_card["base_price"]
    await _send_full_check_card(message, char, reply_to_command=True)


def _check_character_block(character: dict) -> str:
    """Render the requested compact card used by multi-ID `/check` calls."""
    rarity = str(character.get("rarity") or "Common")
    character_id = escape(str(character.get("id") or ""))
    name = escape(str(character.get("name") or ""))
    anime = escape(str(character.get("anime") or ""))
    lines = [
        "─── ❖ 𝗖𝗛𝗘𝗖𝗞 𝗖𝗔𝗥𝗗 ❖ ───",
        "",
        f"◈──『 🏷️ #{character_id} ⋄ 🌸 {name} 』──◈",
        "",
        f"├ ⛩️ {anime}",
    ]
    event = str(character.get("event") or "").strip()
    if event:
        lines.append(f"├ 🎊 {escape(event)}")
    lines.extend((
        f"├ 🔱 {rarity_symbol(rarity)} {escape(rarity)}",
        f"└ 🐾 {_character_value(character)}",
    ))
    return "\n".join(lines)


def _batch_check_caption(characters: list[dict]) -> str:
    """Keep multi-ID `/check` compact while each card shows its own Value."""
    return "\n\n".join(_check_character_block(character) for character in characters)


async def _send_multi_fallback_card(message: Message, character: dict) -> None:
    """Send one compact photo/video-aware card if a photo album is unavailable."""
    caption = _check_character_block(character)
    try:
        await answer_catalog_media(message, character=character, caption=caption)
    except Exception:
        await message.answer(caption, parse_mode="HTML", disable_web_page_preview=True)



async def _send_love_level_check(message: Message, love_card: dict, level: int) -> None:
    """Publicly preview one static owner-provided Love level image."""
    level_media = love_card["levels"].get(level)
    if not level_media:
        await message.reply("❌ That Love image level is unavailable.")
        return
    character = {
        "id": love_card["character_id"],
        "name": love_card["name"],
        "anime": love_card["anime"],
        "rarity": LOVE_RARITY,
        "rarity_score": 0,
        "event": love_card.get("event", ""),
        "image_url": level_media["image_url"],
        "media_type": level_media.get("media_type", "photo"),
        "love_code": love_card["love_code"],
        "love_base_price": love_card["base_price"],
    }
    caption = "\n".join((
        "❤️ ─── LOVE ART VIEW ─── ❤️",
        "",
        f"⛩️ Anime : {escape(str(love_card['anime']))}",
        "",
        f"🌸 Name : {escape(str(love_card['name']))}",
        "",
        f"🏷️ Love Code : <code>{escape(str(love_card['love_code']))}</code>",
        f"📚 Catalog ID : <code>{int(love_card['character_id'])}</code>",
        "",
        f"🔱 Rarity : {rarity_symbol(LOVE_RARITY)}  {escape(rarity_label(LOVE_RARITY).upper())}",
        "",
        f"🖼️ Image Level : <b>{level}</b> / {LOVE_MAX_LEVEL}",
        f"💎 Lovelist Price : <b>{escape(str(love_card['base_price']))} Neko</b>",
        "",
        "Use /lovelist to buy a Love card. This is a public visual preview.",
    ))
    try:
        await reply_catalog_media(message, character=character, caption=caption)
    except Exception:
        await message.reply(caption, parse_mode="HTML", disable_web_page_preview=True)


@router.message(Command("check"))
async def cmd_check(message: Message) -> None:
    tokens = (message.text or "").split()[1:]
    if not tokens:
        await message.reply(
            "🔎 Usage: <code>/check &lt;character_id&gt; [up to 3 IDs]</code>\n"
            "❤️ Love preview: <code>/check L1 1</code> through <code>/check L1 5</code>",
            parse_mode="HTML",
        )
        return

    love_code_number = parse_love_code(tokens[0])
    if love_code_number is not None:
        if len(tokens) != 2 or not tokens[1].isdigit():
            await message.reply(
                "❤️ Usage: <code>/check L-code level</code>\nExample: <code>/check L1 3</code>",
                parse_mode="HTML",
            )
            return
        level = int(tokens[1])
        if not 1 <= level <= LOVE_MAX_LEVEL:
            await message.reply(f"❌ Love image level must be from 1 to {LOVE_MAX_LEVEL}.")
            return
        love_card = await get_love_card_by_code(love_code_number)
        if not love_card:
            await message.reply(f"❌ Love code <code>L{love_code_number}</code> was not found.", parse_mode="HTML")
            return
        await _send_love_level_check(message, love_card, level)
        return

    if len(tokens) > MAX_CHECK_CHARACTERS:
        await message.reply(
            f"❌ You can check up to {MAX_CHECK_CHARACTERS} characters at once.",
            parse_mode="HTML",
        )
        return
    if not all(token.isdigit() and int(token) > 0 for token in tokens):
        await message.reply(
            "❌ Every character ID must be a positive number, or use <code>/check L1 1</code> for Love art.",
            parse_mode="HTML",
        )
        return

    requested_ids = [int(token) for token in tokens]
    if len(requested_ids) == 1:
        await _send_original_single_check(message, requested_ids[0])
        return

    characters_by_id = await get_characters_by_ids(requested_ids)
    missing = [
        str(character_id)
        for character_id in dict.fromkeys(requested_ids)
        if character_id not in characters_by_id
    ]
    if missing:
        await message.reply(
            "❌ No character was found for ID: <code>" + ", ".join(missing) + "</code>.",
            parse_mode="HTML",
        )
        return

    characters = [characters_by_id[character_id] for character_id in requested_ids]
    # Multi-ID checks intentionally retain compact CHECK CARDs. When every
    # result is a photo, one album carries the requested compact cards in its
    # shared caption; mixed/video results fall back to individual compact cards.
    if all(
        character.get("image_url") and not character_is_video(character)
        for character in characters
    ):
        album_caption = _batch_check_caption(characters)
        media = [
            InputMediaPhoto(
                media=characters[0]["image_url"],
                caption=album_caption,
                parse_mode="HTML",
            ),
            *[
                InputMediaPhoto(media=character["image_url"])
                for character in characters[1:]
            ],
        ]
        try:
            await message.bot.send_media_group(chat_id=message.chat.id, media=media)
            return
        except Exception:
            pass

    for character in characters:
        await _send_multi_fallback_card(message, character)
