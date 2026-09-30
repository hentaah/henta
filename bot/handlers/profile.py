"""/profile — visual global collection profile with favorite-image priority."""
from __future__ import annotations

from html import escape

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from bot.services.game import (
    get_favorite_owned_media,
    get_profile_data,
    get_random_owned_media,
)
from bot.services.moderation import format_remaining_time, get_spam_ban_remaining
from bot.services.rich_renderer import rarity_label, rarity_symbol
from bot.services.catalog_media import answer_catalog_media

router = Router()

RARITY_ORDER = [
    "OverLord",
    "Suspicious",
    "Eternal",
    "OP",
    "Sovereign",
    "Mythical",
    "Legendary",
    "Rare",
    "Common",
]


def _two_digits(value: int) -> str:
    """Render counts like 05, while naturally allowing totals above 99."""
    return f"{value:02d}"


def _rank_label(rank: int | None) -> str:
    return f"#{rank}" if rank is not None else "—"


def _profile_caption(display_name: str, user_id: int, profile: dict) -> str:
    owned = profile["unique_owned"]
    total = profile["total_characters"]
    percent = (owned / total * 100) if total else 0.0

    lines = [
        "╭── ✦ 𝗖𝗔𝗧𝗖𝗛𝗘𝗥 𝗣𝗥𝗢𝗙𝗜𝗟𝗘 ✦",
        "│",
        f"├ 👤 𝚄𝚜𝚎𝚛 : {escape(display_name)}",
        "│",
        f"├ 🆔 𝙸𝙳 : <code>{user_id}</code>",
        "│",
        f"╰ 📁 𝚃𝚘𝚝𝚊𝚕 : {owned}/{total} [{percent:.1f}%]",
        "",
        "",
        "╭── ✦ 𝚁𝙰𝚁𝙸𝚃𝚈 𝙱𝚁𝙴𝙰𝙺𝙳𝙾𝚆𝙽 ✦",
        "│",
    ]

    for index, rarity in enumerate(RARITY_ORDER):
        values = profile["rarities"].get(rarity, {"owned": 0, "total": 0})
        is_last = index == len(RARITY_ORDER) - 1
        branch = "╰" if is_last else "├"
        value_prefix = "└" if is_last else "│ └"
        lines.append(f"{branch} {rarity_symbol(rarity)} {rarity_label(rarity)}")
        lines.append(
            f"{value_prefix} {_two_digits(values['owned'])}/{_two_digits(values['total'])}"
        )
        if not is_last:
            lines.append("│")

    lines.extend([
        "",
        "╭── ✦ 𝚁𝙰𝙽𝙺𝙸𝙽𝙶 ✦",
        "│",
        f"├ 🌍 𝙶𝚕𝚘𝚋𝚊𝚕 : {_rank_label(profile['global_rank'])}",
        "│",
        f"╰ 💬 𝙲𝚑𝚊𝚝 : {_rank_label(profile['chat_rank'])}",
    ])
    return "\n".join(lines)


@router.message(Command("profile"))
async def cmd_profile(message: Message):
    user = message.from_user
    if not user:
        return

    chat_id = None if message.chat.type == "private" else message.chat.id
    profile = await get_profile_data(user.id, chat_id)
    display_name = user.full_name or user.username or f"User {user.id}"
    caption = _profile_caption(display_name, user.id, profile)

    remaining = await get_spam_ban_remaining(user.id)
    if remaining:
        caption += (
            "\n\n╭── ⛔ 𝗧𝗘𝗠𝗣𝗢𝗥𝗔𝗥𝗬 𝗕𝗔𝗡\n"
            f"╰ ⏳ Time remaining: <b>{format_remaining_time(remaining)}</b>"
        )

    # A selected favorite always wins. Random collection art is used only if
    # the user does not have any favorite, matching /harem behavior.
    # Love is intentionally visible in Harem but never changes the profile,
    # including its showcased/random profile artwork.
    media = await get_favorite_owned_media(user.id, include_love=False)
    if media is None:
        media = await get_random_owned_media(user.id, include_love=False)

    if media:
        try:
            await answer_catalog_media(message, character=media, caption=caption)
            return
        except Exception:
            pass
    await message.answer(caption, parse_mode="HTML")
