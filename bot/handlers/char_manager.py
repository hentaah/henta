"""Owner-only character management — cards, catalog-hole reports, and CSV export."""
from __future__ import annotations

import asyncio
import csv
import logging
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from html import escape
from io import StringIO

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.config import settings
from bot.db.pool import get_pool
from bot.services.catalog_media import (
    CATALOG_LOG_CHANNEL_USERNAME,
    DATABASE_CHANNEL_USERNAME,
    DATABASE_SEND_DELAY_SECONDS,
    delete_all_database_channel_posts,
    list_database_catalog_arts,
    publish_catalog_change_if_logging_enabled,
    publish_new_art_to_database,
    reply_catalog_media,
    send_catalog_log_toggle_notice,
    send_database_art,
    set_database_add_log_enabled,
)
from bot.services.permissions import can_manage_catalog, is_bot_owner
from bot.services.catalog_staging import (
    STAGED_ADD_START_PREFIX,
    claim_staged_catalog_add_job,
    complete_staged_catalog_add_job_in_transaction,
    create_or_get_staged_catalog_add_job,
    release_staged_catalog_add_job,
)
from bot.services.rich_renderer import rarity_symbol
from bot.services.love import LOVE_RARITY, create_love_card_in_transaction
from bot.services.spawn_engine import clear_group_runtime_cache, reload_character_cache

router = Router()
log = logging.getLogger(__name__)

RARITY_SCORES = {
    "Common": 10,
    "Rare": 25,
    "Legendary": 65,
    "Mythical": 80,
    "Sovereign": 92,
    "OP": 100,
    "Eternal": 110,
    "Suspicious": 120,
    "OverLord": 130,
    # Love cards deliberately have no collection score/profile/leaderboard
    # effect; their progression is persisted separately in love_player_states.
    LOVE_RARITY: 0,
}
VALID_RARITIES = list(RARITY_SCORES)
MAX_EVENT_LABEL_LENGTH = 80
MAX_CATALOG_CHARACTER_ID = 9_223_372_036_854_775_807
# /emptycodes reports compact ranges rather than expanding an accidental huge code
# gap into an unbounded Python list or a too-long Telegram message.
EMPTY_CODE_PAGE_MAX_CHARS = 2_800
EMPTY_CODE_RANGE_EXPANSION_LIMIT = 16


class EventSelectionExpiredError(RuntimeError):
    """An event-picker selection disappeared before its card write committed."""


class FixedCharacterIdOccupiedError(RuntimeError):
    """A missing /updatecharacter code was filled by another writer before final insert."""


class AddChar(StatesGroup):
    waiting_photo = State()
    waiting_kind = State()
    waiting_name = State()
    waiting_anime = State()
    waiting_existing_query = State()
    waiting_rarity = State()
    waiting_love_media = State()
    waiting_love_price = State()
    waiting_alias = State()
    waiting_event = State()


class UpdateChar(StatesGroup):
    waiting_photo = State()
    waiting_name = State()
    waiting_anime = State()
    waiting_rarity = State()
    waiting_alias = State()
    # Both new fixed-code cards and ordinary existing-card edits end with an
    # explicit curated event/None choice before their final catalog write.
    waiting_event = State()


class QuickChange(StatesGroup):
    """Focused, full-owner-only one-field character edit flows."""
    waiting_name = State()
    waiting_anime = State()
    waiting_rarity = State()
    waiting_alias = State()
    waiting_photo = State()


async def _can_manage_catalog(message: Message) -> bool:
    """Return whether the sender is a full owner or catalog-only admin."""
    return bool(
        message.from_user
        and await can_manage_catalog(message.from_user.id)
    )


async def _is_full_owner(message: Message) -> bool:
    """Return whether the sender has every owner-only bot permission."""
    return bool(message.from_user and await is_bot_owner(message.from_user.id))


def _rarity_keyboard(*, include_love: bool = True) -> InlineKeyboardMarkup:
    """Build a rarity picker; Love is add-only and shown only where supported."""
    rarities = [
        rarity for rarity in VALID_RARITIES
        if include_love or rarity != LOVE_RARITY
    ]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=("❤️ Love" if rarity == LOVE_RARITY else rarity),
                    callback_data=f"char_rarity:{rarity}",
                    style="primary",
                )
            ]
            for rarity in rarities
        ]
    )


async def _list_catalog_events() -> list[dict]:
    """Return curated event labels in a stable, button-friendly order."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, label
            FROM catalog_events
            ORDER BY LOWER(label) ASC, id ASC
            """
        )
    return [dict(row) for row in rows]


async def _get_catalog_event(event_id: int) -> str | None:
    """Resolve event IDs on the server; callback data never contains labels."""
    pool = get_pool()
    async with pool.acquire() as conn:
        label = await conn.fetchval(
            "SELECT label FROM catalog_events WHERE id = $1",
            event_id,
        )
    return str(label) if label is not None else None


def _event_button_label(label: str) -> str:
    """Keep dynamic Telegram button text below the 64-character limit."""
    cleaned = " ".join(label.split()) or "Unnamed event"
    return cleaned if len(cleaned) <= 60 else cleaned[:59].rstrip() + "…"


def _event_keyboard(events: list[dict], callback_prefix: str) -> InlineKeyboardMarkup:
    """Build one safe event button per row plus the explicit clear option."""
    rows = [
        [
            InlineKeyboardButton(
                text=_event_button_label(str(event["label"])),
                callback_data=f"{callback_prefix}:{int(event['id'])}",
                style="primary",
            )
        ]
        for event in events
    ]
    rows.append([
        InlineKeyboardButton(
            text="None",
            callback_data=f"{callback_prefix}:none",
            style="danger",
        )
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _prompt_addchar_event(message: Message) -> None:
    events = await _list_catalog_events()
    await message.reply(
        "🎊 Choose an event for this character, or choose None:",
        reply_markup=_event_keyboard(events, "addchar:event"),
    )


def _character_caption(
    character_id: int,
    name: str,
    anime: str,
    rarity: str,
    aliases: str = "",
    event: str = "",
) -> str:
    lines = [
        f"🆔 <code>{character_id}</code> | <b>{escape(name)}</b>",
        f"📺 {escape(anime)}",
        f"{rarity_symbol(rarity)} {escape(rarity)}",
        f"🏷️ {escape(aliases) if aliases else '—'}",
    ]
    if event:
        lines.append(f"🎊 Event: {escape(event)}")
    return "\n".join(lines)


async def _replace_preview(callback: CallbackQuery, text: str) -> None:
    """Replace either a photo/video caption or a text preview safely."""
    if not callback.message:
        return
    try:
        await callback.message.edit_caption(
            caption=text,
            parse_mode="HTML",
            reply_markup=None,
        )
    except Exception:
        try:
            await callback.message.edit_text(
                text,
                parse_mode="HTML",
                reply_markup=None,
            )
        except Exception:
            pass


async def _reply_character_preview(
    message: Message,
    *,
    character: dict,
    caption: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> bool:
    """Reply with a catalog photo/video preview and retain a safe text fallback."""
    if not character.get("image_url"):
        return False
    try:
        await reply_catalog_media(
            message,
            character=character,
            caption=caption,
            reply_markup=reply_markup,
        )
    except Exception:
        return False
    return True


def _catalog_actor_name(actor) -> str:
    """Use a readable human actor label in the catalog-change audit channel."""
    if not actor:
        return "Unknown admin"
    return (
        getattr(actor, "full_name", None)
        or getattr(actor, "username", None)
        or f"User {getattr(actor, 'id', 'unknown')}"
    )


async def _publish_catalog_change_log(
    message: Message,
    *,
    action: str,
    character: dict,
    previous: dict | None = None,
    actor_name: str | None = None,
    changed_fields: tuple[str, ...] = (
        "name",
        "anime",
        "rarity",
        "aliases",
        "event",
        "media_type",
        "image_url",
    ),
) -> None:
    """Best-effort current-media audit post when `/addlog on` is enabled."""
    try:
        outcome = await publish_catalog_change_if_logging_enabled(
            message.bot,
            actor_name=actor_name or _catalog_actor_name(message.from_user),
            action=action,
            character=character,
            previous=previous,
            changed_fields=changed_fields,
        )
    except Exception:
        log.exception("could not read/send catalog audit log for art %s", character.get("id"))
        return
    if outcome is False:
        # The mutation already committed. Report the remote-audit problem but
        # never suggest an admin should retry the catalog mutation itself.
        await message.reply(
            f"⚠️ The character change was saved, but its audit log could not be sent to "
            f"{CATALOG_LOG_CHANNEL_USERNAME}."
        )


async def _publish_new_catalog_art(
    message: Message,
    character: dict,
    *,
    actor_name: str | None = None,
) -> None:
    """Archive every committed new card, then optionally audit the add action."""
    try:
        archived = await publish_new_art_to_database(message.bot, character)
    except Exception:
        log.exception("could not archive newly added character %s", character.get("id"))
        archived = False
    if not archived:
        await message.reply(
            f"⚠️ The character was added, but its ART VIEW could not be sent to "
            f"{DATABASE_CHANNEL_USERNAME}."
        )
    await _publish_catalog_change_log(
        message,
        action="Added new character",
        character=character,
        actor_name=actor_name,
        changed_fields=(),
    )


# ─── Cancellation ────────────────────────────────────────────────

@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    """Cancel an active owner character-management flow."""
    if not await _can_manage_catalog(message):
        return

    active_state = await state.get_state()
    managed_states = {
        AddChar.waiting_photo.state,
        AddChar.waiting_kind.state,
        AddChar.waiting_name.state,
        AddChar.waiting_anime.state,
        AddChar.waiting_existing_query.state,
        AddChar.waiting_rarity.state,
        AddChar.waiting_love_media.state,
        AddChar.waiting_love_price.state,
        AddChar.waiting_alias.state,
        AddChar.waiting_event.state,
        UpdateChar.waiting_photo.state,
        UpdateChar.waiting_name.state,
        UpdateChar.waiting_anime.state,
        UpdateChar.waiting_rarity.state,
        UpdateChar.waiting_alias.state,
        UpdateChar.waiting_event.state,
        QuickChange.waiting_name.state,
        QuickChange.waiting_anime.state,
        QuickChange.waiting_rarity.state,
        QuickChange.waiting_alias.state,
        QuickChange.waiting_photo.state,
        EventRename.waiting_label.state,
    }
    if active_state not in managed_states:
        await message.reply("There is no active character-management operation.")
        return

    # A channel-media session is only claimed while it is actively being
    # edited. Releasing it on /cancel lets another authorized catalog manager
    # use the same still-valid private link instead of leaving it locked.
    data = await state.get_data()
    staged_job_id = data.get("staged_catalog_add_job_id")
    actor = message.from_user
    if isinstance(staged_job_id, int) and actor:
        try:
            await release_staged_catalog_add_job(job_id=staged_job_id, actor_id=actor.id)
        except Exception:
            log.exception("could not release staged catalog add job %s", staged_job_id)

    await state.clear()
    await message.reply("❌ Character management cancelled.")


# ─── /getdatabase ──────────────────────────────────────────────────────

@router.message(Command("getdatabase"))
async def cmd_getdatabase(message: Message):
    """Export the live character catalog as the event-aware import CSV."""
    if not await _is_full_owner(message):
        return

    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, name, anime, rarity, aliases, image_url,
                   COALESCE(media_type, 'photo') AS media_type,
                   COALESCE(event, '') AS event
            FROM characters
            ORDER BY id ASC
            """
        )

    # StringIO + csv.writer correctly quotes future names/anime titles that
    # contain commas, quotes, or line breaks.  No physical CSV is written on
    # the server; the owner receives a snapshot directly from PostgreSQL.
    output = StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow([
        "id", "name", "anime", "rarity", "aliases", "image_url", "media_type", "event",
    ])
    for row in rows:
        writer.writerow([
            row["id"],
            row["name"],
            row["anime"],
            row["rarity"],
            row["aliases"] or "",
            row["image_url"] or "",
            row["media_type"] or "photo",
            row["event"] or "",
        ])

    csv_bytes = output.getvalue().encode("utf-8")
    await message.answer_document(
        document=BufferedInputFile(csv_bytes, filename="waifu_characters.csv"),
        caption=f"📦 Exported {len(rows)} character(s) from the live database.",
    )


# ─── Database-channel catalog publishing ───────────────────────────────────

@router.message(Command("addlog"))
async def cmd_addlog(message: Message) -> None:
    """Full Owner: turn catalog-change audit posts to @HentaCatcherLogs on/off."""
    if not await _is_full_owner(message):
        return
    tokens = (message.text or "").split()
    mode = tokens[1].casefold() if len(tokens) == 2 else ""
    if mode not in {"on", "off"}:
        await message.reply(
            "❌ Usage: <code>/addlog on</code> or <code>/addlog off</code>",
            parse_mode="HTML",
        )
        return
    actor = message.from_user
    if not actor:
        return
    enabled = mode == "on"
    await set_database_add_log_enabled(enabled=enabled, changed_by=actor.id)
    notice_sent = await send_catalog_log_toggle_notice(
        message.bot,
        actor_name=_catalog_actor_name(actor),
        enabled=enabled,
    )
    if enabled:
        text = (
            f"✅ Catalog change logging is <b>ON</b>. Saved character edits will be posted to "
            f"{CATALOG_LOG_CHANNEL_USERNAME} with the current card media. New cards continue "
            f"to be archived as ART VIEW posts in {DATABASE_CHANNEL_USERNAME}."
        )
    else:
        text = (
            f"✅ Catalog change logging is <b>OFF</b>. New cards still archive to "
            f"{DATABASE_CHANNEL_USERNAME}, but future edit audit posts are paused."
        )
    if not notice_sent:
        text += f"\n\n⚠️ The setting was saved, but the toggle notice could not be sent to {CATALOG_LOG_CHANNEL_USERNAME}."
    await message.reply(text, parse_mode="HTML")


async def _dm_database_publish_completion(
    message: Message,
    *,
    sent: int,
    failed: int,
) -> None:
    """Deliver the requested completion report privately to the invoking owner."""
    actor = message.from_user
    if not actor:
        return
    if failed:
        text = (
            f"⚠️ Database-channel publishing finished: {sent} art(s) were added, "
            f"but {failed} could not be sent to {DATABASE_CHANNEL_USERNAME}."
        )
    else:
        text = (
            f"✅ All {sent} art(s) have been added to the database channel "
            f"{DATABASE_CHANNEL_USERNAME}."
        )
    try:
        await message.bot.send_message(actor.id, text, parse_mode="HTML")
    except Exception:
        log.exception("could not DM database publish completion to owner %s", actor.id)
        # If a user has never opened the bot, Telegram can reject a private
        # message. Preserve a visible result in the source chat as a fallback.
        if message.chat.id != actor.id:
            await message.reply(text, parse_mode="HTML")


@router.message(Command("madedatabase"))
async def cmd_madedatabase(message: Message) -> None:
    """Full Owner: archive every existing catalog code to @HentaDatabase in ID order."""
    if not await _is_full_owner(message):
        return
    tokens = (message.text or "").split()
    if len(tokens) != 1:
        await message.reply("❌ Usage: <code>/madedatabase</code>", parse_mode="HTML")
        return

    # The query is ordered by code and returns only actual rows, so sparse IDs
    # are naturally skipped while the full range from code 1 to the highest
    # present art is covered.
    characters = await list_database_catalog_arts()
    await message.reply(
        f"⏳ Sending {len(characters)} catalog art(s) to {DATABASE_CHANNEL_USERNAME}. "
        "I will DM you when it finishes.",
        parse_mode="HTML",
    )
    sent = 0
    failed = 0
    for character in characters:
        try:
            await send_database_art(message.bot, character)
        except Exception:
            failed += 1
            log.exception(
                "could not send catalog art %s to database channel",
                character.get("id"),
            )
        else:
            sent += 1
        if DATABASE_SEND_DELAY_SECONDS:
            await asyncio.sleep(DATABASE_SEND_DELAY_SECONDS)

    await _dm_database_publish_completion(message, sent=sent, failed=failed)


@router.message(Command("rewritedatabase"))
async def cmd_rewritedatabase(message: Message) -> None:
    """Full Owner: clear @HentaDatabase, then publish the current catalog anew."""
    if not await _is_full_owner(message):
        return
    if len((message.text or "").split()) != 1:
        await message.reply("❌ Usage: <code>/rewritedatabase</code>", parse_mode="HTML")
        return

    await message.reply(
        f"⏳ Deleting current posts from {DATABASE_CHANNEL_USERNAME}, then rebuilding it from "
        "the current catalog. I will DM you when it finishes.",
        parse_mode="HTML",
    )
    try:
        deleted, upper_message_id = await delete_all_database_channel_posts(message.bot)
    except Exception as exc:
        log.exception("could not clear database channel before rewrite")
        telegram_detail = " ".join(str(exc).split()) or exc.__class__.__name__
        if "message can't be deleted" in telegram_detail.casefold() or "message cannot be deleted" in telegram_detail.casefold():
            reason = (
                "Telegram refused to delete at least one archive post. The official Bot API cannot "
                "delete messages older than 48 hours, even for a channel admin. Manually clear the "
                "old posts (or use a new archive channel), then run /madedatabase."
            )
        else:
            reason = (
                "Make sure the bot has both Post Messages and Delete Messages administrator rights "
                "in the database channel, then try again."
            )
        await message.reply(
            f"⚠️ I could not clear {DATABASE_CHANNEL_USERNAME}. No replacement catalog was sent.\n\n"
            f"{reason}\n\n"
            f"Telegram detail: <code>{escape(telegram_detail[:400])}</code>",
            parse_mode="HTML",
        )
        return

    # Re-read after the channel clear so a successful rewrite uses the most
    # current catalog state rather than an earlier command-start snapshot.
    characters = await list_database_catalog_arts()
    sent = 0
    failed = 0
    for character in characters:
        try:
            await send_database_art(message.bot, character)
        except Exception:
            failed += 1
            log.exception(
                "could not rewrite catalog art %s to database channel",
                character.get("id"),
            )
        else:
            sent += 1
        if DATABASE_SEND_DELAY_SECONDS:
            await asyncio.sleep(DATABASE_SEND_DELAY_SECONDS)

    actor = message.from_user
    if actor:
        if failed:
            completion = (
                f"⚠️ Database rewrite finished after removing {deleted} channel message(s) through "
                f"ID {upper_message_id}: {sent} art(s) were added, but {failed} failed to send."
            )
        else:
            completion = (
                f"✅ Database rewrite finished. Removed {deleted} channel message(s) and added all "
                f"{sent} current catalog art(s) to {DATABASE_CHANNEL_USERNAME}."
            )
        try:
            await message.bot.send_message(actor.id, completion, parse_mode="HTML")
        except Exception:
            log.exception("could not DM database rewrite completion to owner %s", actor.id)
            if message.chat.id != actor.id:
                await message.reply(completion, parse_mode="HTML")


# ─── /emptycodes ──────────────────────────────────────────────────────

async def _catalog_empty_code_ranges() -> tuple[int, int, list[tuple[int, int]]]:
    """Read missing positive catalog-code ranges from 1 through the current max.

    Iterating existing IDs makes this safe even if a full owner intentionally
    created a very high fixed `/updatecharacter` code. A large gap is kept as one range,
    never expanded into billions of individual values in memory.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id
            FROM characters
            WHERE id > 0
            ORDER BY id ASC
            """
        )

    previous_id = 0
    empty_total = 0
    ranges: list[tuple[int, int]] = []
    for row in rows:
        character_id = int(row["id"])
        if character_id <= previous_id:
            continue
        if character_id > previous_id + 1:
            gap_start, gap_end = previous_id + 1, character_id - 1
            ranges.append((gap_start, gap_end))
            empty_total += gap_end - gap_start + 1
        previous_id = character_id
    return previous_id, empty_total, ranges


def _empty_code_range_label(start_id: int, end_id: int) -> str:
    """Show small holes as copyable IDs and large holes as an exact safe range."""
    if end_id - start_id + 1 <= EMPTY_CODE_RANGE_EXPANSION_LIMIT:
        return ", ".join(str(code) for code in range(start_id, end_id + 1))
    return f"{start_id}–{end_id} ({end_id - start_id + 1:,} IDs)"


def _empty_code_pages(ranges: list[tuple[int, int]]) -> list[list[str]]:
    """Split the missing-code report into Telegram-safe, complete range pages."""
    pages: list[list[str]] = []
    current: list[str] = []
    current_length = 0
    for start_id, end_id in ranges:
        label = _empty_code_range_label(start_id, end_id)
        # Account for each HTML bullet/wrapper as well as the label itself.
        # The report header/footer leaves a generous margin below Telegram's
        # 4096-character text ceiling.
        added_length = len(label) + len("• <code></code>\n")
        if current and current_length + added_length > EMPTY_CODE_PAGE_MAX_CHARS:
            pages.append(current)
            current = []
            current_length = 0
        current.append(label)
        current_length += added_length
    if current:
        pages.append(current)
    return pages


def _empty_codes_keyboard(
    *,
    owner_id: int,
    page: int,
    page_count: int,
) -> InlineKeyboardMarkup | None:
    """Bind /emptycodes pagination to the full owner who requested the report."""
    buttons: list[InlineKeyboardButton] = []
    if page > 0:
        buttons.append(
            InlineKeyboardButton(
                text="⬅️ Prev",
                callback_data=f"empty:page:{owner_id}:{page - 1}",
                style="primary",
            )
        )
    if page + 1 < page_count:
        buttons.append(
            InlineKeyboardButton(
                text="Next ➡️",
                callback_data=f"empty:page:{owner_id}:{page + 1}",
                style="primary",
            )
        )
    return InlineKeyboardMarkup(inline_keyboard=[buttons]) if buttons else None


def _empty_codes_report(
    *,
    max_id: int,
    empty_total: int,
    ranges: list[tuple[int, int]],
    owner_id: int,
    requested_page: int,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Build one live /emptycodes result page without exposing any user data."""
    if max_id <= 0:
        return (
            "ℹ️ <b>Empty catalog codes</b>\n\n"
            "The catalog has no positive character IDs yet, so there is no finite range to inspect.",
            None,
        )
    if not ranges:
        return (
            "✅ <b>Empty catalog codes</b>\n\n"
            f"Checked <code>1–{max_id}</code>. No IDs are missing.",
            None,
        )

    pages = _empty_code_pages(ranges)
    page_count = len(pages)
    page = max(0, min(int(requested_page), page_count - 1))
    labels = pages[page]
    text = (
        "🕳️ <b>Empty catalog codes</b>\n\n"
        f"Checked: <code>1–{max_id}</code>\n"
        f"Missing: <b>{empty_total:,}</b> ID(s) across <b>{len(ranges):,}</b> gap(s)\n"
        f"Page: <b>{page + 1}/{page_count}</b>\n\n"
        + "\n".join(f"• <code>{label}</code>" for label in labels)
        + "\n\nUse <code>/updatecharacter &lt;id&gt;</code> to create a card at an empty code."
    )
    return text, _empty_codes_keyboard(owner_id=owner_id, page=page, page_count=page_count)


@router.message(Command("emptycodes"))
async def cmd_emptycodes(message: Message):
    """Full owner: list every missing positive catalog code through MAX(id)."""
    owner = message.from_user
    if not owner or not await _is_full_owner(message):
        return
    if len((message.text or "").strip().split()) != 1:
        await message.reply("❌ Usage: <code>/emptycodes</code>", parse_mode="HTML")
        return
    try:
        max_id, empty_total, ranges = await _catalog_empty_code_ranges()
    except Exception:
        log.exception("could not list empty catalog codes for owner %s", owner.id)
        await message.reply("⚠️ I could not inspect empty catalog codes right now. Please try again.")
        return
    text, keyboard = _empty_codes_report(
        max_id=max_id,
        empty_total=empty_total,
        ranges=ranges,
        owner_id=owner.id,
        requested_page=0,
    )
    await message.reply(text, parse_mode="HTML", reply_markup=keyboard)


@router.callback_query(F.data.startswith("empty:"))
async def cb_emptycodes(callback: CallbackQuery):
    """Refresh a full owner's live missing-code report page safely."""
    if not callback.from_user or not await is_bot_owner(callback.from_user.id):
        await callback.answer("⛔ Owner only.", show_alert=True)
        return
    if not callback.message:
        await callback.answer("This empty-code report is unavailable.", show_alert=True)
        return

    parts = (callback.data or "").split(":")
    if (
        len(parts) != 4
        or parts[0] != "empty"
        or parts[1] != "page"
        or not all(part.isascii() and part.isdigit() for part in parts[2:])
        or int(parts[2]) <= 0
    ):
        await callback.answer("Invalid empty-code page.", show_alert=True)
        return
    owner_id, requested_page = int(parts[2]), int(parts[3])
    if callback.from_user.id != owner_id:
        await callback.answer("⛔ This empty-code report is not for you.", show_alert=True)
        return

    try:
        max_id, empty_total, ranges = await _catalog_empty_code_ranges()
        text, keyboard = _empty_codes_report(
            max_id=max_id,
            empty_total=empty_total,
            ranges=ranges,
            owner_id=owner_id,
            requested_page=requested_page,
        )
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    except Exception:
        log.exception("could not refresh empty catalog codes for owner %s", owner_id)
        await callback.answer("Could not refresh empty catalog codes. Please try again.", show_alert=True)
        return
    await callback.answer()


# ─── Event catalog, full-event rename, and assignment ──────────────────────


class EventRename(StatesGroup):
    """Full-owner flow that receives a complete replacement event label."""
    waiting_label = State()


def _edevent_keyboard(events: list[dict], *, owner_id: int) -> InlineKeyboardMarkup:
    """Show every catalog event while binding the picker to its full owner."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_event_button_label(str(event["label"])),
                    callback_data=f"edevent:select:{owner_id}:{int(event['id'])}",
                    style="primary",
                )
            ]
            for event in events
        ]
    )


async def _rename_catalog_event(
    *,
    event_id: int,
    expected_label: str,
    new_label: str,
) -> dict:
    """Atomically rename a curated event and every character using its old label.

    Cards intentionally store the rendered event text instead of an event FK.
    The normalized update therefore repairs every legacy case/outer-space
    variant associated with the selected catalog label in the same transaction.
    """
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                event = await conn.fetchrow(
                    """
                    SELECT id, label
                    FROM catalog_events
                    WHERE id = $1
                    FOR UPDATE
                    """,
                    event_id,
                )
                if event is None:
                    return {"success": False, "reason": "event_missing"}

                current_label = str(event["label"])
                # A second owner may have completed a rename after this owner
                # selected the button. Never repurpose that stale request.
                if current_label != expected_label:
                    return {"success": False, "reason": "event_changed"}

                # The index is the final concurrency authority. This preliminary
                # lookup provides a useful normal-case message without locking a
                # second event row (which could deadlock two crossed renames).
                conflict = await conn.fetchrow(
                    """
                    SELECT id
                    FROM catalog_events
                    WHERE LOWER(BTRIM(label)) = LOWER(BTRIM($1))
                      AND id <> $2
                    """,
                    new_label,
                    event_id,
                )
                if conflict:
                    return {"success": False, "reason": "label_exists"}

                if new_label != current_label:
                    await conn.execute(
                        "UPDATE catalog_events SET label = $2 WHERE id = $1",
                        event_id,
                        new_label,
                    )

                linked_rows = await conn.fetch(
                    """
                    UPDATE characters
                    SET event = $2
                    WHERE LOWER(BTRIM(COALESCE(event, ''))) = LOWER(BTRIM($1))
                    RETURNING id
                    """,
                    current_label,
                    new_label,
                )
                return {
                    "success": True,
                    "reason": "unchanged" if new_label == current_label else "ok",
                    "old_label": current_label,
                    "label": new_label,
                    "updated_characters": len(linked_rows),
                }
    except Exception as exc:
        # Concurrent candidate renames are still safe because the normalized
        # unique index rejects duplicates. Avoid importing asyncpg here so the
        # package's offline static validation can use its lightweight stubs.
        if exc.__class__.__name__ == "UniqueViolationError":
            return {"success": False, "reason": "label_exists"}
        raise


@router.message(Command("editevent"))
async def cmd_editevent(message: Message, state: FSMContext):
    """Full owner: choose one event before supplying its complete new label."""
    owner = message.from_user
    if not owner or not await _is_full_owner(message):
        return
    if len((message.text or "").strip().split()) != 1:
        await message.reply("❌ Usage: <code>/editevent</code>", parse_mode="HTML")
        return
    try:
        events = await _list_catalog_events()
    except Exception:
        log.exception("could not list catalog events for /editevent")
        await message.reply("⚠️ I could not load the event catalog right now.")
        return
    if not events:
        await message.reply("ℹ️ There are no catalog events to rename.")
        return

    await state.clear()
    await message.reply(
        "✏️ <b>Rename Catalog Event</b>\n\n"
        "Choose the event you want to rename. You will then send its complete new name from scratch.",
        parse_mode="HTML",
        reply_markup=_edevent_keyboard(events, owner_id=owner.id),
    )


@router.callback_query(F.data.startswith("edevent:"))
async def cb_edevent_select(callback: CallbackQuery, state: FSMContext):
    """Bind an event-rename selection to the owner who opened this picker."""
    parts = (callback.data or "").split(":")
    if (
        len(parts) != 4
        or parts[1] != "select"
        or not all(part.isascii() and part.isdigit() and int(part) > 0 for part in parts[2:])
    ):
        await callback.answer("Invalid event rename selection.", show_alert=True)
        return
    owner_id, event_id = int(parts[2]), int(parts[3])
    if not callback.from_user or callback.from_user.id != owner_id:
        await callback.answer("⛔ Only the owner who opened this event picker can use it.", show_alert=True)
        return
    if not await is_bot_owner(owner_id):
        await callback.answer("⛔ Full owner permission is required.", show_alert=True)
        return
    if not callback.message:
        await callback.answer("This event picker is unavailable.", show_alert=True)
        return

    try:
        event_label = await _get_catalog_event(event_id)
    except Exception:
        log.exception("could not resolve /editevent event %s", event_id)
        await callback.answer("Could not load that event. Try again.", show_alert=True)
        return
    if event_label is None:
        await callback.answer("That event no longer exists. Run /editevent again.", show_alert=True)
        return

    await state.clear()
    await state.update_data(
        edevent_owner_id=owner_id,
        edevent_event_id=event_id,
        edevent_expected_label=event_label,
    )
    await state.set_state(EventRename.waiting_label)
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
    await callback.answer("Event selected")
    await callback.message.answer(
        "✏️ Send the complete new name for event <b>"
        f"{escape(event_label)}</b>.\n\n"
        "This will rename the catalog event and every linked character card together. "
        "Use <code>/cancel</code> to stop.",
        parse_mode="HTML",
    )


@router.message(EventRename.waiting_label, F.text)
async def edevent_new_label(message: Message, state: FSMContext):
    """Finish the full-owner event rename and refresh all catalog caches."""
    owner = message.from_user
    if not owner or not await _is_full_owner(message):
        await state.clear()
        return
    data = await state.get_data()
    event_id = data.get("edevent_event_id")
    expected_label = data.get("edevent_expected_label")
    selected_owner_id = data.get("edevent_owner_id")
    if (
        not isinstance(event_id, int)
        or not isinstance(expected_label, str)
        or not isinstance(selected_owner_id, int)
        or selected_owner_id != owner.id
    ):
        await state.clear()
        await message.reply("❌ This event rename request expired. Run <code>/editevent</code> again.", parse_mode="HTML")
        return

    new_label = (message.text or "").strip()
    if not new_label:
        await message.reply("❌ Send the complete new event name, or use <code>/cancel</code>.", parse_mode="HTML")
        return
    if new_label.casefold() == "none":
        await message.reply("❌ <code>None</code> is reserved for clearing a card event.", parse_mode="HTML")
        return
    if len(new_label) > MAX_EVENT_LABEL_LENGTH:
        await message.reply(
            f"❌ Event labels can be at most {MAX_EVENT_LABEL_LENGTH} characters. "
            "Send a shorter complete name or use <code>/cancel</code>.",
            parse_mode="HTML",
        )
        return

    try:
        result = await _rename_catalog_event(
            event_id=event_id,
            expected_label=expected_label,
            new_label=new_label,
        )
    except Exception:
        log.exception("could not rename catalog event %s", event_id)
        await message.reply("⚠️ I could not rename that event right now. Please try again.")
        return

    if not result.get("success"):
        reason = result.get("reason")
        if reason in {"event_missing", "event_changed"}:
            await state.clear()
            await message.reply(
                "❌ That event changed or no longer exists. Run <code>/editevent</code> again.",
                parse_mode="HTML",
            )
        elif reason == "label_exists":
            await message.reply(
                "❌ Another catalog event already has that label. Send a different complete name "
                "or use <code>/cancel</code>.",
                parse_mode="HTML",
            )
        else:
            await state.clear()
            await message.reply("❌ This event rename could not be completed.")
        return

    await state.clear()
    await reload_character_cache()
    unchanged_note = " (already named that)" if result.get("reason") == "unchanged" else ""
    await message.reply(
        "✅ <b>Catalog event renamed.</b>" + unchanged_note + "\n"
        f"🎊 Event: <b>{escape(str(result['label']))}</b>\n"
        f"Updated <b>{int(result['updated_characters'])}</b> linked character card(s).",
        parse_mode="HTML",
    )


@router.message(Command("addevent"))
async def cmd_addevent(message: Message):
    """Catalog admin/full owner: create one reusable catalog event label."""
    if not await _can_manage_catalog(message):
        return

    parts = (message.text or "").split(maxsplit=1)
    label = parts[1].strip() if len(parts) > 1 else ""
    if not label:
        await message.reply(
            "❌ Usage: <code>/addevent &lt;event label&gt;</code>\n"
            "Example: <code>/addevent bunny🐰</code>",
            parse_mode="HTML",
        )
        return
    if label.casefold() == "none":
        await message.reply("❌ <code>None</code> is reserved for clearing an event.", parse_mode="HTML")
        return
    if len(label) > MAX_EVENT_LABEL_LENGTH:
        await message.reply(
            f"❌ Event labels can be at most {MAX_EVENT_LABEL_LENGTH} characters.",
            parse_mode="HTML",
        )
        return

    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO catalog_events (label, created_by)
            VALUES ($1, $2)
            ON CONFLICT DO NOTHING
            RETURNING id, label
            """,
            label,
            message.from_user.id,
        )
        if row is None:
            existing = await conn.fetchrow(
                """
                SELECT id, label
                FROM catalog_events
                WHERE LOWER(BTRIM(label)) = LOWER(BTRIM($1))
                """,
                label,
            )
        else:
            existing = None

    if row:
        await message.reply(
            f"✅ Event created: <b>{escape(str(row['label']))}</b>",
            parse_mode="HTML",
        )
    else:
        await message.reply(
            f"ℹ️ That event already exists: <b>{escape(str(existing['label']))}</b>",
            parse_mode="HTML",
        )


@router.message(Command("deleteevent"))
async def cmd_deleteevent(message: Message):
    """Remove one reusable event and clear its links without deleting cards."""
    if not await _can_manage_catalog(message):
        return

    parts = (message.text or "").split(maxsplit=1)
    label = parts[1].strip() if len(parts) > 1 else ""
    if not label:
        await message.reply(
            "❌ Usage: <code>/deleteevent &lt;event label&gt;</code>\n"
            "Example: <code>/deleteevent bunny🐰</code>",
            parse_mode="HTML",
        )
        return

    pool = get_pool()
    removed_event = None
    cleared_count = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Rename locks this same event row FOR UPDATE; assignment takes a
            # key-share lock. Thus a deleted event cannot be recreated by a
            # stale /eventchange button after cleanup wins the race.
            event = await conn.fetchrow(
                """
                SELECT id, label
                FROM catalog_events
                WHERE LOWER(BTRIM(label)) = LOWER(BTRIM($1))
                FOR UPDATE
                """,
                label,
            )
            if event:
                removed_event = str(event["label"])
                cleared = await conn.fetch(
                    """
                    UPDATE characters
                    SET event = ''
                    WHERE LOWER(BTRIM(COALESCE(event, ''))) = LOWER(BTRIM($1))
                    RETURNING id
                    """,
                    removed_event,
                )
                cleared_count = len(cleared)
                await conn.execute("DELETE FROM catalog_events WHERE id = $1", event["id"])

    if removed_event is None:
        await message.reply(
            f"❌ Event <b>{escape(label)}</b> does not exist. Check the label and try again.",
            parse_mode="HTML",
        )
        return

    await reload_character_cache()
    await message.reply(
        "✅ <b>Event removed.</b>\n"
        f"🎊 Event: <b>{escape(removed_event)}</b>\n"
        f"Cleared the event field from <b>{cleared_count}</b> linked card(s). "
        "No character cards were deleted.",
        parse_mode="HTML",
    )


@router.message(Command("eventchange"))
async def cmd_eventchange(message: Message):
    """Catalog admin/full owner: choose or clear one character's event."""
    if not await _can_manage_catalog(message):
        return

    parts = (message.text or "").split(maxsplit=1)
    raw_id = parts[1].strip() if len(parts) > 1 else ""
    if not raw_id.isdigit() or int(raw_id) <= 0:
        await message.reply("❌ Usage: <code>/eventchange &lt;character_id&gt;</code>", parse_mode="HTML")
        return

    character_id = int(raw_id)
    pool = get_pool()
    async with pool.acquire() as conn:
        character = await conn.fetchrow(
            """
            SELECT id, name, anime, rarity, aliases, image_url,
                   COALESCE(media_type, 'photo') AS media_type,
                   COALESCE(event, '') AS event
            FROM characters
            WHERE id = $1
            """,
            character_id,
        )
    if not character:
        await message.reply(
            f"❌ No character with ID <code>{character_id}</code> was found.",
            parse_mode="HTML",
        )
        return

    events = await _list_catalog_events()
    current_event = str(character["event"] or "")
    current_text = escape(current_event) if current_event else "None"
    await message.reply(
        "🎊 <b>Change Character Event</b>\n\n"
        f"<code>{character['id']}</code> — <b>{escape(character['name'])}</b>\n"
        f"📺 {escape(character['anime'])}\n"
        f"Current event: <b>{current_text}</b>\n\n"
        "Choose the new event, or choose None to clear it:",
        parse_mode="HTML",
        reply_markup=_event_keyboard(events, f"evchange:{character_id}"),
    )


@router.callback_query(F.data.startswith("evchange:"))
async def cb_evchange(callback: CallbackQuery):
    """Apply a catalog event selected from a server-resolved /eventchange button."""
    if not callback.from_user or not await can_manage_catalog(callback.from_user.id):
        await callback.answer("⛔ Catalog admin only.", show_alert=True)
        return

    parts = (callback.data or "").split(":")
    if len(parts) != 3 or not parts[1].isdigit() or int(parts[1]) <= 0:
        await callback.answer("Invalid event action.", show_alert=True)
        return
    character_id = int(parts[1])
    token = parts[2]
    event_id: int | None = None
    if token == "none":
        event = ""
    elif token.isdigit() and int(token) > 0:
        event = ""
        event_id = int(token)
    else:
        await callback.answer("Invalid event selection.", show_alert=True)
        return

    event_missing = False
    previous = None
    changed = None
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            previous = await conn.fetchrow(
                """
                SELECT id, name, anime, rarity, aliases, image_url,
                       COALESCE(media_type, 'photo') AS media_type,
                       COALESCE(event, '') AS event
                FROM characters
                WHERE id = $1
                FOR UPDATE
                """,
                character_id,
            )
            if previous is not None and event_id is not None:
                # The key-share lock serializes this assignment with /deleteevent
                # and /editevent, ensuring a picker writes the current label.
                event_value = await conn.fetchval(
                    "SELECT label FROM catalog_events WHERE id = $1 FOR KEY SHARE",
                    event_id,
                )
                if event_value is None:
                    event_missing = True
                else:
                    event = str(event_value)
            if previous is not None and not event_missing:
                changed = await conn.fetchrow(
                    """
                    UPDATE characters
                    SET event = $1
                    WHERE id = $2
                    RETURNING id, name, anime, rarity, aliases, image_url,
                              COALESCE(media_type, 'photo') AS media_type,
                              COALESCE(event, '') AS event
                    """,
                    event,
                    character_id,
                )

    if event_missing:
        await callback.answer("That event no longer exists. Choose another one.", show_alert=True)
        return
    if not changed or not previous:
        await callback.answer("That character no longer exists.", show_alert=True)
        return

    current = dict(changed)
    await reload_character_cache()
    if callback.message:
        await _publish_catalog_change_log(
            callback.message,
            action="Changed character event",
            character=current,
            previous=dict(previous),
            actor_name=_catalog_actor_name(callback.from_user),
            changed_fields=("event",),
        )
    event_text = escape(str(current["event"])) if current["event"] else "None"
    await _replace_preview(
        callback,
        "✅ <b>Character event updated.</b>\n"
        f"<code>{current['id']}</code> — <b>{escape(current['name'])}</b>\n"
        f"🎊 Event: <b>{event_text}</b>",
    )
    await callback.answer("Event updated")

# ─── /addcharacter ────────────────────────────────────────────────────

MAX_EXISTING_CHARACTER_MATCHES = 10


def _addchar_kind_keyboard() -> InlineKeyboardMarkup:
    """Choose whether the supplied image belongs to a new or known character."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="✨ New Character",
            callback_data="addchar:kind:new",
            style="primary",
        ),
        InlineKeyboardButton(
            text="🔎 Existing Character",
            callback_data="addchar:kind:existing",
            style="primary",
        ),
    ]])


def _existing_character_button_label(character: dict) -> str:
    """A useful, Telegram-safe primary-button label for a search result."""
    name = " ".join(str(character["name"]).split()) or "Unnamed"
    anime = " ".join(str(character["anime"]).split()) or "Unknown title"
    label = f"{name} — {anime}"
    return label if len(label) <= 60 else label[:59].rstrip() + "…"


async def _find_existing_character_matches(query: str) -> list[dict]:
    """Find exact catalog names or semicolon-separated aliases for /addcharacter."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, name, anime, rarity, rarity_score, aliases, image_url,
                   COALESCE(media_type, 'photo') AS media_type
            FROM characters
            WHERE LOWER(BTRIM(name)) = LOWER(BTRIM($1))
               OR EXISTS (
                    SELECT 1
                    FROM unnest(string_to_array(aliases, ';')) AS alias(value)
                    WHERE LOWER(BTRIM(alias.value)) = LOWER(BTRIM($1))
               )
            ORDER BY rarity_score DESC, id ASC
            LIMIT $2
            """,
            query,
            MAX_EXISTING_CHARACTER_MATCHES,
        )
    return [dict(row) for row in rows]


async def _get_existing_character_metadata(character_id: int) -> dict | None:
    """Re-read a selected result so callback data cannot provide catalog fields."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, name, anime, rarity, rarity_score, aliases, image_url,
                   COALESCE(media_type, 'photo') AS media_type
            FROM characters
            WHERE id = $1
            """,
            character_id,
        )
    return dict(row) if row else None


async def _insert_addchar_record(data: dict, aliases: str) -> tuple[int, int | None]:
    """Allocate a catalog ID and atomically attach Love metadata when selected."""
    is_love_card = data.get("rarity") == LOVE_RARITY
    love_price: Decimal | None = None
    love_levels: dict[int, tuple[str, str]] = {}
    if is_love_card:
        try:
            love_price = Decimal(str(data.get("love_base_price") or "")).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        except (InvalidOperation, ValueError) as error:
            raise ValueError("Love card price is missing") from error
        if not love_price.is_finite() or love_price <= 0:
            raise ValueError("Love card price is invalid")
        for level in range(1, 6):
            file_id = data.get(f"love_level_{level}_file_id")
            if not isinstance(file_id, str) or not file_id.strip():
                raise ValueError("Love card level media is missing")
            love_levels[level] = (file_id.strip(), "photo")
        if str(data.get("media_type") or "").casefold() != "photo":
            raise ValueError("Love Level 1 must be a photo")

    staged_job_id = data.get("staged_catalog_add_job_id")
    staged_actor_id = data.get("staged_catalog_add_actor_id")
    if staged_job_id is not None or staged_actor_id is not None:
        if (
            not isinstance(staged_job_id, int)
            or isinstance(staged_job_id, bool)
            or staged_job_id <= 0
            or not isinstance(staged_actor_id, int)
            or isinstance(staged_actor_id, bool)
            or staged_actor_id <= 0
        ):
            raise ValueError("staged channel media session is invalid")

    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # A shared advisory lock makes MAX(id)+1 safe if multiple catalog
            # admins finish the add flow at the same time.
            await conn.execute("SELECT pg_advisory_xact_lock($1)", -611052346221)

            event = str(data.get("event") or "")
            if event:
                # Serialize this final add-card write with /deleteevent. A picker
                # can sit open for minutes, so its earlier event lookup alone
                # is not enough to keep a deleted label from being recreated.
                live_event = await conn.fetchrow(
                    """
                    SELECT label
                    FROM catalog_events
                    WHERE LOWER(BTRIM(label)) = LOWER(BTRIM($1))
                    FOR KEY SHARE
                    """,
                    event,
                )
                if live_event is None:
                    raise EventSelectionExpiredError
                event = str(live_event["label"])

            new_id = await conn.fetchval("SELECT COALESCE(MAX(id), 0) + 1 FROM characters")
            await conn.execute(
                """
                INSERT INTO characters (
                    id, name, anime, rarity, rarity_score, aliases, image_url, media_type, event
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                """,
                new_id,
                data["name"],
                data["anime"],
                data["rarity"],
                RARITY_SCORES[data["rarity"]],
                aliases,
                data["file_id"],
                data["media_type"],
                event,
            )
            love_code: int | None = None
            if is_love_card:
                creator_id = data.get("love_creator_id")
                if not isinstance(creator_id, int) or creator_id <= 0:
                    raise ValueError("Love card creator is missing")
                assert love_price is not None
                love_code = await create_love_card_in_transaction(
                    conn,
                    character_id=int(new_id),
                    created_by=creator_id,
                    base_price=love_price,
                    level_media=love_levels,
                )

            # Claim completion shares this exact catalog transaction. If the
            # staged token was released, expired, or completed elsewhere, the
            # character insert rolls back instead of creating a duplicate from
            # a stale channel deep link.
            if staged_job_id is not None:
                assert isinstance(staged_actor_id, int)
                completed = await complete_staged_catalog_add_job_in_transaction(
                    conn,
                    job_id=staged_job_id,
                    actor_id=staged_actor_id,
                )
                if not completed:
                    raise ValueError("staged channel media is no longer available")
    return int(new_id), love_code

async def _finish_addchar(
    message: Message,
    state: FSMContext,
    data: dict,
    aliases: str,
    *,
    actor_name: str | None = None,
    actor_id: int | None = None,
) -> bool:
    """Persist the new art, refresh its cache, and show its DB-assigned ID."""
    if data.get("rarity") == LOVE_RARITY:
        if not isinstance(actor_id, int) or not await is_bot_owner(actor_id):
            await state.clear()
            await message.reply("⛔ Only a full owner can finish a Love card.")
            return False
        data = {**data, "love_creator_id": actor_id}
    try:
        new_id, love_code = await _insert_addchar_record(data, aliases)
    except EventSelectionExpiredError:
        await state.clear()
        await message.reply(
            "❌ The selected event no longer exists. Start <code>/addcharacter</code> again.",
            parse_mode="HTML",
        )
        return False
    except ValueError as error:
        await state.clear()
        setup_label = "Love card setup" if data.get("rarity") == LOVE_RARITY else "Character setup"
        await message.reply(
            f"❌ {setup_label} could not be completed: <code>{escape(str(error))}</code>.",
            parse_mode="HTML",
        )
        return False

    await state.clear()
    await reload_character_cache()
    await _publish_new_catalog_art(
        message,
        {
            "id": new_id,
            "name": data["name"],
            "anime": data["anime"],
            "rarity": data["rarity"],
            "aliases": aliases,
            "image_url": data["file_id"],
            "media_type": data["media_type"],
            "event": str(data.get("event") or ""),
        },
        actor_name=actor_name,
    )
    love_notice = ""
    if love_code is not None:
        love_notice = (
            f"\n❤️ <b>Love Code:</b> <code>L{love_code}</code>"
            f"\n💎 <b>Fixed Lovelist Price:</b> {escape(str(data.get('love_base_price') or '0'))} Neko"
            "\n🖼️ Levels 1–5 were saved. Players can preview them with "
            f"<code>/check L{love_code} 1</code> through <code>/check L{love_code} 5</code>."
        )
    await message.reply(
        "✅ <b>Character added!</b>\n"
        + _character_caption(
            new_id,
            data["name"],
            data["anime"],
            data["rarity"],
            aliases,
            str(data.get("event") or ""),
        )
        + love_notice,
        parse_mode="HTML",
    )
    return True


def _chat_type_name(message: Message) -> str:
    """Read an aiogram enum/string chat type without coupling to one version."""
    raw_type = getattr(message.chat, "type", "")
    return str(getattr(raw_type, "value", raw_type)).casefold()


def _is_catalog_staging_channel(message: Message) -> bool:
    """Allow channel staging only from the configured public source channel.

    The optional numeric channel ID takes priority when deployment operators add
    it to `.env`. Until then, the exact public username from the user-provided
    source URL is the allowlist; no arbitrary channel can mint a private add
    token.
    """
    if _chat_type_name(message) != "channel":
        return False
    configured_id = settings.catalog_staging_channel_id
    if configured_id is not None:
        return int(message.chat.id) == int(configured_id)
    expected_username = str(settings.catalog_staging_channel_username or "").strip().lstrip("@").casefold()
    actual_username = str(getattr(message.chat, "username", "") or "").strip().lstrip("@").casefold()
    return bool(expected_username and actual_username == expected_username)


def _staged_catalog_add_url(token: str) -> str:
    """Build a Telegram-safe private deep link from a URL-safe DB token."""
    username = str(settings.bot_username or "").strip().lstrip("@")
    return f"https://t.me/{username}?start={STAGED_ADD_START_PREFIX}{token}"


def _staged_catalog_add_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="📥 Continue in Private",
            url=_staged_catalog_add_url(token),
            style="primary",
        ),
    ]])


@router.channel_post(Command("addcharacter"))
async def cmd_addcharacter_from_staging_channel(message: Message) -> None:
    """Stage replied channel media, then route an authorized manager privately.

    Broadcast channel posts do not include the human admin's Telegram user ID.
    The secure private link is therefore claimed only after `can_manage_catalog`
    verifies the person who taps it; a regular channel viewer cannot start an
    add flow or access the staged file ID.
    """
    if not _is_catalog_staging_channel(message):
        return

    source = message.reply_to_message
    if not source:
        await message.answer(
            "❌ Reply to one Telegram photo or video with <code>/addcharacter</code>.",
            parse_mode="HTML",
        )
        return
    media = _incoming_character_media(source)
    if media is None:
        await message.answer(
            "❌ The replied post must contain one Telegram photo or video.",
            parse_mode="HTML",
        )
        return

    file_id, media_type = media
    try:
        job = await create_or_get_staged_catalog_add_job(
            source_chat_id=int(message.chat.id),
            source_message_id=int(source.message_id),
            file_id=file_id,
            media_type=media_type,
        )
    except Exception:
        log.exception(
            "could not stage catalog media from channel %s message %s",
            message.chat.id,
            source.message_id,
        )
        await message.answer(
            "⚠️ I could not prepare this staged media. No catalog change was made; try again.",
        )
        return

    await message.answer(
        "📦 <b>Staged media is ready.</b>\n\n"
        "Open the private link below with a Catalog Admin or Full Owner account. "
        "The bot will load this exact photo/video, then ask whether it is a new "
        "character or an existing character art before continuing the normal flow.",
        parse_mode="HTML",
        reply_markup=_staged_catalog_add_keyboard(job.token),
    )


async def resume_staged_catalog_add_from_start(
    message: Message,
    state: FSMContext,
    payload: str,
) -> bool:
    """Claim a channel-media deep link and begin the normal AddChar FSM privately.

    Returns ``False`` only when the `/start` payload is unrelated, allowing the
    normal welcome route to continue. Every recognized staging payload receives
    a precise English result and is treated as handled.
    """
    if not payload.startswith(STAGED_ADD_START_PREFIX):
        return False
    actor = message.from_user
    if not actor or _chat_type_name(message) != "private":
        await message.answer("❌ Open this catalog-media link in a private chat with the bot.")
        return True
    if not await can_manage_catalog(actor.id):
        await message.answer("⛔ Catalog Admin or Full Owner access is required for this staged media.")
        return True

    token = payload[len(STAGED_ADD_START_PREFIX):].strip()
    job, reason = await claim_staged_catalog_add_job(token=token, actor_id=actor.id)
    if job is None:
        errors = {
            "not_found": "❌ This staged media link is invalid or no longer exists.",
            "expired": "⌛ This staged media link expired. Reply to the channel media with /addcharacter again.",
            "completed": "ℹ️ This staged media was already added to the catalog.",
            "claimed_by_other": "⛔ Another authorized catalog manager is already editing this staged media.",
            "unavailable": "⚠️ This staged media is no longer available. Create a new channel request.",
            "invalid": "❌ This staged media link is invalid.",
        }
        await message.answer(errors.get(reason, "⚠️ This staged media could not be opened."))
        return True

    await state.clear()
    await state.update_data(
        file_id=job.file_id,
        media_type=job.media_type,
        staged_catalog_add_job_id=job.id,
        staged_catalog_add_actor_id=actor.id,
        staged_catalog_add_token=job.token,
        staged_source_chat_id=job.source_chat_id,
        staged_source_message_id=job.source_message_id,
    )
    await state.set_state(AddChar.waiting_kind)
    await message.answer(
        "✅ <b>Channel media loaded.</b>\n\n"
        "🪪 Is this art for a new character or an existing character?",
        parse_mode="HTML",
        reply_markup=_addchar_kind_keyboard(),
    )
    return True


@router.message(Command("addcharacter"))
async def cmd_addcharacter(message: Message, state: FSMContext):
    """Catalog admin/full owner: start by collecting photo or video character media."""
    if not await _can_manage_catalog(message):
        return
    await state.clear()
    await state.set_state(AddChar.waiting_photo)
    await message.reply("📸 Send the character photo or 🎬 Telegram video.")


def _incoming_character_media(message: Message) -> tuple[str, str] | None:
    """Extract one accepted Telegram card media file ID and its durable kind."""
    if message.video:
        return message.video.file_id, "video"
    if message.photo:
        return message.photo[-1].file_id, "photo"
    return None


@router.message(AddChar.waiting_photo, F.photo | F.video)
async def addchar_media(message: Message, state: FSMContext):
    if not await _can_manage_catalog(message):
        await state.clear()
        return
    media = _incoming_character_media(message)
    if media is None:
        await message.reply("📸 Please send a Telegram photo or 🎬 video.")
        return
    file_id, media_type = media
    await state.update_data(file_id=file_id, media_type=media_type)
    await state.set_state(AddChar.waiting_kind)
    await message.reply(
        "🪪 Is this art for a new character or an existing character?",
        reply_markup=_addchar_kind_keyboard(),
    )


@router.callback_query(AddChar.waiting_kind, F.data.startswith("addchar:kind:"))
async def addchar_kind(callback: CallbackQuery, state: FSMContext):
    """Route the uploaded photo/video to the normal or metadata-copy add flow."""
    if not await can_manage_catalog(callback.from_user.id):
        await state.clear()
        await callback.answer("⛔ Catalog admin only.", show_alert=True)
        return

    kind = (callback.data or "").rsplit(":", 1)[-1]
    if kind == "new":
        await state.update_data(add_mode="new")
        await state.set_state(AddChar.waiting_name)
        await _replace_preview(callback, "✅ <b>New character selected.</b>")
        if callback.message:
            await callback.message.reply("✏️ What is the character's name?")
        await callback.answer("New character")
        return

    if kind == "existing":
        await state.update_data(add_mode="existing")
        await state.set_state(AddChar.waiting_existing_query)
        await _replace_preview(callback, "✅ <b>Existing character selected.</b>")
        if callback.message:
            await callback.message.reply(
                "🔎 Send the existing character's exact name or one of its aliases."
            )
        await callback.answer("Search existing character")
        return

    await callback.answer("Invalid character type.", show_alert=True)


@router.message(AddChar.waiting_name, F.text)
async def addchar_name(message: Message, state: FSMContext):
    if not await _can_manage_catalog(message):
        await state.clear()
        return
    name = (message.text or "").strip()
    if not name:
        await message.reply("❌ The character name cannot be empty.")
        return
    await state.update_data(name=name)
    await state.set_state(AddChar.waiting_anime)
    await message.reply("📺 What anime or game is the character from?")


@router.message(AddChar.waiting_anime, F.text)
async def addchar_anime(message: Message, state: FSMContext):
    if not await _can_manage_catalog(message):
        await state.clear()
        return
    anime = (message.text or "").strip()
    if not anime:
        await message.reply("❌ The anime or game name cannot be empty.")
        return
    await state.update_data(anime=anime)
    await state.set_state(AddChar.waiting_rarity)
    await message.reply("⭐ Choose a rarity:", reply_markup=_rarity_keyboard())


@router.message(AddChar.waiting_existing_query, F.text)
async def addchar_existing_query(message: Message, state: FSMContext):
    """Search canonical catalog metadata after an existing-art image upload."""
    if not await _can_manage_catalog(message):
        await state.clear()
        return

    query = (message.text or "").strip()
    if not query:
        await message.reply("❌ Send an existing character name or alias.")
        return

    matches = await _find_existing_character_matches(query)
    if not matches:
        await message.reply(
            "❌ No existing character was found. Send another exact name or alias, "
            "or use <code>/cancel</code>.",
            parse_mode="HTML",
        )
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=_existing_character_button_label(character),
            callback_data=f"addchar:existing:{character['id']}",
            style="primary",
        )
    ] for character in matches])
    await message.reply(
        "✅ <b>Character found.</b> Choose the character whose name, anime/game, "
        "and aliases should be copied to this new art:",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


@router.callback_query(AddChar.waiting_existing_query, F.data.startswith("addchar:existing:"))
async def addchar_existing_selected(callback: CallbackQuery, state: FSMContext):
    """Copy only trusted existing metadata; the supplied photo remains new art."""
    if not await can_manage_catalog(callback.from_user.id):
        await state.clear()
        await callback.answer("⛔ Catalog admin only.", show_alert=True)
        return

    raw_id = (callback.data or "").rsplit(":", 1)[-1]
    if not raw_id.isdigit() or int(raw_id) <= 0:
        await callback.answer("Invalid character selection.", show_alert=True)
        return

    character = await _get_existing_character_metadata(int(raw_id))
    if not character:
        await callback.answer("That character is no longer available.", show_alert=True)
        return

    data = await state.get_data()
    if not data.get("file_id"):
        await state.clear()
        await callback.answer("This add session expired. Start /addcharacter again.", show_alert=True)
        return

    await state.update_data(
        add_mode="existing",
        name=character["name"],
        anime=character["anime"],
        aliases=character["aliases"] or "",
        copied_from_id=int(character["id"]),
    )
    await state.set_state(AddChar.waiting_rarity)
    await _replace_preview(
        callback,
        "✅ <b>Character found.</b>\n"
        f"Copying: <code>{character['id']}</code> — <b>{escape(character['name'])}</b>\n"
        f"📺 {escape(character['anime'])}\n"
        "The new art will receive its own database ID. Choose its rarity next.",
    )
    if callback.message:
        await callback.message.reply("⭐ Choose a rarity:", reply_markup=_rarity_keyboard())
    await callback.answer("Character metadata copied")


@router.callback_query(AddChar.waiting_rarity, F.data.startswith("char_rarity:"))
async def addchar_rarity(callback: CallbackQuery, state: FSMContext):
    if not await can_manage_catalog(callback.from_user.id):
        await state.clear()
        await callback.answer("⛔ Catalog admin only.", show_alert=True)
        return
    rarity = (callback.data or "").split(":", 1)[-1]
    if rarity not in VALID_RARITIES:
        await callback.answer("Invalid rarity.", show_alert=True)
        return

    data = await state.get_data()
    if rarity == LOVE_RARITY:
        # Love is intentionally an owner-only catalog action: it requires four
        # additional permanent level photos and an immutable player purchase
        # price, unlike an ordinary rarity dropdown selection.
        if not await is_bot_owner(callback.from_user.id):
            await callback.answer("⛔ Only a full owner can create Love cards.", show_alert=True)
            return
        if str(data.get("media_type") or "").casefold() != "photo":
            await callback.answer("Love Level 1 must be a Telegram photo.", show_alert=True)
            if callback.message:
                await callback.message.reply(
                    "❤️ Love cards use five photos. Start /addcharacter again and send a photo for Level 1."
                )
            await state.clear()
            return
        required = ("file_id", "name", "anime")
        if any(not data.get(key) for key in required):
            await state.clear()
            await callback.answer("This add session expired. Start /addcharacter again.", show_alert=True)
            return
        await state.update_data(
            rarity=LOVE_RARITY,
            love_level_1_file_id=str(data["file_id"]),
            love_next_level=2,
        )
        await state.set_state(AddChar.waiting_love_media)
        await _replace_preview(callback, "❤️ <b>Love card selected.</b>")
        if callback.message:
            await callback.message.reply(
                "❤️ <b>Love Level 2 of 5</b>\n\n"
                "Send the Level 2 Telegram <b>photo</b> now. Love cards require five photos: "
                "the original upload is Level 1, followed by Levels 2–5.",
                parse_mode="HTML",
            )
        await callback.answer("Love Level 2")
        return

    await state.update_data(rarity=rarity)
    if data.get("add_mode") == "existing":
        # Existing mode copied name/anime/aliases from a selected database row.
        # It deliberately skips free-text fields and asks only for rarity, then
        # lets the catalog admin choose an event (or explicit None).
        required = ("file_id", "media_type", "name", "anime", "aliases", "rarity")
        current = await state.get_data()
        if any(key not in current for key in required):
            await state.clear()
            await callback.answer("This add session expired. Start /addcharacter again.", show_alert=True)
            return
        if not callback.message:
            await state.clear()
            await callback.answer("This add session is unavailable.", show_alert=True)
            return
        await state.set_state(AddChar.waiting_event)
        await _prompt_addchar_event(callback.message)
        await callback.answer("Choose an event")
        return

    await state.set_state(AddChar.waiting_alias)
    if callback.message:
        await callback.message.reply(
            "🏷️ Send aliases separated by semicolons, or type <code>skip</code>.",
            parse_mode="HTML",
        )
    await callback.answer()


@router.message(AddChar.waiting_love_media, F.photo)
async def addchar_love_level_photo(message: Message, state: FSMContext):
    """Collect owner-supplied Love Level 2–5 art before accepting its price."""
    if not await _is_full_owner(message):
        await state.clear()
        return
    data = await state.get_data()
    level = data.get("love_next_level")
    if not isinstance(level, int) or level < 2 or level > 5:
        await state.clear()
        await message.reply("❌ This Love card session expired. Start /addcharacter again.")
        return
    if not message.photo:
        await message.reply("❤️ Please send a Telegram photo for this Love level.")
        return
    await state.update_data(**{f"love_level_{level}_file_id": message.photo[-1].file_id})
    if level < 5:
        next_level = level + 1
        await state.update_data(love_next_level=next_level)
        await message.reply(
            f"❤️ <b>Love Level {next_level} of 5</b>\n\n"
            f"Level {level} saved. Send the Level {next_level} Telegram <b>photo</b> now.",
            parse_mode="HTML",
        )
        return
    await state.update_data(love_next_level=6)
    await state.set_state(AddChar.waiting_love_price)
    await message.reply(
        "💎 <b>Love purchase price</b>\n\n"
        "Send the fixed Neko price players will pay in /lovelist. "
        "This exact initial price will also be used whenever a player sells this Love card.",
        parse_mode="HTML",
    )


@router.message(AddChar.waiting_love_media)
async def addchar_love_level_requires_photo(message: Message, state: FSMContext):
    if not await _is_full_owner(message):
        await state.clear()
        return
    await message.reply("❤️ Love levels must be Telegram photos. Send the requested photo or use /cancel.")


@router.message(AddChar.waiting_love_price, F.text)
async def addchar_love_price(message: Message, state: FSMContext):
    """Validate the immutable owner-set Lovelist price, then continue metadata."""
    if not await _is_full_owner(message):
        await state.clear()
        return
    raw_price = (message.text or "").strip().replace(",", "")
    try:
        price = Decimal(raw_price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        await message.reply("❌ Send a positive Neko price, for example <code>2500</code>.", parse_mode="HTML")
        return
    if not price.is_finite() or price <= 0 or price > Decimal("9999999999999999999.99"):
        await message.reply("❌ Send a valid positive Neko price within the wallet limit.")
        return
    data = await state.get_data()
    required = [f"love_level_{level}_file_id" for level in range(1, 6)]
    if any(not data.get(key) for key in required):
        await state.clear()
        await message.reply("❌ This Love card session expired. Start /addcharacter again.")
        return
    await state.update_data(love_base_price=str(price))
    if data.get("add_mode") == "existing":
        await state.set_state(AddChar.waiting_event)
        await _prompt_addchar_event(message)
        return
    await state.set_state(AddChar.waiting_alias)
    await message.reply(
        "🏷️ Send aliases separated by semicolons, or type <code>skip</code>.",
        parse_mode="HTML",
    )


@router.message(AddChar.waiting_alias, F.text)
async def addchar_alias(message: Message, state: FSMContext):
    if not await _can_manage_catalog(message):
        await state.clear()
        return
    aliases = "" if (message.text or "").strip().lower() == "skip" else (message.text or "").strip()
    data = await state.get_data()
    if data.get("add_mode") != "new":
        await state.clear()
        await message.reply("❌ This add session expired. Start <code>/addcharacter</code> again.", parse_mode="HTML")
        return
    await state.update_data(aliases=aliases)
    await state.set_state(AddChar.waiting_event)
    await _prompt_addchar_event(message)


@router.callback_query(AddChar.waiting_event, F.data.startswith("addchar:event:"))
async def addchar_event(callback: CallbackQuery, state: FSMContext):
    """Finish either add-char path after the catalog admin chooses its event."""
    if not await can_manage_catalog(callback.from_user.id):
        await state.clear()
        await callback.answer("⛔ Catalog admin only.", show_alert=True)
        return
    if not callback.message:
        await state.clear()
        await callback.answer("This add session is unavailable.", show_alert=True)
        return

    token = (callback.data or "").rsplit(":", 1)[-1]
    if token == "none":
        event = ""
    elif token.isdigit() and int(token) > 0:
        event = await _get_catalog_event(int(token))
        if event is None:
            await callback.answer("That event no longer exists. Choose another one.", show_alert=True)
            return
    else:
        await callback.answer("Invalid event selection.", show_alert=True)
        return

    await state.update_data(event=event)
    data = await state.get_data()
    aliases = str(data.get("aliases") or "")
    required = ("file_id", "media_type", "name", "anime", "rarity")
    if any(key not in data for key in required):
        await state.clear()
        await callback.answer("This add session expired. Start /addcharacter again.", show_alert=True)
        return
    added = await _finish_addchar(
        callback.message,
        state,
        data,
        aliases,
        actor_name=_catalog_actor_name(callback.from_user),
        actor_id=callback.from_user.id,
    )
    await callback.answer(
        "Character added" if added else "Event no longer exists",
        show_alert=not added,
    )

async def _insert_missing_upchar_record(data: dict, aliases: str) -> tuple[int, str]:
    """Create a card at a deliberately requested missing /updatecharacter code.

    This uses the normal add-character advisory lock so a concurrent /addcharacter
    cannot allocate the same new maximum ID between the original missing-ID
    lookup and this flow's final insert. The primary key remains the final
    authority for non-bot writers and is never overwritten on conflict.
    """
    character_id = data.get("update_id")
    if (
        not isinstance(character_id, int)
        or not 0 < character_id <= MAX_CATALOG_CHARACTER_ID
    ):
        raise ValueError("missing fixed character id")

    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1)", -611052346221)

            event = str(data.get("event") or "")
            if event:
                # Match /addcharacter's final live-event check. A label deleted or
                # renamed while this flow was open cannot be resurrected on a
                # new fixed-ID card.
                live_event = await conn.fetchrow(
                    """
                    SELECT label
                    FROM catalog_events
                    WHERE LOWER(BTRIM(label)) = LOWER(BTRIM($1))
                    FOR KEY SHARE
                    """,
                    event,
                )
                if live_event is None:
                    raise EventSelectionExpiredError
                event = str(live_event["label"])

            inserted = await conn.fetchrow(
                """
                INSERT INTO characters (
                    id, name, anime, rarity, rarity_score, aliases, image_url, media_type, event
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                ON CONFLICT (id) DO NOTHING
                RETURNING id
                """,
                character_id,
                data["name"],
                data["anime"],
                data["rarity"],
                RARITY_SCORES[data["rarity"]],
                aliases,
                data["file_id"],
                data["media_type"],
                event,
            )
            if inserted is None:
                raise FixedCharacterIdOccupiedError
    return character_id, event


async def _prompt_missing_upchar_event(message: Message, character_id: int) -> None:
    """Offer the same optional curated event selection for a new fixed-ID card."""
    owner = message.from_user
    if not owner:
        return
    events = await _list_catalog_events()
    await message.reply(
        "🎊 Choose an event for this new character, or choose None:",
        reply_markup=_event_keyboard(events, f"ucn:{owner.id}:{character_id}"),
    )


async def _prompt_existing_upchar_event(message: Message, character_id: int) -> None:
    """Make every ordinary /updatecharacter edit explicitly choose its event."""
    owner = message.from_user
    if not owner:
        return
    events = await _list_catalog_events()
    await message.reply(
        "🎊 Choose the event for this updated character, or choose None to clear it:",
        reply_markup=_event_keyboard(events, f"uce:{owner.id}:{character_id}"),
    )


async def _finish_missing_upchar(
    message: Message,
    state: FSMContext,
    *,
    actor_name: str | None = None,
) -> bool:
    """Insert the collected replacement card only while its requested ID is free."""
    data = await state.get_data()
    character_id = data.get("update_id")
    aliases = str(data.get("aliases") or "")
    required = ("file_id", "media_type", "name", "anime", "rarity", "update_mode")
    if (
        not isinstance(character_id, int)
        or data.get("update_mode") != "create_missing"
        or any(key not in data for key in required)
    ):
        await state.clear()
        await message.reply("❌ This fixed-code character session expired. Start <code>/updatecharacter</code> again.", parse_mode="HTML")
        return False

    try:
        inserted_id, event = await _insert_missing_upchar_record(data, aliases)
    except EventSelectionExpiredError:
        await state.clear()
        await message.reply(
            "❌ The selected event no longer exists. Start <code>/updatecharacter</code> again.",
            parse_mode="HTML",
        )
        return False
    except FixedCharacterIdOccupiedError:
        await state.clear()
        await message.reply(
            f"❌ Code <code>{character_id}</code> was filled while you were editing. "
            "Review it with <code>/updatecharacter</code> first; no existing card was overwritten.",
            parse_mode="HTML",
        )
        return False

    await state.clear()
    await reload_character_cache()
    await _publish_new_catalog_art(
        message,
        {
            "id": inserted_id,
            "name": data["name"],
            "anime": data["anime"],
            "rarity": data["rarity"],
            "aliases": aliases,
            "image_url": data["file_id"],
            "media_type": data["media_type"],
            "event": event,
        },
        actor_name=actor_name,
    )
    await message.reply(
        "✅ <b>Character created at the requested code!</b>\n"
        + _character_caption(
            inserted_id,
            str(data["name"]),
            str(data["anime"]),
            str(data["rarity"]),
            aliases,
            event,
        ),
        parse_mode="HTML",
    )
    return True


# ─── /updatecharacter ─────────────────────────────────────────────────────

@router.message(Command("updatecharacter"))
async def cmd_updatecharacter(message: Message, state: FSMContext):
    if not await _can_manage_catalog(message):
        return

    parts = (message.text or "").split(maxsplit=1)
    raw_id = parts[1].strip() if len(parts) == 2 else ""
    if (
        not raw_id.isascii()
        or not raw_id.isdigit()
        or len(raw_id) > 19
        or not 0 < int(raw_id) <= MAX_CATALOG_CHARACTER_ID
    ):
        await message.reply(
            "❌ Usage: <code>/updatecharacter &lt;positive_character_id&gt;</code>",
            parse_mode="HTML",
        )
        return

    character_id = int(raw_id)
    pool = get_pool()
    async with pool.acquire() as conn:
        character = await conn.fetchrow(
            "SELECT id, name, anime, rarity, aliases, image_url, "
            "COALESCE(media_type, 'photo') AS media_type, "
            "COALESCE(event, '') AS event FROM characters WHERE id = $1",
            character_id,
        )

    if not character:
        # A deleted card leaves a deliberately reusable catalog code. Rather
        # than forcing an unrelated auto-assigned /addcharacter ID, turn /updatecharacter
        # into a fixed-code create flow for any currently missing positive ID.
        owner = message.from_user
        if not owner:
            return
        await state.clear()
        await state.update_data(
            update_id=character_id,
            update_mode="create_missing",
            update_owner_id=owner.id,
        )
        await state.set_state(UpdateChar.waiting_photo)
        await message.reply(
            f"ℹ️ Code <code>{character_id}</code> is empty. Send the character photo or video to "
            "create a new card at this exact code.",
            parse_mode="HTML",
        )
        return

    if str(character["rarity"] or "").casefold() == LOVE_RARITY.casefold():
        await message.reply(
            "❤️ Love cards keep five linked level photos and a fixed Lovelist price. "
            "They cannot be replaced through /updatecharacter."
        )
        return

    caption = _character_caption(
        character["id"],
        character["name"],
        character["anime"],
        character["rarity"],
        character["aliases"] or "",
        character["event"] or "",
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="✅ Edit",
                callback_data=f"update:edit:{character_id}",
            ),
            InlineKeyboardButton(text="❌ Cancel", callback_data="update:cancel"),
        ]]
    )

    if await _reply_character_preview(
        message,
        character=dict(character),
        caption=caption,
        reply_markup=keyboard,
    ):
        return
    await message.reply(caption, parse_mode="HTML", reply_markup=keyboard)


@router.callback_query(F.data.startswith("update:"))
async def cb_update(callback: CallbackQuery, state: FSMContext):
    if not await can_manage_catalog(callback.from_user.id):
        await callback.answer("⛔ Catalog admin only.", show_alert=True)
        return

    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "cancel":
        await state.clear()
        await _replace_preview(callback, "❌ <b>Cancelled.</b>")
        await callback.answer("Cancelled")
        return

    if action != "edit" or len(parts) != 3 or not parts[2].isdigit():
        await callback.answer("Invalid action.", show_alert=True)
        return

    await state.clear()
    await state.update_data(
        update_id=int(parts[2]),
        update_mode="edit_existing",
        update_owner_id=callback.from_user.id,
    )
    await state.set_state(UpdateChar.waiting_photo)
    if callback.message:
        await callback.message.reply("📸 Send the new character photo or 🎬 Telegram video.")
    await callback.answer()


@router.message(UpdateChar.waiting_photo, F.photo | F.video)
async def update_media(message: Message, state: FSMContext):
    if not await _can_manage_catalog(message):
        await state.clear()
        return
    media = _incoming_character_media(message)
    if media is None:
        await message.reply("📸 Please send a Telegram photo or 🎬 video.")
        return
    file_id, media_type = media
    await state.update_data(file_id=file_id, media_type=media_type)
    await state.set_state(UpdateChar.waiting_name)
    await message.reply("✏️ What is the new character name?")


@router.message(UpdateChar.waiting_name, F.text)
async def update_name(message: Message, state: FSMContext):
    if not await _can_manage_catalog(message):
        await state.clear()
        return
    name = (message.text or "").strip()
    if not name:
        await message.reply("❌ The character name cannot be empty.")
        return
    await state.update_data(name=name)
    await state.set_state(UpdateChar.waiting_anime)
    await message.reply("📺 What is the new anime or game name?")


@router.message(UpdateChar.waiting_anime, F.text)
async def update_anime(message: Message, state: FSMContext):
    if not await _can_manage_catalog(message):
        await state.clear()
        return
    anime = (message.text or "").strip()
    if not anime:
        await message.reply("❌ The anime or game name cannot be empty.")
        return
    await state.update_data(anime=anime)
    await state.set_state(UpdateChar.waiting_rarity)
    await message.reply("⭐ Choose a rarity:", reply_markup=_rarity_keyboard(include_love=False))


@router.callback_query(UpdateChar.waiting_rarity, F.data.startswith("char_rarity:"))
async def update_rarity(callback: CallbackQuery, state: FSMContext):
    if not callback.from_user or not await can_manage_catalog(callback.from_user.id):
        await state.clear()
        await callback.answer("⛔ Catalog admin only.", show_alert=True)
        return
    rarity = (callback.data or "").split(":", 1)[-1]
    if rarity not in VALID_RARITIES or rarity == LOVE_RARITY:
        await callback.answer("Love cards can only be created through /addcharacter.", show_alert=True)
        return
    await state.update_data(rarity=rarity)
    await state.set_state(UpdateChar.waiting_alias)
    if callback.message:
        await callback.message.reply(
            "🏷️ Send new aliases separated by semicolons, or type <code>skip</code>.",
            parse_mode="HTML",
        )
    await callback.answer()


@router.message(UpdateChar.waiting_alias, F.text)
async def update_alias(message: Message, state: FSMContext):
    if not await _can_manage_catalog(message):
        await state.clear()
        return
    aliases = "" if (message.text or "").strip().lower() == "skip" else (message.text or "").strip()
    data = await state.get_data()
    character_id = data.get("update_id")
    mode = data.get("update_mode")
    if not isinstance(character_id, int) or mode not in {"edit_existing", "create_missing"}:
        await state.clear()
        await message.reply("❌ This /updatecharacter session expired. Start <code>/updatecharacter</code> again.", parse_mode="HTML")
        return

    await state.update_data(aliases=aliases)
    await state.set_state(UpdateChar.waiting_event)
    if mode == "create_missing":
        # Fixed-code creation follows the same core metadata collection as an
        # edit, then gets an optional curated event/None choice before insert.
        await _prompt_missing_upchar_event(message, character_id)
    else:
        await _prompt_existing_upchar_event(message, character_id)


@router.callback_query(UpdateChar.waiting_event, F.data.startswith("ucn:"))
async def fixed_code_upchar_event(callback: CallbackQuery, state: FSMContext):
    """Finish a missing-code /updatecharacter flow after its optional event choice."""
    if not callback.from_user or not await can_manage_catalog(callback.from_user.id):
        await state.clear()
        await callback.answer("⛔ Catalog admin only.", show_alert=True)
        return
    if not callback.message:
        await state.clear()
        await callback.answer("This fixed-code character session is unavailable.", show_alert=True)
        return

    parts = (callback.data or "").split(":")
    if (
        len(parts) != 4
        or parts[0] != "ucn"
        or not all(part.isascii() and part.isdigit() and int(part) > 0 for part in parts[1:3])
    ):
        await callback.answer("Invalid fixed-code event selection.", show_alert=True)
        return
    owner_id, character_id = int(parts[1]), int(parts[2])
    token = parts[3]
    data = await state.get_data()
    if (
        owner_id != callback.from_user.id
        or data.get("update_owner_id") != owner_id
        or data.get("update_id") != character_id
        or data.get("update_mode") != "create_missing"
    ):
        await state.clear()
        await callback.answer("This fixed-code character session is not yours or has expired.", show_alert=True)
        return

    if token == "none":
        event = ""
    elif token.isascii() and token.isdigit() and int(token) > 0:
        event = await _get_catalog_event(int(token))
        if event is None:
            await callback.answer("That event no longer exists. Choose another one.", show_alert=True)
            return
    else:
        await callback.answer("Invalid event selection.", show_alert=True)
        return

    await state.update_data(event=event)
    created = await _finish_missing_upchar(
        callback.message,
        state,
        actor_name=_catalog_actor_name(callback.from_user),
    )
    await callback.answer(
        "Character created at requested code" if created else "Character was not created",
        show_alert=not created,
    )


async def _finish_existing_upchar(
    message: Message,
    state: FSMContext,
    *,
    event_token: str,
    actor_name: str | None = None,
) -> tuple[bool, str]:
    """Commit an existing-card /updatecharacter edit with its chosen event."""
    data = await state.get_data()
    character_id = data.get("update_id")
    aliases = str(data.get("aliases") or "")
    required = ("file_id", "media_type", "name", "anime", "rarity", "update_mode")
    if (
        not isinstance(character_id, int)
        or data.get("update_mode") != "edit_existing"
        or any(key not in data for key in required)
    ):
        await state.clear()
        await message.reply(
            "❌ This /updatecharacter session expired. Start <code>/updatecharacter</code> again.",
            parse_mode="HTML",
        )
        return False, "expired"

    pool = get_pool()
    previous = None
    current = None
    async with pool.acquire() as conn:
        async with conn.transaction():
            previous = await conn.fetchrow(
                """
                SELECT id, name, anime, rarity, aliases, image_url,
                       COALESCE(media_type, 'photo') AS media_type,
                       COALESCE(event, '') AS event
                FROM characters
                WHERE id = $1
                FOR UPDATE
                """,
                character_id,
            )
            if previous is None:
                return False, "not_found"

            if event_token == "none":
                event = ""
            elif event_token.isascii() and event_token.isdigit() and int(event_token) > 0:
                live_event = await conn.fetchrow(
                    """
                    SELECT label
                    FROM catalog_events
                    WHERE id = $1
                    FOR KEY SHARE
                    """,
                    int(event_token),
                )
                if live_event is None:
                    return False, "event_missing"
                event = str(live_event["label"])
            else:
                return False, "invalid_event"

            current = await conn.fetchrow(
                """
                UPDATE characters
                SET name = $2,
                    anime = $3,
                    rarity = $4,
                    rarity_score = $5,
                    aliases = $6,
                    image_url = $7,
                    media_type = $8,
                    event = $9
                WHERE id = $1
                RETURNING id, name, anime, rarity, aliases, image_url,
                          COALESCE(media_type, 'photo') AS media_type,
                          COALESCE(event, '') AS event
                """,
                character_id,
                data["name"],
                data["anime"],
                data["rarity"],
                RARITY_SCORES[data["rarity"]],
                aliases,
                data["file_id"],
                data["media_type"],
                event,
            )

    if current is None or previous is None:
        await state.clear()
        return False, "not_found"

    await state.clear()
    current_data = dict(current)
    await reload_character_cache()
    await _publish_catalog_change_log(
        message,
        action="Updated character information",
        character=current_data,
        previous=dict(previous),
        actor_name=actor_name,
    )
    await message.reply(
        "✅ <b>Character updated!</b>\n"
        + _character_caption(
            current_data["id"],
            current_data["name"],
            current_data["anime"],
            current_data["rarity"],
            current_data["aliases"] or "",
            current_data["event"] or "",
        ),
        parse_mode="HTML",
    )
    return True, "ok"


@router.callback_query(UpdateChar.waiting_event, F.data.startswith("uce:"))
async def existing_upchar_event(callback: CallbackQuery, state: FSMContext) -> None:
    """Apply the explicit event selection at the end of an existing-card update."""
    if not callback.from_user or not await can_manage_catalog(callback.from_user.id):
        await state.clear()
        await callback.answer("⛔ Catalog admin only.", show_alert=True)
        return
    if not callback.message:
        await state.clear()
        await callback.answer("This character update session is unavailable.", show_alert=True)
        return

    parts = (callback.data or "").split(":")
    if (
        len(parts) != 4
        or parts[0] != "uce"
        or not all(part.isascii() and part.isdigit() and int(part) > 0 for part in parts[1:3])
    ):
        await callback.answer("Invalid character event selection.", show_alert=True)
        return
    owner_id, character_id, event_token = int(parts[1]), int(parts[2]), parts[3]
    data = await state.get_data()
    if (
        owner_id != callback.from_user.id
        or data.get("update_owner_id") != owner_id
        or data.get("update_id") != character_id
        or data.get("update_mode") != "edit_existing"
    ):
        await state.clear()
        await callback.answer("This character update session is not yours or has expired.", show_alert=True)
        return

    updated, reason = await _finish_existing_upchar(
        callback.message,
        state,
        event_token=event_token,
        actor_name=_catalog_actor_name(callback.from_user),
    )
    if reason == "event_missing":
        await callback.answer("That event no longer exists. Choose another one.", show_alert=True)
        return
    if reason == "invalid_event":
        await state.clear()
        await callback.answer("Invalid event selection.", show_alert=True)
        return
    if reason == "not_found":
        await state.clear()
        await callback.answer("This character no longer exists.", show_alert=True)
        return
    await callback.answer("Character updated" if updated else "Character was not updated", show_alert=not updated)


# ─── Focused owner-only field changes ─────────────────────────────

def _quick_rarity_keyboard() -> InlineKeyboardMarkup:
    """Use callbacks separate from /addcharacter and /updatecharacter rarity pickers."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text=rarity,
                callback_data=f"quickchange:rarity:{rarity}",
                style="primary",
            )
        ] for rarity in VALID_RARITIES if rarity != LOVE_RARITY]
    )


def _split_quick_change_command(message: Message) -> tuple[int | None, str | None]:
    """Read `/namechange <id> [new value]` while preserving values with spaces."""
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 2 or not parts[1].isdigit() or int(parts[1]) <= 0:
        return None, None
    return int(parts[1]), (parts[2].strip() if len(parts) > 2 else None)


def _prepare_quick_change_value(field: str, raw_value: str) -> tuple[str | None, str | None]:
    """Validate one replacement value and canonicalize rarity spelling."""
    value = raw_value.strip()
    if field == "name":
        return (value, None) if value else (None, "❌ The character name cannot be empty.")
    if field == "anime":
        return (value, None) if value else (None, "❌ The anime or game name cannot be empty.")
    if field == "aliases":
        # Match the established /addcharacter and /updatecharacter convention: `skip`
        # intentionally clears aliases while ordinary text is stored exactly.
        return ("" if value.casefold() == "skip" else value, None) if value else (
            None,
            "❌ Send aliases or type <code>skip</code> to clear them.",
        )
    if field == "rarity":
        canonical = {rarity.casefold(): rarity for rarity in VALID_RARITIES}
        selected = canonical.get(value.casefold())
        if selected and selected != LOVE_RARITY:
            return selected, None
        return None, "❌ Invalid rarity. Choose one from the buttons."
    return None, "❌ Invalid character field."


async def _get_quick_change_character(character_id: int):
    pool = get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            """
            SELECT id, name, anime, rarity, aliases, image_url,
                   COALESCE(media_type, 'photo') AS media_type,
                   COALESCE(event, '') AS event
            FROM characters
            WHERE id = $1
            """,
            character_id,
        )


async def _apply_quick_change(
    message: Message,
    state: FSMContext,
    *,
    character_id: int,
    field: str,
    value: str,
    label: str,
    actor_name: str | None = None,
) -> None:
    """Update one approved field and audit the exact before/after card state."""
    await state.clear()

    # Keep the statements separate: building a mapping eagerly would also
    # evaluate the rarity-score lookup for a name/anime/alias replacement.
    if field == "name":
        sql = """
            UPDATE characters
            SET name = $2
            WHERE id = $1
            RETURNING id, name, anime, rarity, aliases, image_url,
                      COALESCE(media_type, 'photo') AS media_type,
                      COALESCE(event, '') AS event
        """
        params = (character_id, value)
    elif field == "anime":
        sql = """
            UPDATE characters
            SET anime = $2
            WHERE id = $1
            RETURNING id, name, anime, rarity, aliases, image_url,
                      COALESCE(media_type, 'photo') AS media_type,
                      COALESCE(event, '') AS event
        """
        params = (character_id, value)
    elif field == "rarity":
        sql = """
            UPDATE characters
            SET rarity = $2, rarity_score = $3
            WHERE id = $1
            RETURNING id, name, anime, rarity, aliases, image_url,
                      COALESCE(media_type, 'photo') AS media_type,
                      COALESCE(event, '') AS event
        """
        params = (character_id, value, RARITY_SCORES[value])
    elif field == "aliases":
        sql = """
            UPDATE characters
            SET aliases = $2
            WHERE id = $1
            RETURNING id, name, anime, rarity, aliases, image_url,
                      COALESCE(media_type, 'photo') AS media_type,
                      COALESCE(event, '') AS event
        """
        params = (character_id, value)
    else:
        await message.reply("❌ Invalid character field.")
        return

    pool = get_pool()
    previous = None
    character = None
    love_rarity_protected = False
    async with pool.acquire() as conn:
        async with conn.transaction():
            previous = await conn.fetchrow(
                """
                SELECT id, name, anime, rarity, aliases, image_url,
                       COALESCE(media_type, 'photo') AS media_type,
                       COALESCE(event, '') AS event
                FROM characters
                WHERE id = $1
                FOR UPDATE
                """,
                character_id,
            )
            # Inline /rchange invokes this common writer directly, so enforce
            # the add-only Love invariant here rather than trusting its UI
            # picker or only the callback path.
            if previous and field == "rarity" and str(previous["rarity"] or "").casefold() == LOVE_RARITY.casefold():
                love_rarity_protected = True
            elif previous:
                character = await conn.fetchrow(sql, *params)

    if love_rarity_protected:
        await message.reply("❤️ Love cards cannot be converted with /raritychange.")
        return
    if not character or not previous:
        await message.reply("❌ This character no longer exists.")
        return

    current = dict(character)
    before = dict(previous)
    await reload_character_cache()
    await _publish_catalog_change_log(
        message,
        action=f"Changed {label}",
        character=current,
        previous=before,
        actor_name=actor_name,
        changed_fields=(field,),
    )
    await message.reply(
        f"✅ <b>{label} updated!</b>\n"
        + _character_caption(
            current["id"],
            current["name"],
            current["anime"],
            current["rarity"],
            current["aliases"] or "",
            current["event"] or "",
        ),
        parse_mode="HTML",
    )


async def _begin_quick_change(
    message: Message,
    state: FSMContext,
    *,
    command: str,
    field: str,
    label: str,
    next_state: State,
    prompt: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Start a one-field flow, or accept an optional same-line new value."""
    if not await _is_full_owner(message):
        return

    character_id, inline_value = _split_quick_change_command(message)
    if character_id is None:
        await message.reply(
            f"❌ Usage: <code>/{command} &lt;character_id&gt; [new value]</code>",
            parse_mode="HTML",
        )
        return

    if inline_value is not None:
        value, error = _prepare_quick_change_value(field, inline_value)
        if error:
            await message.reply(error, parse_mode="HTML")
            return
        assert value is not None
        await _apply_quick_change(
            message,
            state,
            character_id=character_id,
            field=field,
            value=value,
            label=label,
        )
        return

    character = await _get_quick_change_character(character_id)
    if not character:
        await message.reply(
            f"❌ No character with ID <code>{character_id}</code> was found.",
            parse_mode="HTML",
        )
        return

    await state.clear()
    await state.update_data(quick_change_id=character_id)
    await state.set_state(next_state)
    preview = (
        f"✏️ <b>Change {escape(label)}</b>\n\n"
        + _character_caption(
            character["id"],
            character["name"],
            character["anime"],
            character["rarity"],
            character["aliases"] or "",
        )
        + f"\n\n{prompt}"
    )
    # Show the current photo or video when Telegram accepts it, while retaining
    # a reliable text fallback for a stale file ID.
    if await _reply_character_preview(
        message,
        character=dict(character),
        caption=preview,
        reply_markup=reply_markup,
    ):
        return
    await message.reply(preview, parse_mode="HTML", reply_markup=reply_markup)


async def _finish_quick_text_change(
    message: Message,
    state: FSMContext,
    *,
    field: str,
    label: str,
) -> None:
    """Validate the next owner message and write only its selected field."""
    if not await _is_full_owner(message):
        await state.clear()
        return

    value, error = _prepare_quick_change_value(field, message.text or "")
    if error:
        await message.reply(error, parse_mode="HTML")
        return

    data = await state.get_data()
    character_id = data.get("quick_change_id")
    if not isinstance(character_id, int):
        await state.clear()
        await message.reply("❌ This edit session expired. Start the command again.")
        return

    assert value is not None
    await _apply_quick_change(
        message,
        state,
        character_id=character_id,
        field=field,
        value=value,
        label=label,
    )


@router.message(Command("photochange"))
async def cmd_photochange(message: Message, state: FSMContext):
    """Owner-only: start a photo-or-video update for exactly one character card."""
    if not await _is_full_owner(message):
        return

    parts = (message.text or "").split(maxsplit=1)
    raw_id = parts[1].strip() if len(parts) > 1 else ""
    if not raw_id.isdigit() or int(raw_id) <= 0:
        await message.reply(
            "❌ Usage: <code>/photochange &lt;character_id&gt;</code>",
            parse_mode="HTML",
        )
        return

    character_id = int(raw_id)
    character = await _get_quick_change_character(character_id)
    if not character:
        await message.reply(
            f"❌ No character with ID <code>{character_id}</code> was found.",
            parse_mode="HTML",
        )
        return
    if str(character["rarity"] or "").casefold() == LOVE_RARITY.casefold():
        await message.reply(
            "❤️ Love card media is level-linked. Use a dedicated Love catalog maintenance flow "
            "instead of /photochange so Levels 1–5 remain consistent."
        )
        return

    await state.clear()
    await state.update_data(quick_photo_change_id=character_id)
    await state.set_state(QuickChange.waiting_photo)
    await message.reply(
        "🖼️ <b>Change Character Media</b>\n\n"
        + _character_caption(
            character["id"],
            character["name"],
            character["anime"],
            character["rarity"],
            character["aliases"] or "",
        )
        + "\n\nSend the new card photo or Telegram video now. Only this card's media will be updated.",
        parse_mode="HTML",
    )


@router.message(QuickChange.waiting_photo, F.photo | F.video)
async def quick_change_media(message: Message, state: FSMContext):
    """Save the Telegram photo/video file ID selected through /photochange."""
    if not await _is_full_owner(message):
        await state.clear()
        return

    data = await state.get_data()
    character_id = data.get("quick_photo_change_id")
    if not isinstance(character_id, int):
        await state.clear()
        await message.reply("❌ This media edit session expired. Start /photochange again.")
        return

    media = _incoming_character_media(message)
    if media is None:
        await message.reply("📸 Please send a Telegram photo or 🎬 video, or use /cancel to stop.")
        return
    file_id, media_type = media
    pool = get_pool()
    previous = None
    character = None
    async with pool.acquire() as conn:
        async with conn.transaction():
            previous = await conn.fetchrow(
                """
                SELECT id, name, anime, rarity, aliases, image_url,
                       COALESCE(media_type, 'photo') AS media_type,
                       COALESCE(event, '') AS event
                FROM characters
                WHERE id = $1
                FOR UPDATE
                """,
                character_id,
            )
            if previous:
                character = await conn.fetchrow(
                    """
                    UPDATE characters
                    SET image_url = $2,
                        media_type = $3
                    WHERE id = $1
                    RETURNING id, name, anime, rarity, aliases, image_url,
                              COALESCE(media_type, 'photo') AS media_type,
                              COALESCE(event, '') AS event
                    """,
                    character_id,
                    file_id,
                    media_type,
                )
    await state.clear()

    if not character or not previous:
        await message.reply("❌ This character no longer exists.")
        return

    current = dict(character)
    await reload_character_cache()
    await _publish_catalog_change_log(
        message,
        action="Changed character media",
        character=current,
        previous=dict(previous),
        changed_fields=("media_type", "image_url"),
    )
    await message.reply(
        "✅ <b>Character media updated!</b>\n"
        + _character_caption(
            current["id"],
            current["name"],
            current["anime"],
            current["rarity"],
            current["aliases"] or "",
            current["event"] or "",
        ),
        parse_mode="HTML",
    )


@router.message(QuickChange.waiting_photo)
async def quick_change_media_requires_media(message: Message, state: FSMContext):
    """Require a Telegram photo/video without modifying the target card otherwise."""
    if not await _is_full_owner(message):
        await state.clear()
        return
    await message.reply("📸 Please send a Telegram photo or 🎬 video, or use /cancel to stop this media update.")


@router.message(Command("namechange"))
async def cmd_namechange(message: Message, state: FSMContext):
    """Owner-only: change only a character name."""
    await _begin_quick_change(
        message,
        state,
        command="nchange",
        field="name",
        label="character name",
        next_state=QuickChange.waiting_name,
        prompt="✏️ Send the new character name.",
    )


@router.message(Command("animechange"))
async def cmd_animechange(message: Message, state: FSMContext):
    """Owner-only: change only an anime/game field."""
    await _begin_quick_change(
        message,
        state,
        command="achange",
        field="anime",
        label="anime or game",
        next_state=QuickChange.waiting_anime,
        prompt="📺 Send the new anime or game name.",
    )


@router.message(Command("raritychange"))
async def cmd_raritychange(message: Message, state: FSMContext):
    """Owner-only: change only rarity and its matching score."""
    await _begin_quick_change(
        message,
        state,
        command="rchange",
        field="rarity",
        label="rarity",
        next_state=QuickChange.waiting_rarity,
        prompt="⭐ Choose the new rarity below.",
        reply_markup=_quick_rarity_keyboard(),
    )


@router.message(Command("aliaschange"))
async def cmd_aliaschange(message: Message, state: FSMContext):
    """Owner-only: change only semicolon-separated aliases."""
    await _begin_quick_change(
        message,
        state,
        command="lchange",
        field="aliases",
        label="aliases",
        next_state=QuickChange.waiting_alias,
        prompt="🏷️ Send aliases separated by semicolons, or type <code>skip</code> to clear them.",
    )


@router.message(QuickChange.waiting_name, F.text)
async def quick_change_name(message: Message, state: FSMContext):
    await _finish_quick_text_change(message, state, field="name", label="Character name")


@router.message(QuickChange.waiting_anime, F.text)
async def quick_change_anime(message: Message, state: FSMContext):
    await _finish_quick_text_change(message, state, field="anime", label="Anime or game")


@router.message(QuickChange.waiting_alias, F.text)
async def quick_change_alias(message: Message, state: FSMContext):
    await _finish_quick_text_change(message, state, field="aliases", label="Aliases")


@router.callback_query(QuickChange.waiting_rarity, F.data.startswith("quickchange:rarity:"))
async def quick_change_rarity(callback: CallbackQuery, state: FSMContext):
    """Apply the one selected rarity without touching other character data."""
    if not await is_bot_owner(callback.from_user.id):
        await state.clear()
        await callback.answer("⛔ Owner only.", show_alert=True)
        return

    rarity = (callback.data or "").rsplit(":", 1)[-1]
    if rarity not in VALID_RARITIES or rarity == LOVE_RARITY:
        await callback.answer("Love cannot be set through a rarity change.", show_alert=True)
        return

    data = await state.get_data()
    character_id = data.get("quick_change_id")
    if not isinstance(character_id, int) or not callback.message:
        await state.clear()
        await callback.answer("This edit session expired.", show_alert=True)
        return

    current_character = await _get_quick_change_character(character_id)
    if current_character and str(current_character["rarity"] or "").casefold() == LOVE_RARITY.casefold():
        await state.clear()
        await callback.answer("Love cards cannot be converted with /raritychange.", show_alert=True)
        return

    await _apply_quick_change(
        callback.message,
        state,
        character_id=character_id,
        field="rarity",
        value=rarity,
        label="Rarity",
        actor_name=_catalog_actor_name(callback.from_user),
    )
    await callback.answer("Rarity updated")


# ─── /deletecharacter ────────────────────────────────────────────────────

@router.message(Command("deletecharacter"))
async def cmd_deletecharacter(message: Message):
    """Show a full-owner confirmed global purge for one catalog character."""
    if not await _is_full_owner(message):
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        await message.reply(
            "❌ Usage: <code>/deletecharacter &lt;character_id&gt;</code>",
            parse_mode="HTML",
        )
        return

    character_id = int(parts[1].strip())
    pool = get_pool()
    async with pool.acquire() as conn:
        character = await conn.fetchrow(
            "SELECT id, name, anime, rarity, aliases, image_url, "
            "COALESCE(media_type, 'photo') AS media_type, "
            "COALESCE(event, '') AS event "
            "FROM characters WHERE id = $1",
            character_id,
        )

    if not character:
        await message.reply(
            f"❌ No character with ID <code>{character_id}</code> was found.",
            parse_mode="HTML",
        )
        return

    # The confirmation makes an irreversible global catalog purge deliberate.
    # Confirmation deletes every live collection copy and recovery-backup item
    # for this card rather than refusing because players already own it.
    caption = (
        "⚠️ <b>Delete this character everywhere?</b>\n\n"
        + _character_caption(
            character["id"],
            character["name"],
            character["anime"],
            character["rarity"],
            character["aliases"] or "",
            character["event"] or "",
        )
        + "\n\n<b>This is permanent.</b> It removes this card from every "
          "player collection and recovery backup, clears active/announced spawns, "
          "cancels pending gifts and owner grants containing it, removes related "
          "trade records, and then deletes the catalog card."
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="🗑️ Delete permanently",
                callback_data=f"delchar:confirm:{character_id}",
            ),
            InlineKeyboardButton(text="❌ Cancel", callback_data="delchar:cancel"),
        ]]
    )

    if await _reply_character_preview(
        message,
        character=dict(character),
        caption=caption,
        reply_markup=keyboard,
    ):
        return
    await message.reply(caption, parse_mode="HTML", reply_markup=keyboard)


async def _purge_catalog_character_references(
    conn,
    *,
    character_id: int,
    rarity_score: int,
) -> dict[str, object]:
    """Remove every live/dependent use of one globally deleted catalog card.

    The caller already holds ``FOR UPDATE`` on the character row.  Delete
    dependent rows before the catalog row so normal foreign keys remain valid.
    Live player statistics are reconciled from the surviving ranked harem rows.
    Recovery snapshots retain their other cards and their aggregate group stats
    are reduced by each removed ranked copy's saved group context, so restoring
    a changed snapshot cannot recreate that card's contribution.
    """
    # A catalog-preserving /resetcharacter must also erase Love progression
    # and escrow state; catalog deletion would cascade this later, but reset
    # intentionally leaves the Love definition available in /lovelist.
    love_state_rows = await conn.fetch(
        """
        DELETE FROM love_player_states
        WHERE character_id = $1
        RETURNING user_id
        """,
        character_id,
    )
    active_spawn_rows = await conn.fetch(
        """
        DELETE FROM active_spawns
        WHERE character_id = $1
        RETURNING chat_id
        """,
        character_id,
    )
    scheduled_op_rows = await conn.fetch(
        """
        DELETE FROM scheduled_op_spawns
        WHERE character_id = $1
        RETURNING chat_id
        """,
        character_id,
    )
    harem_rows = await conn.fetch(
        """
        DELETE FROM harem
        WHERE character_id = $1
        RETURNING user_id, chat_id, is_ranked
        """,
        character_id,
    )

    # Reconcile only ranked copies. Visible-only /sudo all - inventory never
    # changes a player's ranking or group leaderboard totals.
    ranked_user_ids: set[int] = set()
    ranked_removals: dict[tuple[int, int], int] = {}
    for row in harem_rows:
        if not bool(row["is_ranked"]):
            continue
        user_id = int(row["user_id"])
        chat_id = int(row["chat_id"])
        ranked_user_ids.add(user_id)
        key = (user_id, chat_id)
        ranked_removals[key] = ranked_removals.get(key, 0) + 1

    if ranked_user_ids:
        await conn.execute(
            """
            UPDATE users AS u
            SET total_claims = (
                    SELECT COUNT(*)::INT
                    FROM harem AS h
                    WHERE h.user_id = u.user_id AND h.is_ranked = TRUE
                ),
                highest_rarity = (
                    SELECT c.rarity
                    FROM harem AS h
                    JOIN characters AS c ON c.id = h.character_id
                    WHERE h.user_id = u.user_id AND h.is_ranked = TRUE
                    ORDER BY c.rarity_score DESC, c.id ASC, h.id ASC
                    LIMIT 1
                )
            WHERE u.user_id = ANY($1::BIGINT[])
            """,
            sorted(ranked_user_ids),
        )

    if ranked_removals:
        # A ranked harem row carries its original group context. This follows
        # the established /doomcharacter-copy accounting behavior, while a group
        # total is recalculated from the resulting per-user rows below.
        adjustments = [
            (
                user_id,
                chat_id,
                removed_count,
                removed_count * max(0, int(rarity_score)),
            )
            for (user_id, chat_id), removed_count in sorted(ranked_removals.items())
        ]
        await conn.executemany(
            """
            UPDATE user_group_stats
            SET claims = GREATEST(claims - $3, 0),
                rarity_score = GREATEST(rarity_score - $4, 0)
            WHERE user_id = $1 AND chat_id = $2
            """,
            adjustments,
        )
        await conn.executemany(
            """
            DELETE FROM user_group_stats
            WHERE user_id = $1 AND chat_id = $2 AND claims <= 0
            """,
            [(user_id, chat_id) for user_id, chat_id in sorted(ranked_removals)],
        )
        affected_group_ids = sorted({chat_id for _user_id, chat_id in ranked_removals})
        await conn.execute(
            """
            UPDATE group_stats AS g
            SET total_claims = COALESCE(
                (
                    SELECT SUM(s.claims)
                    FROM user_group_stats AS s
                    WHERE s.chat_id = g.chat_id
                ),
                0
            )
            WHERE g.chat_id = ANY($1::BIGINT[])
            """,
            affected_group_ids,
        )

    backup_item_rows = await conn.fetch(
        """
        DELETE FROM harem_deletion_backup_items
        WHERE character_id = $1
        RETURNING backup_id, chat_id, is_ranked
        """,
        character_id,
    )
    affected_backup_ids = sorted({int(row["backup_id"]) for row in backup_item_rows})
    backup_ranked_removals: dict[tuple[int, int], int] = {}
    for row in backup_item_rows:
        if not bool(row["is_ranked"]):
            continue
        key = (int(row["backup_id"]), int(row["chat_id"]))
        backup_ranked_removals[key] = backup_ranked_removals.get(key, 0) + 1

    if backup_ranked_removals:
        # /deleteharem stores per-group aggregates alongside exact backup items.
        # Subtract each removed ranked item from its saved group so the other
        # snapshot cards retain their original restoration behavior.
        backup_stat_adjustments = [
            (
                backup_id,
                chat_id,
                removed_count,
                removed_count * max(0, int(rarity_score)),
            )
            for (backup_id, chat_id), removed_count in sorted(backup_ranked_removals.items())
        ]
        await conn.executemany(
            """
            UPDATE harem_deletion_backup_group_stats
            SET claims = GREATEST(claims - $3, 0),
                rarity_score = GREATEST(rarity_score - $4, 0)
            WHERE backup_id = $1 AND chat_id = $2
            """,
            backup_stat_adjustments,
        )
        await conn.executemany(
            """
            DELETE FROM harem_deletion_backup_group_stats
            WHERE backup_id = $1 AND chat_id = $2 AND claims <= 0
            """,
            [
                (backup_id, chat_id)
                for backup_id, chat_id in sorted(backup_ranked_removals)
            ],
        )

    if affected_backup_ids:
        await conn.execute(
            """
            UPDATE harem_deletion_backups AS backup
                SET record_count = (
                    SELECT COUNT(*)::INT
                    FROM harem_deletion_backup_items AS item
                    WHERE item.backup_id = backup.id
                ),
                original_total_claims = (
                    SELECT COUNT(*)::INT
                    FROM harem_deletion_backup_items AS item
                    WHERE item.backup_id = backup.id AND item.is_ranked = TRUE
                ),
                original_highest_rarity = (
                    SELECT c.rarity
                    FROM harem_deletion_backup_items AS item
                    JOIN characters AS c ON c.id = item.character_id
                    WHERE item.backup_id = backup.id AND item.is_ranked = TRUE
                    ORDER BY c.rarity_score DESC, c.id ASC, item.original_harem_id ASC
                    LIMIT 1
                )
            WHERE backup.id = ANY($1::BIGINT[])
              AND backup.restored_at IS NULL
            """,
            affected_backup_ids,
        )
        # A snapshot with no remaining card cannot be restored meaningfully.
        # Deleting its header also keeps the backup tables free of empty rows.
        await conn.execute(
            """
            DELETE FROM harem_deletion_backups AS backup
            WHERE backup.id = ANY($1::BIGINT[])
              AND backup.restored_at IS NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM harem_deletion_backup_items AS item
                  WHERE item.backup_id = backup.id
              )
            """,
            affected_backup_ids,
        )

    # Trades have direct character foreign keys, so every historical/pending
    # trade containing this globally removed card must be purged before the
    # catalog row can be deleted. Pending multi-card gift/grant rows are marked
    # failed rather than partially changing what a confirmation would transfer.
    trade_rows = await conn.fetch(
        """
        DELETE FROM trades
        WHERE from_character_id = $1 OR to_character_id = $1
        RETURNING id
        """,
        character_id,
    )
    failed_gift_rows = await conn.fetch(
        """
        UPDATE gifts
        SET status = 'failed', resolved_at = NOW()
        WHERE status = 'pending' AND $1 = ANY(character_ids)
        RETURNING id
        """,
        character_id,
    )
    failed_sudo_rows = await conn.fetch(
        """
        UPDATE sudo_grants
        SET status = 'failed', resolved_at = NOW()
        WHERE status = 'pending' AND $1 = ANY(character_ids)
        RETURNING id
        """,
        character_id,
    )
    await conn.execute(
        """
        UPDATE groups
        SET recent_spawn_ids = array_remove(
            COALESCE(recent_spawn_ids, ARRAY[]::BIGINT[]),
            $1
        )
        WHERE $1 = ANY(COALESCE(recent_spawn_ids, ARRAY[]::BIGINT[]))
        """,
        character_id,
    )

    return {
        "harem_copies": len(harem_rows),
        "love_states": len(love_state_rows),
        "backup_items": len(backup_item_rows),
        "backup_snapshots": len(affected_backup_ids),
        "active_spawns": len(active_spawn_rows),
        "active_spawn_chat_ids": sorted({int(row["chat_id"]) for row in active_spawn_rows}),
        "scheduled_op_spawns": len(scheduled_op_rows),
        "trade_records": len(trade_rows),
        "failed_gifts": len(failed_gift_rows),
        "failed_sudo_grants": len(failed_sudo_rows),
    }


@router.message(Command("resetcharacter"))
async def cmd_resetcharacter(message: Message) -> None:
    """Full Owner: remove one catalog card from every player-owned location."""
    if not await _is_full_owner(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    raw_id = parts[1].strip() if len(parts) > 1 else ""
    if not raw_id.isdigit() or int(raw_id) <= 0:
        await message.reply(
            "❌ Usage: <code>/resetcharacter &lt;character_id&gt;</code>",
            parse_mode="HTML",
        )
        return
    character_id = int(raw_id)
    pool = get_pool()
    async with pool.acquire() as conn:
        character = await conn.fetchrow(
            """
            SELECT id, name, anime, rarity, aliases, image_url,
                   COALESCE(media_type, 'photo') AS media_type,
                   COALESCE(event, '') AS event
            FROM characters
            WHERE id = $1
            """,
            character_id,
        )
    if not character:
        await message.reply(
            f"❌ No character with ID <code>{character_id}</code> was found.",
            parse_mode="HTML",
        )
        return

    caption = (
        "⚠️ <b>Reset this character from every player Harem?</b>\n\n"
        + _character_caption(
            character["id"],
            character["name"],
            character["anime"],
            character["rarity"],
            character["aliases"] or "",
            character["event"] or "",
        )
        + "\n\nThe catalog card stays. This removes all player-owned copies, player-Market "
          "escrow, pending transfers involving this card, recovery-backup copies, and "
          "currently active/queued spawns of this card."
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="♻️ Reset every player copy",
            callback_data=f"resetchar:confirm:{character_id}",
        ),
        InlineKeyboardButton(text="❌ Cancel", callback_data="resetchar:cancel"),
    ]])
    if await _reply_character_preview(
        message,
        character=dict(character),
        caption=caption,
        reply_markup=keyboard,
    ):
        return
    await message.reply(caption, parse_mode="HTML", reply_markup=keyboard)


@router.callback_query(F.data.startswith("resetchar:"))
async def cb_resetcharacter(callback: CallbackQuery) -> None:
    """Apply a confirmed all-Harem reset while keeping the catalog row intact."""
    if not callback.from_user or not await is_bot_owner(callback.from_user.id):
        await callback.answer("⛔ Owner only.", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    if action == "cancel":
        await _replace_preview(callback, "❌ <b>Character ownership reset cancelled.</b>")
        await callback.answer("Cancelled")
        return
    if action != "confirm" or len(parts) != 3 or not parts[2].isdigit() or int(parts[2]) <= 0:
        await callback.answer("Invalid reset action.", show_alert=True)
        return
    character_id = int(parts[2])

    pool = get_pool()
    reset_summary: dict[str, object] | None = None
    current_character: dict | None = None
    player_listing_count = 0
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                character = await conn.fetchrow(
                    """
                    SELECT id, name, anime, rarity, aliases, image_url, rarity_score,
                           COALESCE(media_type, 'photo') AS media_type,
                           COALESCE(event, '') AS event
                    FROM characters
                    WHERE id = $1
                    FOR UPDATE
                    """,
                    character_id,
                )
                if character:
                    current_character = dict(character)
                    # Player listings are escrowed copies outside `harem`; remove
                    # them first so this reset cannot leave a purchasable owner card.
                    player_rows = await conn.fetch(
                        """
                        DELETE FROM market_listings
                        WHERE character_id = $1 AND listing_kind = 'player'
                        RETURNING id
                        """,
                        character_id,
                    )
                    player_listing_count = len(player_rows)
                    reset_summary = await _purge_catalog_character_references(
                        conn,
                        character_id=character_id,
                        rarity_score=int(character["rarity_score"] or 0),
                    )
    except Exception as exc:
        log.exception("could not reset player ownership for character %s: %s", character_id, exc)
        await _replace_preview(
            callback,
            "⚠️ <b>I could not reset this character's player ownership.</b>\n"
            "No success was reported; check the database and try again.",
        )
        await callback.answer("Reset failed", show_alert=True)
        return

    if reset_summary is None or current_character is None:
        await _replace_preview(callback, "❌ <b>This character no longer exists.</b>")
        await callback.answer("Not found")
        return

    for chat_id in reset_summary["active_spawn_chat_ids"]:
        try:
            await clear_group_runtime_cache(int(chat_id))
        except Exception as exc:
            log.warning("could not clear reset-character cache for group %s: %s", chat_id, exc)
    try:
        await reload_character_cache()
    except Exception:
        # The SQL transaction remains authoritative and the card itself was not
        # edited; a cache refresh failure must not turn a completed reset into a
        # false failure report.
        log.exception("could not reload character cache after /resetcharacter %s", character_id)

    if callback.message:
        await _publish_catalog_change_log(
            callback.message,
            action="Reset all player ownership",
            character=current_character,
            actor_name=_catalog_actor_name(callback.from_user),
            changed_fields=(),
        )

    result_lines = [
        "♻️ <b>Character ownership reset complete.</b>",
        f"The catalog card <code>{character_id}</code> was preserved.",
        f"Removed <b>{int(reset_summary['harem_copies'])}</b> Harem copy/copies.",
        f"Reset <b>{int(reset_summary['love_states'])}</b> Love progression/escrow state(s).",
        f"Removed <b>{player_listing_count}</b> player-Market escrow listing(s).",
        f"Removed <b>{int(reset_summary['backup_items'])}</b> recovery-backup item(s).",
        f"Cleared <b>{int(reset_summary['active_spawns'])}</b> active and "
        f"<b>{int(reset_summary['scheduled_op_spawns'])}</b> queued OP spawn(s).",
        f"Removed <b>{int(reset_summary['trade_records'])}</b> related trade record(s).",
    ]
    if reset_summary["failed_gifts"] or reset_summary["failed_sudo_grants"]:
        result_lines.append(
            f"Cancelled <b>{int(reset_summary['failed_gifts'])}</b> pending gift(s) and "
            f"<b>{int(reset_summary['failed_sudo_grants'])}</b> pending owner grant(s)."
        )
    await _replace_preview(callback, "\n".join(result_lines))
    await callback.answer("Character ownership reset")


@router.callback_query(F.data.startswith("delchar:"))
async def cb_delchar(callback: CallbackQuery):
    """Globally purge a catalog card only after full-owner confirmation."""
    if not await is_bot_owner(callback.from_user.id):
        await callback.answer("⛔ Owner only.", show_alert=True)
        return

    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "cancel":
        await _replace_preview(callback, "❌ <b>Character deletion cancelled.</b>")
        await callback.answer("Cancelled")
        return

    if action != "confirm" or len(parts) != 3 or not parts[2].isdigit():
        await callback.answer("Invalid action.", show_alert=True)
        return

    character_id = int(parts[2])
    pool = get_pool()
    purge_summary: dict[str, object] | None = None
    deleted_character: dict | None = None

    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                # This catalog lock conflicts with foreign-key inserts, so a
                # simultaneous claim/grant cannot create a new copy between this
                # global purge and the final catalog-row deletion.
                character = await conn.fetchrow(
                    """
                    SELECT id, name, anime, rarity, aliases, image_url, rarity_score,
                           COALESCE(media_type, 'photo') AS media_type,
                           COALESCE(event, '') AS event
                    FROM characters
                    WHERE id = $1
                    FOR UPDATE
                    """,
                    character_id,
                )
                if character:
                    deleted_character = dict(character)
                    purge_summary = await _purge_catalog_character_references(
                        conn,
                        character_id=character_id,
                        rarity_score=int(character["rarity_score"] or 0),
                    )
                    await conn.execute("DELETE FROM characters WHERE id = $1", character_id)
    except Exception as exc:
        log.exception("could not complete global /deletecharacter %s: %s", character_id, exc)
        await _replace_preview(
            callback,
            "⚠️ <b>I could not confirm the global character deletion.</b>\n"
            "No success was reported; check the database and try again.",
        )
        await callback.answer("Deletion failed", show_alert=True)
        return

    if purge_summary is None:
        await _replace_preview(callback, "❌ <b>This character no longer exists.</b>")
        await callback.answer("Not found")
        return

    if deleted_character is not None and callback.message:
        await _publish_catalog_change_log(
            callback.message,
            action="Deleted catalog character",
            character=deleted_character,
            actor_name=_catalog_actor_name(callback.from_user),
            changed_fields=(),
        )

    # The database transaction is authoritative. Redis is only an optional
    # representation of a now-removed active spawn, so cache eviction is best
    # effort after a successful commit.
    for chat_id in purge_summary["active_spawn_chat_ids"]:
        try:
            await clear_group_runtime_cache(int(chat_id))
        except Exception as exc:
            log.warning("could not clear purged-character cache for group %s: %s", chat_id, exc)

    cache_reloaded = True
    try:
        await reload_character_cache()
    except Exception as exc:
        cache_reloaded = False
        log.exception("could not reload character cache after global /deletecharacter %s: %s", character_id, exc)

    cleanup_lines = [
        f"Removed <b>{int(purge_summary['harem_copies'])}</b> collection copy/copies.",
        f"Removed <b>{int(purge_summary['backup_items'])}</b> recovery-backup item(s) "
        f"across <b>{int(purge_summary['backup_snapshots'])}</b> snapshot(s).",
        f"Cleared <b>{int(purge_summary['active_spawns'])}</b> active and "
        f"<b>{int(purge_summary['scheduled_op_spawns'])}</b> announced OP spawn(s).",
        f"Removed <b>{int(purge_summary['trade_records'])}</b> related trade record(s).",
    ]
    if purge_summary["failed_gifts"] or purge_summary["failed_sudo_grants"]:
        cleanup_lines.append(
            f"Cancelled <b>{int(purge_summary['failed_gifts'])}</b> pending gift(s) and "
            f"<b>{int(purge_summary['failed_sudo_grants'])}</b> pending owner grant(s)."
        )
    if not cache_reloaded:
        cleanup_lines.append(
            "⚠️ The deletion committed, but the character cache could not reload. "
            "Use the Admin-panel cache reload before another random spawn."
        )

    await _replace_preview(
        callback,
        "🗑️ <b>Character deleted globally.</b>\n"
        f"ID: <code>{character_id}</code>\n\n"
        + "\n".join(cleanup_lines),
    )
    await callback.answer("Deleted globally")
