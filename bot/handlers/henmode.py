"""/henmode — save the Anime/Rarity layout used later by /harem."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.services.game import (
    get_harem_display_preference,
    set_harem_display_anime,
    set_harem_display_rarity,
)
from bot.handlers.common import dot_player_command_filter
from bot.services.rich_renderer import rarity_label, rarity_symbol

router = Router()

RARITY_ORDER = [
    "Love",
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


def _mode_keyboard(user_id: int, mode: str, tier: str | None = None) -> InlineKeyboardMarkup:
    """The two requested inline choices; this panel saves a setting only."""
    anime_active = mode == "anime"
    rarity_text = "🔱 Rarity" if not tier else f"{rarity_symbol(tier)} {rarity_label(tier)}"
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="📺 Anime ✓" if anime_active else "📺 Anime",
            callback_data=f"hen:anime:{user_id}",
            style="success" if anime_active else "primary",
        ),
        InlineKeyboardButton(
            text=(rarity_text + " ✓") if not anime_active else rarity_text,
            callback_data=f"hen:rarities:{user_id}",
            style="success" if not anime_active else "primary",
        ),
    ]])


def _rarity_picker_keyboard(user_id: int) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(
            text=f"{rarity_symbol(tier)} {rarity_label(tier)}",
            callback_data=f"hen:tier:{tier}:{user_id}",
            style="primary",
        )
        for tier in RARITY_ORDER
    ]
    rows = [buttons[index:index + 2] for index in range(0, len(buttons), 2)]
    rows.append([
        InlineKeyboardButton(
            text="⬅️ Back to modes",
            callback_data=f"hen:back:{user_id}",
            style="primary",
        )
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _mode_text(mode: str, tier: str | None = None, *, saved: bool = False) -> str:
    if mode == "rarity" and tier:
        current = f"{rarity_symbol(tier)} <b>{rarity_label(tier)}</b>"
        detail = "<code>/harem</code> will show only characters from this rarity tier."
    else:
        current = "📺 <b>Anime</b>"
        detail = "<code>/harem</code> will group your characters by anime."
    prefix = "✅ <b>Harem mode saved</b>\n\n" if saved else "<b>⚙️ Harem Display Mode</b>\n\n"
    return prefix + f"Current mode: {current}\n{detail}\n\nChoose a mode below."


async def _edit_panel(callback: CallbackQuery, text: str,
                      keyboard: InlineKeyboardMarkup) -> None:
    if not callback.message:
        return
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    except Exception:
        pass


async def _ensure_panel_owner(callback: CallbackQuery, user_id_text: str) -> bool:
    if not user_id_text.isdigit() or callback.from_user.id != int(user_id_text):
        await callback.answer("⛔ This mode panel is not for you.", show_alert=True)
        return False
    return True


@router.message(Command("henmode"))
@router.message(dot_player_command_filter("henmode"))
async def cmd_henmode(message: Message):
    """Open settings only — harem cards remain exclusively in /harem."""
    user = message.from_user
    if not user:
        return
    mode, tier = await get_harem_display_preference(user.id)
    await message.answer(
        _mode_text(mode, tier),
        parse_mode="HTML",
        reply_markup=_mode_keyboard(user.id, mode, tier),
    )


@router.callback_query(F.data.startswith("hen:"))
async def cb_henmode(callback: CallbackQuery):
    if not callback.message:
        await callback.answer("This panel is no longer available.", show_alert=True)
        return

    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    user_id = callback.from_user.id

    if action == "anime" and len(parts) == 3:
        if not await _ensure_panel_owner(callback, parts[2]):
            return
        await set_harem_display_anime(user_id)
        await _edit_panel(
            callback,
            _mode_text("anime", saved=True),
            _mode_keyboard(user_id, "anime"),
        )
        await callback.answer("Anime mode saved")
        return

    if action == "rarities" and len(parts) == 3:
        if not await _ensure_panel_owner(callback, parts[2]):
            return
        await _edit_panel(
            callback,
            "<b>🔱 Select a rarity tier</b>\n\n"
            "After saving, <code>/harem</code> will show only that tier.",
            _rarity_picker_keyboard(user_id),
        )
        await callback.answer()
        return

    if action == "tier" and len(parts) == 4:
        tier = parts[2]
        if tier not in RARITY_ORDER:
            await callback.answer("Invalid rarity tier.", show_alert=True)
            return
        if not await _ensure_panel_owner(callback, parts[3]):
            return
        if not await set_harem_display_rarity(user_id, tier):
            await callback.answer("Couldn't save this rarity tier.", show_alert=True)
            return
        await _edit_panel(
            callback,
            _mode_text("rarity", tier, saved=True),
            _mode_keyboard(user_id, "rarity", tier),
        )
        await callback.answer(f"{tier} mode saved")
        return

    if action == "back" and len(parts) == 3:
        if not await _ensure_panel_owner(callback, parts[2]):
            return
        mode, tier = await get_harem_display_preference(user_id)
        await _edit_panel(callback, _mode_text(mode, tier), _mode_keyboard(user_id, mode, tier))
        await callback.answer()
        return

    await callback.answer("Invalid harem mode action.", show_alert=True)
