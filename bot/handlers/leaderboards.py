"""/topusers /ctop /catchtop /topgroups — leaderboards.

Most leaderboard commands retain their direct HTML tables.  /ctop renders
its group ranking as a Pillow-generated Telegram photo instead.
"""
from __future__ import annotations

from html import escape

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message

from bot.config import HENTACATCHER_GP_CHAT_ID
from bot.services.permissions import is_bot_owner
from bot.services.game import (
    get_leaderboard_global,
    get_leaderboard_chat,
    get_leaderboard_groups,
    get_recent_catch_leaderboard,
)
from bot.services.leaderboard_image import create_leaderboard_image
from bot.services.rich_renderer import render_leaderboard_table, blocks_to_html

router = Router()


@router.message(Command("topusers"))
async def cmd_topusers(message: Message):
    top = await get_leaderboard_global(limit=10)
    if not top:
        await message.answer("📭 No collectors yet. Be the first!", parse_mode="HTML")
        return
    rows = [
        [f"{i+1}", u["full_name"][:20], str(u["total_claims"]), u["highest_rarity"] or "—"]
        for i, u in enumerate(top)
    ]
    blocks = render_leaderboard_table(
        title="🏆 Global Top Collectors",
        headers=["#", "User", "Claims", "Best Rarity"],
        rows=rows,
    )
    await message.answer(blocks_to_html(blocks), parse_mode="HTML")


@router.message(Command("ctop"))
async def cmd_ctop(message: Message):
    if message.chat.type == "private":
        await message.answer("ℹ️ /ctop only works in groups.", parse_mode="HTML")
        return
    top = await get_leaderboard_chat(message.chat.id, limit=10)
    if not top:
        await message.answer("📭 No collectors in this group yet.", parse_mode="HTML")
        return
    rows = [
        [f"{i+1}", u["full_name"][:20], str(u["claims"])]
        for i, u in enumerate(top)
    ]
    blocks = render_leaderboard_table(
        title="🏅 This Group's Top Collectors",
        headers=["#", "User", "Claims"],
        rows=rows,
    )
    image_bytes = create_leaderboard_image(top)
    await message.answer_photo(
        photo=BufferedInputFile(image_bytes, filename="ctop_leaderboard.png"),
        caption=blocks_to_html(blocks),
        parse_mode="HTML",
    )


def _catchtop_window_days(message: Message) -> int | None:
    """Accept exactly `/catchtop 1` or `/catchtop 7`, never a loose duration."""
    parts = (message.text or "").strip().split()
    if len(parts) != 2 or parts[1] not in {"1", "7"}:
        return None
    return int(parts[1])


def _catchtop_name(row: dict) -> str:
    """Escape a compact current player label for the HTML table fallback."""
    raw_name = " ".join(str(row.get("display_name") or "Player").split())
    return escape((raw_name or "Player")[:24])


@router.message(Command("catchtop"))
async def cmd_catchtop(message: Message):
    """Full Owner: show up to three recent exact-catch leaders from Main Group."""
    actor = message.from_user
    if not actor or not await is_bot_owner(actor.id):
        return

    window_days = _catchtop_window_days(message)
    if window_days is None:
        await message.answer(
            "❌ Usage: <code>/catchtop 1</code> or <code>/catchtop 7</code>\n"
            "This leaderboard counts successful <b>Main Group</b> catches only.",
            parse_mode="HTML",
        )
        return

    leaders = await get_recent_catch_leaderboard(
        chat_id=HENTACATCHER_GP_CHAT_ID,
        window_days=window_days,
        limit=3,
    )
    period_label = "the last 24 hours" if window_days == 1 else "the last 7 days"
    if not leaders:
        await message.answer(
            "📭 No successful Main Group catches were recorded during "
            f"<b>{period_label}</b>.",
            parse_mode="HTML",
        )
        return

    rows = [
        [str(position), _catchtop_name(leader), str(int(leader["catches"]))]
        for position, leader in enumerate(leaders, start=1)
    ]
    title = (
        "🏆 Main Group Catch Top — Last 24 Hours"
        if window_days == 1
        else "🏆 Main Group Catch Top — Last 7 Days"
    )
    blocks = render_leaderboard_table(
        title=title,
        headers=["#", "Player", "Catches"],
        rows=rows,
    )
    await message.answer(blocks_to_html(blocks), parse_mode="HTML")


@router.message(Command("topgroups"))
async def cmd_topgroups(message: Message):
    top = await get_leaderboard_groups(limit=10)
    if not top:
        await message.answer("📭 No groups registered yet.", parse_mode="HTML")
        return
    rows = [
        [f"{i+1}", (g["title"] or g["username"] or "—")[:25],
         str(g["claims"]), str(g["spawns"])]
        for i, g in enumerate(top)
    ]
    blocks = render_leaderboard_table(
        title="🌐 Global Top Groups",
        headers=["#", "Group", "Claims", "Spawns"],
        rows=rows,
    )
    await message.answer(blocks_to_html(blocks), parse_mode="HTML")
