"""/catch — claim the currently spawned character in the group."""
from __future__ import annotations

import asyncio
import time

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message

from bot.services.matcher import matches
from bot.services.spawn_engine import get_active_spawn, clear_active_spawn
from bot.services.game import (
    CATCH_LIMIT,
    CATCH_PROTECTION_SECONDS,
    claim_character,
    count_user_harem,
    get_user_catch_limit_status,
)
from bot.services.rich_renderer import rarity_symbol, rarity_label_bold, anime_emoji
from bot.handlers.common import claim_keyboard, see_collection_keyboard

router = Router()


async def _reply_after_delay(message: Message, *args, **kwargs):
    """Delay only the visible /catch response; claim state stays immediate."""
    await asyncio.sleep(2)
    return await message.reply(*args, **kwargs)


SEP = "────────────────────────"


def _bold_num(s: str) -> str:
    bold_digits = "𝟬𝟭𝟮𝟯𝟰𝟱𝟲𝟳𝟴𝟵"
    return "".join(bold_digits[int(c)] if c.isdigit() else c for c in s)


def _catch_limit_reached_text() -> str:
    """Keep the user-facing cap response identical before and during a race."""
    return (
        f"⛔ You reached your catch limit today ({CATCH_LIMIT}/{CATCH_LIMIT}). "
        "You cannot catch another character until the next shared 9-hour reset."
    )


def _catch_protection_text() -> str:
    """English feedback for the universal first-four-seconds spawn guard."""
    return (
        f"⏳ Catch protection is active. Please wait {CATCH_PROTECTION_SECONDS} seconds "
        "after a card spawns before trying /catch again."
    )


def _replaced_spawn_text() -> str:
    """Tell an old-answer player that only the newest group card is claimable."""
    return (
        "⏳ That character was replaced by a newer spawn. "
        "Only the latest spawned card can be caught."
    )


@router.message(Command("catch"), F.chat.type != "private")
async def cmd_catch(message: Message):
    if not message.text:
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await _reply_after_delay(message, "❓ Usage: <code>/catch &lt;name&gt;</code>", parse_mode="HTML")
        return

    guess = parts[1].strip()
    user = message.from_user
    chat_id = message.chat.id

    spawn = await get_active_spawn(chat_id)
    if not spawn:
        await _reply_after_delay(message, "📭 No active spawn in this chat. Wait for the next one!")
        return

    now = time.time()
    if now > float(spawn.get("expires_at", 0) or 0):
        await clear_active_spawn(chat_id)
        await _reply_after_delay(message, "⏰ The spawn has expired. Wait for the next one!")
        return

    # Match the current card first. If an exact answer belongs only to the
    # immediate still-live card replaced by this one, explain the replacement
    # before the four-second gate or generic wrong-answer response. This never
    # reveals the current spoiler's identity and still permits shared aliases.
    matched_current, _score = matches(guess, spawn["name"], spawn.get("aliases", ""))
    superseded_name = str(spawn.get("superseded_name") or "")
    if not matched_current and superseded_name:
        was_replaced, _score = matches(
            guess,
            superseded_name,
            str(spawn.get("superseded_aliases") or ""),
        )
        if was_replaced:
            await _reply_after_delay(message, _replaced_spawn_text())
            return

    # Fast player feedback happens before matching or catch-limit work. The
    # service repeats the same check under a DB row lock, so a direct caller or
    # a concurrent request cannot claim before four full seconds have passed.
    spawned_at = float(spawn.get("spawned_at", 0) or 0)
    if now < spawned_at + CATCH_PROTECTION_SECONDS:
        await _reply_after_delay(message, _catch_protection_text())
        return

    # Fast feedback at the cap comes before guessing. claim_character repeats
    # the check atomically inside its transaction to close concurrent races.
    limit_status = await get_user_catch_limit_status(user.id, now=now)
    if limit_status.exhausted:
        await _reply_after_delay(message, _catch_limit_reached_text())
        return

    if not matched_current:
        await _reply_after_delay(message, "❌ Your Guess is not true")
        return

    result = await claim_character(
        chat_id=chat_id,
        user_id=user.id,
        username=user.username or "",
        full_name=user.full_name,
        spawn=spawn,
        expected_character_id=int(spawn["character_id"]),
    )

    if not result.success:
        if result.reason == "catch_protected":
            await _reply_after_delay(message, _catch_protection_text())
        elif result.reason == "expired":
            await _reply_after_delay(message, "⏰ Spawn expired mid-claim. Race lost!")
        elif result.reason == "no_active_spawn":
            await _reply_after_delay(message, "🤔 No active spawn — someone else just claimed it.")
        elif result.reason == "replaced":
            await _reply_after_delay(message, _replaced_spawn_text())
        elif result.reason == "catch_limit":
            await _reply_after_delay(message, _catch_limit_reached_text())
        elif result.reason == "overlord_already_claimed":
            await _reply_after_delay(message, 
                "☬ You already have this exact OverLord card. Another eligible player can still catch it."
            )
        elif result.reason == "overlord_owner_cap_reached":
            await _reply_after_delay(message,
                "☬ This OverLord card has reached its 10-owner cap and will no longer spawn."
            )
        elif result.reason == "love_not_spawnable":
            await _reply_after_delay(message, "❤️ Love cards are not catchable; use /lovelist instead.")
        else:
            # Keep a future/defensive service rejection visible and delayed
            # exactly once rather than silently consuming a /catch command.
            await _reply_after_delay(message, "⚠️ Your catch could not be completed. Please try again.")
        return

    seconds_passed = max(0, int(time.time() - float(spawn.get("spawned_at", time.time()))))
    sym = rarity_symbol(result.rarity)
    rar = rarity_label_bold(result.rarity)
    bold_id = _bold_num(str(result.character_id))
    ani_em = anime_emoji(result.anime)

    # All state work above has already completed. Delay the visible success
    # response exactly once, after the active-spawn transaction is settled.
    await asyncio.sleep(2)

    # Reply to spawn message
    catch_card = (
        f"🪼 <b>{user.full_name}</b> 𝗬𝗼𝘂 𝗖𝗮𝘂𝗴𝗵𝘁 𝗮 𝗡𝗲𝘄 𝗖𝗵𝗮𝗿𝗮𝗰𝘁𝗲𝗿!\n"
        f"🆔 {bold_id}\n"
        f"🪪 {result.name}\n"
        f"📼 {result.anime} [{ani_em}]\n"
        f"⟨ {sym} {rar} ⟩\n"
        f"🎯 𝗖𝗮𝘁𝗰𝗵 𝗟𝗶𝗺𝗶𝘁: {_bold_num(str(result.catch_limit_count))}/{_bold_num(str(result.catch_limit))}\n"
        f"{SEP}\n"
        f"⏱️ {_bold_num(str(seconds_passed))} 𝗦𝗲𝗰𝗼𝗻𝗱𝘀 𝗣𝗮𝘀𝘀𝗲𝗱 𝗧𝗼 𝗖𝗮𝘁𝗰𝗵!"
    )

    owned_count = await count_user_harem(user.id)
    await message.reply_to_message.reply(
        catch_card,
        parse_mode="HTML",
        reply_markup=claim_keyboard(result.character_id),
    ) if message.reply_to_message else None

    # Reply to the spawn photo message directly
    try:
        await message.bot.send_message(
            chat_id=chat_id,
            text=catch_card,
            parse_mode="HTML",
            reply_to_message_id=spawn["message_id"],
            reply_markup=see_collection_keyboard(user.id, owned_count),
        )
    except Exception:
        await message.reply(
            catch_card,
            parse_mode="HTML",
            reply_markup=see_collection_keyboard(user.id, owned_count),
        )
