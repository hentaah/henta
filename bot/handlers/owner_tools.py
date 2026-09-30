"""Slash-command owner controls plus private collection/recovery tools."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.db.pool import get_pool
from bot.services.game import (
    delete_harem_copy,
    delete_user_harem,
    get_character_catch_report,
    get_characters_by_ids,
    get_event_character_codes,
    get_leaderboard_groups,
    get_harem_copy_for_deletion,
    get_latest_harem_deletion_backup,
    restore_harem_deletion_backup,
    set_event_spawn_enabled,
    set_rarity_spawn_enabled,
)
from bot.services.harem_purge import (
    MAX_CATALOG_CHARACTER_ID as GLOBAL_HAREM_PURGE_MAX_CHARACTER_ID,
    MAX_GLOBAL_HAREM_PURGE_CODES,
    cancel_global_harem_purge_job,
    create_global_harem_purge_job,
    execute_global_harem_purge_job,
)
from bot.services.maintenance import set_bot_killed
from bot.services.player_database_reset import (
    cancel_player_database_reset_job,
    claim_player_database_reset_job,
    create_player_database_reset_job,
    finish_player_database_reset_job,
    reset_all_player_game_state,
)
from bot.services.permissions import (
    grant_bot_owner,
    grant_catalog_admin,
    is_bot_owner,
    is_root_owner,
    list_role_members,
    revoke_bot_owner,
    revoke_catalog_admin,
)
from bot.services.rich_renderer import rarity_label_bold, rarity_symbol
from bot.services.spawn_engine import clear_group_runtime_cache, reload_character_cache

router = Router()
log = logging.getLogger(__name__)



# Stay below Telegram's global broadcast throughput while delivering one copy
# per group. The task runs in the background so a confirmation callback itself
# stays responsive even for a large group list.
BROADCAST_SEND_DELAY_SECONDS = 0.05
BROADCAST_PREVIEW_LIMIT = 700


async def _replace_card(callback: CallbackQuery, text: str) -> None:
    """Replace a text or photo confirmation card without leaking its buttons."""
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


def _reply_target(message: Message):
    reply = message.reply_to_message
    return reply.from_user if reply else None


# Telegram user IDs are positive signed BIGINT values.  The parser deliberately
# accepts exactly one decimal argument so a mistyped multi-token command cannot
# silently promote the replied user instead of the supplied numeric target.
MAX_TELEGRAM_USER_ID = 9_223_372_036_854_775_807


def _numeric_role_target_argument(message: Message) -> tuple[bool, int | None]:
    """Return whether a role command supplied an ID and its validated value.

    ``(False, None)`` means no argument was given and the normal reply-target
    flow should be used. ``(True, None)`` means an argument was supplied but is
    not a valid positive Telegram user ID; callers show command-specific usage.
    """
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


# Group/supergroup chat IDs are negative signed BIGINT values. `/left` must
# receive exactly one such value so it cannot be redirected to a private user
# chat or a malformed extra argument.
MIN_TELEGRAM_GROUP_CHAT_ID = -(MAX_TELEGRAM_USER_ID + 1)


def _negative_group_chat_id_argument(message: Message) -> int | None:
    """Parse one valid negative Telegram group ID from a `/left` command."""
    parts = (getattr(message, "text", None) or "").strip().split(maxsplit=1)
    if len(parts) != 2:
        return None
    raw = parts[1].strip()
    if not re.fullmatch(r"-[0-9]{1,19}", raw):
        return None
    chat_id = int(raw)
    if not MIN_TELEGRAM_GROUP_CHAT_ID <= chat_id < 0:
        return None
    return chat_id


# ─── /getgroups — private owner export of the current /topgroups rows ──────
TOP_GROUPS_EXPORT_LIMIT = 10
_PUBLIC_GROUP_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")


def _public_group_url(username: object) -> str | None:
    """Return a safe public t.me link from a stored/current group username."""
    raw = str(username or "").strip()
    if raw.startswith("@"):
        raw = raw[1:]
    if not _PUBLIC_GROUP_USERNAME_RE.fullmatch(raw):
        return None
    return f"https://t.me/{raw}"


def _safe_invite_link(value: object) -> str | None:
    """Accept only normal Telegram links returned by the Bot API."""
    # ``getChat`` supplies a string, while a few wrappers expose a
    # ChatInviteLink-like object. Supporting both preserves the same strict URL
    # allowlist without ever trusting a link from user input.
    raw_value = getattr(value, "invite_link", value)
    link = str(raw_value or "").strip()
    if not link or any(char.isspace() for char in link):
        return None
    if link.startswith(("https://t.me/", "https://telegram.me/")):
        return link
    return None


async def _resolve_top_group_link(bot, group: dict) -> str | None:
    """Resolve the current public or already-existing private group link.

    A live ``getChat`` result takes priority over a stored username: a group may
    have become private since its last message reached the bot, in which case
    its currently exposed primary invite link is the only useful export. If
    Telegram does not expose a primary invite link, the command deliberately
    does not create, export, revoke, or otherwise change an invite link.
    """
    stored_public = _public_group_url(group.get("username"))
    try:
        chat = await bot.get_chat(chat_id=int(group["chat_id"]))
    except Exception as exc:
        log.info("could not resolve top-group link for %s: %s", group.get("chat_id"), exc)
        # A transient lookup failure should not discard a known stored public
        # username, but it must never fabricate a private invite link.
        return stored_public

    direct = _public_group_url(getattr(chat, "username", None))
    if direct:
        return direct

    private_link = _safe_invite_link(getattr(chat, "invite_link", None))
    if private_link:
        return private_link

    # getChat succeeded and says this group currently has neither an accessible
    # public username nor a primary private invite. A stale stored username may
    # point to a different chat, so do not display it as a fallback here.
    return None


def _top_group_display_name(group: dict) -> str:
    """Choose a stable readable name without rendering untrusted HTML."""
    title = str(group.get("title") or "").strip()
    if title:
        return title
    username = str(group.get("username") or "").strip().lstrip("@")
    if username:
        return f"@{username}"
    return f"Group {int(group.get('chat_id') or 0)}"


def _top_group_links_text(groups_and_links: list[tuple[dict, str | None]]) -> str:
    """Render copyable named links, with transparent private-link fallback."""
    lines = [
        "🔗 <b>Top Group Links</b>",
        "────────────────────────",
        "Matches the current <code>/topgroups</code> top 10.",
        "",
    ]
    for index, (group, link) in enumerate(groups_and_links, start=1):
        lines.append(f"<b>{index}. {escape(_top_group_display_name(group))}</b>")
        if link:
            safe_link = escape(link, quote=True)
            lines.append(f"🔗 <a href=\"{safe_link}\">{safe_link}</a>")
        else:
            lines.append("🔒 No accessible public or primary invite link.")
        # Keep the numeric chat ID directly under every link (and under the
        # no-link notice), so an owner can immediately use /left <group_id>.
        lines.append(f"🆔 <code>{int(group['chat_id'])}</code>")
        if index != len(groups_and_links):
            lines.append("")
    return "\n".join(lines)


async def _send_top_group_export_to_owner(message: Message, owner_id: int, text: str) -> None:
    """Keep potentially sensitive group links visible only to the full owner."""
    if message.chat.type == "private":
        await message.answer(text, parse_mode="HTML", disable_web_page_preview=True)
        return

    try:
        await message.bot.send_message(
            chat_id=owner_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception:
        await message.reply(
            "🔒 I could not send the group-link export privately. Start a private chat "
            "with me, then use <code>/getgroups</code> there.",
            parse_mode="HTML",
        )
        return
    await message.reply("✅ The current top-group link export was sent to your private chat.")


@router.message(Command("getgroups"))
async def cmd_getgroups(message: Message) -> None:
    """Full owner: privately export titles/links for exactly the /topgroups rows."""
    actor = message.from_user
    if not actor or not await is_bot_owner(actor.id):
        await message.reply("🚫 Only the bot owner can use this command.", parse_mode="HTML")
        return

    groups = await get_leaderboard_groups(limit=TOP_GROUPS_EXPORT_LIMIT)
    if not groups:
        await _send_top_group_export_to_owner(
            message,
            actor.id,
            "📭 <b>No groups are currently listed in /topgroups.</b>",
        )
        return

    groups_and_links = [
        (group, await _resolve_top_group_link(message.bot, group))
        for group in groups
    ]
    await _send_top_group_export_to_owner(
        message,
        actor.id,
        _top_group_links_text(groups_and_links),
    )


# ─── /left — full-owner group departure and tracking cleanup ───────────────
async def _leave_and_remove_group(bot, chat_id: int) -> dict:
    """Leave one group and atomically clear its group-only tracking rows.

    A successful Bot API leave occurs inside the database transaction: if the
    leave request is rejected, no leaderboard/spawn cleanup is committed. User
    harems are deliberately global and are never deleted just because the bot
    leaves the group where a card was acquired.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            group = await conn.fetchrow(
                "SELECT chat_id, title FROM groups WHERE chat_id = $1 FOR UPDATE",
                chat_id,
            )

            left = await bot.leave_chat(chat_id=chat_id)
            if left is False:
                raise RuntimeError(f"Telegram did not confirm departure from {chat_id}")

            # Stop every group-scoped runtime/stat path before deleting the
            # registered group. `group_stats` also has an ON DELETE CASCADE,
            # but is explicit here so cleanup is clear and idempotent.
            await conn.execute("DELETE FROM active_spawns WHERE chat_id = $1", chat_id)
            await conn.execute("DELETE FROM scheduled_op_spawns WHERE chat_id = $1", chat_id)
            await conn.execute("DELETE FROM user_group_stats WHERE chat_id = $1", chat_id)
            await conn.execute(
                "DELETE FROM harem_deletion_backup_group_stats WHERE chat_id = $1",
                chat_id,
            )
            await conn.execute("DELETE FROM group_stats WHERE chat_id = $1", chat_id)
            deleted = await conn.fetchrow(
                "DELETE FROM groups WHERE chat_id = $1 RETURNING chat_id, title",
                chat_id,
            )

    row = deleted or group
    return {
        "chat_id": chat_id,
        "was_tracked": bool(row),
        "title": str(row["title"] or "") if row else "",
    }


@router.message(Command("left"))
async def cmd_left(message: Message) -> None:
    """Full owner: leave one negative-ID group and remove it from tracking."""
    actor = message.from_user
    if not actor or not await is_bot_owner(actor.id):
        return

    chat_id = _negative_group_chat_id_argument(message)
    if chat_id is None:
        await message.reply(
            "❌ Usage: <code>/left &lt;negative_group_id&gt;</code>\n"
            "Example: <code>/left -1003529827660</code>",
            parse_mode="HTML",
        )
        return

    invoked_in_target_group = bool(message.chat and int(message.chat.id) == chat_id)
    if invoked_in_target_group:
        # There is no reliable way to post a success card after leaving the
        # very chat that contained this command, so acknowledge before leaving.
        await message.reply(
            "👋 Leaving this group and removing its tracking data now.",
            parse_mode="HTML",
        )

    try:
        removed = await _leave_and_remove_group(message.bot, chat_id)
    except Exception as exc:
        log.warning("/left could not complete for chat %s: %s", chat_id, exc)
        try:
            await message.reply(
                "⚠️ I could not safely complete the group leave and cleanup. "
                "Check the ID and the bot's access, then try again.",
                parse_mode="HTML",
            )
        except Exception:
            # If Telegram accepted the leave but an unexpected later operation
            # failed, the bot may no longer be able to answer in that chat.
            pass
        return

    try:
        await clear_group_runtime_cache(chat_id)
    except Exception as exc:
        # Cache cleanup is best effort; the SQL state and successful departure
        # remain authoritative even if Redis is temporarily unavailable.
        log.warning("could not clear departed-group cache for %s: %s", chat_id, exc)

    if invoked_in_target_group:
        return

    title = escape(removed["title"])
    details = f"\nGroup: <b>{title}</b>" if title else ""
    tracking_detail = (
        "Tracking rows and group leaderboard data were removed."
        if removed["was_tracked"]
        else "No registered group row existed; any orphaned group tracking rows were cleared."
    )
    await message.reply(
        "👋 <b>Bot left the group and removed its tracking.</b>"
        f"{details}\n🆔 <code>{chat_id}</code>\n"
        f"{tracking_detail}\n"
        "Global player harems were not changed.",
        parse_mode="HTML",
    )


# ─── /broadcast — owner-confirmed group announcement ────────────────────────

def _broadcast_keyboard(owner_id: int, job_id: int) -> InlineKeyboardMarkup:
    """Bind a broadcast confirmation to its creating full owner and DB job."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="✅ Confirm broadcast",
            callback_data=f"broadcast:confirm:{owner_id}:{job_id}",
            style="success",
        ),
        InlineKeyboardButton(
            text="❌ Cancel",
            callback_data=f"broadcast:cancel:{owner_id}:{job_id}",
            style="danger",
        ),
    ]])


def _broadcast_preview(source_text: str) -> str:
    """Keep a confirmation readable without truncating the delivered text."""
    preview = source_text
    if len(preview) > BROADCAST_PREVIEW_LIMIT:
        preview = preview[: BROADCAST_PREVIEW_LIMIT - 1].rstrip() + "…"
    return escape(preview)


async def _claim_broadcast_job(job_id: int, owner_id: int) -> dict | None:
    """Atomically turn one pending confirmation into the sole sending job."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE broadcast_jobs
            SET status = 'sending', confirmed_at = NOW()
            WHERE id = $1 AND owner_id = $2 AND status = 'pending'
            RETURNING id, source_chat_id, source_message_id, source_text
            """,
            job_id,
            owner_id,
        )
    return dict(row) if row else None


async def _finish_broadcast_job(
    job_id: int,
    *,
    delivered_count: int,
    failed_count: int,
    status: str,
) -> None:
    """Persist a useful completion summary after a background fanout."""
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE broadcast_jobs
            SET status = $2,
                delivered_count = $3,
                failed_count = $4,
                resolved_at = NOW()
            WHERE id = $1 AND status = 'sending'
            """,
            job_id,
            status,
            delivered_count,
            failed_count,
        )


async def _registered_broadcast_chat_ids() -> list[int]:
    """Return every known bot-group destination in stable order.

    `groups.enabled` is not the automatic-spawn switch in this package, so it
    must not silently narrow an owner-requested broadcast to only a subset.
    Individual stale/inaccessible group rows are isolated during delivery.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT chat_id FROM groups ORDER BY chat_id ASC")
    return [int(row["chat_id"]) for row in rows]


async def _edit_broadcast_result(message: Message | None, text: str) -> None:
    """Replace the confirmation card when its original chat message survives."""
    if not message:
        return
    try:
        await message.edit_text(text, parse_mode="HTML", reply_markup=None)
    except Exception:
        try:
            await message.edit_caption(caption=text, parse_mode="HTML", reply_markup=None)
        except Exception:
            pass


async def _deliver_broadcast(bot, job: dict, confirmation_message: Message | None) -> None:
    """Fan out one claimed job without letting one inaccessible group abort it."""
    delivered = 0
    failed = 0
    fatal_error = False
    try:
        destination_ids = await _registered_broadcast_chat_ids()
        for chat_id in destination_ids:
            try:
                # Copy first so formatting/entities remain exactly as the owner
                # replied. A plain-text fallback still succeeds if the original
                # source message disappears before confirmation.
                await bot.copy_message(
                    chat_id=chat_id,
                    from_chat_id=int(job["source_chat_id"]),
                    message_id=int(job["source_message_id"]),
                )
                delivered += 1
            except Exception as copy_exc:
                try:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=str(job["source_text"]),
                        parse_mode=None,
                    )
                    delivered += 1
                except Exception as send_exc:
                    failed += 1
                    log.warning(
                        "broadcast job %s failed for chat %s: copy=%s; text fallback=%s",
                        job["id"],
                        chat_id,
                        copy_exc,
                        send_exc,
                    )
            if BROADCAST_SEND_DELAY_SECONDS:
                await asyncio.sleep(BROADCAST_SEND_DELAY_SECONDS)
    except Exception as exc:
        fatal_error = True
        log.exception("broadcast job %s stopped before completion: %s", job["id"], exc)

    status = "completed" if not fatal_error else "failed"
    try:
        await _finish_broadcast_job(
            int(job["id"]),
            delivered_count=delivered,
            failed_count=failed,
            status=status,
        )
    except Exception as exc:
        log.exception("could not store result for broadcast job %s: %s", job["id"], exc)

    if fatal_error:
        summary = (
            "⚠️ <b>Broadcast stopped early.</b>\n"
            f"✅ Delivered: <b>{delivered}</b>\n"
            f"❌ Failed: <b>{failed}</b>"
        )
    elif delivered == 0 and failed == 0:
        summary = "ℹ️ <b>No registered groups were available for this broadcast.</b>"
    else:
        summary = (
            "📣 <b>Broadcast completed.</b>\n"
            f"✅ Delivered: <b>{delivered}</b>\n"
            f"❌ Failed: <b>{failed}</b>"
        )
    await _edit_broadcast_result(confirmation_message, summary)


_broadcast_tasks: set[asyncio.Task] = set()


def _keep_broadcast_task(task: asyncio.Task) -> None:
    """Retain a background fanout and consume an unexpected task exception."""
    _broadcast_tasks.add(task)

    def _finished(done: asyncio.Task) -> None:
        _broadcast_tasks.discard(done)
        try:
            done.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("unhandled broadcast task failure")

    task.add_done_callback(_finished)


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message):
    """Full owner: reply to a text message and request a confirmed fanout."""
    actor = message.from_user
    if not actor or not await is_bot_owner(actor.id):
        return

    source = message.reply_to_message
    source_text = (source.text or "") if source else ""
    if not source or not source_text.strip():
        await message.reply(
            "❌ Reply to a text message with <code>/broadcast</code>.",
            parse_mode="HTML",
        )
        return

    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO broadcast_jobs (
                owner_id, source_chat_id, source_message_id, source_text
            )
            VALUES ($1, $2, $3, $4)
            RETURNING id
            """,
            actor.id,
            source.chat.id,
            source.message_id,
            source_text,
        )
    job_id = int(row["id"])
    await message.reply(
        "📣 <b>Broadcast this text to every registered bot group?</b>\n\n"
        f"<blockquote>{_broadcast_preview(source_text)}</blockquote>\n\n"
        "Only you can confirm this action.",
        parse_mode="HTML",
        reply_markup=_broadcast_keyboard(actor.id, job_id),
        disable_web_page_preview=True,
    )


@router.callback_query(F.data.startswith("broadcast:"))
async def cb_broadcast(callback: CallbackQuery):
    """Confirm/cancel one owner-bound persistent broadcast request."""
    parts = (callback.data or "").split(":")
    if (
        len(parts) != 4
        or parts[1] not in {"confirm", "cancel"}
        or not parts[2].isdigit()
        or not parts[3].isdigit()
    ):
        await callback.answer("Invalid broadcast action.", show_alert=True)
        return

    action, owner_id, job_id = parts[1], int(parts[2]), int(parts[3])
    if callback.from_user.id != owner_id:
        await callback.answer("⛔ Only the requesting owner can confirm this.", show_alert=True)
        return
    if not await is_bot_owner(callback.from_user.id):
        await callback.answer("⛔ Owner only.", show_alert=True)
        return

    if action == "cancel":
        pool = get_pool()
        async with pool.acquire() as conn:
            cancelled = await conn.fetchval(
                """
                UPDATE broadcast_jobs
                SET status = 'cancelled', resolved_at = NOW()
                WHERE id = $1 AND owner_id = $2 AND status = 'pending'
                RETURNING 1
                """,
                job_id,
                owner_id,
            )
        if not cancelled:
            await callback.answer("This broadcast is no longer pending.", show_alert=True)
            return
        await _replace_card(callback, "❌ <b>Broadcast cancelled.</b>")
        await callback.answer("Cancelled")
        return

    job = await _claim_broadcast_job(job_id, owner_id)
    if not job:
        await callback.answer("This broadcast is no longer pending.", show_alert=True)
        return

    await _replace_card(
        callback,
        "📣 <b>Broadcast started.</b> Sending to registered bot groups…",
    )
    _keep_broadcast_task(asyncio.create_task(
        _deliver_broadcast(callback.bot, job, callback.message)
    ))
    await callback.answer("Broadcast started")


# ─── /stopspawn and /restorespawn — global event spawn controls ────────────

async def _set_event_spawn_state(message: Message, *, enabled: bool) -> None:
    """Apply one full-owner event pause/restore and refresh random selection."""
    actor = message.from_user
    if not actor or not await is_bot_owner(actor.id):
        return

    command = "restorespawn" if enabled else "stopspawn"
    parts = (message.text or "").split(maxsplit=1)
    event_label = parts[1].strip() if len(parts) > 1 else ""
    if not event_label:
        await message.reply(
            f"❌ Usage: <code>/{command} &lt;event name&gt;</code>\n"
            f"Example: <code>/{command} bunny🐰</code>",
            parse_mode="HTML",
        )
        return

    state = await set_event_spawn_enabled(event_label, enabled=enabled)
    if not state:
        await message.reply(
            f"❌ Event <b>{escape(event_label)}</b> was not found. Check the label and try again.",
            parse_mode="HTML",
        )
        return

    # Weighted automatic selection is intentionally cached, so reload it after
    # every state change. The engine still performs a live locked check before
    # posting in case a cache reload races/fails.
    cache_reloaded = True
    try:
        await reload_character_cache()
    except Exception as exc:
        cache_reloaded = False
        log.exception("could not reload character cache after /%s: %s", command, exc)

    label = escape(str(state["label"]))
    was_enabled = bool(state["was_spawn_enabled"])
    if enabled:
        heading = "✅ <b>Event spawning restored.</b>" if not was_enabled else "ℹ️ <b>Event spawning was already enabled.</b>"
        detail = "Future cards from this event can spawn again."
    else:
        heading = "⏸️ <b>Event spawning stopped.</b>" if was_enabled else "ℹ️ <b>Event spawning was already stopped.</b>"
        detail = (
            "No future card from this event can be spawned until "
            "<code>/restorespawn</code> is used. Any card already active remains catchable."
        )

    lines = [
        heading,
        f"🎊 Event: <b>{label}</b>",
        detail,
    ]
    cancelled_count = int(state["cancelled_scheduled_op_count"])
    if not enabled and cancelled_count:
        lines.append(f"Cancelled <b>{cancelled_count}</b> pending automatic OP spawn(s) for this event.")
    if not cache_reloaded:
        lines.append(
            "⚠️ The database state was saved, but the character cache could not reload. "
            "Use the Admin-panel cache reload before relying on random selection."
        )
    await message.reply("\n".join(lines), parse_mode="HTML")


@router.message(Command("stopspawn"))
async def cmd_stopspawn(message: Message):
    """Full owner: stop every future spawn for one named event globally."""
    await _set_event_spawn_state(message, enabled=False)


@router.message(Command("restorespawn"))
async def cmd_restorespawn(message: Message):
    """Full owner: allow future cards from one named event globally again."""
    await _set_event_spawn_state(message, enabled=True)


# ─── /stoprarityspawn and /spawnstart — global rarity spawn controls ───────

async def _set_rarity_spawn_state(message: Message, *, enabled: bool) -> None:
    """Apply one full-owner rarity-tier pause/restore separately from events."""
    actor = message.from_user
    if not actor or not await is_bot_owner(actor.id):
        return

    command = "spawnstart" if enabled else "stoprarityspawn"
    parts = (message.text or "").split(maxsplit=1)
    rarity_label = parts[1].strip() if len(parts) > 1 else ""
    if not rarity_label:
        await message.reply(
            f"❌ Usage: <code>/{command} &lt;rarity name&gt;</code>\n"
            f"Example: <code>/{command} Mythical</code>",
            parse_mode="HTML",
        )
        return

    state = await set_rarity_spawn_enabled(rarity_label, enabled=enabled)
    if not state:
        await message.reply(
            f"❌ Rarity <b>{escape(rarity_label)}</b> was not found. Check the tier name and try again.",
            parse_mode="HTML",
        )
        return
    if state.get("reason") == "love_not_spawnable":
        await message.reply(
            "ℹ️ <b>Love</b> cards are Lovelist-only and never spawn in groups.",
            parse_mode="HTML",
        )
        return

    # Weighted automatic selection is cached. The engine also performs a final
    # transaction-locked policy check, so the persisted pause stays safe even
    # if this best-effort cache refresh is temporarily unavailable.
    cache_reloaded = True
    try:
        await reload_character_cache()
    except Exception as exc:
        cache_reloaded = False
        log.exception("could not reload character cache after /%s: %s", command, exc)

    label = escape(str(state["label"]))
    was_enabled = bool(state["was_spawn_enabled"])
    if enabled:
        heading = "✅ <b>Rarity spawning restored.</b>" if not was_enabled else "ℹ️ <b>Rarity spawning was already enabled.</b>"
        detail = "Future cards in this rarity can spawn again."
    else:
        heading = "⏸️ <b>Rarity spawning stopped.</b>" if was_enabled else "ℹ️ <b>Rarity spawning was already stopped.</b>"
        detail = (
            "No future card in this rarity can spawn until "
            "<code>/spawnstart</code> is used. Any card already active remains catchable."
        )

    lines = [heading, f"🔱 Rarity: <b>{label}</b>", detail]
    cancelled_count = int(state["cancelled_scheduled_op_count"])
    if not enabled and cancelled_count:
        lines.append(f"Cancelled <b>{cancelled_count}</b> pending automatic OP spawn(s) in this rarity.")
    if not cache_reloaded:
        lines.append(
            "⚠️ The database state was saved, but the character cache could not reload. "
            "Use the Admin-panel cache reload before relying on random selection."
        )
    await message.reply("\n".join(lines), parse_mode="HTML")


@router.message(Command("stoprarityspawn"))
async def cmd_stop_rarity_spawn(message: Message):
    """Full owner: stop every future spawn for one named rarity tier globally."""
    await _set_rarity_spawn_state(message, enabled=False)


@router.message(Command("spawnstart"))
async def cmd_spawn_start(message: Message):
    """Full owner: allow future cards from one named rarity tier globally again."""
    await _set_rarity_spawn_state(message, enabled=True)


@router.message(Command("kill"))
async def cmd_kill(message: Message):
    """Full owner: pause member-facing messages, callbacks, and inline mode."""
    actor = message.from_user
    if not actor or not await is_bot_owner(actor.id):
        return
    await set_bot_killed(True, actor.id)
    await message.reply(
        "🔒 <b>Maintenance mode enabled.</b>\n"
        "Members are now blocked in messages, callbacks, and inline mode. "
        "Root/full owners and catalog admins remain enabled.",
        parse_mode="HTML",
    )


@router.message(Command("unkill"))
async def cmd_unkill(message: Message):
    """Full owner: restore normal member access after a global kill."""
    actor = message.from_user
    if not actor or not await is_bot_owner(actor.id):
        return
    await set_bot_killed(False, actor.id)
    await message.reply(
        "🔓 <b>Maintenance mode disabled.</b> All members can use the bot again.",
        parse_mode="HTML",
    )


@router.message(Command("upadmin"))
async def cmd_upad(message: Message):
    """Grant catalog access by reply, or let the root owner use a numeric ID."""
    actor = message.from_user
    if not actor or not await is_bot_owner(actor.id):
        return

    has_numeric_target, numeric_target_id = _numeric_role_target_argument(message)
    if has_numeric_target:
        # A reply-targeted /upadmin remains available to every full owner.  The
        # direct-ID form intentionally belongs only to the configured root
        # owner, because it can grant access to an account never seen by the bot.
        if not is_root_owner(actor.id):
            await message.reply(
                "🚫 Only the primary owner can grant a role by numeric user ID.",
                parse_mode="HTML",
            )
            return
        if numeric_target_id is None:
            await message.reply(
                "❌ Usage: <code>/upadmin &lt;positive_user_id&gt;</code>\n"
                "Or reply to a user with <code>/upadmin</code>.",
                parse_mode="HTML",
            )
            return
        if is_root_owner(numeric_target_id):
            await message.reply("ℹ️ This user is already the primary owner.")
            return
        if await is_bot_owner(numeric_target_id):
            await message.reply("ℹ️ This user already has full owner access.")
            return

        await grant_catalog_admin(numeric_target_id, actor.id)
        await message.reply(
            f"✅ User <code>{numeric_target_id}</code> is now a catalog admin.\n"
            "Allowed commands: <code>/addcharacter</code>, <code>/updatecharacter</code>, "
            "<code>/addevent</code>, <code>/deleteevent</code>, and <code>/eventchange</code>.",
            parse_mode="HTML",
        )
        return

    target = _reply_target(message)
    if not target:
        await message.reply(
            "❌ Reply to a user with <code>/upadmin</code> or use "
            "<code>/upadmin &lt;positive_user_id&gt;</code> as the primary owner.",
            parse_mode="HTML",
        )
        return
    if target.is_bot:
        await message.reply("🚫 A bot cannot be a catalog admin.")
        return
    if is_root_owner(target.id) or await is_bot_owner(target.id):
        await message.reply("ℹ️ This user already has full owner access.")
        return

    await grant_catalog_admin(target.id, actor.id)
    await message.reply(
        f"✅ <b>{escape(target.full_name)}</b> is now a catalog admin.\n"
        "Allowed commands: <code>/addcharacter</code>, <code>/updatecharacter</code>, "
        "<code>/addevent</code>, <code>/deleteevent</code>, and <code>/eventchange</code>.",
        parse_mode="HTML",
    )


@router.message(Command("upowner"))
async def cmd_upow(message: Message):
    """Root owner: grant full ownership by reply or a numeric Telegram user ID."""
    actor = message.from_user
    if not actor or not is_root_owner(actor.id):
        return

    has_numeric_target, numeric_target_id = _numeric_role_target_argument(message)
    if has_numeric_target:
        if numeric_target_id is None:
            await message.reply(
                "❌ Usage: <code>/upowner &lt;positive_user_id&gt;</code>\n"
                "Or reply to a user with <code>/upowner</code>.",
                parse_mode="HTML",
            )
            return
        if is_root_owner(numeric_target_id):
            await message.reply("ℹ️ This user is already the primary owner.")
            return

        await grant_bot_owner(numeric_target_id, actor.id)
        await message.reply(
            f"👑 User <code>{numeric_target_id}</code> is now a full bot owner.",
            parse_mode="HTML",
        )
        return

    target = _reply_target(message)
    if not target:
        await message.reply(
            "❌ Reply to a user with <code>/upowner</code> or use "
            "<code>/upowner &lt;positive_user_id&gt;</code>.",
            parse_mode="HTML",
        )
        return
    if target.is_bot:
        await message.reply("🚫 A bot cannot be promoted to owner.")
        return
    if is_root_owner(target.id):
        await message.reply("ℹ️ This user is already the primary owner.")
        return

    await grant_bot_owner(target.id, actor.id)
    await message.reply(
        f"👑 <b>{escape(target.full_name)}</b> is now a full bot owner.",
        parse_mode="HTML",
    )


# ─── /downowner, /downadmin, /mainlist — persistent role removal/listing ───────────────
def _role_member_lines(member_ids: list[int]) -> list[str]:
    return [f"• <code>{int(member_id)}</code>" for member_id in member_ids] or ["• <i>none</i>"]


def _role_list_pages(role_members: dict[str, list[int]]) -> list[str]:
    """Render role IDs in Telegram-safe chunks while retaining all members."""
    owner_ids = sorted({int(value) for value in role_members.get("owner_ids", [])})
    admin_ids = sorted({int(value) for value in role_members.get("admin_ids", [])} - set(owner_ids))
    lines = [
        "👥 <b>Bot Role Members</b>",
        "────────────────────────",
        f"👑 <b>Full Owners ({len(owner_ids)})</b>",
        *_role_member_lines(owner_ids),
        "",
        f"🛠 <b>Catalog Admins ({len(admin_ids)})</b>",
        *_role_member_lines(admin_ids),
    ]

    # A normal roster fits in one Telegram message. Keep the fallback so a
    # large historic roster never makes /mainlist silently fail at 4096 chars.
    maximum_length = 3_800
    pages: list[str] = []
    current: list[str] = []
    for line in lines:
        candidate = "\n".join([*current, line])
        if current and len(candidate) > maximum_length:
            pages.append("\n".join(current))
            current = ["👥 <b>Bot Role Members (continued)</b>", "────────────────────────", line]
        else:
            current.append(line)
    if current:
        pages.append("\n".join(current))
    return pages


@router.message(Command("downowner"))
async def cmd_dnow(message: Message) -> None:
    """Root owner: remove a delegated full owner by reply or numeric ID."""
    actor = message.from_user
    if not actor or not is_root_owner(actor.id):
        return

    has_numeric_target, numeric_target_id = _numeric_role_target_argument(message)
    if has_numeric_target:
        if numeric_target_id is None:
            await message.reply(
                "❌ Usage: <code>/downowner &lt;positive_user_id&gt;</code>\n"
                "Or reply to a full owner with <code>/downowner</code>.",
                parse_mode="HTML",
            )
            return
        target_id = numeric_target_id
        target_label = f"User <code>{target_id}</code>"
    else:
        target = _reply_target(message)
        if not target:
            await message.reply(
                "❌ Reply to a full owner with <code>/downowner</code> or use "
                "<code>/downowner &lt;positive_user_id&gt;</code>.",
                parse_mode="HTML",
            )
            return
        if target.is_bot:
            await message.reply("🚫 A bot cannot be a full bot owner.")
            return
        target_id = int(target.id)
        target_label = f"<b>{escape(target.full_name)}</b> (<code>{target_id}</code>)"

    if is_root_owner(target_id):
        await message.reply("🚫 The configured primary owner cannot be removed.")
        return
    if not await is_bot_owner(target_id):
        await message.reply("ℹ️ This user is not a delegated full bot owner.")
        return
    if not await revoke_bot_owner(target_id):
        await message.reply("ℹ️ This full-owner role is already gone.")
        return

    await message.reply(
        f"✅ {target_label} was removed from the full-owner role.",
        parse_mode="HTML",
    )


@router.message(Command("downadmin"))
async def cmd_dnad(message: Message) -> None:
    """Full owner: remove a standalone catalog admin by reply or numeric ID."""
    actor = message.from_user
    if not actor or not await is_bot_owner(actor.id):
        return

    has_numeric_target, numeric_target_id = _numeric_role_target_argument(message)
    if has_numeric_target:
        if numeric_target_id is None:
            await message.reply(
                "❌ Usage: <code>/downadmin &lt;positive_user_id&gt;</code>\n"
                "Or reply to a catalog admin with <code>/downadmin</code>.",
                parse_mode="HTML",
            )
            return
        target_id = numeric_target_id
        target_label = f"User <code>{target_id}</code>"
    else:
        target = _reply_target(message)
        if not target:
            await message.reply(
                "❌ Reply to a catalog admin with <code>/downadmin</code> or use "
                "<code>/downadmin &lt;positive_user_id&gt;</code>.",
                parse_mode="HTML",
            )
            return
        if target.is_bot:
            await message.reply("🚫 A bot cannot be a catalog admin.")
            return
        target_id = int(target.id)
        target_label = f"<b>{escape(target.full_name)}</b> (<code>{target_id}</code>)"

    if await is_bot_owner(target_id):
        await message.reply("ℹ️ This user is a full owner, not a catalog admin.")
        return
    if not await revoke_catalog_admin(target_id):
        await message.reply("ℹ️ This user is not a catalog admin.")
        return

    await message.reply(
        f"✅ {target_label} was removed from the catalog-admin role.",
        parse_mode="HTML",
    )


@router.message(Command("mainlist"))
async def cmd_mlist(message: Message) -> None:
    """Full owner: show numeric IDs for every effective owner/admin role."""
    actor = message.from_user
    if not actor or not await is_bot_owner(actor.id):
        return

    try:
        role_members = await list_role_members()
        pages = _role_list_pages(role_members)
    except Exception:
        log.exception("could not build /mainlist role roster")
        await message.reply(
            "⚠️ I could not load the role member list right now. Please try again.",
            parse_mode="HTML",
        )
        return

    for index, page in enumerate(pages):
        if index == 0:
            await message.reply(page, parse_mode="HTML")
        else:
            await message.answer(page, parse_mode="HTML")


def _deletedatabase_keyboard(owner_id: int, job_id: int) -> InlineKeyboardMarkup:
    """A persisted single-use root confirmation; callback data stays compact."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="🗑️ Delete all player data",
            callback_data=f"dbreset:confirm:{owner_id}:{job_id}",
            style="danger",
        ),
        InlineKeyboardButton(
            text="❌ Cancel",
            callback_data=f"dbreset:cancel:{owner_id}:{job_id}",
            style="success",
        ),
    ]])


@router.message(Command("deletedatabase"))
async def cmd_deletedatabase(message: Message) -> None:
    """Root owner: request a deliberate no-backup reset while preserving catalog art."""
    actor = message.from_user
    if not actor or not is_root_owner(actor.id):
        return
    if len((message.text or "").split()) != 1:
        await message.reply("❌ Usage: <code>/deletedatabase</code>", parse_mode="HTML")
        return

    job_id = await create_player_database_reset_job(actor.id)
    await message.reply(
        "⚠️ <b>Delete all player game data?</b>\n\n"
        "This is an irreversible <b>root-owner-only</b> reset. <b>No backup will be created.</b>\n\n"
        "It permanently clears every Harem copy (including player-market escrow), "
        "pending card actions, collection achievements, Neko wallets/passes/games, "
        "Daily Neko state, card daily state, catch limits, and catch leaderboards. "
        "Visible active cards and queued OP spawns are also cancelled.\n\n"
        "✅ <b>Preserved:</b> catalog <code>characters</code>, catalog events, group settings, "
        "owners/admins, and official catalog Market listings.\n\n"
        "This confirmation expires in 15 minutes. Only you can confirm it.",
        parse_mode="HTML",
        reply_markup=_deletedatabase_keyboard(actor.id, job_id),
    )


@router.callback_query(F.data.startswith("dbreset:"))
async def cb_deletedatabase(callback: CallbackQuery) -> None:
    """Consume exactly one live root confirmation and execute the atomic wipe."""
    parts = (callback.data or "").split(":")
    if (
        len(parts) != 4
        or parts[1] not in {"confirm", "cancel"}
        or not parts[2].isdigit()
        or not parts[3].isdigit()
    ):
        await callback.answer("Invalid database reset action.", show_alert=True)
        return

    action, owner_id, job_id = parts[1], int(parts[2]), int(parts[3])
    if callback.from_user.id != owner_id:
        await callback.answer("⛔ Only the requesting root owner can confirm this.", show_alert=True)
        return
    if not is_root_owner(callback.from_user.id):
        await callback.answer("⛔ Root owner only.", show_alert=True)
        return

    if action == "cancel":
        if not await cancel_player_database_reset_job(job_id, owner_id):
            await callback.answer("This reset confirmation is no longer pending.", show_alert=True)
            return
        await _replace_card(callback, "❌ <b>Database deletion cancelled.</b>")
        await callback.answer("Cancelled")
        return

    if not await claim_player_database_reset_job(job_id, owner_id):
        await callback.answer(
            "This reset confirmation is expired or has already been used.",
            show_alert=True,
        )
        return

    await _replace_card(
        callback,
        "⏳ <b>Deleting all player game data…</b>\n"
        "The catalog characters are being preserved. Please wait.",
    )
    await callback.answer("Database deletion started")
    try:
        result = await reset_all_player_game_state()
    except Exception as exc:
        log.exception("root player database reset job %s failed", job_id)
        try:
            await finish_player_database_reset_job(
                job_id,
                owner_id,
                success=False,
                error_detail=type(exc).__name__,
            )
        except Exception:
            log.exception("could not mark failed database reset job %s", job_id)
        await _replace_card(
            callback,
            "❌ <b>Database deletion failed.</b> No partial changes were committed; please try again.",
        )
        return

    try:
        await finish_player_database_reset_job(job_id, owner_id, success=True)
    except Exception:
        # The reset has already committed atomically. A job-status write cannot
        # change that fact, so preserve accurate completion feedback and log it.
        log.exception("could not mark completed database reset job %s", job_id)

    await _replace_card(
        callback,
        "✅ <b>All player game data was deleted.</b>\n\n"
        f"Removed <b>{result.harem_copies_removed}</b> Harem copy/copies, "
        f"<b>{result.player_market_listings_removed}</b> player Market escrow listing(s), "
        f"and invalidated <b>{result.active_spawns_removed}</b> active spawn(s) plus "
        f"<b>{result.scheduled_op_spawns_removed}</b> queued OP spawn(s).\n"
        f"Reset card/Daily Neko/Neko game state and catch limits for <b>{result.users_reset}</b> user(s).\n\n"
        "No backup was created. Catalog <code>characters</code> and their media were preserved.",
    )


@router.message(Command("deleteharem"))
async def cmd_dharem(message: Message):
    """Full owner: ask for confirmation before clearing a user's harem."""
    actor = message.from_user
    if not actor or not await is_bot_owner(actor.id):
        return

    target = _reply_target(message)
    if not target:
        await message.reply("❌ Reply to a user with <code>/deleteharem</code>.", parse_mode="HTML")
        return
    if target.is_bot:
        await message.reply("🚫 A bot does not have a collection to delete.")
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="✅ Delete collection",
            callback_data=f"dharem:confirm:{actor.id}:{target.id}",
            style="success",
        ),
        InlineKeyboardButton(
            text="❌ Cancel",
            callback_data=f"dharem:cancel:{actor.id}",
            style="danger",
        ),
    ]])
    await message.reply(
        "⚠️ <b>Delete this user's entire collection?</b>\n\n"
        f"User: <b>{escape(target.full_name)}</b>\n"
        f"ID: <code>{target.id}</code>\n\n"
        "This clears every live harem record and collection stat, and fails "
        "their pending trades and gifts. A private recovery snapshot is saved "
        "for an owner to restore later with <code>/backharem</code>.",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


@router.callback_query(F.data.startswith("dharem:"))
async def cb_dharem(callback: CallbackQuery):
    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    owner_id = parts[2] if len(parts) > 2 else ""

    if not owner_id.isdigit() or callback.from_user.id != int(owner_id):
        await callback.answer("⛔ Only the requesting owner can confirm this.", show_alert=True)
        return
    if not await is_bot_owner(callback.from_user.id):
        await callback.answer("⛔ Owner only.", show_alert=True)
        return

    if action == "cancel":
        await _replace_card(callback, "❌ <b>Collection deletion cancelled.</b>")
        await callback.answer("Cancelled")
        return

    if action != "confirm" or len(parts) != 4 or not parts[3].isdigit():
        await callback.answer("Invalid action.", show_alert=True)
        return

    target_id = int(parts[3])
    deleted = await delete_user_harem(target_id, deleted_by=callback.from_user.id)
    if deleted.backup_id is None:
        text = (
            "ℹ️ <b>No collection records were found.</b>\n"
            f"User <code>{target_id}</code> had nothing to archive or remove."
        )
    else:
        text = (
            "🗑️ <b>Collection cleared.</b>\n"
            f"Removed <b>{deleted.deleted_count}</b> harem record(s) from user "
            f"<code>{target_id}</code>.\n"
            f"♻️ Recovery backup <code>#{deleted.backup_id}</code> was saved. "
            "Reply to this user with <code>/backharem</code> to restore it."
        )
    await _replace_card(callback, text)
    await callback.answer("Collection cleared")


# ─── /deletefromharem — permanent global multi-code owner purge ───────────

def _parse_global_harem_purge_codes(message: Message) -> tuple[list[int] | None, str | None]:
    """Parse unique positive catalog IDs after `/deletefromharem` safely."""
    tokens = (message.text or "").split()[1:]
    if not tokens:
        return None, "missing"
    if len(tokens) > MAX_GLOBAL_HAREM_PURGE_CODES:
        return None, "too_many"

    codes: list[int] = []
    seen: set[int] = set()
    for token in tokens:
        if not re.fullmatch(r"[0-9]{1,19}", token):
            return None, "invalid"
        code = int(token)
        if not 0 < code <= GLOBAL_HAREM_PURGE_MAX_CHARACTER_ID:
            return None, "invalid"
        if code not in seen:
            seen.add(code)
            codes.append(code)
    return codes, None


def _global_harem_purge_keyboard(owner_id: int, job_id: int) -> InlineKeyboardMarkup:
    """Bind a destructive bulk-confirmation button to its owner and DB job."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="🗑️ Permanently remove from all Harems",
            callback_data=f"ghpurge:confirm:{owner_id}:{job_id}",
            style="danger",
        ),
        InlineKeyboardButton(
            text="❌ Cancel",
            callback_data=f"ghpurge:cancel:{owner_id}:{job_id}",
        ),
    ]])


def _global_harem_purge_preview_lines(characters: list[dict]) -> list[str]:
    """Render a compact confirmation-safe catalog preview for up to 100 codes."""
    preview_limit = 16
    lines = [
        f"• <code>{int(character['id'])}</code> — "
        f"{escape(str(character.get('name') or 'Unknown'))} "
        f"({escape(str(character.get('rarity') or 'Common'))})"
        for character in characters[:preview_limit]
    ]
    remaining = len(characters) - len(lines)
    if remaining:
        lines.append(f"• … and {remaining} more code(s).")
    return lines


@router.message(Command("deletefromharem"))
async def cmd_deletefromharem(message: Message) -> None:
    """Full Owner: permanently purge selected catalog codes from every Harem."""
    actor = message.from_user
    if not actor or not await is_bot_owner(actor.id):
        return

    codes, error = _parse_global_harem_purge_codes(message)
    if error == "missing":
        await message.reply(
            "❌ Usage: <code>/deletefromharem &lt;code&gt; [more codes]</code>\n"
            "Example: <code>/deletefromharem 12 45 78</code>",
            parse_mode="HTML",
        )
        return
    if error == "too_many":
        await message.reply(
            f"❌ You can remove up to <b>{MAX_GLOBAL_HAREM_PURGE_CODES}</b> catalog codes at once.",
            parse_mode="HTML",
        )
        return
    if error or not codes:
        await message.reply(
            "❌ Every value after <code>/deletefromharem</code> must be a positive catalog ID.",
            parse_mode="HTML",
        )
        return

    # Preview current catalog definitions before persisting a confirmation job.
    # The service repeats the authoritative catalog validation in its own DB
    # transaction so a card cannot be substituted between preview and click.
    characters_by_id = await get_characters_by_ids(codes)
    missing_codes = [code for code in codes if code not in characters_by_id]
    if missing_codes:
        await message.reply(
            "❌ No catalog character was found for code(s): <code>"
            + ", ".join(str(code) for code in missing_codes)
            + "</code>.",
            parse_mode="HTML",
        )
        return
    characters = [characters_by_id[code] for code in codes]

    job_id, reason, missing = await create_global_harem_purge_job(
        owner_id=actor.id,
        character_ids=codes,
    )
    if job_id is None:
        if reason == "character_not_found":
            await message.reply(
                "❌ These catalog code(s) disappeared before confirmation could be prepared: <code>"
                + ", ".join(str(code) for code in missing)
                + "</code>.",
                parse_mode="HTML",
            )
        else:
            await message.reply("❌ The global Harem deletion request could not be prepared.")
        return

    preview = "\n".join(_global_harem_purge_preview_lines(characters))
    await message.reply(
        "⚠️ <b>Permanently remove these codes from every player's Harem?</b>\n\n"
        f"{preview}\n\n"
        "This creates <b>no recovery backup</b>. It permanently removes all live Harem "
        "copies, player-Market escrow, Love progression/escrow state, and matching "
        "copies in pending recovery snapshots. Pending trades, gifts, and owner grants "
        "that could restore these codes will be failed.\n\n"
        "Catalog cards and official catalog-stock Market listings remain available for "
        "future use. Confirm only if this is intended.",
        parse_mode="HTML",
        reply_markup=_global_harem_purge_keyboard(actor.id, job_id),
    )


@router.callback_query(F.data.startswith("ghpurge:"))
async def cb_deletefromharem(callback: CallbackQuery) -> None:
    """Run or cancel one durable owner-bound global multi-code Harem purge."""
    parts = (callback.data or "").split(":")
    if (
        len(parts) != 4
        or parts[1] not in {"confirm", "cancel"}
        or not parts[2].isdigit()
        or not parts[3].isdigit()
        or not callback.from_user
    ):
        await callback.answer("Invalid global Harem deletion action.", show_alert=True)
        return

    action, owner_id, job_id = parts[1], int(parts[2]), int(parts[3])
    if callback.from_user.id != owner_id:
        await callback.answer("⛔ Only the requesting owner can confirm this.", show_alert=True)
        return
    if not await is_bot_owner(callback.from_user.id):
        await callback.answer("⛔ Owner only.", show_alert=True)
        return

    if action == "cancel":
        cancelled, _reason = await cancel_global_harem_purge_job(
            job_id=job_id,
            owner_id=owner_id,
        )
        if not cancelled:
            await callback.answer("This purge is no longer pending.", show_alert=True)
            return
        await _replace_card(callback, "❌ <b>Global Harem deletion cancelled.</b>")
        await callback.answer("Cancelled")
        return

    try:
        result = await execute_global_harem_purge_job(job_id=job_id, owner_id=owner_id)
    except Exception as exc:
        log.exception("could not complete /deletefromharem job %s: %s", job_id, exc)
        await _replace_card(
            callback,
            "⚠️ <b>The global Harem deletion was not completed.</b>\n"
            "No success was reported; review the database before sending a new request.",
        )
        await callback.answer("Purge failed", show_alert=True)
        return

    if not result.success:
        messages = {
            "not_found": "This global Harem deletion job no longer exists.",
            "not_owner": "Only the owner who created this purge can execute it.",
            "not_pending": "This global Harem deletion has already been resolved.",
            "invalid_arguments": "This global Harem deletion request is invalid.",
        }
        await callback.answer(messages.get(result.reason, "This purge could not be completed."), show_alert=True)
        return

    codes = ", ".join(str(code) for code in result.requested_character_ids)
    result_lines = [
        "🗑️ <b>Global Harem deletion complete.</b>",
        f"Codes: <code>{codes}</code>",
        f"Removed <b>{result.harem_copies_removed}</b> live Harem copy/copies.",
        f"Removed <b>{result.player_listings_removed}</b> player-Market escrow listing(s).",
        f"Removed <b>{result.love_states_removed}</b> Love state(s).",
        f"Permanently removed <b>{result.backup_items_removed}</b> recovery-backup item(s).",
        f"Failed <b>{result.pending_trades_failed}</b> pending trade(s), "
        f"<b>{result.pending_gifts_failed}</b> gift(s), and "
        f"<b>{result.pending_sudo_grants_failed}</b> owner grant(s).",
        "No recovery backup was created.",
    ]
    if result.missing_character_ids:
        result_lines.append(
            "Skipped code(s) no longer in the catalog: <code>"
            + ", ".join(str(code) for code in result.missing_character_ids)
            + "</code>."
        )
    await _replace_card(callback, "\n".join(result_lines))
    await callback.answer("Global Harem deletion complete")


# ─── /doomcharacter — owner delete exactly one copy ───────────────────

def _delharem_one_keyboard(owner_id: int, harem_id: int) -> InlineKeyboardMarkup:
    """Keep callback data under Telegram's 64-byte limit with one harem row ID."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="🗑️ Remove one copy",
            callback_data=f"delharem:one:confirm:{owner_id}:{harem_id}",
            style="danger",
        ),
        InlineKeyboardButton(
            text="❌ Cancel",
            callback_data=f"delharem:one:cancel:{owner_id}",
            style="primary",
        ),
    ]])


@router.message(Command("doomcharacter"))
async def cmd_delharem(message: Message):
    """Full owner: delete exactly one copy of a replied user's character."""
    actor = message.from_user
    if not actor or not await is_bot_owner(actor.id):
        return

    target = _reply_target(message)
    if not target:
        await message.reply(
            "❌ Reply to a user with <code>/doomcharacter &lt;character_id&gt;</code>.",
            parse_mode="HTML",
        )
        return
    if target.is_bot:
        await message.reply("🚫 A bot does not have a harem to delete.")
        return

    parts = (message.text or "").split(maxsplit=1)
    raw_character_id = parts[1].strip() if len(parts) > 1 else ""
    if not raw_character_id or not raw_character_id.isdigit() or int(raw_character_id) <= 0:
        await message.reply(
            "❌ Usage: reply to a user with <code>/doomcharacter &lt;character_id&gt;</code>.\n"
            "Use <code>/deleteharem</code> on a reply to clear the user's full harem.",
            parse_mode="HTML",
        )
        return

    character_id = int(raw_character_id)
    preview = await get_harem_copy_for_deletion(target.id, character_id)
    if not preview:
        await message.reply(
            f"❌ <b>{escape(target.full_name)}</b> does not own character "
            f"<code>{character_id}</code>.",
            parse_mode="HTML",
        )
        return

    await message.reply(
        "⚠️ <b>Remove exactly one character copy?</b>\n\n"
        f"User: <b>{escape(target.full_name)}</b>\n"
        f"Character: <code>{preview['character_id']}</code> — "
        f"<b>{escape(preview['name'])}</b>\n"
        f"Anime / Game: {escape(preview['anime'])}\n"
        f"Rarity: {rarity_symbol(preview['rarity'])} {escape(preview['rarity'])}\n"
        f"Current copies: <b>{int(preview['copy_count'])}</b>\n\n"
        "Exactly one copy will be removed. Other characters and copies stay. "
        "A non-favorite copy is selected first when possible.",
        parse_mode="HTML",
        reply_markup=_delharem_one_keyboard(actor.id, int(preview["harem_id"])),
    )


@router.callback_query(F.data.startswith("delharem:one:"))
async def cb_delharem(callback: CallbackQuery):
    """Execute an owner-bound one-copy /doomcharacter confirmation exactly once."""
    parts = (callback.data or "").split(":")
    if (
        len(parts) < 4
        or parts[1] != "one"
        or parts[2] not in {"confirm", "cancel"}
        or not parts[3].isdigit()
    ):
        await callback.answer("Invalid harem deletion action.", show_alert=True)
        return

    action, owner_id = parts[2], int(parts[3])
    if callback.from_user.id != owner_id:
        await callback.answer("⛔ Only the requesting owner can confirm this.", show_alert=True)
        return
    if not await is_bot_owner(callback.from_user.id):
        await callback.answer("⛔ Owner only.", show_alert=True)
        return

    if action == "cancel":
        if len(parts) != 4:
            await callback.answer("Invalid cancellation action.", show_alert=True)
            return
        await _replace_card(callback, "❌ <b>Harem deletion cancelled.</b>")
        await callback.answer("Cancelled")
        return

    if len(parts) != 5 or not parts[4].isdigit():
        await callback.answer("Invalid deletion action.", show_alert=True)
        return

    deleted_copy = await delete_harem_copy(int(parts[4]))
    if not deleted_copy.success:
        await _replace_card(
            callback,
            "❌ <b>This character copy is no longer available.</b>\n"
            "It may already have been removed or transferred.",
        )
        await callback.answer("Copy unavailable", show_alert=True)
        return

    await _replace_card(
        callback,
        "🗑️ <b>One character copy removed.</b>\n"
        f"User: <code>{deleted_copy.user_id}</code>\n"
        f"Character: <code>{deleted_copy.character_id}</code> — "
        f"<b>{escape(deleted_copy.character_name)}</b>\n"
        f"Remaining copies of this character: <b>{deleted_copy.remaining_copies}</b>",
    )
    await callback.answer("One copy removed")


def _bharem_keyboard(owner_id: int, backup_id: int) -> InlineKeyboardMarkup:
    """Confirmation controls tied to both the initiating owner and snapshot."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="♻️ Restore collection",
            callback_data=f"bharem:restore:{owner_id}:{backup_id}",
            style="success",
        ),
        InlineKeyboardButton(
            text="❌ Cancel",
            callback_data=f"bharem:cancel:{owner_id}:{backup_id}",
            style="danger",
        ),
    ]])


@router.message(Command("backharem"))
async def cmd_bharem(message: Message):
    """Owner-only: restore the latest collection archived by /deleteharem on reply."""
    actor = message.from_user
    if not actor or not await is_bot_owner(actor.id):
        return

    target = _reply_target(message)
    if not target:
        await message.reply("❌ Reply to a user with <code>/backharem</code>.", parse_mode="HTML")
        return
    if target.is_bot:
        await message.reply("🚫 A bot does not have a recoverable collection.")
        return

    backup = await get_latest_harem_deletion_backup(target.id)
    if not backup:
        await message.reply(
            "ℹ️ No pending <code>/deleteharem</code> recovery backup was found for this user.",
            parse_mode="HTML",
        )
        return

    backup_id = int(backup["id"])
    record_count = int(backup["record_count"])
    await message.reply(
        "♻️ <b>Restore this deleted collection?</b>\n\n"
        f"User: <b>{escape(target.full_name)}</b>\n"
        f"Recovery backup: <code>#{backup_id}</code>\n"
        f"Archived records: <b>{record_count}</b>\n\n"
        "Any characters caught after deletion stay in the harem. The recovery "
        "snapshot can only be restored once.",
        parse_mode="HTML",
        reply_markup=_bharem_keyboard(actor.id, backup_id),
    )


@router.callback_query(F.data.startswith("bharem:"))
async def cb_bharem(callback: CallbackQuery):
    """Restore exactly one pending backup after the requesting owner confirms."""
    parts = (callback.data or "").split(":")
    if (
        len(parts) != 4
        or parts[1] not in {"restore", "cancel"}
        or not parts[2].isdigit()
        or not parts[3].isdigit()
    ):
        await callback.answer("Invalid recovery action.", show_alert=True)
        return

    action, owner_id, backup_id = parts[1], int(parts[2]), int(parts[3])
    if callback.from_user.id != owner_id:
        await callback.answer("⛔ Only the requesting owner can confirm this.", show_alert=True)
        return
    if not await is_bot_owner(callback.from_user.id):
        await callback.answer("⛔ Owner only.", show_alert=True)
        return

    if action == "cancel":
        await _replace_card(callback, "❌ <b>Collection recovery cancelled.</b>")
        await callback.answer("Cancelled")
        return

    restored = await restore_harem_deletion_backup(
        backup_id,
        restored_by=callback.from_user.id,
    )
    if not restored.success:
        await _replace_card(
            callback,
            "❌ <b>This recovery backup is no longer available.</b>\n"
            "It may already have been restored.",
        )
        await callback.answer("Recovery unavailable", show_alert=True)
        return

    if restored.reason == "empty_backup":
        text = "ℹ️ <b>The recovery backup was empty.</b> Nothing needed to be restored."
    else:
        text = (
            "♻️ <b>Collection restored.</b>\n"
            f"Returned <b>{restored.restored_count}</b> archived harem record(s) "
            f"to user <code>{restored.user_id}</code>."
        )
    await _replace_card(callback, text)
    await callback.answer("Collection restored")


# ─── /ownercheck — owner-only character reports and event-code lookup ─────────

_REPORT_NAME_WIDTH = 34
_REPORT_USER_ID_WIDTH = 14
_REPORT_COUNT_WIDTH = 7


def _clean_report_cell(value: object, width: int) -> str:
    """Keep untrusted Telegram names to one safe, readable table cell."""
    cleaned = " ".join(str(value or "Unknown User").replace("|", "¦").split())
    if not cleaned:
        cleaned = "Unknown User"
    if len(cleaned) > width:
        return cleaned[: max(1, width - 1)] + "…"
    return cleaned


def _ocheck_report_text(report: dict, admin_name: str, admin_id: int) -> str:
    """Build the UTF-8 text-file body from live global database statistics."""
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    rarity = str(report["rarity"])
    admin_label = _clean_report_cell(admin_name, 80)
    lines = [
        "╔════════════════════ CHARACTER CATCH REPORT ════════════════════╗",
        "║          Live Global Collection Analytics • Owner Only          ║",
        "╚═════════════════════════════════════════════════════════════════╝",
        "",
        "◆ CHARACTER DETAILS",
        f"  • Character ID          : {int(report['id'])}",
        f"  • Character Name        : {_clean_report_cell(report['name'], 120)}",
        f"  • Anime / Game          : {_clean_report_cell(report['anime'], 120)}",
        f"  • Rarity                : {rarity_symbol(rarity)} 𝗥𝗔𝗥𝗜𝗧𝗬: {rarity_label_bold(rarity)}",
        f"  • Total Global Catches  : {int(report['total_catches'])}",
        f"  • Total Unique Catchers : {int(report['total_unique_catchers'])}",
        f"  • Generated for Admin   : {admin_label} ({admin_id})",
        f"  • Generated at          : {generated_at}",
        "",
        "┌──────┬────────────────────────────────────┬────────────────┬─────────┐",
        "│ RANK │ USER NAME                          │ USER ID        │ CATCHES │",
        "├──────┼────────────────────────────────────┼────────────────┼─────────┤",
    ]

    catchers = report["catchers"]
    if catchers:
        for rank, catcher in enumerate(catchers, start=1):
            name = _clean_report_cell(catcher["display_name"], _REPORT_NAME_WIDTH)
            user_id = str(int(catcher["user_id"]))
            count = int(catcher["catch_count"])
            lines.append(
                f"│ {rank:>4} │ {name:<{_REPORT_NAME_WIDTH}} │ "
                f"{user_id:>{_REPORT_USER_ID_WIDTH}} │ {count:>{_REPORT_COUNT_WIDTH}} │"
            )
    else:
        lines.append("│  --  │ No one has caught this character yet. │                │       0 │")

    lines.extend([
        "└──────┴────────────────────────────────────┴────────────────┴─────────┘",
        "",
        "════════════════════════════ END OF REPORT ═══════════════════════════",
    ])
    return "\n".join(lines) + "\n"


def _ocheck_caption(report: dict) -> str:
    """Compact styled Telegram caption for the full downloadable report."""
    return (
        "📊 <b>𝗔𝗗𝗠𝗜𝗡 𝗥𝗘𝗣𝗢𝗥𝗧 — 𝗖𝗛𝗔𝗥𝗔𝗖𝗧𝗘𝗥</b> "
        f"<code>{int(report['id'])}</code>\n"
        f"╭─ 📝 <b>𝗨𝗡𝗜𝗤𝗨𝗘 𝗖𝗔𝗧𝗖𝗛𝗘𝗥𝗦:</b> {int(report['total_unique_catchers'])}\n"
        f"╰─ 🎯 <b>𝗧𝗢𝗧𝗔𝗟 𝗚𝗟𝗢𝗕𝗔𝗟 𝗖𝗔𝗧𝗖𝗛𝗘𝗦:</b> {int(report['total_catches'])}"
    )


@router.message(Command("ownercheck"))
async def cmd_ownercheck(message: Message):
    """Owner-only: one character report or event-code lookup."""
    actor = message.from_user
    if not actor or not await is_bot_owner(actor.id):
        return

    parts = (message.text or "").split(maxsplit=1)
    query = parts[1].strip() if len(parts) > 1 else ""
    if not query:
        await message.reply(
            "❌ Usage: <code>/ownercheck &lt;character_id&gt;</code> or "
            "<code>/ownercheck &lt;event name&gt;</code>\n"
            "Example: <code>/ownercheck bunny🐰</code>",
            parse_mode="HTML",
        )
        return

    # Keep the existing numeric-ID report exactly intact. Every other
    # non-numeric argument is an event-label lookup, including multi-word
    # event names.
    if not query.isdigit():
        event = await get_event_character_codes(query)
        if not event:
            await message.reply(
                f"❌ Event <b>{escape(query)}</b> was not found. Check the label and try again.",
                parse_mode="HTML",
            )
            return

        character_ids = event["character_ids"]
        label = escape(str(event["label"]))
        if not character_ids:
            await message.reply(
                f"ℹ️ Event <b>{label}</b> exists but has no assigned character codes yet.",
                parse_mode="HTML",
            )
            return

        codes = " ".join(str(character_id) for character_id in character_ids)
        await message.reply(
            "🎊 <b>Event character codes</b>\n"
            f"🎉 Event: <b>{label}</b>\n"
            f"🎴 Cards: <b>{len(character_ids)}</b>\n"
            f"<code>{codes}</code>",
            parse_mode="HTML",
        )
        return

    character_id = int(query)
    if character_id <= 0:
        await message.reply(
            "❌ Usage: <code>/ownercheck &lt;character_id&gt;</code> or "
            "<code>/ownercheck &lt;event name&gt;</code>",
            parse_mode="HTML",
        )
        return
    report = await get_character_catch_report(character_id)
    if not report:
        await message.reply(
            f"❌ No character with ID <code>{character_id}</code> was found.",
            parse_mode="HTML",
        )
        return

    report_bytes = _ocheck_report_text(report, actor.full_name, actor.id).encode("utf-8")
    document = BufferedInputFile(
        report_bytes,
        filename=f"character_catch_report_{character_id}.txt",
    )
    await message.answer_document(
        document=document,
        caption=_ocheck_caption(report),
        parse_mode="HTML",
    )
