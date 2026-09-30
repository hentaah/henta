"""/rarity — show the ranked portion of a user's harem by rarity."""
from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from bot.db.pool import get_pool
from bot.services.rich_renderer import rarity_symbol

router = Router()

RARITIES = ["OverLord", "Suspicious", "Eternal", "OP", "Sovereign", "Mythical", "Legendary", "Rare", "Common"]
BAR_FULL = "●"
BAR_EMPTY = "○"
BAR_LEN = 9


def _bar(owned: int, total: int) -> str:
    if total == 0:
        return BAR_EMPTY * BAR_LEN
    filled = round((owned / total) * BAR_LEN)
    return BAR_FULL * filled + BAR_EMPTY * (BAR_LEN - filled)


@router.message(Command("rarity"))
async def cmd_rarity(message: Message):
    user = message.from_user
    if not user:
        return

    pool = get_pool()
    async with pool.acquire() as conn:
        banned = await conn.fetchval(
            "SELECT is_banned FROM users WHERE user_id = $1", user.id
        )
        if banned:
            return

        owned_rows = await conn.fetch(
            """
            SELECT c.rarity, COUNT(DISTINCT c.id) AS owned
            FROM harem h
            JOIN characters c ON c.id = h.character_id
            WHERE h.user_id = $1 AND h.is_ranked = TRUE
              AND LOWER(BTRIM(c.rarity)) <> 'love'
            GROUP BY c.rarity
            """,
            user.id,
        )
        total_rows = await conn.fetch(
            """
            SELECT rarity, COUNT(*) AS total
            FROM characters
            WHERE LOWER(BTRIM(rarity)) <> 'love'
            GROUP BY rarity
            """,
        )

    owned_map = {r["rarity"]: r["owned"] for r in owned_rows}
    total_map = {r["rarity"]: r["total"] for r in total_rows}

    total_owned = sum(owned_map.values())
    total_all = sum(total_map.values())
    overall_pct = round((total_owned / total_all) * 100, 1) if total_all else 0

    lines = [f"✦ {user.full_name}'s 𝗥𝗮𝗿𝗶𝘁𝘆 𝗖𝗵𝗮𝗿𝘁 ✦", ""]

    for rarity in RARITIES:
        sym = rarity_symbol(rarity)
        owned = owned_map.get(rarity, 0)
        total = total_map.get(rarity, 0)
        if total == 0:
            continue
        pct = round((owned / total) * 100) if total else 0
        bar = _bar(owned, total)
        lines.append(f"{sym} {rarity}")
        lines.append(f"{bar} {owned}/{total} ({pct}%)")
        lines.append("")

    lines.append("────────────────────────")
    lines.append(f"📈 Overall: {total_owned}/{total_all} ({overall_pct}%)")

    await message.reply("\n".join(lines))
