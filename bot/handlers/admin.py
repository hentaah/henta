"""Slash-command owner controls for message-only spawns and administration."""
from __future__ import annotations

import logging
import re
import time
from html import escape

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from bot.config import DEFAULT_GROUP_SPAWN_INTERVAL, default_spawn_interval_for_group, settings
from bot.db.pool import get_pool
from bot.services.spawn_engine import ForcedSpawnError, reload_character_cache, force_spawn
from bot.services.game import (
    accept_sudo_grant,
    cancel_sudo_grant,
    create_sudo_grant,
    find_catalog_characters_by_anime,
    find_catalog_characters_by_name,
    get_all_catalog_characters,
    get_catalog_characters_by_rarity,
    get_character_by_id,
    get_characters_by_ids,
)
from bot.services.permissions import is_bot_owner
from bot.services.moderation import (
    MAX_BOOM_REASON_LENGTH,
    MAX_MANUAL_MUTE_DAYS,
    apply_boom_ban,
    apply_manual_mute,
    list_active_boom_bans,
    normalize_boom_reason,
)
from bot.services.rich_renderer import (
    heading, paragraph, text_bold, text_plain, text_code, blocks_to_html,
)
from bot.services.catalog_media import answer_catalog_media
from bot.services.love import LOVE_RARITY, get_love_card_by_code, parse_love_code

router = Router()
log = logging.getLogger(__name__)



# A bare rarity keyword is intentionally resolved before character/anime text,
# so replying with `/sudo common` always means the Common catalog tier.
SUDO_RARITY_ALIASES = {
    rarity.casefold(): rarity
    for rarity in (
        "Common", "Rare", "Legendary", "Mythical", "Sovereign", "OP", "Eternal",
        "Suspicious", "OverLord", LOVE_RARITY,
    )
}

# Keep direct moderation targets inside the PostgreSQL BIGINT/Telegram-user-ID
# domain. A reply target remains supported and carries its visible name.
MAX_TELEGRAM_USER_ID = 9_223_372_036_854_775_807
# Keep each `/boomlist` message safely below Telegram's text limit after HTML
# escaping; long lists are sent as successive cards.
BOOM_LIST_PAGE_MAX_CHARS = 3_500


class BoomReason(StatesGroup):
    """Short owner-only prompt between selecting a Boom target and committing it."""

    waiting_reason = State()


def _numeric_boom_target_argument(message: Message) -> tuple[bool, int | None]:
    """Parse exactly one positive numeric target after `/boom` when supplied."""
    parts = (getattr(message, "text", None) or "").strip().split(maxsplit=1)
    if len(parts) < 2:
        return False, None
    raw = parts[1].strip()
    if not re.fullmatch(r"[0-9]{1,19}", raw):
        return True, None
    target_id = int(raw)
    if not 0 < target_id <= MAX_TELEGRAM_USER_ID:
        return True, None
    return True, target_id


def admin_panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💬 Spawn Messages",     callback_data="admin:setspawn", style="primary"),
            InlineKeyboardButton(text="📊 Bot Stats",          callback_data="admin:stats", style="primary"),
        ],
        [
            InlineKeyboardButton(text="🔄 Reload Char Cache", callback_data="admin:reload", style="success"),
            InlineKeyboardButton(text="🧹 Clear All Spawns",  callback_data="admin:clear", style="danger"),
        ],
        [InlineKeyboardButton(text="📚 Owner Command Guide", callback_data="admin:guide", style="primary")],
    ])



def owner_command_guide_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🎴 Catalog", callback_data="adminguide:catalog", style="primary"),
            InlineKeyboardButton(text="🎁 Collection", callback_data="adminguide:collection", style="primary"),
        ],
        [
            InlineKeyboardButton(text="👑 Roles", callback_data="adminguide:roles", style="primary"),
            InlineKeyboardButton(text="🛠️ Control", callback_data="adminguide:control", style="primary"),
        ],
        [
            InlineKeyboardButton(text="🚫 Moderation", callback_data="adminguide:moderation", style="danger"),
            InlineKeyboardButton(text="❌ Close", callback_data="adminguide:close", style="danger"),
        ],
    ])


def owner_command_guide_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⬅️ Owner Command Guide", callback_data="adminguide:home", style="primary"),
    ]])


OWNER_GUIDE_HOME = "<b>📚 Owner Commands</b>"

OWNER_GUIDES = {
    "catalog": (
        "<b>🎴 Catalog</b>\n\n"
        "<code>/addcharacter</code> — add a card\n"
        "<code>/updatecharacter</code> — edit a card\n"
        "<code>/namechange</code> — rename a card\n"
        "<code>/animechange</code> — change anime\n"
        "<code>/raritychange</code> — change rarity\n"
        "<code>/aliaschange</code> — edit aliases\n"
        "<code>/photochange</code> — replace media\n"
        "<code>/addevent</code> — create event\n"
        "<code>/deleteevent</code> — remove event\n"
        "<code>/editevent</code> — rename event\n"
        "<code>/eventchange</code> — assign event\n"
        "<code>/deletecharacter</code> — delete card\n"
        "<code>/resetcharacter</code> — reset catalog <i>(Full Owner)</i>\n"
        "<code>/emptycodes</code> — find free IDs\n"
        "<code>/getdatabase</code> — download catalog\n"
        "<code>/madedatabase</code> — import catalog <i>(Full Owner)</i>\n"
        "<code>/rewritedatabase</code> — replace catalog <i>(Full Owner)</i>\n"
        "<code>/addlog on|off</code> — catalog logs <i>(Full Owner)</i>\n"
        "<code>/cancel</code> — cancel current step"
    ),
    "collection": (
        "<b>🎁 Collection</b>\n\n"
        "<code>/sudo</code> — grant cards\n"
        "<code>/haremownercheck</code> — inspect Harem\n"
        "<code>/deleteharem</code> — clear a Harem\n"
        "<code>/doomcharacter</code> — remove one copy\n"
        "<code>/backharem</code> — restore Harem\n"
        "<code>/ownercheck</code> — card catch report\n"
        "<code>/resetlimit</code> — reset catch limit\n"
        "<code>/nekoownercheck</code> — inspect wallet\n"
        "<code>/deletewallet</code> — clear wallet\n"
        "<code>/backwallet</code> — restore wallet\n"
        "<code>/sudoneko</code> — add Neko\n"
        "<code>/doomneko</code> — remove Neko\n"
        "<code>/nekopass</code> — set Pass\n"
        "<code>/addmarket</code> — list market card\n"
        "<code>/ownermarketcheck</code> — inspect listing\n"
        "<code>/deletemarket</code> — remove listing"
    ),
    "roles": (
        "<b>👑 Roles</b>\n\n"
        "<code>/upadmin</code> — grant catalog admin\n"
        "<code>/upowner</code> — grant owner\n"
        "<code>/downowner</code> — revoke owner\n"
        "<code>/downadmin</code> — revoke admin\n"
        "<code>/mainlist</code> — list roles"
    ),
    "control": (
        "<b>🛠️ Control</b>\n\n"
        "<code>/admin</code> — open this panel\n"
        "<code>/changemessage</code> — set spawn threshold\n"
        "<code>/spawn [character_id]</code> — force a card\n"
        "<code>/spawnstatus</code> — view group spawn\n"
        "<code>/getgroups</code> — export groups\n"
        "<code>/left &lt;chat_id&gt;</code> — leave a group\n"
        "<code>/stopspawn &lt;event&gt;</code> — pause event\n"
        "<code>/restorespawn &lt;event&gt;</code> — resume event\n"
        "<code>/stoprarityspawn &lt;rarity&gt;</code> — pause rarity\n"
        "<code>/spawnstart &lt;rarity&gt;</code> — resume rarity\n"
        "<code>/broadcast</code> — send announcement\n"
        "<code>/kill</code> — enable maintenance\n"
        "<code>/unkill</code> — disable maintenance\n"
        "<code>/deletedatabase</code> — erase player data <i>(Root owner only)</i>"
    ),
    "moderation": (
        "<b>🚫 Moderation</b>\n\n"
        "<code>/boom</code> — Boom with reason\n"
        "<code>/boomlist</code> — list active Booms\n"
        "<code>/unban</code> — remove Boom\n"
        "<code>/mute</code> — mute user\n"
        "<code>/unmute</code> — remove mute"
    ),
}

async def _edit_owner_guide(callback: CallbackQuery, text: str,
                            keyboard: InlineKeyboardMarkup | None) -> None:
    if not callback.message:
        return
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    except Exception:
        pass


@router.message(Command("admin"))
async def cmd_admin(message: Message):
    """Owner-only admin panel."""
    user = message.from_user
    if not user or not await is_bot_owner(user.id):
        await message.answer(
            "🚫 <b>Access denied.</b> Only the bot owner can use this command.",
            parse_mode="HTML",
        )
        return

    pool = get_pool()
    async with pool.acquire() as conn:
        users     = await conn.fetchval("SELECT COUNT(*) FROM users")
        chars     = await conn.fetchval("SELECT COUNT(*) FROM characters")
        groups_db = await conn.fetchval("SELECT COUNT(*) FROM groups")

    html = (
        "<b>🎛️ Admin Control Panel</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👤 <b>Your ID:</b> <code>{settings.owner_id}</code>\n"
        "💬 <b>Default spawn thresholds:</b> 100 in @HentaCatcherGP; "
        f"{DEFAULT_GROUP_SPAWN_INTERVAL} in other groups\n"
        f"👥 <b>Total users:</b> {users}\n"
        f"📚 <b>Character pool:</b> {chars}\n"
        f"🌐 <b>Groups:</b> {groups_db}\n\n"
        "<b>👇 Tap an action below.</b>"
    )
    await message.answer(html, parse_mode="HTML", reply_markup=admin_panel_keyboard())


@router.callback_query(F.data.startswith("admin:"))
async def cb_admin(callback: CallbackQuery):
    if not await is_bot_owner(callback.from_user.id):
        await callback.answer("🚫 Access denied", show_alert=True)
        return

    action = callback.data.split(":", 1)[1]

    if action == "stats":
        pool = get_pool()
        async with pool.acquire() as conn:
            users     = await conn.fetchval("SELECT COUNT(*) FROM users")
            chars     = await conn.fetchval("SELECT COUNT(*) FROM characters")
            groups_db = await conn.fetchval("SELECT COUNT(*) FROM groups")
            ranked_harem = await conn.fetchval(
                "SELECT COUNT(*) FROM harem WHERE is_ranked = TRUE"
            )
            inventory_only_harem = await conn.fetchval(
                "SELECT COUNT(*) FROM harem WHERE is_ranked = FALSE"
            )
        html = (
            "<b>📊 Bot Statistics</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"👥 Total users: <b>{users}</b>\n"
            f"📚 Character pool: <b>{chars}</b>\n"
            f"🌐 Registered groups: <b>{groups_db}</b>\n"
            f"💾 Total ranked claims: <b>{ranked_harem}</b>\n"
            f"📦 Inventory-only harem rows: <b>{inventory_only_harem}</b>\n"
        )
        await callback.message.answer(html, parse_mode="HTML")

    elif action == "setspawn":
        await callback.message.answer(
            "💬 Use <code>/changemessage &lt;messages&gt;</code> in a group to set its message-only spawn threshold.",
            parse_mode="HTML",
        )

    elif action == "reload":
        n = await reload_character_cache()
        await callback.message.answer(
            f"🔄 Character cache reloaded — <b>{n}</b> characters now in memory.",
            parse_mode="HTML",
        )

    elif action == "clear":
        pool = get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                rows = await conn.fetch("DELETE FROM active_spawns RETURNING chat_id")
                scheduled_rows = await conn.fetch(
                    "DELETE FROM scheduled_op_spawns RETURNING chat_id"
                )

        # Also clear the matching Redis keys — previously left to expire
        # naturally, meaning /guess could still succeed against a
        # supposedly-cleared spawn for up to ~100s after this ran.
        if rows:
            from bot.services.redis_client import get_redis
            redis = get_redis()
            keys = [f"spawn:{r['chat_id']}" for r in rows]
            await redis.delete(*keys)

        await callback.message.answer(
            f"🧹 Cleared <b>{len(rows)}</b> active spawn(s) from DB and cache, "
            f"plus <b>{len(scheduled_rows)}</b> announced OP spawn(s).",
            parse_mode="HTML",
        )

    elif action == "guide":
        await callback.message.answer(
            OWNER_GUIDE_HOME,
            parse_mode="HTML",
            reply_markup=owner_command_guide_keyboard(),
        )

    await callback.answer()



@router.callback_query(F.data.startswith("adminguide:"))
async def cb_owner_command_guide(callback: CallbackQuery):
    """Show hidden owner command reminders without exposing them in / menu."""
    if not await is_bot_owner(callback.from_user.id):
        await callback.answer("🚫 Access denied", show_alert=True)
        return

    action = (callback.data or "").split(":", 1)[1]
    if action == "home":
        await _edit_owner_guide(callback, OWNER_GUIDE_HOME, owner_command_guide_keyboard())
    elif action == "close":
        await _edit_owner_guide(callback, "📚 <b>Owner command guide closed.</b>", None)
    elif action in OWNER_GUIDES:
        await _edit_owner_guide(callback, OWNER_GUIDES[action], owner_command_guide_back_keyboard())
    else:
        await callback.answer("Invalid guide action.", show_alert=True)
        return
    await callback.answer()


@router.message(Command("changemessage"))
async def cmd_changemessage(message: Message):
    """Owner-only: set a group's automatic spawn threshold in messages."""
    user = message.from_user
    chat = message.chat
    if not user or chat.type == "private":
        await message.answer("ℹ️ This command is for groups.", parse_mode="HTML")
        return

    if not await is_bot_owner(user.id):
        await message.answer("🚫 Only the bot owner can use this.", parse_mode="HTML")
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        blocks = [
            heading("💬 Change Spawn Message Threshold"),
            paragraph(
                text_plain("Usage: "),
                text_code("/changemessage <messages>"),
                text_plain("\n\nExample: "),
                text_code("/changemessage 300"),
                text_plain(" — one automatic spawn for every 300 group messages."),
            ),
        ]
        await message.answer(blocks_to_html(blocks), parse_mode="HTML")
        return

    message_threshold = int(parts[1].strip())
    if message_threshold < 1 or message_threshold > 100000:
        await message.answer(
            "❌ Message threshold must be between 1 and 100000.",
            parse_mode="HTML",
        )
        return

    # UPSERT means an owner can set the threshold even before ordinary group
    # activity has created a row.  No time setting is stored or consulted.
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO groups (
                chat_id, title, username, spawn_interval, spawn_interval_custom,
                msg_counter, updated_at
            )
            VALUES ($1, $2, $3, $4, TRUE, 0, NOW())
            ON CONFLICT (chat_id) DO UPDATE SET
              spawn_interval = EXCLUDED.spawn_interval,
              spawn_interval_custom = TRUE,
              msg_counter = 0,
              updated_at = NOW()
            """,
            chat.id,
            chat.title or chat.username or "",
            chat.username,
            message_threshold,
        )

    # Start the new setting from a clean durable count rather than inheriting
    # messages accumulated under the previous threshold. Clear the old Redis
    # key only as a best-effort legacy cleanup; it is no longer authoritative.
    try:
        from bot.services.redis_client import get_redis
        await get_redis().delete(f"grp:{chat.id}:counter")
    except Exception:
        pass

    await message.answer(
        f"✅ Automatic spawn set to every <b>{message_threshold}</b> group message(s). Counter reset.",
        parse_mode="HTML",
    )


@router.message(Command("spawnstatus"), F.chat.type != "private")
async def cmd_spawnstatus(message: Message):
    """Full owner: diagnose durable spawn progress for this group only."""
    actor = message.from_user
    if not actor or not await is_bot_owner(actor.id):
        return

    pool = get_pool()
    async with pool.acquire() as conn:
        group = await conn.fetchrow(
            """
            SELECT chat_id, title, spawn_interval, claim_window, msg_counter
            FROM groups
            WHERE chat_id = $1
            """,
            message.chat.id,
        )
        active = await conn.fetchrow(
            """
            SELECT s.character_id, s.expires_at, c.name, c.rarity
            FROM active_spawns AS s
            JOIN characters AS c ON c.id = s.character_id
            WHERE s.chat_id = $1
            """,
            message.chat.id,
        )
        pending_op = await conn.fetchrow(
            """
            SELECT q.character_id, q.spawn_at, c.name
            FROM scheduled_op_spawns AS q
            JOIN characters AS c ON c.id = q.character_id
            WHERE q.chat_id = $1
            """,
            message.chat.id,
        )

    if not group:
        await message.reply(
            "ℹ️ No spawn settings have been recorded for this group yet.",
            parse_mode="HTML",
        )
        return

    threshold = max(1, int(group["spawn_interval"] or default_spawn_interval_for_group(message.chat.id)))
    claim_window = max(1, int(group["claim_window"] or settings.spawn_claim_window))
    counter = max(0, int(group["msg_counter"] or 0))
    remaining = max(0, threshold - counter)
    now = time.time()
    title = escape(str(group["title"] or message.chat.title or message.chat.id))

    lines = [
        "<b>📊 Spawn Status</b>",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"💬 Group: <b>{title}</b>",
        f"🆔 Chat ID: <code>{message.chat.id}</code>",
        f"🔢 Message progress: <b>{counter}/{threshold}</b>",
        f"⏭️ Messages until next threshold: <b>{remaining}</b>",
        f"⏱️ Claim window: <b>{claim_window}</b> seconds",
    ]

    if active:
        seconds_left = max(0, int(float(active["expires_at"]) - now))
        lines.append(
            f"🎴 Current card: <code>#{int(active['character_id'])}</code> "
            f"{escape(str(active['name']))} ({escape(str(active['rarity']))}) — "
            f"<b>{seconds_left}s</b> left"
        )
    else:
        lines.append("🎴 Current card: <i>none</i>")

    if pending_op:
        seconds_until = max(0, int(float(pending_op["spawn_at"]) - now))
        lines.append(
            f"⚡ Pending OP: <code>#{int(pending_op['character_id'])}</code> "
            f"{escape(str(pending_op['name']))} — appears in <b>{seconds_until}s</b>"
        )
    else:
        lines.append("⚡ Pending OP: <i>none</i>")

    lines.extend([
        "",
        "<i>Normal active cards do not pause message counting; the newest threshold card replaces them.</i>",
    ])
    await message.reply("\n".join(lines), parse_mode="HTML")


@router.message(Command("spawn"), F.chat.type != "private")
async def cmd_spawn(message: Message):
    """Owner-only: force a random spawn or a chosen catalog ID in this group."""
    user = message.from_user
    if not user or not await is_bot_owner(user.id):
        await message.answer(
            "🚫 <b>Access denied.</b> Only the bot owner can force a spawn.",
            parse_mode="HTML",
        )
        return

    parts = (message.text or "").split(maxsplit=1)
    requested_id: int | None = None
    if len(parts) > 1:
        raw_id = parts[1].strip()
        if not raw_id.isdigit() or int(raw_id) <= 0:
            await message.answer(
                "❌ Usage: <code>/spawn [character_id]</code>",
                parse_mode="HTML",
            )
            return
        requested_id = int(raw_id)

    try:
        await force_spawn(message.bot, message.chat.id, character_id=requested_id)
    except ForcedSpawnError as exc:
        if exc.reason == "not_found":
            await message.answer(
                f"❌ No character with ID <code>{exc.character_id}</code> was found.",
                parse_mode="HTML",
            )
        elif exc.reason == "recently_spawned":
            await message.answer(
                f"⏳ Character <code>{exc.character_id}</code> was spawned recently in this group. "
                "Wait for five other spawns before using this ID again.",
                parse_mode="HTML",
            )
        elif exc.reason == "event_spawn_stopped":
            await message.answer(
                f"⏸️ Character <code>{exc.character_id}</code> belongs to an event whose spawning is stopped. "
                "Use <code>/restorespawn &lt;event name&gt;</code> first.",
                parse_mode="HTML",
            )
        elif exc.reason == "rarity_spawn_stopped":
            await message.answer(
                f"⏸️ Character <code>{exc.character_id}</code> belongs to a rarity whose spawning is stopped. "
                "Use <code>/spawnstart &lt;rarity name&gt;</code> first.",
                parse_mode="HTML",
            )
        elif exc.reason == "overlord_main_group_only":
            await message.answer(
                "☬ OverLord cards may only be spawned in @HentaCatcherGP.",
                parse_mode="HTML",
            )
        elif exc.reason == "overlord_owner_cap_reached":
            await message.answer(
                "☬ This OverLord card already has 10 distinct owners and can no longer spawn.",
                parse_mode="HTML",
            )
        elif exc.reason == "overlord_cadence_not_ready":
            await message.answer(
                "☬ This OverLord card is waiting for its 70 automatic Main Group spawn cadence.",
                parse_mode="HTML",
            )
        elif exc.reason == "love_not_spawnable":
            await message.answer(
                "❤️ Love cards never spawn. Add them through /addcharacter and let players use /lovelist.",
                parse_mode="HTML",
            )
        else:
            await message.answer("❌ The requested character cannot be spawned right now.")


SUDO_MAX_EXPLICIT_IDS = 100


def _sudo_keyboard(grant_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="✅ Accept",
            callback_data=f"sudo:accept:{grant_id}",
            style="success",
        ),
        InlineKeyboardButton(
            text="❌ Cancel",
            callback_data=f"sudo:cancel:{grant_id}",
            style="danger",
        ),
    ]])


async def _replace_sudo_card(callback: CallbackQuery, text: str) -> None:
    """Safely replace either a single-photo or a text grant confirmation."""
    if not callback.message:
        return
    try:
        await callback.message.edit_caption(
            caption=text, parse_mode="HTML", reply_markup=None,
        )
    except Exception:
        try:
            await callback.message.edit_text(
                text, parse_mode="HTML", reply_markup=None,
            )
        except Exception:
            pass


def _sudo_character_list(characters: list[dict], limit: int = 12) -> str:
    """Keep a bulk confirmation readable even for an anime/all selection."""
    lines = [
        f"• <code>{character['id']}</code> — "
        f"{escape(character['name'])} ({escape(character['anime'])})"
        for character in characters[:limit]
    ]
    remaining = len(characters) - len(lines)
    if remaining > 0:
        lines.append(f"• … and {remaining} more.")
    return "\n".join(lines)


def _sudo_usage() -> str:
    return (
        "🛠️ Usage: reply to a user's message with one of these:\n"
        "<code>/sudo 1</code>\n"
        "<code>/sudo 1 2 3</code>\n"
        "<code>/sudo Character Name or Alias</code>\n"
        "<code>/sudo Anime or Game Name</code>\n"
        "<code>/sudo common</code> (or rare, legendary, mythical, sovereign, OP, Eternal, Suspicious, OverLord, Love)\n"
        "<code>/sudo all</code>\n"
        "<code>/sudo all -</code> (missing cards only; unranked)\n\n"
        "If a name and an anime title are identical, use "
        "<code>/sudo name: ...</code> or <code>/sudo anime: ...</code>."
    )


async def _send_sudo_confirmation(
    message: Message,
    *,
    owner_id: int,
    target,
    characters: list[dict],
    selection_kind: str,
    selection_text: str = "",
    is_ranked: bool = True,
) -> None:
    """Persist a selection, then show the matching single or bulk prompt."""
    grant_id, reason = await create_sudo_grant(
        chat_id=message.chat.id,
        owner_id=owner_id,
        target_user_id=target.id,
        character_ids=[character["id"] for character in characters],
        selection_kind=selection_kind,
        is_ranked=is_ranked,
    )
    if grant_id is None:
        await message.answer("❌ Couldn't prepare this owner grant.", parse_mode="HTML")
        return

    keyboard = _sudo_keyboard(grant_id)
    target_name = escape(target.full_name)
    if len(characters) == 1:
        # Preserve the bot's original single-character /sudo card and photo.
        char = characters[0]
        caption = (
            "🛠️ <b>Owner confirmation needed</b>\n\n"
            f"The owner wants to give <b>{escape(char['name'])}</b> "
            f"({escape(char['anime'])}, {escape(char['rarity'])}) to "
            f"<b>{target_name}</b>.\n\n"
            "Give it to them?"
        )
        try:
            await answer_catalog_media(
                message,
                character=char,
                caption=caption,
                reply_markup=keyboard,
            )
            return
        except Exception:
            await message.answer(caption, parse_mode="HTML", reply_markup=keyboard)
            return

    if selection_kind == "all":
        if not is_ranked:
            caption = (
                f"⚠️ <b>Give missing characters to {target_name} as unranked cards?</b>\n"
                "Only catalog IDs they do not already own will be added.\n"
                "These cards stay visible in /harem but do not affect rankings, "
                "group stats, or global catcher counts.\n\n"
                "<i>This action cannot be undone.</i>"
            )
        else:
            # Preserve normal /sudo all wording/layout exactly.
            caption = (
                f"⚠️ <b>Give ALL characters to {target_name}?</b>\n"
                "Every character in the database will be added to their harem.\n"
                "Already owned characters will be skipped.\n\n"
                "<i>This action cannot be undone.</i>"
            )
    elif selection_kind == "name":
        caption = (
            "⚠️ <b>Owner confirmation needed</b>\n\n"
            f"Give every catalog art named <b>{escape(selection_text)}</b> "
            f"to <b>{target_name}</b>?\n"
            "Already owned characters will be skipped.\n\n"
            f"{_sudo_character_list(characters)}"
        )
    elif selection_kind == "anime":
        caption = (
            "⚠️ <b>Owner confirmation needed</b>\n\n"
            f"Give every character from <b>{escape(selection_text)}</b> "
            f"to <b>{target_name}</b>?\n"
            "Already owned characters will be skipped.\n\n"
            f"{_sudo_character_list(characters)}"
        )
    elif selection_kind == "rarity":
        caption = (
            "⚠️ <b>Owner confirmation needed</b>\n\n"
            f"Give every <b>{escape(selection_text)}</b> rarity character "
            f"to <b>{target_name}</b>?\n"
            "Already owned characters will be skipped.\n\n"
            f"{_sudo_character_list(characters)}"
        )
    else:  # explicit one-or-many ID selection
        caption = (
            "⚠️ <b>Owner confirmation needed</b>\n\n"
            f"Give these characters to <b>{target_name}</b>?\n"
            "Every listed ID will be added as a copy; repeated IDs give repeated copies.\n\n"
            f"{_sudo_character_list(characters)}"
        )
    await message.answer(caption, parse_mode="HTML", reply_markup=keyboard)


@router.message(Command("sudo"), F.chat.type != "private")
async def cmd_sudo(message: Message):
    """Owner grant: IDs, name, anime/game, exact rarity tier, or all."""
    user = message.from_user
    if not user or not await is_bot_owner(user.id):
        await message.answer(
            "🚫 <b>Access denied.</b> Only the bot owner can use /sudo.",
            parse_mode="HTML",
        )
        return

    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.answer(_sudo_usage(), parse_mode="HTML")
        return
    target = message.reply_to_message.from_user
    if target.is_bot:
        await message.answer("🚫 Can't grant characters to a bot.", parse_mode="HTML")
        return

    parts = (message.text or "").split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else ""
    if not arg:
        await message.answer(_sudo_usage(), parse_mode="HTML")
        return

    characters: list[dict]
    selection_kind = "ids"
    selection_text = ""
    is_ranked = True
    normalized_tokens = arg.casefold().split()
    if normalized_tokens == ["all", "-"]:
        characters = await get_all_catalog_characters()
        selection_kind = "all"
        is_ranked = False
        if not characters:
            await message.answer("❌ The character catalog is empty.", parse_mode="HTML")
            return
    elif arg.casefold() == "all":
        characters = await get_all_catalog_characters()
        selection_kind = "all"
        if not characters:
            await message.answer("❌ The character catalog is empty.", parse_mode="HTML")
            return
    elif arg.casefold() in SUDO_RARITY_ALIASES:
        # Keep a bare rarity keyword unambiguous even if a catalog character or
        # anime/game title happens to share that word.
        selection_text = SUDO_RARITY_ALIASES[arg.casefold()]
        characters = await get_catalog_characters_by_rarity(selection_text)
        selection_kind = "rarity"
        if not characters:
            await message.answer(
                f"❌ No <b>{escape(selection_text)}</b> characters are in the catalog.",
                parse_mode="HTML",
            )
            return
    elif (love_code_number := parse_love_code(arg)) is not None:
        love_card = await get_love_card_by_code(love_code_number)
        if not love_card:
            await message.answer(
                f"❌ Love code <code>L{love_code_number}</code> was not found.",
                parse_mode="HTML",
            )
            return
        level_one = love_card["levels"].get(1, {})
        characters = [{
            "id": love_card["character_id"],
            "name": love_card["name"],
            "anime": love_card["anime"],
            "rarity": LOVE_RARITY,
            "rarity_score": 0,
            "image_url": level_one.get("image_url", love_card.get("catalog_image_url", "")),
            "media_type": level_one.get("media_type", "photo"),
            "event": love_card.get("event", ""),
            "love_code": love_card["love_code"],
        }]
        selection_text = love_card["love_code"]
        selection_kind = "ids"
    else:
        tokens = arg.split()
        if all(token.isdigit() for token in tokens):
            if len(tokens) > SUDO_MAX_EXPLICIT_IDS:
                await message.answer(
                    f"❌ You can select up to {SUDO_MAX_EXPLICIT_IDS} explicit IDs at once.",
                    parse_mode="HTML",
                )
                return
            # Preserve repeated numeric IDs as deliberate requested copies:
            # /sudo 41 41 must grant two #41 records after confirmation.
            requested_ids = [int(token) for token in tokens]
            unique_requested_ids = list(dict.fromkeys(requested_ids))
            characters_by_id = await get_characters_by_ids(unique_requested_ids)
            missing = [str(character_id) for character_id in unique_requested_ids
                       if character_id not in characters_by_id]
            if missing:
                await message.answer(
                    "❌ No character with ID <code>" + ", ".join(missing) + "</code>.",
                    parse_mode="HTML",
                )
                return
            characters = [characters_by_id[character_id] for character_id in requested_ids]
        else:
            # Bare text works naturally for the requested /sudo Character Name
            # and /sudo Anime or Game Name forms. Prefixes resolve the rare
            # ambiguity where a character and anime share the exact same name.
            lowered = arg.casefold()
            forced_kind = None
            query = arg
            if lowered.startswith("name:"):
                forced_kind, query = "name", arg.split(":", 1)[1].strip()
            elif lowered.startswith("character:"):
                forced_kind, query = "name", arg.split(":", 1)[1].strip()
            elif lowered.startswith("anime:"):
                forced_kind, query = "anime", arg.split(":", 1)[1].strip()
            elif lowered.startswith("game:"):
                forced_kind, query = "anime", arg.split(":", 1)[1].strip()

            if not query:
                await message.answer(_sudo_usage(), parse_mode="HTML")
                return
            name_matches = [] if forced_kind == "anime" else await find_catalog_characters_by_name(query)
            anime_matches = [] if forced_kind == "name" else await find_catalog_characters_by_anime(query)
            if forced_kind == "name":
                characters, selection_kind = name_matches, "name"
            elif forced_kind == "anime":
                characters, selection_kind = anime_matches, "anime"
            elif name_matches and anime_matches:
                await message.answer(
                    "⚠️ This text matches both a character name and an anime/game title.\n"
                    "Use <code>/sudo name: ...</code> or <code>/sudo anime: ...</code>.",
                    parse_mode="HTML",
                )
                return
            elif name_matches:
                characters, selection_kind = name_matches, "name"
            else:
                characters, selection_kind = anime_matches, "anime"
            selection_text = query
            if not characters:
                await message.answer(
                    "❌ No exact character name, alias, or anime/game title was found.",
                    parse_mode="HTML",
                )
                return

    await _send_sudo_confirmation(
        message,
        owner_id=user.id,
        target=target,
        characters=characters,
        selection_kind=selection_kind,
        selection_text=selection_text,
        is_ranked=is_ranked,
    )


async def _sudo_target_name(callback: CallbackQuery, target_user_id: int) -> str:
    """Use live group information when possible without trusting callback data."""
    fallback = f"User {target_user_id}"
    if not callback.message:
        return fallback
    try:
        member = await callback.bot.get_chat_member(callback.message.chat.id, target_user_id)
        return member.user.full_name or fallback
    except Exception:
        return fallback


@router.callback_query(F.data.startswith("sudo:"))
async def cb_sudo(callback: CallbackQuery):
    """Confirm/cancel only the exact persistent owner-grant proposal."""
    parts = (callback.data or "").split(":")
    if len(parts) != 3 or parts[1] not in {"accept", "cancel"} or not parts[2].isdigit():
        await callback.answer("Invalid owner grant action.", show_alert=True)
        return
    if not await is_bot_owner(callback.from_user.id):
        await callback.answer("🚫 Not your call to make.", show_alert=True)
        return

    action, grant_id = parts[1], int(parts[2])
    if action == "cancel":
        cancelled, _reason = await cancel_sudo_grant(grant_id, callback.from_user.id)
        if not cancelled:
            await callback.answer("🚫 This owner grant is not pending for you.", show_alert=True)
            return
        await _replace_sudo_card(callback, "❌ <b>Cancelled.</b>")
        await callback.answer("Cancelled")
        return

    accepted, reason, target_id, added_ids, requested_ids = await accept_sudo_grant(
        grant_id,
        callback.from_user.id,
    )
    if not accepted:
        if reason == "not_yours":
            await callback.answer("🚫 Not your call to make.", show_alert=True)
            return
        if reason == "catalog_changed":
            await _replace_sudo_card(
                callback,
                "❌ <b>Couldn't complete the grant.</b> The catalog changed before confirmation.",
            )
        else:
            await _replace_sudo_card(callback, "❌ <b>This owner grant is no longer available.</b>")
        await callback.answer("Failed", show_alert=True)
        return

    assert target_id is not None
    if reason == "already_owned":
        result = "ℹ️ User already owns all selected characters."
    elif reason == "ok_unranked":
        result = (
            f"✅ Done! Added <b>{len(added_ids)}</b> missing character(s) as "
            f"unranked harem inventory for user <code>{target_id}</code>."
        )
    elif len(requested_ids) == 1 and len(added_ids) == 1:
        character = await get_character_by_id(added_ids[0])
        target_name = await _sudo_target_name(callback, target_id)
        character_name = escape(character["name"]) if character else f"character #{added_ids[0]}"
        result = (
            f"✅ <b>Accepted.</b> {character_name} added to "
            f"<b>{escape(target_name)}</b>'s harem — courtesy of the owner."
        )
    else:
        result = f"✅ Done! Added <b>{len(added_ids)}</b> characters to user <code>{target_id}</code>."

    await _replace_sudo_card(callback, result)
    await callback.answer("Accepted")


@router.message(Command("boom"))
async def cmd_boom(message: Message, state: FSMContext):
    """Full owner: choose a permanent-Boom target, then request its reason."""
    actor = message.from_user
    if not actor or not await is_bot_owner(actor.id):
        return

    has_numeric_target, numeric_target_id = _numeric_boom_target_argument(message)
    if has_numeric_target:
        if numeric_target_id is None:
            await message.reply(
                "❌ Usage: <code>/boom &lt;positive_user_id&gt;</code>\n"
                "Or reply to a user with <code>/boom</code>.",
                parse_mode="HTML",
            )
            return
        target_id = numeric_target_id
        target_username: str | None = None
        target_full_name = f"User {target_id}"
    else:
        if not message.reply_to_message or not message.reply_to_message.from_user:
            await message.reply(
                "❌ Reply to a user with <code>/boom</code> or use "
                "<code>/boom &lt;positive_user_id&gt;</code>.",
                parse_mode="HTML",
            )
            return
        target = message.reply_to_message.from_user
        if target.is_bot:
            await message.reply("🚫 Cannot Boom a bot.")
            return
        target_id = int(target.id)
        target_username = target.username or None
        target_full_name = target.full_name or f"User {target_id}"

    if await is_bot_owner(target_id):
        await message.reply("🚫 Cannot Boom the owner.")
        return

    await state.set_state(BoomReason.waiting_reason)
    await state.update_data(
        boom_target_id=target_id,
        boom_target_username=target_username,
        boom_target_full_name=target_full_name,
    )
    await message.reply(
        "📝 <b>Boom reason required</b>\n\n"
        f"Send the reason for <b>{escape(target_full_name)}</b> "
        f"(<code>{target_id}</code>) in your next message.\n"
        f"Maximum: <b>{MAX_BOOM_REASON_LENGTH}</b> characters.",
        parse_mode="HTML",
    )


@router.message(BoomReason.waiting_reason, F.text, ~F.text.startswith("/"))
async def receive_boom_reason(message: Message, state: FSMContext):
    """Commit a selected Boom only after its owner provides a valid reason."""
    actor = message.from_user
    if not actor or not await is_bot_owner(actor.id):
        await state.clear()
        return

    reason = normalize_boom_reason(message.text)
    if reason is None:
        await message.reply(
            f"❌ Send a non-empty Boom reason up to <b>{MAX_BOOM_REASON_LENGTH}</b> characters.",
            parse_mode="HTML",
        )
        return

    data = await state.get_data()
    try:
        target_id = int(data["boom_target_id"])
    except (KeyError, TypeError, ValueError):
        await state.clear()
        await message.reply("⚠️ This Boom request expired. Start again with <code>/boom</code>.", parse_mode="HTML")
        return
    target_username = data.get("boom_target_username")
    target_full_name = str(data.get("boom_target_full_name") or f"User {target_id}")

    # A role can change while the owner is typing. Never apply a stale prompt
    # to a user who became a full owner in the meantime.
    if await is_bot_owner(target_id):
        await state.clear()
        await message.reply("🚫 Cannot Boom the owner.")
        return

    actor_name = actor.full_name or actor.username or f"Owner {actor.id}"
    try:
        saved = await apply_boom_ban(
            user_id=target_id,
            username=str(target_username) if target_username else None,
            full_name=target_full_name,
            reason=reason,
            boomed_by_user_id=actor.id,
            boomed_by_name=actor_name,
        )
    except Exception:
        log.exception("could not save Boom for user %s", target_id)
        await message.reply("⚠️ Could not save this Boom right now. Please try again.")
        return

    await state.clear()
    await message.reply(
        "💥 <b>BOOM saved.</b>\n"
        f"👤 User: <b>{escape(target_full_name)}</b> (<code>{target_id}</code>)\n"
        f"📝 Reason: <i>{escape(str(saved['reason']))}</i>\n"
        f"👮 Boomed by: {escape(str(saved['boomed_by_name']))}",
        parse_mode="HTML",
    )


def _boom_list_pages(rows: list[dict]) -> list[str]:
    """Render the requested `user_id - reason` lines within Telegram limits."""
    if not rows:
        return ["ℹ️ <b>BOOM list is empty.</b>"]

    header = "🚫 <b>BOOM LIST</b>\n\n"
    pages: list[str] = []
    current = header
    for row in rows:
        line = f"<code>{int(row['user_id'])}</code> - {escape(str(row['reason']))}"
        candidate = f"{current}\n{line}" if current != header else f"{current}{line}"
        if len(candidate) > BOOM_LIST_PAGE_MAX_CHARS and current != header:
            pages.append(current)
            current = header + line
        else:
            current = candidate
    pages.append(current)
    return pages


@router.message(Command("boomlist"))
async def cmd_boomlist(message: Message):
    """Full owner: list every active permanent Boom and its reason."""
    actor = message.from_user
    if not actor or not await is_bot_owner(actor.id):
        return

    rows = await list_active_boom_bans()
    for page in _boom_list_pages(rows):
        await message.reply(page, parse_mode="HTML")


@router.message(Command("unban"))
async def cmd_unban(message: Message):
    """Owner-only: unban a user."""
    if not message.from_user or not await is_bot_owner(message.from_user.id):
        return

    target_id = None
    target_name = "User"

    if message.reply_to_message and message.reply_to_message.from_user:
        target_id = message.reply_to_message.from_user.id
        target_name = message.reply_to_message.from_user.full_name
    else:
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) > 1 and parts[1].strip().isdigit():
            target_id = int(parts[1].strip())

    if not target_id:
        await message.reply(
            "❌ Reply to a user or use <code>/unban &lt;user_id&gt;</code>.",
            parse_mode="HTML",
        )
        return

    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            updated = await conn.execute(
                "UPDATE users SET is_banned = FALSE, spam_banned_until = NULL "
                "WHERE user_id = $1",
                target_id,
            )
            if updated != "UPDATE 0":
                # `/boomlist` represents active permanent Booms only. Keep its
                # durable reason record synchronized when an owner unbans.
                await conn.execute("DELETE FROM boom_bans WHERE user_id = $1", target_id)

    if updated == "UPDATE 0":
        await message.reply(f"❌ User <code>{target_id}</code> not found.", parse_mode="HTML")
    else:
        await message.reply(
            f"✅ <b>{target_name}</b> (<code>{target_id}</code>) has been unbanned.",
            parse_mode="HTML",
        )


@router.message(Command("resetlimit"))
async def cmd_resetlimit(message: Message):
    """Full owner: reset a replied user's current global /catch allowance."""
    actor = message.from_user
    if not actor or not await is_bot_owner(actor.id):
        return

    reply = message.reply_to_message
    target = reply.from_user if reply else None
    if not target:
        await message.reply("❌ Reply to a user with <code>/resetlimit</code>.", parse_mode="HTML")
        return
    if target.is_bot:
        await message.reply("🚫 A bot does not have a catch limit.")
        return

    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (
                user_id, username, full_name, catch_window_key, catch_window_claims, last_seen_at
            )
            VALUES ($1, $2, $3, -1, 0, NOW())
            ON CONFLICT (user_id) DO UPDATE SET
              username = EXCLUDED.username,
              full_name = EXCLUDED.full_name,
              catch_window_key = -1,
              catch_window_claims = 0,
              last_seen_at = NOW()
            """,
            target.id,
            target.username or "",
            target.full_name,
        )

    await message.reply(
        f"✅ <b>{escape(target.full_name)}</b>'s catch limit was reset. "
        "They can catch again now.",
        parse_mode="HTML",
    )


def _manual_mute_days_argument(message: Message) -> int | None:
    """Accept exactly one bounded whole-day argument after `/mute`."""
    parts = (message.text or "").strip().split()
    if len(parts) != 2 or not re.fullmatch(r"[0-9]{1,4}", parts[1]):
        return None
    days = int(parts[1])
    return days if 1 <= days <= MAX_MANUAL_MUTE_DAYS else None


@router.message(Command("mute"))
async def cmd_mute(message: Message):
    """Full owner: reply-mute a user from all bot surfaces for whole days."""
    actor = message.from_user
    if not actor or not await is_bot_owner(actor.id):
        return

    days = _manual_mute_days_argument(message)
    if days is None:
        await message.reply(
            "❌ Usage: reply to a user with <code>/mute &lt;days&gt;</code>.\n"
            f"Days must be between 1 and {MAX_MANUAL_MUTE_DAYS}.",
            parse_mode="HTML",
        )
        return

    reply = message.reply_to_message
    target = reply.from_user if reply else None
    if not target:
        await message.reply(
            "❌ Reply to a user with <code>/mute &lt;days&gt;</code>.",
            parse_mode="HTML",
        )
        return
    if target.is_bot:
        await message.reply("🚫 A bot cannot receive a temporary bot mute.")
        return
    if await is_bot_owner(target.id):
        await message.reply("🚫 A full owner cannot be muted.")
        return

    try:
        result = await apply_manual_mute(
            user_id=target.id,
            username=target.username,
            full_name=target.full_name,
            days=days,
        )
    except Exception:
        log.exception("could not set manual mute for %s", target.id)
        await message.reply("⚠️ I could not set that temporary bot mute right now.")
        return

    name = escape(target.full_name or f"User {target.id}")
    extra = " Their permanent ban remains active." if result.get("permanently_banned") else ""
    await message.reply(
        f"✅ <b>{name}</b> was muted from bot actions for <b>{days}</b> "
        f"day{'s' if days != 1 else ''}.{extra}",
        parse_mode="HTML",
    )


@router.message(Command("unmute"))
async def cmd_unmute(message: Message):
    """Full owner: clear a replied user's automatic or owner-set bot mute."""
    actor = message.from_user
    if not actor or not await is_bot_owner(actor.id):
        return

    reply = message.reply_to_message
    target = reply.from_user if reply else None
    if not target:
        await message.reply("❌ Reply to a user with <code>/unmute</code>.", parse_mode="HTML")
        return
    if target.is_bot:
        await message.reply("🚫 A bot cannot receive a temporary bot mute.")
        return

    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE users
            SET spam_banned_until = NULL,
                manual_muted_until = NULL
            WHERE user_id = $1
            RETURNING is_banned
            """,
            target.id,
        )

    if not row:
        await message.reply(
            f"ℹ️ <b>{escape(target.full_name)}</b> has no bot user record or temporary bot mute.",
            parse_mode="HTML",
        )
        return

    if row["is_banned"]:
        await message.reply(
            f"⚠️ Temporary bot mute cleared for <b>{escape(target.full_name)}</b>, "
            "but their permanent ban remains active.",
            parse_mode="HTML",
        )
        return

    await message.reply(
        f"✅ Temporary bot mute cleared for <b>{escape(target.full_name)}</b>.",
        parse_mode="HTML",
    )
