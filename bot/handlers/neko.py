"""Member decimal Neko wallet/Casino commands and owner wallet controls."""
from __future__ import annotations

import logging
import re
from datetime import timezone
from decimal import Decimal
from html import escape

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.services.neko import (
    CASINO_CURRENT_PAYOUT_VERSION,
    CASINO_MAX_STAKE,
    CASINO_MIN_STAKE,
    CASINO_PAYOUT_VERSION_BALANCED,
    CASINO_PAYOUT_VERSION_GRADUAL,
    CASINO_GRID_COLUMNS,
    CASINO_GRID_SIZE,
    CASINO_MAX_SAFE_PICKS,
    CASINO_SAFE_PICK_INCREMENT,
    DAILY_NEKO_REWARD_INTERVAL_SECONDS,
    MAX_NEKO_PASS_DAYS,
    adjust_neko_wallet,
    cancel_neko_transfer,
    casino_multiplier,
    casino_payout,
    cash_out_neko_casino,
    claim_daily_neko_reward,
    clear_neko_casino_input_session,
    confirm_neko_transfer,
    create_neko_casino_game,
    create_neko_dice_bet,
    create_neko_transfer,
    format_neko_amount,
    get_latest_neko_wallet_backup,
    get_neko_casino_game_audit,
    get_neko_casino_input_session,
    get_neko_wallet,
    grant_neko_pass,
    parse_casino_stake,
    reset_all_neko_wallets,
    reset_neko_wallet,
    restore_neko_wallet_backup,
    reveal_neko_casino_tile,
    select_neko_dice_bet,
    settle_neko_dice_bet,
    start_neko_casino_input_session,
    valid_neko_amount,
    valid_neko_pass_days,
)
from bot.handlers.common import dot_player_command_filter, non_command_reply_text_filter
from bot.middlewares.membership import ensure_required_channel_membership
from bot.services.permissions import is_bot_owner
router = Router()
log = logging.getLogger(__name__)
DAILY_NEKO_REWARD_HOURS = DAILY_NEKO_REWARD_INTERVAL_SECONDS // 60 // 60
# Fixed Telegram-hosted wallet artwork. The wallet card is always its caption,
# so users receive one combined media message rather than a separate text card.
NEKOWALLET_IMAGE_FILE_ID = "AgACAgQAAyEFAAT_2IEuAAICGmqlp3XurPUzsFJxQQKfbEf_GdoCAAJmDmsb59wwUUjz3yWhaqVuAQADAgADeQADPQQ"


def _display_name(value: str | None, fallback_user_id: int) -> str:
    """Keep Telegram display names to one escaped, card-safe line."""
    return " ".join((value or "").split()) or f"User {fallback_user_id}"


def _neko(value: object) -> str:
    """Render stored Decimal currency without discarding casino cents."""
    return format_neko_amount(value)


def _wallet_card(wallet: dict) -> str:
    """Render the requested compact, database-backed Neko balance card."""
    user_id = int(wallet["user_id"])
    name = escape(_display_name(str(wallet.get("full_name") or ""), user_id))
    pass_state = "Active" if wallet.get("pass_active") else "Inactive"
    return (
        "╓ 🐾 Neko Balance\n\n"
        "║ ────────────────\n\n"
        f"╟ 👤 User: {name}\n\n"
        f"╟ 🆔 ID: {user_id}\n\n"
        f"╟ 💵 Balance: {_neko(wallet['balance'])} Neko\n\n"
        f"╟ ⭐ Global Rank: #{int(wallet['rank'])}\n\n"
        "║\n\n"
        f"╙ 🌟 Legacy Neko Pass record: {pass_state}"
    )


def _format_remaining(seconds: int) -> str:
    """Render a short English rolling-window duration without a fake reset time."""
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 60 * 60)
    minutes, seconds = divmod(remainder, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    if seconds and not hours:
        parts.append(f"{seconds} second{'s' if seconds != 1 else ''}")
    return " and ".join(parts) or "less than a minute"


def _positive_command_number(message: Message) -> Decimal | None:
    """Accept one positive Neko value with no more than two decimal places."""
    parts = (message.text or "").strip().split()
    if len(parts) != 2 or not re.fullmatch(r"[0-9]{1,19}(?:\.[0-9]{1,2})?", parts[1]):
        return None
    candidate = Decimal(parts[1])
    return candidate if valid_neko_amount(candidate) else None


def _replied_human_target(message: Message):
    """Resolve only a replied human target, matching /haremownercheck-style tools."""
    reply = message.reply_to_message
    target = reply.from_user if reply else None
    if not target or getattr(target, "is_bot", False):
        return None
    return target


async def _edit_confirmation_card(callback: CallbackQuery, text: str) -> None:
    """Clear a text confirmation keyboard, safely handling Telegram edit forms."""
    if not callback.message:
        return
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=None)
    except Exception:
        try:
            await callback.message.edit_caption(caption=text, parse_mode="HTML", reply_markup=None)
        except Exception:
            pass


def _transfer_keyboard(sender_id: int, transfer_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="✅ Confirm",
            callback_data=f"nekogive:confirm:{sender_id}:{transfer_id}",
            style="success",
        ),
        InlineKeyboardButton(
            text="❌ Cancel",
            callback_data=f"nekogive:cancel:{sender_id}:{transfer_id}",
            style="danger",
        ),
    ]])


def _owner_wallet_keyboard(
    *,
    action: str,
    owner_id: int,
    object_id: int,
) -> InlineKeyboardMarkup:
    """Bind destructive owner wallet buttons to the owner who opened them."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="✅ Confirm",
            callback_data=f"ownerneko:{action}:confirm:{owner_id}:{object_id}",
            style="success",
        ),
        InlineKeyboardButton(
            text="❌ Cancel",
            callback_data=f"ownerneko:{action}:cancel:{owner_id}:{object_id}",
            style="danger",
        ),
    ]])



def _reset_all_neko_keyboard(owner_id: int) -> InlineKeyboardMarkup:
    """Bind the irreversible all-wallet reset to its initiating full owner."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="⚠️ Reset all Neko",
            callback_data=f"resetallneko:confirm:{owner_id}",
            style="danger",
        ),
        InlineKeyboardButton(
            text="❌ Cancel",
            callback_data=f"resetallneko:cancel:{owner_id}",
            style="success",
        ),
    ]])


# ─── Neko Mines /casino UI ─────────────────────────────────────────────────

def _casino_rules(game: dict) -> tuple[int, int, int, Decimal, int]:
    """Read the stored geometry and payout schedule for a durable game."""
    columns = int(game.get("grid_columns") or CASINO_GRID_COLUMNS)
    grid_size = int(game.get("grid_size") or CASINO_GRID_SIZE)
    max_safe_picks = int(game.get("max_safe_picks") or CASINO_MAX_SAFE_PICKS)
    safe_pick_increment = Decimal(game.get("safe_pick_increment") or CASINO_SAFE_PICK_INCREMENT)
    payout_version = int(game.get("payout_version") or CASINO_CURRENT_PAYOUT_VERSION)
    return columns, grid_size, max_safe_picks, safe_pick_increment, payout_version


def _casino_card(game: dict) -> str:
    """Render an active or terminal Mines board without leaking active bombs."""
    stake = _neko(game["stake"])
    safe_count = len(game.get("safe_positions") or [])
    _, _, max_safe_picks, safe_pick_increment, payout_version = _casino_rules(game)
    multiplier = casino_multiplier(
        safe_count,
        max_safe_picks=max_safe_picks,
        safe_pick_increment=safe_pick_increment,
        payout_version=payout_version,
    )
    status = game.get("status")
    if status == "active":
        current_payout = casino_payout(
            game["stake"],
            safe_count,
            max_safe_picks=max_safe_picks,
            safe_pick_increment=safe_pick_increment,
            payout_version=payout_version,
        )
        profit_multiplier = max(Decimal("0.000"), multiplier - Decimal("1.000"))
        progress_hint = (
            "First safe tile has no profit; all safe tiles reach x3.000."
            if payout_version == CASINO_PAYOUT_VERSION_GRADUAL and max_safe_picks == CASINO_MAX_SAFE_PICKS
            else "Pick a tile or take your current payout."
        )
        return (
            "╓ 💣 Neko Mines\n\n"
            "║ ────────────────\n\n"
            f"╟ 🆔 Game ID: {int(game['id'])}\n\n"
            f"╟ 🎲 Stake reserved: {stake} Neko\n\n"
            f"╟ ✅ Safe tiles: {safe_count}/{max_safe_picks}\n\n"
            f"╟ 📈 Get now: {_neko(current_payout)} Neko (x{multiplier:.3f} total)\n\n"
            f"╟ 💹 Profit over stake: +x{profit_multiplier:.3f}\n\n"
            f"╟ 💵 Available wallet: {_neko(game.get('wallet_balance'))} Neko\n\n"
            f"╙ {progress_hint}"
        )
    if status == "lost":
        return (
            "╓ 💥 Neko Mines — Bomb!\n\n"
            "║ ────────────────\n\n"
            f"╟ 🆔 Game ID: {int(game['id'])}\n\n"
            f"╟ 🎲 Reserved stake burned: {stake} Neko\n\n"
            f"╟ ✅ Safe tiles found: {safe_count}/{max_safe_picks}\n\n"
            f"╟ 💵 Available wallet: {_neko(game.get('wallet_balance'))} Neko\n\n"
            "╙ All tiles are revealed below."
        )
    if status == "cashed_out":
        return (
            "╓ ✅ Neko Mines — Cashed Out\n\n"
            "║ ────────────────\n\n"
            f"╟ 🆔 Game ID: {int(game['id'])}\n\n"
            f"╟ 🎲 Original stake: {stake} Neko\n\n"
            f"╟ ✅ Safe tiles: {safe_count}/{max_safe_picks}\n\n"
            f"╟ 📈 Payout: {_neko(game.get('payout'))} Neko (x{multiplier:.3f})\n\n"
            f"╟ 💵 Available wallet: {_neko(game.get('wallet_balance'))} Neko\n\n"
            "╙ All tiles are revealed below."
        )
    return (
        "╓ ⛔ Neko Mines — Cancelled\n\n"
        "║ ────────────────\n\n"
        f"╟ 🆔 Game ID: {int(game['id'])}\n\n"
        f"╟ 🎲 Reserved stake forfeited: {stake} Neko\n\n"
        f"╟ 💵 Available wallet: {_neko(game.get('wallet_balance'))} Neko\n\n"
        "╙ This game is no longer active."
    )


def _casino_keyboard(game: dict, *, owner_id: int) -> InlineKeyboardMarkup:
    """Render the stored board geometry; only active games include Get."""
    game_id = int(game["id"])
    safe_positions = {int(value) for value in game.get("safe_positions") or []}
    is_active = game.get("status") == "active"
    bomb_positions = {int(value) for value in game.get("bomb_positions") or []}
    columns, grid_size, max_safe_picks, safe_pick_increment, payout_version = _casino_rules(game)
    keyboard: list[list[InlineKeyboardButton]] = []
    for row_start in range(0, grid_size, columns):
        row: list[InlineKeyboardButton] = []
        for tile_index in range(row_start, min(row_start + columns, grid_size)):
            if is_active:
                label = "✅" if tile_index in safe_positions else "❓"
                callback_data = f"casino:tile:{owner_id}:{game_id}:{tile_index}"
            else:
                # A bomb finishes the game and the terminal board explicitly
                # reveals every bomb versus safe cell. The same immutable board
                # is shown after cashout/cancellation, but never has a Get row.
                label = "💣" if tile_index in bomb_positions else "✅"
                callback_data = f"casino:done:{owner_id}:{game_id}"
            row.append(InlineKeyboardButton(text=label, callback_data=callback_data))
        keyboard.append(row)
    if is_active:
        current_payout = casino_payout(
            game["stake"],
            len(safe_positions),
            max_safe_picks=max_safe_picks,
            safe_pick_increment=safe_pick_increment,
            payout_version=payout_version,
        )
        keyboard.append([
            InlineKeyboardButton(
                text=f"💰 Get {_neko(current_payout)} Neko",
                callback_data=f"casino:get:{owner_id}:{game_id}",
                style="success",
            ),
        ])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

async def _edit_casino_board(callback: CallbackQuery, game: dict) -> None:
    """Refresh the clicked game message; terminal grids reveal cells without Get."""
    if not callback.message:
        return
    try:
        await callback.message.edit_text(
            _casino_card(game),
            reply_markup=_casino_keyboard(game, owner_id=int(game["user_id"])),
        )
    except Exception:
        # A duplicate Telegram update can naturally arrive after a successful
        # edit. State is already transaction-safe, so an alert is sufficient.
        log.debug("could not refresh Neko casino board %s", game.get("id"), exc_info=True)


def _casino_mode_keyboard(owner_id: int) -> InlineKeyboardMarkup:
    """Render the requested Mine/Dice mode picker for one player."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Mine", callback_data=f"casinotype:mine:{owner_id}"),
        InlineKeyboardButton(text="Dice", callback_data=f"casinotype:dice:{owner_id}"),
    ]])


def _dice_choice_keyboard(owner_id: int, bet_id: int) -> InlineKeyboardMarkup:
    """Render Odd/Even and exact-face Dice prediction buttons."""
    def button(label: str, choice: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            text=label,
            callback_data=f"casinodice:choice:{owner_id}:{bet_id}:{choice}",
        )

    return InlineKeyboardMarkup(inline_keyboard=[
        [button("Odd", "odd"), button("Even", "even")],
        [button("1", "1"), button("2", "2"), button("3", "3")],
        [button("4", "4"), button("5", "5"), button("6", "6")],
    ])


def _casino_stake_prompt(mode: str) -> str:
    """Use the same bounded value prompt for Mine and Dice flows."""
    title = "⛏ <b>Mine selected</b>" if mode == "mine" else "🎲 <b>Dice selected</b>"
    return (
        f"{title}\n\n"
        "Reply to this message with your stake.\n"
        f"Minimum: <b>{_neko(CASINO_MIN_STAKE)} Neko</b>\n"
        f"Maximum: <b>{_neko(CASINO_MAX_STAKE)} Neko</b>"
    )


def _dice_choice_label(choice: str) -> str:
    return choice.upper() if choice in {"odd", "even"} else choice


async def _reply_casino_start_failure(message: Message, *, result: dict, stake: Decimal) -> None:
    """Keep Mine/Dice stake-entry errors concise and consistent."""
    reason = str(result.get("reason") or "")
    if reason == "insufficient_balance":
        await message.reply(
            f"❌ You have {_neko(result.get('balance'))} Neko available, but this stake needs "
            f"{_neko(stake)} Neko.",
        )
    elif reason == "active_game":
        game = result.get("game")
        if game and message.from_user:
            await message.reply(
                "ℹ️ You already have an active Neko Mines game. Here is its current board:\n\n"
                + _casino_card(game),
                reply_markup=_casino_keyboard(game, owner_id=message.from_user.id),
            )
        else:
            await message.reply("ℹ️ Finish or Get your active Neko Mines game before starting another Casino game.")
    elif reason == "active_dice":
        await message.reply("ℹ️ Finish your active Dice bet before starting another Casino game.")
    elif reason == "cooldown":
        await message.reply(
            "⏳ Your next Casino game is available in "
            f"{_format_remaining(int(result.get('remaining_seconds') or 0))}."
        )
    elif reason == "balance_limit":
        await message.reply("⚠️ This wager could exceed the safe Neko wallet limit if it wins.")
    else:
        await message.reply(
            f"❌ Reply with a Neko value from {_neko(CASINO_MIN_STAKE)} to "
            f"{_neko(CASINO_MAX_STAKE)}.",
        )


@router.message(Command("casino"))
@router.message(dot_player_command_filter("casino", accepts_arguments=True))
async def cmd_casino(message: Message) -> None:
    """Open the no-argument Mine/Dice Casino menu."""
    user = message.from_user
    if not user or not message.chat:
        return
    parts = (message.text or "").strip().split()
    if len(parts) != 1:
        await message.reply(
            "ℹ️ Use <code>/casino</code> without a value, then choose Mine or Dice.",
            parse_mode="HTML",
        )
        return
    await message.reply(
        "🎰 <b>Casino</b>\n\nChoose a game:",
        parse_mode="HTML",
        reply_markup=_casino_mode_keyboard(user.id),
    )


@router.callback_query(F.data.startswith("casinotype:"))
async def cb_casino_mode(callback: CallbackQuery, bot: Bot) -> None:
    """Bind Mine/Dice amount entry to the player who opened the menu."""
    parts = (callback.data or "").split(":")
    if (
        len(parts) != 3
        or parts[1] not in {"mine", "dice"}
        or not parts[2].isascii()
        or not parts[2].isdigit()
    ):
        await callback.answer("Invalid Casino mode.", show_alert=True)
        return
    if not callback.from_user or not callback.message:
        await callback.answer("This Casino menu is unavailable.", show_alert=True)
        return

    mode, owner_id_text = parts[1:]
    owner_id = int(owner_id_text)
    if callback.from_user.id != owner_id:
        await callback.answer("⛔ Only the player who opened this Casino menu can use it.", show_alert=True)
        return
    if not await ensure_required_channel_membership(
        bot=bot,
        user_id=owner_id,
        message=callback.message,
        command="casino",
    ):
        await callback.answer("Join both official channels to use Casino.", show_alert=True)
        return

    try:
        await start_neko_casino_input_session(
            user_id=owner_id,
            username=callback.from_user.username,
            full_name=callback.from_user.full_name or callback.from_user.username,
            chat_id=callback.message.chat.id,
            prompt_message_id=callback.message.message_id,
            mode=mode,
        )
        await callback.message.edit_text(
            _casino_stake_prompt(mode),
            parse_mode="HTML",
            reply_markup=None,
        )
    except Exception:
        log.exception("could not open Casino %s stake prompt for %s", mode, owner_id)
        await callback.answer("Could not open the Casino prompt. Try /casino again.", show_alert=True)
        return
    await callback.answer(f"{mode.title()} selected")


@router.message(F.reply_to_message, F.text, non_command_reply_text_filter())
async def casino_stake_input(message: Message, bot: Bot) -> None:
    """Consume only a player's reply to their durable Mine/Dice stake prompt."""
    user = message.from_user
    reply = message.reply_to_message
    if not user or user.is_bot or not message.chat or not reply:
        return

    try:
        mode = await get_neko_casino_input_session(
            user_id=user.id,
            chat_id=message.chat.id,
            prompt_message_id=reply.message_id,
        )
    except Exception:
        log.exception("could not load Casino input session for %s", user.id)
        raise SkipHandler
    if mode is None:
        # This broad reply filter must not consume unrelated reply messages in
        # the Neko router or later routers.
        raise SkipHandler
    if not await ensure_required_channel_membership(
        bot=bot,
        user_id=user.id,
        message=message,
        command="casino",
    ):
        return

    stake = parse_casino_stake(message.text or "")
    if stake is None:
        await message.reply(
            f"❌ Reply with a Neko value from {_neko(CASINO_MIN_STAKE)} to "
            f"{_neko(CASINO_MAX_STAKE)}.",
        )
        return

    try:
        if mode == "mine":
            result = await create_neko_casino_game(
                user_id=user.id,
                username=user.username,
                full_name=user.full_name or user.username,
                chat_id=message.chat.id,
                stake=stake,
            )
            if result.get("success"):
                await clear_neko_casino_input_session(
                    user_id=user.id,
                    chat_id=message.chat.id,
                    prompt_message_id=reply.message_id,
                )
                game = result["game"]
                await message.reply(
                    _casino_card(game),
                    reply_markup=_casino_keyboard(game, owner_id=user.id),
                )
                return
            await _reply_casino_start_failure(message, result=result, stake=stake)
            return

        result = await create_neko_dice_bet(
            user_id=user.id,
            username=user.username,
            full_name=user.full_name or user.username,
            chat_id=message.chat.id,
            stake=stake,
        )
    except Exception:
        log.exception("could not prepare Casino %s stake for %s", mode, user.id)
        await message.reply("⚠️ I could not prepare this Casino game right now. Please try again.")
        return

    if not result.get("success"):
        await _reply_casino_start_failure(message, result=result, stake=stake)
        return

    await clear_neko_casino_input_session(
        user_id=user.id,
        chat_id=message.chat.id,
        prompt_message_id=reply.message_id,
    )
    await message.reply(
        "🎲 <b>Choose your Dice prediction</b>\n\n"
        f"Stake: <b>{_neko(stake)} Neko</b>\n"
        "ODD or EVEN wins <b>1.5×</b> your stake.\n"
        "An exact number wins <b>2.5×</b> your stake.\n"
        "A wrong roll loses only this stake.",
        parse_mode="HTML",
        reply_markup=_dice_choice_keyboard(user.id, int(result["bet_id"])),
    )


@router.callback_query(F.data.startswith("casinodice:"))
async def cb_casino_dice_choice(callback: CallbackQuery, bot: Bot) -> None:
    """Lock one Dice prediction, reserve its stake, then request a 🎲 reply."""
    parts = (callback.data or "").split(":")
    if (
        len(parts) != 5
        or parts[1] != "choice"
        or not all(part.isascii() and part.isdigit() for part in parts[2:4])
        or parts[4] not in {"odd", "even", "1", "2", "3", "4", "5", "6"}
    ):
        await callback.answer("Invalid Dice choice.", show_alert=True)
        return
    if not callback.from_user or not callback.message:
        await callback.answer("This Dice choice is unavailable.", show_alert=True)
        return

    owner_id = int(parts[2])
    bet_id = int(parts[3])
    choice = parts[4]
    if callback.from_user.id != owner_id:
        await callback.answer("⛔ Only the player who opened this Dice bet can use it.", show_alert=True)
        return
    if not await ensure_required_channel_membership(
        bot=bot,
        user_id=owner_id,
        message=callback.message,
        command="casino",
    ):
        await callback.answer("Join both official channels to use Casino.", show_alert=True)
        return

    try:
        result = await select_neko_dice_bet(
            bet_id=bet_id,
            user_id=owner_id,
            chat_id=callback.message.chat.id,
            choice=choice,
            roll_prompt_message_id=callback.message.message_id,
        )
    except Exception:
        log.exception("could not select Dice choice for bet %s", bet_id)
        await callback.answer("Could not lock this Dice choice. Try again.", show_alert=True)
        return

    if not result.get("success"):
        reason = str(result.get("reason") or "")
        if reason == "insufficient_balance":
            await callback.answer(
                f"You have {_neko(result.get('balance'))} Neko available.",
                show_alert=True,
            )
        elif reason == "cooldown":
            await callback.answer(
                "Your next Casino game is available in "
                f"{_format_remaining(int(result.get('remaining_seconds') or 0))}.",
                show_alert=True,
            )
        elif reason == "balance_limit":
            await callback.answer("This wager could exceed the safe Neko wallet limit if it wins.", show_alert=True)
        elif reason == "active_game":
            await callback.answer("Finish or Get your active Neko Mines game first.", show_alert=True)
        elif reason in {"not_selecting", "not_found"}:
            await callback.answer("This Dice bet is no longer available.", show_alert=True)
        else:
            await callback.answer("This Dice choice could not be used.", show_alert=True)
        return

    prompt = (
        "🎲 <b>Dice prediction locked</b>\n\n"
        f"Choice: <b>{_dice_choice_label(choice)}</b>\n"
        f"Stake: <b>{_neko(result['stake'])} Neko</b>\n\n"
        "Reply to <b>this message</b> with one Telegram 🎲 dice."
    )
    try:
        await callback.message.edit_text(prompt, parse_mode="HTML", reply_markup=None)
    except Exception:
        log.debug("could not refresh Dice prompt for bet %s", bet_id, exc_info=True)
    await callback.answer("Prediction locked — reply with 🎲")


@router.message(F.dice)
async def casino_dice_roll(message: Message) -> None:
    """Settle only a standard Telegram 🎲 reply bound to an awaiting Dice bet."""
    user = message.from_user
    reply = message.reply_to_message
    dice = message.dice
    if (
        not user
        or user.is_bot
        or not message.chat
        or not reply
        or not dice
        or dice.emoji != "🎲"
    ):
        return

    try:
        result = await settle_neko_dice_bet(
            user_id=user.id,
            chat_id=message.chat.id,
            roll_prompt_message_id=reply.message_id,
            dice_value=dice.value,
        )
    except Exception:
        log.exception("could not settle Dice result for %s", user.id)
        await message.reply("⚠️ I could not settle this Dice bet right now. Please try again.")
        return

    if not result.get("success"):
        # An ordinary Telegram dice message that is not a pending Casino reply
        # must remain silent, so it does not clutter a group chat.
        if result.get("reason") != "not_found":
            await message.reply("⚠️ This Dice bet could not be settled.")
        return

    choice = _dice_choice_label(str(result["choice"]))
    if result["won"]:
        await message.reply(
            "✅ <b>You won the Dice bet!</b>\n\n"
            f"🎲 Roll: <b>{int(result['dice_value'])}</b>\n"
            f"🎯 Choice: <b>{choice}</b>\n"
            f"💰 Payout: <b>{_neko(result['payout'])} Neko</b> "
            f"(<b>{result['multiplier']}×</b> stake)\n"
            f"🐾 Balance: <b>{_neko(result['balance'])} Neko</b>",
            parse_mode="HTML",
        )
        return

    await message.reply(
        "❌ <b>You lost the Dice bet.</b>\n\n"
        f"🎲 Roll: <b>{int(result['dice_value'])}</b>\n"
        f"🎯 Choice: <b>{choice}</b>\n"
        f"💸 Lost stake: <b>{_neko(result['stake'])} Neko</b>\n"
        f"🐾 Balance: <b>{_neko(result['balance'])} Neko</b>",
        parse_mode="HTML",
    )


def _casino_audit_timestamp(value: object) -> str:
    """Render a stored UTC timestamp for a private owner audit card."""
    if value is None:
        return "—"
    if getattr(value, "tzinfo", None) is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.strftime("%Y-%m-%d %H:%M:%S UTC")


def _casino_audit_card(game: dict) -> str:
    """Show a Full Owner every immutable board cell for one Mines Game ID."""
    columns, grid_size, _, _, payout_version = _casino_rules(game)
    bombs = {int(value) for value in game.get("bomb_positions") or []}
    player_id = int(game["user_id"])
    player_name = escape(_display_name(str(game.get("player_full_name") or ""), player_id))
    username = str(game.get("player_username") or "").strip()
    username_line = f"@{escape(username)}" if username else "—"
    status = {
        "active": "Active",
        "cashed_out": "Cashed out",
        "lost": "Lost",
        "cancelled": "Cancelled",
    }.get(str(game.get("status") or ""), "Unknown")
    payout = game.get("payout")
    payout_line = f"{_neko(payout)} Neko" if payout is not None else "—"
    if grid_size == 16:
        payout_rule = "Legacy 4×4 x0.25"
    elif payout_version == CASINO_PAYOUT_VERSION_GRADUAL:
        payout_rule = "Gradual v3 (first safe x1, maximum x3)"
    elif payout_version == CASINO_PAYOUT_VERSION_BALANCED:
        payout_rule = "Balanced v2"
    else:
        payout_rule = "Linear v1"

    rows: list[str] = []
    for row_start in range(0, grid_size, columns):
        cells = []
        for tile_index in range(row_start, min(row_start + columns, grid_size)):
            cell_number = tile_index + 1
            marker = "💣" if tile_index in bombs else "✅"
            cells.append(f"{cell_number} {marker}")
        rows.append("   ".join(cells))
    board = "\n".join(rows)
    return (
        "🔎 <b>Neko Mines Owner Audit</b>\n\n"
        f"🆔 Game ID: <code>{int(game['id'])}</code>\n"
        f"👤 Player: {player_name} (<code>{player_id}</code>)\n"
        f"🔗 Username: {username_line}\n"
        f"💬 Chat ID: <code>{int(game['chat_id'])}</code>\n"
        f"📍 Status: <b>{status}</b>\n"
        f"🎲 Stake: <b>{_neko(game['stake'])} Neko</b>\n"
        f"💰 Payout: <b>{payout_line}</b>\n"
        f"📈 Rule: <b>{payout_rule}</b>\n"
        f"🕒 Started: <code>{_casino_audit_timestamp(game.get('created_at'))}</code>\n"
        f"🏁 Resolved: <code>{_casino_audit_timestamp(game.get('resolved_at'))}</code>\n\n"
        "<b>Full immutable board</b>\n"
        f"<code>{board}</code>\n\n"
        "💣 Bomb · ✅ Safe"
    )


@router.message(Command("casinoanwser"))
async def cmd_casino_anwser(message: Message) -> None:
    """Full Owner private audit for one durable Mines Game ID.

    The command spelling intentionally matches the requested `/casinoanwser`.
    It is not added to public Help or Telegram's member command menu.
    """
    if not message.from_user or not message.chat or message.chat.type != "private":
        return
    if not await _require_full_owner(message):
        return
    parts = (message.text or "").strip().split()
    game_id: int | None = None
    if len(parts) == 2 and re.fullmatch(r"[1-9][0-9]{0,18}", parts[1]):
        candidate = int(parts[1])
        if candidate <= 9_223_372_036_854_775_807:
            game_id = candidate
    if game_id is None:
        await message.reply(
            "❌ Usage: <code>/casinoanwser &lt;game_id&gt;</code>",
            parse_mode="HTML",
        )
        return
    try:
        game = await get_neko_casino_game_audit(game_id=game_id)
    except Exception:
        log.exception("could not load owner casino audit for game %s", game_id)
        await message.reply("⚠️ I could not load that Neko Mines game right now.")
        return
    if not game:
        await message.reply(f"ℹ️ No Neko Mines game exists with Game ID <code>{game_id}</code>.", parse_mode="HTML")
        return
    await message.reply(_casino_audit_card(game), parse_mode="HTML")


@router.callback_query(F.data.startswith("casino:"))
async def cb_casino(callback: CallbackQuery) -> None:
    """Apply an owner-bound Mines tile/Get action through the locked DB game."""
    parts = (callback.data or "").split(":")
    if (
        len(parts) not in {4, 5}
        or parts[1] not in {"tile", "get", "done"}
        or (parts[1] == "tile" and len(parts) != 5)
        or (parts[1] in {"get", "done"} and len(parts) != 4)
        or not all(part.isascii() and part.isdigit() for part in parts[2:])
    ):
        await callback.answer("Invalid Neko Mines action.", show_alert=True)
        return
    if not callback.from_user or not callback.message:
        await callback.answer("This Neko Mines board is unavailable.", show_alert=True)
        return

    action = parts[1]
    owner_id, game_id = int(parts[2]), int(parts[3])
    if callback.from_user.id != owner_id:
        await callback.answer("⛔ Only the player who started this Neko Mines game can use it.", show_alert=True)
        return
    if action == "done":
        await callback.answer("This Neko Mines game is already finished.", show_alert=True)
        return

    try:
        if action == "tile":
            result = await reveal_neko_casino_tile(
                game_id=game_id,
                user_id=owner_id,
                tile_index=int(parts[4]),
            )
        else:
            result = await cash_out_neko_casino(game_id=game_id, user_id=owner_id)
    except Exception:
        log.exception("could not process Neko casino action %s for game %s", action, game_id)
        await callback.answer("Could not process this Neko Mines action. Please try again.", show_alert=True)
        return

    if result.get("success"):
        game = result["game"]
        await _edit_casino_board(callback, game)
        outcome = result.get("outcome")
        if outcome == "lost":
            await callback.answer("💥 Bomb! Your reserved stake was burned.", show_alert=True)
        elif outcome == "cashed_out":
            await callback.answer(f"✅ {_neko(game['payout'])} Neko credited to your wallet.")
        else:
            await callback.answer("✅ Safe tile!")
        return

    reason = result.get("reason")
    if reason == "already_revealed":
        await callback.answer("That safe tile is already revealed.", show_alert=True)
    elif reason == "not_yours":
        await callback.answer("⛔ This Neko Mines game is not yours.", show_alert=True)
    elif reason in {"not_active", "not_found"}:
        game = result.get("game")
        if game:
            await _edit_casino_board(callback, game)
        await callback.answer("This Neko Mines game is no longer active.", show_alert=True)
    elif reason == "balance_limit":
        await callback.answer("Your wallet is at its safe balance limit; Get was not completed.", show_alert=True)
    else:
        await callback.answer("This Neko Mines action could not be completed.", show_alert=True)


# ─── Wallet, /dailyneko, and /nekogive ─────────────────────────────────────

@router.message(Command("nekowallet"))
async def cmd_neko_wallet(message: Message) -> None:
    """Show the requester's real database-backed Neko wallet card."""
    user = message.from_user
    if not user:
        return
    try:
        wallet = await get_neko_wallet(
            user_id=user.id,
            username=user.username,
            full_name=user.full_name or user.username,
        )
    except Exception:
        log.exception("could not load Neko wallet for %s", user.id)
        await message.reply("⚠️ I could not load your Neko wallet right now. Please try again.")
        return
    await message.reply_photo(
        photo=NEKOWALLET_IMAGE_FILE_ID,
        caption=_wallet_card(wallet),
        parse_mode="HTML",
    )


@router.message(Command("dailyneko"))
async def cmd_dailyneko(message: Message) -> None:
    """Credit this user's independent rolling-24-hour 1–49 Neko reward."""
    user = message.from_user
    if not user:
        return
    try:
        result = await claim_daily_neko_reward(
            user_id=user.id,
            username=user.username,
            full_name=user.full_name or user.username,
        )
    except Exception:
        # Do not promise a free retry after a transport failure: the service
        # may have committed the wallet credit before its result was lost.
        log.exception("could not process /dailyneko for user %s", user.id)
        await message.reply(
            "⚠️ I could not confirm your daily Neko reward right now. Please try "
            "<code>/dailyneko</code> again; if it was granted, your cooldown will be shown.",
            parse_mode="HTML",
        )
        return

    if result.get("success"):
        await message.reply(
            "🎁 <b>Daily Neko Reward!</b>\n\n"
            f"You received <b>+{_neko(result['reward'])} Neko</b>.\n"
            f"New balance: <b>{_neko(result['balance'])} Neko</b>.\n\n"
            f"Your next daily Neko reward is available in {DAILY_NEKO_REWARD_HOURS} hours.",
            parse_mode="HTML",
        )
        return

    if result.get("reason") == "cooldown":
        await message.reply(
            "⏳ <b>Daily Neko reward not ready.</b>\n\n"
            f"Your personal {DAILY_NEKO_REWARD_HOURS}-hour cooldown ends in "
            f"<b>{_format_remaining(int(result.get('remaining_seconds') or 0))}</b>.",
            parse_mode="HTML",
        )
    elif result.get("reason") == "balance_limit":
        await message.reply(
            "⚠️ Your Neko wallet is at its safe balance limit. Your daily reward was not used.",
        )
    else:
        await message.reply("⚠️ Your daily Neko reward could not be prepared. Please try again.")


@router.callback_query(F.data.startswith("nekoquest:"))
async def cb_retired_neko_quest(callback: CallbackQuery) -> None:
    """Give stale pre-v31 quest panels a clear, non-mutating answer."""
    await callback.answer("Neko quests are no longer available.", show_alert=True)


@router.message(Command("nekogive"))
async def cmd_neko_give(message: Message) -> None:
    """Create a sender-confirmed decimal Neko transfer to the replied human user."""
    sender = message.from_user
    if not sender or not message.chat:
        return
    amount = _positive_command_number(message)
    if amount is None:
        await message.reply(
            "❌ Usage: <code>/nekogive &lt;amount&gt;</code> while replying to a user.\n"
            "Amounts support up to two decimal places.",
            parse_mode="HTML",
        )
        return
    target = _replied_human_target(message)
    if not target:
        await message.reply(
            "❌ Reply to a human user with <code>/nekogive &lt;amount&gt;</code>.",
            parse_mode="HTML",
        )
        return
    if target.id == sender.id:
        await message.reply("❌ You cannot send Neko to yourself.")
        return
    try:
        result = await create_neko_transfer(
            chat_id=message.chat.id,
            sender_user_id=sender.id,
            sender_username=sender.username,
            sender_full_name=sender.full_name or sender.username,
            recipient_user_id=target.id,
            recipient_username=target.username,
            recipient_full_name=target.full_name or target.username,
            amount=amount,
        )
    except Exception:
        log.exception("could not create Neko transfer from %s", sender.id)
        await message.reply("⚠️ I could not prepare that Neko transfer. Please try again.")
        return

    if not result.get("success"):
        if result.get("reason") == "insufficient_balance":
            await message.reply(
                f"❌ You have {_neko(result['balance'])} Neko, but this transfer needs "
                f"{_neko(amount)} Neko.",
            )
        elif result.get("reason") == "self_transfer":
            await message.reply("❌ You cannot send Neko to yourself.")
        else:
            await message.reply("❌ That Neko amount is not valid.")
        return

    target_name = escape(_display_name(target.full_name or target.username, target.id))
    await message.reply(
        "💸 <b>Confirm Neko Transfer</b>\n\n"
        f"Send <b>{_neko(amount)} Neko</b> to <b>{target_name}</b>?\n\n"
        "Your current wallet balance will be checked again when you confirm.",
        parse_mode="HTML",
        reply_markup=_transfer_keyboard(sender.id, int(result["transfer_id"])),
    )


@router.callback_query(F.data.startswith("nekogive:"))
async def cb_neko_give(callback: CallbackQuery) -> None:
    """Apply/cancel only the exact sender's still-pending transfer request."""
    parts = (callback.data or "").split(":")
    if len(parts) != 4 or parts[1] not in {"confirm", "cancel"} or not all(
        part.isascii() and part.isdigit() for part in parts[2:]
    ):
        await callback.answer("Invalid Neko transfer action.", show_alert=True)
        return
    action, sender_id_text, transfer_id_text = parts[1:]
    sender_id, transfer_id = int(sender_id_text), int(transfer_id_text)
    if not callback.from_user or callback.from_user.id != sender_id:
        await callback.answer("⛔ Only the sender can confirm this Neko transfer.", show_alert=True)
        return
    if not callback.message:
        await callback.answer("This Neko transfer prompt is unavailable.", show_alert=True)
        return

    if action == "cancel":
        try:
            cancelled = await cancel_neko_transfer(transfer_id=transfer_id, sender_user_id=sender_id)
        except Exception:
            log.exception("could not cancel Neko transfer %s", transfer_id)
            await callback.answer("Could not cancel the transfer. Try again.", show_alert=True)
            return
        if cancelled:
            await _edit_confirmation_card(callback, "❌ <b>Neko transfer cancelled.</b>")
            await callback.answer("Cancelled")
        else:
            await _edit_confirmation_card(callback, "ℹ️ <b>This Neko transfer is no longer pending.</b>")
            await callback.answer("No pending transfer")
        return

    try:
        result = await confirm_neko_transfer(transfer_id=transfer_id, sender_user_id=sender_id)
    except Exception:
        log.exception("could not confirm Neko transfer %s", transfer_id)
        await callback.answer("Could not confirm the transfer. Try again.", show_alert=True)
        return

    if result.get("success"):
        await _edit_confirmation_card(
            callback,
            "✅ <b>Neko transfer complete!</b>\n\n"
            f"Sent <b>{_neko(result['amount'])} Neko</b>.\n"
            f"Your new balance: <b>{_neko(result['sender_balance'])} Neko</b>.",
        )
        await callback.answer("Neko sent")
        return

    reason = result.get("reason")
    if reason == "insufficient_balance":
        text = (
            "❌ <b>Neko transfer could not be completed.</b>\n\n"
            f"Your current balance is {_neko(result['balance'])} Neko, which is no longer enough "
            "for this transfer."
        )
    elif reason == "not_yours":
        await callback.answer("⛔ This Neko transfer is not yours.", show_alert=True)
        return
    elif reason in {"not_found", "not_pending"}:
        text = "ℹ️ <b>This Neko transfer is no longer pending.</b>"
    elif reason == "recipient_balance_limit":
        text = "⚠️ <b>Neko transfer could not be completed.</b> The recipient wallet is at its safe limit."
    else:
        text = "⚠️ <b>Neko transfer could not be completed.</b>"
    await _edit_confirmation_card(callback, text)
    await callback.answer("Transfer not completed")


# ─── Full-owner Neko controls ──────────────────────────────────────────────

async def _require_full_owner(message: Message) -> bool:
    """Keep owner wallet commands hidden from non-full-owners."""
    return bool(message.from_user and await is_bot_owner(message.from_user.id))


async def _require_owner_reply_target(message: Message, command: str):
    target = _replied_human_target(message)
    if target:
        return target
    await message.reply(f"❌ Reply to a human user with <code>/{command}</code>.", parse_mode="HTML")
    return None


@router.message(Command("resetallneko"))
async def cmd_reset_all_neko(message: Message) -> None:
    """Full owner: ask before zeroing every funded Neko wallet."""
    owner = message.from_user
    if not owner or not await _require_full_owner(message):
        return
    await message.reply(
        "⚠️ <b>Reset all Neko wallets?</b>\n\n"
        "This will set every registered player's available Neko balance to "
        "<b>0 Neko</b>, forfeit every active Neko Mines stake, and cancel every active Dice bet.\n\n"
        "This global economy action cannot be undone. Confirm only if you are sure.",
        parse_mode="HTML",
        reply_markup=_reset_all_neko_keyboard(owner.id),
    )


@router.message(Command("nekoownercheck"))
async def cmd_nekoownercheck(message: Message) -> None:
    """Full owner: display the replied user's live Neko wallet card."""
    if not await _require_full_owner(message):
        return
    target = await _require_owner_reply_target(message, "nekoownercheck")
    if not target:
        return
    try:
        wallet = await get_neko_wallet(
            user_id=target.id,
            username=target.username,
            full_name=target.full_name or target.username,
        )
    except Exception:
        log.exception("could not load owner-requested Neko wallet for %s", target.id)
        await message.reply("⚠️ I could not load that Neko wallet right now.")
        return
    await message.reply(_wallet_card(wallet), parse_mode="HTML")


@router.message(Command("deletewallet"))
async def cmd_deletewallet(message: Message) -> None:
    """Full owner: ask before snapshotting and zeroing a replied wallet."""
    owner = message.from_user
    if not owner or not await _require_full_owner(message):
        return
    target = await _require_owner_reply_target(message, "deletewallet")
    if not target:
        return
    try:
        wallet = await get_neko_wallet(
            user_id=target.id,
            username=target.username,
            full_name=target.full_name or target.username,
        )
    except Exception:
        log.exception("could not prepare Neko wallet reset for %s", target.id)
        await message.reply("⚠️ I could not read that Neko wallet right now.")
        return

    target_name = escape(_display_name(str(wallet.get("full_name") or ""), target.id))
    await message.reply(
        "⚠️ <b>Reset Neko wallet?</b>\n\n"
        f"<b>{target_name}</b>'s available balance of <b>{_neko(wallet['balance'])} Neko</b> "
        "will become <b>0 Neko</b>.\n\n"
        "A restorable available-balance snapshot will be saved. Any active Neko Mines stake "
        "will be forfeited and any active Dice bet will be cancelled. Neko Pass is unchanged.",
        parse_mode="HTML",
        reply_markup=_owner_wallet_keyboard(action="reset", owner_id=owner.id, object_id=target.id),
    )


@router.message(Command("backwallet"))
async def cmd_backwallet(message: Message) -> None:
    """Full owner: confirm replacement from the target's newest unused backup."""
    owner = message.from_user
    if not owner or not await _require_full_owner(message):
        return
    target = await _require_owner_reply_target(message, "backwallet")
    if not target:
        return
    try:
        backup = await get_latest_neko_wallet_backup(user_id=target.id)
        wallet = await get_neko_wallet(
            user_id=target.id,
            username=target.username,
            full_name=target.full_name or target.username,
        )
    except Exception:
        log.exception("could not prepare Neko wallet restore for %s", target.id)
        await message.reply("⚠️ I could not read that Neko wallet backup right now.")
        return
    if not backup:
        await message.reply("ℹ️ This user has no restorable Neko wallet backup.")
        return

    target_name = escape(_display_name(str(wallet.get("full_name") or ""), target.id))
    await message.reply(
        "⚠️ <b>Restore Neko wallet?</b>\n\n"
        f"This will replace <b>{target_name}</b>'s current balance of "
        f"<b>{_neko(wallet['balance'])} Neko</b> with the saved balance of "
        f"<b>{_neko(backup['original_balance'])} Neko</b>.\n\n"
        "It replaces the balance; it does not add Neko. Neko Pass is unchanged.",
        parse_mode="HTML",
        reply_markup=_owner_wallet_keyboard(action="restore", owner_id=owner.id, object_id=int(backup["id"])),
    )


@router.message(Command("sudoneko"))
async def cmd_sudoneko(message: Message) -> None:
    """Full owner: add an exact positive decimal Neko amount to a replied wallet."""
    owner = message.from_user
    if not owner or not await _require_full_owner(message):
        return
    amount = _positive_command_number(message)
    if amount is None:
        await message.reply(
            "❌ Usage: <code>/sudoneko &lt;amount&gt;</code> while replying to a user.\n"
            "Amounts support up to two decimal places.",
            parse_mode="HTML",
        )
        return
    target = await _require_owner_reply_target(message, "sudoneko")
    if not target:
        return
    try:
        result = await adjust_neko_wallet(
            user_id=target.id,
            username=target.username,
            full_name=target.full_name or target.username,
            amount=amount,
            adjusted_by=owner.id,
            add=True,
        )
    except Exception:
        log.exception("could not add Neko to %s", target.id)
        await message.reply("⚠️ I could not update that Neko wallet right now.")
        return
    if not result.get("success"):
        await message.reply("⚠️ That wallet has reached its safe Neko balance limit.")
        return
    target_name = escape(_display_name(target.full_name or target.username, target.id))
    await message.reply(
        f"✅ Added <b>{_neko(amount)} Neko</b> to <b>{target_name}</b>. New balance: "
        f"<b>{_neko(result['new_balance'])} Neko</b>.",
        parse_mode="HTML",
    )


@router.message(Command("doomneko"))
async def cmd_doomneko(message: Message) -> None:
    """Full owner: deduct up to a decimal amount, never below zero, from a wallet."""
    owner = message.from_user
    if not owner or not await _require_full_owner(message):
        return
    amount = _positive_command_number(message)
    if amount is None:
        await message.reply(
            "❌ Usage: <code>/doomneko &lt;amount&gt;</code> while replying to a user.\n"
            "Amounts support up to two decimal places.",
            parse_mode="HTML",
        )
        return
    target = await _require_owner_reply_target(message, "doomneko")
    if not target:
        return
    try:
        result = await adjust_neko_wallet(
            user_id=target.id,
            username=target.username,
            full_name=target.full_name or target.username,
            amount=amount,
            adjusted_by=owner.id,
            add=False,
        )
    except Exception:
        log.exception("could not deduct Neko from %s", target.id)
        await message.reply("⚠️ I could not update that Neko wallet right now.")
        return
    if not result.get("success"):
        await message.reply("❌ That Neko amount is not valid.")
        return
    target_name = escape(_display_name(target.full_name or target.username, target.id))
    await message.reply(
        f"✅ Removed <b>{_neko(result['changed_amount'])} Neko</b> from <b>{target_name}</b>. "
        f"New balance: <b>{_neko(result['new_balance'])} Neko</b>.",
        parse_mode="HTML",
    )


@router.message(Command("nekopass"))
async def cmd_neko_pass(message: Message) -> None:
    """Full owner: activate or extend a replied user's time-limited pass."""
    owner = message.from_user
    if not owner or not await _require_full_owner(message):
        return
    parts = (message.text or "").strip().split()
    days = None
    if len(parts) == 2 and re.fullmatch(r"[0-9]{1,5}", parts[1]):
        candidate = int(parts[1])
        if valid_neko_pass_days(candidate):
            days = candidate
    if days is None:
        await message.reply(
            "❌ Usage: <code>/nekopass &lt;days&gt;</code> while replying to a user.\n"
            f"Days must be between 1 and {MAX_NEKO_PASS_DAYS}.",
            parse_mode="HTML",
        )
        return
    target = await _require_owner_reply_target(message, "nekopass")
    if not target:
        return
    try:
        result = await grant_neko_pass(
            user_id=target.id,
            username=target.username,
            full_name=target.full_name or target.username,
            days=days,
            granted_by=owner.id,
        )
    except Exception:
        log.exception("could not grant Neko Pass to %s", target.id)
        await message.reply("⚠️ I could not update that Neko Pass right now.")
        return
    if not result.get("success"):
        await message.reply("❌ That Neko Pass duration is not valid.")
        return
    expires_at = result["expires_at"].astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    verb = "extended" if result.get("was_active") else "activated"
    target_name = escape(_display_name(target.full_name or target.username, target.id))
    await message.reply(
        f"✅ Neko Pass {verb} for <b>{target_name}</b> by <b>{days}</b> "
        f"day{'s' if days != 1 else ''}.\n"
        f"Expires: <b>{expires_at}</b>\n\n"
        "Neko Pass status is stored on this wallet; it does not change Daily Neko or Casino payouts.",
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("resetallneko:"))
async def cb_reset_all_neko(callback: CallbackQuery) -> None:
    """Execute an initiator-bound, full-owner global Neko reset confirmation."""
    parts = (callback.data or "").split(":")
    if (
        len(parts) != 3
        or parts[1] not in {"confirm", "cancel"}
        or not parts[2].isascii()
        or not parts[2].isdigit()
    ):
        await callback.answer("Invalid all-wallet reset action.", show_alert=True)
        return

    choice, owner_id_text = parts[1:]
    owner_id = int(owner_id_text)
    if not callback.from_user or callback.from_user.id != owner_id:
        await callback.answer("⛔ Only the owner who opened this action can use it.", show_alert=True)
        return
    if not await is_bot_owner(owner_id):
        await callback.answer("⛔ Full owner permission is required.", show_alert=True)
        return
    if not callback.message:
        await callback.answer("This all-wallet reset prompt is unavailable.", show_alert=True)
        return

    if choice == "cancel":
        await _edit_confirmation_card(callback, "❌ <b>All Neko wallet reset cancelled.</b>")
        await callback.answer("Cancelled")
        return

    try:
        result = await reset_all_neko_wallets(reset_by=owner_id)
    except Exception:
        log.exception("could not reset all Neko wallets")
        await callback.answer("Could not reset all Neko wallets. Try again.", show_alert=True)
        return

    reset_count = int(result.get("reset_user_count") or 0)
    cancelled_games = int(result.get("cancelled_casino_games") or 0)
    cancelled_dice_bets = int(result.get("cancelled_dice_bets") or 0)
    await _edit_confirmation_card(
        callback,
        "✅ <b>All Neko wallets reset.</b>\n\n"
        f"Set <b>{reset_count}</b> funded wallet(s) to <b>0 Neko</b>.\n"
        f"Removed <b>{_neko(result.get('removed_balance'))} Neko</b> from circulation.\n"
        f"Forfeited <b>{cancelled_games}</b> active Neko Mines game(s).\n"
        f"Cancelled <b>{cancelled_dice_bets}</b> active Dice bet(s).",
    )
    await callback.answer("All Neko wallets reset")


@router.callback_query(F.data.startswith("ownerneko:"))
async def cb_owner_neko_wallet(callback: CallbackQuery) -> None:
    """Execute owner-bound reset/restore confirmations only for a full owner."""
    parts = (callback.data or "").split(":")
    if (
        len(parts) != 5
        or parts[1] not in {"reset", "restore"}
        or parts[2] not in {"confirm", "cancel"}
        or not all(part.isascii() and part.isdigit() for part in parts[3:])
    ):
        await callback.answer("Invalid owner wallet action.", show_alert=True)
        return
    action, choice, owner_id_text, object_id_text = parts[1:]
    owner_id, object_id = int(owner_id_text), int(object_id_text)
    if not callback.from_user or callback.from_user.id != owner_id:
        await callback.answer("⛔ Only the owner who opened this action can use it.", show_alert=True)
        return
    if not await is_bot_owner(owner_id):
        await callback.answer("⛔ Full owner permission is required.", show_alert=True)
        return
    if not callback.message:
        await callback.answer("This owner wallet prompt is unavailable.", show_alert=True)
        return

    if choice == "cancel":
        label = "wallet reset" if action == "reset" else "wallet restore"
        await _edit_confirmation_card(callback, f"❌ <b>Neko {label} cancelled.</b>")
        await callback.answer("Cancelled")
        return

    try:
        if action == "reset":
            result = await reset_neko_wallet(user_id=object_id, reset_by=owner_id)
            cancelled_games = int(result.get("cancelled_casino_games") or 0)
            cancelled_dice_bets = int(result.get("cancelled_dice_bets") or 0)
            game_note = (
                f"\nForfeited <b>{cancelled_games}</b> active Neko Mines game(s)."
                if cancelled_games else ""
            )
            dice_note = (
                f"\nCancelled <b>{cancelled_dice_bets}</b> active Dice bet(s)."
                if cancelled_dice_bets else ""
            )
            await _edit_confirmation_card(
                callback,
                "✅ <b>Neko wallet reset.</b>\n\n"
                f"Saved <b>{_neko(result['old_balance'])} Neko</b> in a restorable backup.\n"
                "Current balance: <b>0 Neko</b>." + game_note + dice_note,
            )
            await callback.answer("Wallet reset")
            return
        result = await restore_neko_wallet_backup(backup_id=object_id, restored_by=owner_id)
    except Exception:
        log.exception("could not confirm owner Neko %s %s", action, object_id)
        await callback.answer("Could not update the wallet. Try again.", show_alert=True)
        return

    if not result.get("success"):
        await _edit_confirmation_card(callback, "ℹ️ <b>This Neko wallet backup is no longer available.</b>")
        await callback.answer("Backup unavailable")
        return
    await _edit_confirmation_card(
        callback,
        "✅ <b>Neko wallet restored.</b>\n\n"
        f"Replaced the current <b>{_neko(result['old_balance'])} Neko</b> balance with "
        f"<b>{_neko(result['restored_balance'])} Neko</b>.",
    )
    await callback.answer("Wallet restored")
