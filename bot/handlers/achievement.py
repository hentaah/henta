"""/achievement — global rarity and anime/game collection milestones."""
from __future__ import annotations

from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.services.achievements import (
    AchievementSnapshot,
    AnimeAchievementProgress,
    RARITY_REWARD_TARGETS,
    RarityAchievementProgress,
    canonical_collector_rarity,
    claim_rarity_achievement_reward,
    get_achievement_snapshot,
    get_rarity_reward_status,
)
from bot.services.rich_renderer import anime_emoji, rarity_symbol


router = Router()
ANIME_PAGE_SIZE = 8
BAR_LENGTH = 10
RARITY_COLLECTOR_EMOJIS = {
    "Common": "🍀",
    "Rare": "💎",
    "Legendary": "🔥",
    "Mythical": "🌌",
    "Sovereign": "👑",
    "OP": "⚡",
}


class AchievementRewardClaim(StatesGroup):
    waiting_for_character_code = State()


def _progress_bar(owned: int, total: int) -> tuple[str, int]:
    """Return a compact filled/empty bar and integer completion percentage."""
    if total <= 0:
        return "░" * BAR_LENGTH, 0
    percentage = min(100, round((owned * 100) / total))
    filled = min(BAR_LENGTH, round((owned * BAR_LENGTH) / total))
    return ("▰" * filled) + ("▱" * (BAR_LENGTH - filled)), percentage


def _rarity_status_text(progress: RarityAchievementProgress, *, private: bool) -> str:
    """Describe one rarity milestone without exposing a claim button here."""
    if progress.total <= 0:
        return "⚪ No catalog cards yet"
    if not progress.unlocked:
        return "🔒 In progress"

    target = RARITY_REWARD_TARGETS.get(progress.rarity)
    if target is None:
        if private:
            return (
                "🏆 <b>Unlocked!</b> Congratulations — contact <b>@ItsShaah</b> "
                "or <b>@YeBivaffa</b> privately for your Eternal reward."
            )
        return "🏆 <b>Unlocked!</b> Open /achievement in private for Eternal reward instructions."
    if progress.reward_claimed:
        return f"🏆 <b>Unlocked!</b> 🎁 {escape(target)} reward claimed"
    if private:
        return f"🏆 <b>Unlocked!</b> 🎁 Choose one {escape(target)} reward card below"
    return f"🏆 <b>Unlocked!</b> 🎁 Claim one {escape(target)} reward in private"


def _rarity_panel_text(
    *,
    player_name: str,
    snapshot: AchievementSnapshot,
    private: bool,
) -> str:
    lines = [
        f"<b>🏆 {escape(player_name)}'s Collector Achievements</b>",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "Complete every unique card in a rarity to unlock its Collector achievement.",
        "<i>Progress is global and uses ranked cards, exactly like /rarity.</i>",
        "",
    ]
    for progress in snapshot.rarity_progress:
        bar, percentage = _progress_bar(progress.owned, progress.total)
        lines.extend([
            f"<b>{RARITY_COLLECTOR_EMOJIS[progress.rarity]} "
            f"{rarity_symbol(progress.rarity)} {escape(progress.rarity)} Collector</b>",
            f"<code>{bar}</code> <b>{progress.owned}/{progress.total}</b> ({percentage}%)",
            _rarity_status_text(progress, private=private),
            "",
        ])
    lines.append(f"<i>Unlocked achievements: {snapshot.unlocked_count}</i>")
    return "\n".join(lines)


def _anime_status_text(progress: AnimeAchievementProgress) -> str:
    if progress.unlocked:
        return f"🏆 <b>{escape(progress.anime)} Collector unlocked!</b> Congratulations!"
    return "🔒 In progress — complete this title to unlock its Collector achievement"


def _anime_panel_text(
    *,
    player_name: str,
    snapshot: AchievementSnapshot,
    page: int,
) -> tuple[str, int, int]:
    anime_progress = snapshot.anime_progress
    total_pages = max(1, (len(anime_progress) + ANIME_PAGE_SIZE - 1) // ANIME_PAGE_SIZE)
    page = max(1, min(page, total_pages))
    start = (page - 1) * ANIME_PAGE_SIZE
    visible = anime_progress[start:start + ANIME_PAGE_SIZE]

    lines = [
        f"<b>🎬 {escape(player_name)}'s Anime/Game Collectors</b>",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "Only titles where you own at least one ranked card are listed.",
        "Anime Collector achievements award congratulations only — no character reward.",
        "",
    ]
    if not visible:
        lines.append("You do not own a ranked character from any anime/game yet.")
    else:
        for progress in visible:
            bar, percentage = _progress_bar(progress.owned, progress.total)
            lines.extend([
                f"<b>{anime_emoji(progress.anime)} {escape(progress.anime)} Collector</b>",
                f"<code>{bar}</code> <b>{progress.owned}/{progress.total}</b> ({percentage}%)",
                _anime_status_text(progress),
                "",
            ])
    lines.append(f"<i>Page {page}/{total_pages} • Unlocked achievements: {snapshot.unlocked_count}</i>")
    return "\n".join(lines), page, total_pages


def _rarity_keyboard(
    *,
    user_id: int,
    snapshot: AchievementSnapshot,
    private: bool,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    progress_by_rarity = {item.rarity: item for item in snapshot.rarity_progress}
    claimable_count = 0
    for source, target in RARITY_REWARD_TARGETS.items():
        source_progress = progress_by_rarity[source]
        target_progress = progress_by_rarity[target]
        if (
            source_progress.unlocked
            and not source_progress.reward_claimed
            and target_progress.total > 0
        ):
            claimable_count += 1
            if private:
                rows.append([
                    InlineKeyboardButton(
                        text=f"🎁 Claim a {target} card",
                        callback_data=f"achievement:claim:{user_id}:{source}",
                    ),
                ])

    if claimable_count and not private:
        rows.append([
            InlineKeyboardButton(
                text="🔒 Claim rewards in private",
                callback_data=f"achievement:private:{user_id}",
            ),
        ])
    rows.append([
        InlineKeyboardButton(
            text="🎬 Anime/Game Collectors",
            callback_data=f"achievement:anime:{user_id}:1",
        ),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _anime_keyboard(*, user_id: int, page: int, total_pages: int) -> InlineKeyboardMarkup:
    navigation: list[InlineKeyboardButton] = []
    if page > 1:
        navigation.append(
            InlineKeyboardButton(
                text="⬅️ Previous",
                callback_data=f"achievement:anime:{user_id}:{page - 1}",
            )
        )
    navigation.append(
        InlineKeyboardButton(text=f"{page}/{total_pages}", callback_data="achievement:noop")
    )
    if page < total_pages:
        navigation.append(
            InlineKeyboardButton(
                text="Next ➡️",
                callback_data=f"achievement:anime:{user_id}:{page + 1}",
            )
        )
    return InlineKeyboardMarkup(inline_keyboard=[
        navigation,
        [
            InlineKeyboardButton(
                text="🏆 Rarity Collectors",
                callback_data=f"achievement:rarity:{user_id}",
            ),
        ],
    ])


async def _send_rarity_panel(message: Message, *, user_id: int, player_name: str) -> None:
    snapshot = await get_achievement_snapshot(user_id)
    private = message.chat.type == "private"
    await message.answer(
        _rarity_panel_text(player_name=player_name, snapshot=snapshot, private=private),
        parse_mode="HTML",
        reply_markup=_rarity_keyboard(user_id=user_id, snapshot=snapshot, private=private),
        disable_web_page_preview=True,
    )


async def _edit_rarity_panel(
    callback: CallbackQuery,
    *,
    user_id: int,
    player_name: str,
) -> None:
    if not callback.message:
        return
    snapshot = await get_achievement_snapshot(user_id)
    private = callback.message.chat.type == "private"
    text = _rarity_panel_text(player_name=player_name, snapshot=snapshot, private=private)
    keyboard = _rarity_keyboard(user_id=user_id, snapshot=snapshot, private=private)
    try:
        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    except Exception:
        await callback.message.answer(
            text,
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )


async def _edit_anime_panel(
    callback: CallbackQuery,
    *,
    user_id: int,
    player_name: str,
    page: int,
) -> None:
    if not callback.message:
        return
    snapshot = await get_achievement_snapshot(user_id)
    text, page, total_pages = _anime_panel_text(
        player_name=player_name,
        snapshot=snapshot,
        page=page,
    )
    keyboard = _anime_keyboard(user_id=user_id, page=page, total_pages=total_pages)
    try:
        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    except Exception:
        await callback.message.answer(
            text,
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )


@router.message(Command("achievement"))
async def cmd_achievement(message: Message, state: FSMContext):
    """Show a player's persistent rarity Collector achievements."""
    if not message.from_user:
        return
    # Running /achievement again abandons only this feature's private
    # reward-code prompt; it must never wipe an owner's unrelated catalog FSM.
    if await state.get_state() == AchievementRewardClaim.waiting_for_character_code.state:
        await state.clear()
    await _send_rarity_panel(
        message,
        user_id=message.from_user.id,
        player_name=message.from_user.full_name or "Collector",
    )


@router.callback_query(F.data.startswith("achievement:"))
async def cb_achievement(callback: CallbackQuery, state: FSMContext):
    """Navigate only the achievement panel that belongs to the clicking user."""
    if not callback.from_user:
        return
    parts = (callback.data or "").split(":")
    if len(parts) == 2 and parts[1] == "noop":
        await callback.answer()
        return
    if len(parts) < 3 or not parts[2].isdigit():
        await callback.answer("Invalid achievement action.", show_alert=True)
        return

    action = parts[1]
    panel_user_id = int(parts[2])
    if callback.from_user.id != panel_user_id:
        await callback.answer("This achievement panel belongs to another player.", show_alert=True)
        return
    player_name = callback.from_user.full_name or "Collector"

    if action == "rarity" and len(parts) == 3:
        await _edit_rarity_panel(
            callback,
            user_id=panel_user_id,
            player_name=player_name,
        )
        await callback.answer()
        return

    if action == "anime" and len(parts) == 4 and parts[3].isdigit():
        await _edit_anime_panel(
            callback,
            user_id=panel_user_id,
            player_name=player_name,
            page=max(1, int(parts[3])),
        )
        await callback.answer()
        return

    if action == "private" and len(parts) == 3:
        await callback.answer(
            "Open a private chat with the bot and use /achievement to claim your reward.",
            show_alert=True,
        )
        return

    if action == "claim" and len(parts) == 4:
        source = canonical_collector_rarity(parts[3])
        if source is None:
            await callback.answer("Invalid rarity reward.", show_alert=True)
            return
        if not callback.message or callback.message.chat.type != "private":
            await callback.answer(
                "Open a private chat with the bot and use /achievement to claim this reward.",
                show_alert=True,
            )
            return
        status = await get_rarity_reward_status(panel_user_id, source)
        if not status.unlocked:
            await callback.answer("This Collector achievement is not unlocked.", show_alert=True)
            return
        if status.already_claimed:
            await callback.answer("This Collector reward was already claimed.", show_alert=True)
            return
        if not status.claimable or not status.target_rarity:
            await callback.answer("No selectable catalog reward is available for this tier.", show_alert=True)
            return

        await state.set_state(AchievementRewardClaim.waiting_for_character_code)
        await state.update_data(source_rarity=source)
        await callback.message.answer(
            "🎁 <b>Collector reward ready!</b>\n\n"
            f"Send the numeric character code for the <b>{escape(status.target_rarity)}</b> "
            "card you want. Only that exact rarity will be accepted.\n\n"
            "Send <code>cancel</code> to stop, or use <code>/achievement</code> to return.",
            parse_mode="HTML",
        )
        await callback.answer("Send the character code")
        return

    await callback.answer("Invalid achievement action.", show_alert=True)


@router.message(AchievementRewardClaim.waiting_for_character_code, F.chat.type == "private", F.text)
async def receive_achievement_reward_code(message: Message, state: FSMContext):
    """Validate the user's chosen code against the exact unlocked reward tier."""
    if not message.from_user:
        return
    raw_code = (message.text or "").strip()
    if raw_code.casefold() == "cancel":
        await state.clear()
        await message.answer("❌ Collector reward selection cancelled.", parse_mode="HTML")
        return
    # Card captions often show IDs as #123; accept that display form as well
    # as the bare numeric code requested in the prompt.
    character_code = raw_code[1:].strip() if raw_code.startswith("#") else raw_code
    if not character_code.isdigit() or int(character_code) <= 0:
        await message.answer(
            "❌ Send a numeric character code, or send <code>cancel</code> to stop.",
            parse_mode="HTML",
        )
        return

    state_data = await state.get_data()
    source = canonical_collector_rarity(state_data.get("source_rarity"))
    if source is None:
        await state.clear()
        await message.answer(
            "❌ This reward prompt has expired. Use <code>/achievement</code> again.",
            parse_mode="HTML",
        )
        return

    result = await claim_rarity_achievement_reward(
        user_id=message.from_user.id,
        username=message.from_user.username or "",
        full_name=message.from_user.full_name or "",
        private_chat_id=message.chat.id,
        source_rarity=source,
        character_id=int(character_code),
    )
    if result.success:
        await state.clear()
        await message.answer(
            "🎉 <b>Reward claimed!</b>\n\n"
            f"<b>{escape(result.name)}</b> from <b>{escape(result.anime)}</b> "
            f"({rarity_symbol(result.rarity)} {escape(result.rarity)} • "
            f"<code>#{result.character_id}</code>) was added to your collection.",
            parse_mode="HTML",
        )
        await _send_rarity_panel(
            message,
            user_id=message.from_user.id,
            player_name=message.from_user.full_name or "Collector",
        )
        return

    if result.reason == "wrong_rarity":
        await message.answer(
            "❌ Character <code>#{}</code> is <b>{}</b>. "
            "You must choose a <b>{}</b> character for this reward."
            .format(
                result.character_id,
                escape(result.actual_rarity or "Unknown"),
                escape(result.required_rarity),
            ),
            parse_mode="HTML",
        )
        return
    if result.reason == "not_found":
        await message.answer(
            "❌ No catalog character exists with that code. Send a valid <b>{}</b> code."
            .format(escape(result.required_rarity or "reward")),
            parse_mode="HTML",
        )
        return

    await state.clear()
    if result.reason == "already_claimed":
        text = "ℹ️ This Collector reward was already claimed."
    else:
        text = "❌ This Collector reward is no longer available. Use <code>/achievement</code> again."
    await message.answer(text, parse_mode="HTML")
