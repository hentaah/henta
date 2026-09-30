"""Shared catalog-media delivery, catalog archives, and audit-log helpers.

A character's historic ``image_url`` column stores its Telegram file ID. Newer
cards may use either a Telegram photo or video, and ``media_type`` tells every
consumer which Bot API method is safe to use.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from html import escape
from typing import Any, Iterable

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import (
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
)

from bot.db.pool import get_pool
from bot.services.rich_renderer import rarity_label, rarity_symbol

log = logging.getLogger(__name__)

MEDIA_TYPE_PHOTO = "photo"
MEDIA_TYPE_VIDEO = "video"
VALID_MEDIA_TYPES = frozenset({MEDIA_TYPE_PHOTO, MEDIA_TYPE_VIDEO})

# The catalog archive destination is fixed by the requested command contract.
DATABASE_CHANNEL_ID = -1004366309728
DATABASE_CHANNEL_USERNAME = "@HentaDatabase"
# Catalog edit audit entries deliberately use a separate public log channel.
CATALOG_LOG_CHANNEL_ID = "@HentaCatcherLogs"
CATALOG_LOG_CHANNEL_USERNAME = "@HentaCatcherLogs"

# A short spacing keeps bulk archive/rewrite delivery orderly. Telegram's own
# retry-after response remains the authority when a longer delay is needed.
DATABASE_SEND_DELAY_SECONDS = 0.05
DATABASE_SEND_ATTEMPTS = 3
DATABASE_DELETE_BATCH_SIZE = 100
DATABASE_DELETE_DELAY_SECONDS = 0.03
# Telegram reserves the first channel message for its non-deletable creation
# service record on normal channels. Database archive art begins afterwards.
DATABASE_FIRST_DELETABLE_ARCHIVE_MESSAGE_ID = 2
MAX_AUDIT_CAPTION_LENGTH = 1_000


def _catalog_value(character: Any, key: str, default: Any = "") -> Any:
    """Read a catalog field from dicts and asyncpg-style records defensively."""
    getter = getattr(character, "get", None)
    if callable(getter):
        try:
            return getter(key, default)
        except (KeyError, IndexError, TypeError, AttributeError):
            # A minimal record/test-double can expose get(key) only.
            try:
                value = getter(key)
            except (KeyError, IndexError, TypeError, AttributeError):
                return default
            return default if value is None else value
    try:
        return character[key]
    except (KeyError, IndexError, TypeError, AttributeError):
        return default


def normalize_media_type(value: object) -> str:
    """Return a safe media kind, treating every legacy card as a photo."""
    normalized = str(value or "").strip().casefold()
    return MEDIA_TYPE_VIDEO if normalized == MEDIA_TYPE_VIDEO else MEDIA_TYPE_PHOTO


def character_is_video(character: Any) -> bool:
    """Return whether a catalog record must use Telegram's video APIs."""
    return normalize_media_type(_catalog_value(character, "media_type", MEDIA_TYPE_PHOTO)) == MEDIA_TYPE_VIDEO


def database_art_view_caption(character: Any) -> str:
    """Format the intentionally compact ART VIEW used by database archives."""
    character_id = _catalog_value(
        character,
        "id",
        _catalog_value(character, "character_id", ""),
    )
    anime = _catalog_value(character, "anime", "")
    name = _catalog_value(character, "name", "")
    rarity = _catalog_value(character, "rarity", "Common")
    rarity_text = str(rarity or "Common")
    return "\n".join((
        "⚡ ─── ART VIEW ─── ⚡",
        f"📂 Anime    : {escape(str(anime or ''))}",
        f"🎴 Name     : {escape(str(name or ''))}",
        f"🆔 ID       : {escape(str(character_id or ''))}",
        f"🔱 Rarity   : {rarity_symbol(rarity_text)}  {escape(rarity_label(rarity_text).upper())}",
        "────────────────────────",
    ))


def _compact_log_value(value: object, *, maximum: int = 120, empty: str = "None") -> str:
    """Collapse and bound a user-controlled value before an audit caption."""
    text = " ".join(str(value or "").split())
    if not text:
        return empty
    if len(text) > maximum:
        text = text[: maximum - 1].rstrip() + "…"
    return escape(text)


def _audit_field_value(field: str, character: Any) -> str:
    """Format one current/previous field without exposing Telegram file IDs."""
    raw = _catalog_value(character, field, "")
    if field == "media_type":
        return "Video" if normalize_media_type(raw) == MEDIA_TYPE_VIDEO else "Photo"
    if field == "image_url":
        return "updated"
    if field in {"aliases", "event"}:
        return _compact_log_value(raw, maximum=140)
    return _compact_log_value(raw, maximum=160)


def _bounded_caption(lines: Iterable[str]) -> str:
    """Keep every complete escaped line inside Telegram's media-caption limit."""
    accepted: list[str] = []
    truncated = False
    for line in lines:
        candidate = "\n".join([*accepted, line])
        if len(candidate) > MAX_AUDIT_CAPTION_LENGTH:
            truncated = True
            break
        accepted.append(line)
    if truncated:
        suffix = "… additional changes omitted"
        if len("\n".join([*accepted, suffix])) <= MAX_AUDIT_CAPTION_LENGTH:
            accepted.append(suffix)
    return "\n".join(accepted)


def catalog_change_log_caption(
    *,
    actor_name: str,
    action: str,
    character: Any,
    previous: Any | None = None,
    changed_fields: Iterable[str] = (
        "name",
        "anime",
        "rarity",
        "aliases",
        "event",
        "media_type",
        "image_url",
    ),
) -> str:
    """Build an audit caption over the current card media and changed fields.

    The current card details make each log post useful on its own. File IDs are
    intentionally never exposed: a changed photo/video is described as media
    updated while the actual new media is attached to the channel post.
    """
    character_id = _catalog_value(character, "id", _catalog_value(character, "character_id", ""))
    rarity = str(_catalog_value(character, "rarity", "Common") or "Common")
    lines = [
        "📝 ─── CATALOG CHANGE LOG ─── 📝",
        f"👤 Admin : {_compact_log_value(actor_name, maximum=100, empty='Unknown admin')}",
        f"🛠️ Action : {_compact_log_value(action, maximum=130, empty='Updated character')}",
        f"🆔 ID : {_compact_log_value(character_id, maximum=30, empty='Unknown')}",
        f"🎴 Name : {_audit_field_value('name', character)}",
        f"📂 Anime : {_audit_field_value('anime', character)}",
        f"🔱 Rarity : {rarity_symbol(rarity)} {_audit_field_value('rarity', character)}",
        f"🎊 Event : {_audit_field_value('event', character)}",
        f"🏷️ Aliases : {_audit_field_value('aliases', character)}",
        f"🖼️ Media : {_audit_field_value('media_type', character)}",
    ]
    if previous is not None:
        change_lines: list[str] = []
        labels = {
            "name": "Name",
            "anime": "Anime",
            "rarity": "Rarity",
            "aliases": "Aliases",
            "event": "Event",
            "media_type": "Media type",
            "image_url": "Media file",
        }
        seen: set[str] = set()
        for field in changed_fields:
            if field in seen or field not in labels:
                continue
            seen.add(field)
            old_raw = _catalog_value(previous, field, "")
            new_raw = _catalog_value(character, field, "")
            if str(old_raw or "") == str(new_raw or ""):
                continue
            if field == "image_url":
                change_lines.append("• Media file: updated")
            else:
                change_lines.append(
                    f"• {labels[field]}: {_audit_field_value(field, previous)} → "
                    f"{_audit_field_value(field, character)}"
                )
        if change_lines:
            lines.append("Changes:")
            lines.extend(change_lines)
        else:
            lines.append("Changes: No stored value changed.")
    lines.append("────────────────────────")
    return _bounded_caption(lines)


def _retry_after_seconds(error: TelegramRetryAfter) -> float:
    """Normalize aiogram versions that expose retry_after as seconds/timedelta."""
    retry_after = getattr(error, "retry_after", 1)
    if isinstance(retry_after, timedelta):
        return max(1.0, retry_after.total_seconds())
    try:
        return max(1.0, float(retry_after))
    except (TypeError, ValueError):
        return 1.0


async def _deliver_with_retry(deliver) -> Any:
    """Run one Telegram delivery, honoring server-provided flood-control delays."""
    for attempt in range(DATABASE_SEND_ATTEMPTS):
        try:
            return await deliver()
        except TelegramRetryAfter as error:
            if attempt + 1 >= DATABASE_SEND_ATTEMPTS:
                raise
            await asyncio.sleep(_retry_after_seconds(error))
    raise RuntimeError("unreachable Telegram delivery retry state")


async def send_catalog_media_to_chat(
    bot: Bot,
    *,
    chat_id: int | str,
    character: Any,
    caption: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    disable_notification: bool = False,
) -> Any:
    """Send one catalog card as photo/video, with a caption-only fallback."""
    raw_file_id = _catalog_value(character, "image_url", "")
    file_id = str(raw_file_id or "").strip()
    common = {
        "chat_id": chat_id,
        "caption": caption,
        "parse_mode": "HTML",
        "reply_markup": reply_markup,
        "disable_notification": disable_notification,
    }
    if not file_id:
        return await _deliver_with_retry(lambda: bot.send_message(
            chat_id=chat_id,
            text=caption,
            parse_mode="HTML",
            reply_markup=reply_markup,
            disable_notification=disable_notification,
        ))
    if character_is_video(character):
        return await _deliver_with_retry(
            lambda: bot.send_video(video=file_id, supports_streaming=True, **common)
        )
    return await _deliver_with_retry(lambda: bot.send_photo(photo=file_id, **common))


async def reply_catalog_media(
    message: Message,
    *,
    character: Any,
    caption: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Any:
    """Reply with photo/video catalog media or text when a file ID is absent."""
    raw_file_id = _catalog_value(character, "image_url", "")
    file_id = str(raw_file_id or "").strip()
    if not file_id:
        return await message.reply(caption, parse_mode="HTML", reply_markup=reply_markup)
    if character_is_video(character):
        return await message.reply_video(
            video=file_id,
            caption=caption,
            parse_mode="HTML",
            reply_markup=reply_markup,
            supports_streaming=True,
        )
    return await message.reply_photo(
        photo=file_id,
        caption=caption,
        parse_mode="HTML",
        reply_markup=reply_markup,
    )


async def edit_catalog_media(
    message: Message,
    *,
    character: Any,
    caption: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> bool:
    """Replace an existing card with the appropriate photo/video media type."""
    raw_file_id = _catalog_value(character, "image_url", "")
    file_id = str(raw_file_id or "").strip()
    if not file_id:
        return False
    if character_is_video(character):
        media = InputMediaVideo(
            media=file_id,
            caption=caption,
            parse_mode="HTML",
            supports_streaming=True,
        )
    else:
        media = InputMediaPhoto(media=file_id, caption=caption, parse_mode="HTML")
    await message.edit_media(media=media, reply_markup=reply_markup)
    return True


async def answer_catalog_media(
    message: Message,
    *,
    character: Any,
    caption: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Any:
    """Answer with photo/video catalog media or text when a file ID is absent."""
    raw_file_id = _catalog_value(character, "image_url", "")
    file_id = str(raw_file_id or "").strip()
    if not file_id:
        return await message.answer(caption, parse_mode="HTML", reply_markup=reply_markup)
    if character_is_video(character):
        return await message.answer_video(
            video=file_id,
            caption=caption,
            parse_mode="HTML",
            reply_markup=reply_markup,
            supports_streaming=True,
        )
    return await message.answer_photo(
        photo=file_id,
        caption=caption,
        parse_mode="HTML",
        reply_markup=reply_markup,
    )


async def send_database_art(bot: Bot, character: Any) -> Any:
    """Archive exactly one catalog art with the compact database ART VIEW."""
    return await send_catalog_media_to_chat(
        bot,
        chat_id=DATABASE_CHANNEL_ID,
        character=character,
        caption=database_art_view_caption(character),
    )


async def publish_new_art_to_database(bot: Bot, character: Any) -> bool:
    """Mirror a committed new card to @HentaDatabase without undoing the card.

    New-card archive delivery is intentionally independent from `/addlog`: the
    database channel remains the catalog's current art archive, while add-log
    controls the separate change-audit channel.
    """
    try:
        await send_database_art(bot, character)
    except Exception:
        log.exception("could not archive newly added character %s", _catalog_value(character, "id", "?"))
        return False
    return True


async def send_catalog_change_log(
    bot: Bot,
    *,
    actor_name: str,
    action: str,
    character: Any,
    previous: Any | None = None,
    changed_fields: Iterable[str] = (
        "name",
        "anime",
        "rarity",
        "aliases",
        "event",
        "media_type",
        "image_url",
    ),
) -> Any:
    """Send one current-media audit card to @HentaCatcherLogs."""
    return await send_catalog_media_to_chat(
        bot,
        chat_id=CATALOG_LOG_CHANNEL_ID,
        character=character,
        caption=catalog_change_log_caption(
            actor_name=actor_name,
            action=action,
            character=character,
            previous=previous,
            changed_fields=changed_fields,
        ),
    )


async def database_add_log_enabled() -> bool:
    """Read the durable catalog-change audit toggle; false is the safe default."""
    pool = get_pool()
    async with pool.acquire() as conn:
        value = await conn.fetchval(
            "SELECT database_add_log_enabled FROM bot_runtime_settings WHERE singleton = TRUE"
        )
    return bool(value)


async def set_database_add_log_enabled(*, enabled: bool, changed_by: int) -> None:
    """Persist a Full Owner's catalog-change audit-log preference."""
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO bot_runtime_settings (
                singleton, database_add_log_enabled, changed_by, changed_at
            )
            VALUES (TRUE, $1, $2, NOW())
            ON CONFLICT (singleton) DO UPDATE SET
              database_add_log_enabled = EXCLUDED.database_add_log_enabled,
              changed_by = EXCLUDED.changed_by,
              changed_at = NOW()
            """,
            bool(enabled),
            changed_by,
        )


async def publish_catalog_change_if_logging_enabled(
    bot: Bot,
    *,
    actor_name: str,
    action: str,
    character: Any,
    previous: Any | None = None,
    changed_fields: Iterable[str] = (
        "name",
        "anime",
        "rarity",
        "aliases",
        "event",
        "media_type",
        "image_url",
    ),
) -> bool | None:
    """Log a catalog mutation only while `/addlog on` is active.

    ``None`` means logging is off. ``False`` means the mutation committed but
    channel delivery failed; no catalog transaction is ever rolled back because
    a remote audit post could not be sent.
    """
    if not await database_add_log_enabled():
        return None
    try:
        await send_catalog_change_log(
            bot,
            actor_name=actor_name,
            action=action,
            character=character,
            previous=previous,
            changed_fields=changed_fields,
        )
    except Exception:
        log.exception("could not send catalog audit log for character %s", _catalog_value(character, "id", "?"))
        return False
    return True


async def send_catalog_log_toggle_notice(bot: Bot, *, actor_name: str, enabled: bool) -> bool:
    """Record who enabled/disabled audit logging without requiring card media."""
    state = "enabled" if enabled else "disabled"
    text = "\n".join((
        "📝 ─── CATALOG CHANGE LOG ─── 📝",
        f"👤 Admin : {_compact_log_value(actor_name, maximum=100, empty='Unknown admin')}",
        f"🛠️ Action : Audit logging {state}",
        "────────────────────────",
    ))
    try:
        await _deliver_with_retry(lambda: bot.send_message(
            chat_id=CATALOG_LOG_CHANNEL_ID,
            text=text,
            parse_mode="HTML",
        ))
    except Exception:
        log.exception("could not send catalog audit-toggle notice")
        return False
    return True


def _is_missing_message_error(error: TelegramBadRequest) -> bool:
    """Return true only for harmless message-ID gaps, never permission failures."""
    detail = str(error).casefold()
    return any(fragment in detail for fragment in (
        "message to delete not found",
        "message not found",
        "message_id_invalid",
        "message identifier is not specified",
    ))


async def _delete_database_message_batch(bot: Bot, message_ids: list[int]) -> int:
    """Delete one known ID batch, tolerating only harmless Telegram ID gaps."""
    if not message_ids:
        return 0
    delete_many = getattr(bot, "delete_messages", None)
    if callable(delete_many):
        try:
            result = await _deliver_with_retry(lambda: delete_many(
                chat_id=DATABASE_CHANNEL_ID,
                message_ids=message_ids,
            ))
            if result is not False:
                return len(message_ids)
        except (TelegramBadRequest, TypeError, AttributeError):
            # A gap can make a bulk API call reject the entire batch on some
            # Telegram/API versions. Retry individual IDs and ignore only gaps.
            pass

    deleted = 0
    for message_id in message_ids:
        try:
            await _deliver_with_retry(lambda message_id=message_id: bot.delete_message(
                chat_id=DATABASE_CHANNEL_ID,
                message_id=message_id,
            ))
        except TelegramBadRequest as error:
            # Gaps are expected; permission/age/ownership failures must abort the
            # rewrite rather than pretending the channel was successfully cleared.
            if _is_missing_message_error(error):
                continue
            raise
        deleted += 1
    return deleted


async def delete_all_database_channel_posts(bot: Bot) -> tuple[int, int]:
    """Delete every database-channel post through a freshly created ID boundary.

    The owner confirmed that @HentaDatabase contains only bot-uploaded catalog
    arts. Telegram's Bot API does not expose channel history, so this function
    posts a temporary marker to obtain the current highest message ID, then
    deletes every archive ID after the reserved non-deletable channel-creation
    record (normally ID 1) through that marker in safe batches. It returns
    ``(deleted_count, upper_message_id)``; the temporary marker is included.
    """
    marker = await _deliver_with_retry(lambda: bot.send_message(
        chat_id=DATABASE_CHANNEL_ID,
        text="⏳ Rewriting the catalog database. This temporary marker will be removed.",
        disable_notification=True,
    ))
    try:
        upper_message_id = int(getattr(marker, "message_id"))
    except (TypeError, ValueError, AttributeError) as error:
        raise RuntimeError("database rewrite marker has no valid message ID") from error
    if upper_message_id <= 0:
        raise RuntimeError("database rewrite marker has no valid message ID")
    if upper_message_id < DATABASE_FIRST_DELETABLE_ARCHIVE_MESSAGE_ID:
        # This is only possible for an unusual brand-new channel without the
        # normal creation service record. Clean up the marker itself instead.
        await _delete_database_message_batch(bot, [upper_message_id])
        return 1, upper_message_id

    deleted = 0
    try:
        for start in range(
            DATABASE_FIRST_DELETABLE_ARCHIVE_MESSAGE_ID,
            upper_message_id + 1,
            DATABASE_DELETE_BATCH_SIZE,
        ):
            stop = min(start + DATABASE_DELETE_BATCH_SIZE, upper_message_id + 1)
            deleted += await _delete_database_message_batch(bot, list(range(start, stop)))
            if DATABASE_DELETE_DELAY_SECONDS and stop <= upper_message_id:
                await asyncio.sleep(DATABASE_DELETE_DELAY_SECONDS)
    except Exception:
        # Do not leave a misleading visible “rewriting” marker if an old or
        # protected post prevents clearing the channel. Preserve the original
        # exception so the command can report its exact Telegram reason.
        try:
            await _delete_database_message_batch(bot, [upper_message_id])
        except Exception:
            pass
        raise
    return deleted, upper_message_id


async def list_database_catalog_arts() -> list[dict[str, Any]]:
    """Read positive catalog codes in numeric order, naturally skipping holes."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, name, anime, rarity, image_url,
                   COALESCE(media_type, 'photo') AS media_type
            FROM characters
            WHERE id > 0
            ORDER BY id ASC
            """
        )
    return [dict(row) for row in rows]
