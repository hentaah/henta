"""Spawn engine — picks a weighted-random character and posts it to a chat.

Spawns are triggered only by a group message-count threshold: every N group
messages, where N is the per-group owner setting.

The active spawn is authoritative in `active_spawns`; Redis is only a
best-effort cache whose revision is checked before a catch. The Telegram
message_id is captured so we can edit it on claim.
"""
from __future__ import annotations

import asyncio
import logging
import json
import random
import time
import uuid
from dataclasses import dataclass
from bot.services.rich_renderer import rarity_symbol
from aiogram import Bot

from bot.config import HENTACATCHER_GP_CHAT_ID, default_spawn_interval_for_group, settings
from bot.db.pool import get_pool
from bot.services.redis_client import get_redis
from bot.services.special_ranks import (
    MAIN_SUSPICIOUS_SPAWNS_REQUIRED,
    OVERLORD_COUNTDOWN_REMAINING,
    OVERLORD_LOWER_SPAWNS_REQUIRED,
    OVERLORD_NOTICE_AT_LOWER_SPAWNS,
    OVERLORD_RARITY,
    OVERLORD_UNIQUE_OWNER_CAP,
    SUSPICIOUS_EXTERNAL_LOWER_SPAWNS_REQUIRED,
    SUSPICIOUS_MAIN_GROUP_WEIGHT,
    SUSPICIOUS_RARITY,
    is_love,
    is_overlord,
    is_suspicious,
)

# Automatic flagship-group rarity weights — rarer = less frequent. Eternal,
# Love, Suspicious, and OverLord are selected (or excluded) by dedicated policy.
# OP has the requested automatic spawn weight of 3.
RARITY_WEIGHTS = {
    OVERLORD_RARITY: 0,
    SUSPICIOUS_RARITY: SUSPICIOUS_MAIN_GROUP_WEIGHT,
    "Love":      0,
    "Eternal":   0,
    "OP":        3,
    "Sovereign": 3,
    "Mythical":  6,
    "Legendary": 15,
    "Rare":      40,
    "Common":    60,
}

# Outside the flagship group Suspicious is cadence-only: after 70 lower-rank
# automatic cards, the next automatic card is selected directly as Suspicious.
EXTERNAL_RARITY_WEIGHTS = {
    **RARITY_WEIGHTS,
    SUSPICIOUS_RARITY: 0,
    OVERLORD_RARITY: 0,
}

# Owner /spawn behavior retains the historic manual Eternal option. Love is
# never force-spawnable; OverLord remains zero-weight even here and may only be
# explicitly owner-spawned in the flagship group under its ten-owner cap.
FORCED_SPAWN_RARITY_WEIGHTS = {
    **RARITY_WEIGHTS,
    "Eternal": 0.5,
    "Love": 0,
    SUSPICIOUS_RARITY: SUSPICIOUS_MAIN_GROUP_WEIGHT,
    OVERLORD_RARITY: 0,
}

# An OP card is selected first, announced once, then persisted in the small
# queue below so it can be emitted exactly one minute later—even after a bot
# restart. The text is intentionally the exact wording requested for players.
OP_SPAWN_ANNOUNCEMENT_DELAY_SECONDS = 60
OP_SPAWN_ANNOUNCEMENT_TEXT = "Special Character Going To Spawn In 1 Minute!"
SCHEDULED_OP_SPAWN_BATCH_SIZE = 20
# A short lease lets a restarted process reclaim a card that was selected but
# not yet emitted, while normally keeping one worker responsible for each row.
OP_SPAWN_PROCESSING_LEASE_SECONDS = 30

OVERLORD_COUNTDOWN_NOTICE = (
    f"☬ OverLord countdown: {OVERLORD_COUNTDOWN_REMAINING} lower-rank character "
    "spawns remain before an OverLord card can appear."
)

log = logging.getLogger(__name__)

# A selected ID is excluded from the next five spawn events in the same group.
# The queue is persisted in groups.recent_spawn_ids, rather than kept only in
# memory, so restarting the bot cannot immediately repeat an art.
RECENT_SPAWN_EXCLUSION_COUNT = 5


class ForcedSpawnError(RuntimeError):
    """A selected card cannot be emitted under the current spawn policy."""

    def __init__(self, reason: str, character_id: int):
        super().__init__(reason)
        self.reason = reason
        self.character_id = character_id


# Cache of (id, name, anime, rarity, rarity_score, file_id, media_type, aliases) tuples
# Loaded once at startup from Postgres, refreshed periodically.
_character_cache: list[tuple] | None = None
_cache_lock = asyncio.Lock()
# Serializes automatic selection/emission per chat in this worker. PostgreSQL
# remains durable for all counters; this narrow local guard prevents a burst of
# 100+ messages from preparing two stale special-rank selections before the
# first visible spawn has updated its cadence row.
_automatic_spawn_locks: dict[int, asyncio.Lock] = {}
_automatic_spawn_locks_guard = asyncio.Lock()


async def _automatic_spawn_lock_for(chat_id: int) -> asyncio.Lock:
    """Return a stable in-process automatic-spawn lock for one group."""
    async with _automatic_spawn_locks_guard:
        lock = _automatic_spawn_locks.get(int(chat_id))
        if lock is None:
            lock = asyncio.Lock()
            _automatic_spawn_locks[int(chat_id)] = lock
        return lock


@dataclass
class SpawnContext:
    chat_id: int
    character_id: int
    name: str
    anime: str
    rarity: str
    rarity_score: int
    image_url: str
    media_type: str
    aliases: str
    message_id: int
    spawned_at: float
    expires_at: int


async def load_character_cache() -> int:
    """Pre-load only currently spawn-enabled characters for weighted random.

    Event controls (`/stopspawn`) and independent rarity controls
    (`/stoprarityspawn`) both filter this cache. A missing control row means
    enabled, while the final locked live check at emission remains authoritative
    if an owner changes either policy after cache selection.
    """
    global _character_cache
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT c.id, c.name, c.anime, c.rarity, c.rarity_score, c.image_url,
                   COALESCE(c.media_type, 'photo') AS media_type, c.aliases
            FROM characters AS c
            LEFT JOIN catalog_events AS event_control
              ON LOWER(BTRIM(event_control.label)) =
                 LOWER(BTRIM(COALESCE(c.event, '')))
            LEFT JOIN catalog_rarity_spawn_controls AS rarity_control
              ON rarity_control.rarity_key = LOWER(BTRIM(COALESCE(c.rarity, '')))
            WHERE LOWER(BTRIM(COALESCE(c.rarity, ''))) <> 'love'
              AND COALESCE(rarity_control.spawn_enabled, TRUE) = TRUE
              AND (
                  BTRIM(COALESCE(c.event, '')) = ''
                  OR COALESCE(event_control.spawn_enabled, TRUE) = TRUE
              )
            """
        )
    _character_cache = [tuple(r) for r in rows]
    return len(_character_cache)


async def reload_character_cache() -> int:
    """Thread-safe reload of the character cache (called by admin panel)."""
    async with _cache_lock:
        return await load_character_cache()


def _rarity_weight(rarity: object, rarity_weights: dict[str, float | int]) -> float | int:
    """Look up controlled rarity weights case-insensitively and defensively."""
    normalized = str(rarity).strip().casefold()
    for label, weight in rarity_weights.items():
        if label.casefold() == normalized:
            return weight
    return 10


def _pick_weighted(
    excluded_ids: set[int] | None = None,
    *,
    rarity_weights: dict[str, float | int] = RARITY_WEIGHTS,
) -> tuple:
    """Pick a nonzero-weight character while avoiding five recent group IDs.

    A very small catalog can have every card in the recent queue. In that
    exceptional case it falls back to the full cache. The caller supplies the
    weight table, keeping automatic Eternal exclusion separate from owner
    force-spawn overrides.
    """
    if not _character_cache:
        raise RuntimeError("Character cache not loaded")
    # Filter zero-weight rarities before considering recent IDs. Otherwise an
    # Eternal-only "eligible" subset could wrongly suppress the documented
    # fallback to older non-Eternal cards when all spawnable cards are recent.
    weighted_choices = [
        (character, _rarity_weight(character[3], rarity_weights))
        for character in _character_cache
    ]
    weighted_choices = [
        (character, weight)
        for character, weight in weighted_choices
        if weight > 0
    ]
    if not weighted_choices:
        raise RuntimeError("No spawnable characters are available in the cache")

    if excluded_ids:
        eligible = [
            (character, weight)
            for character, weight in weighted_choices
            if int(character[0]) not in excluded_ids
        ]
        if eligible:
            weighted_choices = eligible

    return random.choices(
        [character for character, _weight in weighted_choices],
        weights=[weight for _character, weight in weighted_choices],
        k=1,
    )[0]


def _pick_cached_rarity(
    rarity: str,
    excluded_ids: set[int] | None = None,
) -> tuple | None:
    """Randomly pick one exact cached tier, preferring non-recent card IDs."""
    if not _character_cache:
        raise RuntimeError("Character cache not loaded")
    choices = [
        character
        for character in _character_cache
        if str(character[3]).strip().casefold() == rarity.casefold()
    ]
    if not choices:
        return None
    if excluded_ids:
        preferred = [character for character in choices if int(character[0]) not in excluded_ids]
        if preferred:
            choices = preferred
    return random.choice(choices)


async def _get_special_spawn_progress(chat_id: int) -> tuple[int, int, int]:
    """Return `(main_overlord, main_suspicious, external_suspicious)` progress."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                overlord_lower_spawn_count,
                main_suspicious_spawn_count,
                suspicious_lower_spawn_count
            FROM groups
            WHERE chat_id = $1
            """,
            chat_id,
        )
    if not row:
        return 0, 0, 0
    return (
        max(0, int(row["overlord_lower_spawn_count"] or 0)),
        max(0, int(row["main_suspicious_spawn_count"] or 0)),
        max(0, int(row["suspicious_lower_spawn_count"] or 0)),
    )


async def _overlord_claim_count(character_id: int, *, conn=None) -> int:
    """Count durable distinct owners for exactly one OverLord catalog card."""
    async def _read(connection) -> int:
        return int(await connection.fetchval(
            "SELECT COUNT(*) FROM overlord_claims WHERE character_id = $1",
            character_id,
        ) or 0)

    if conn is not None:
        return await _read(conn)
    pool = get_pool()
    async with pool.acquire() as pooled_conn:
        return await _read(pooled_conn)


async def _pick_available_overlord(
    excluded_ids: set[int] | None = None,
) -> tuple | None:
    """Pick a spawn-policy-enabled OverLord below its ten-owner cap."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT c.id, c.name, c.anime, c.rarity, c.rarity_score, c.image_url,
                   COALESCE(c.media_type, 'photo') AS media_type, c.aliases
            FROM characters AS c
            LEFT JOIN catalog_events AS event_control
              ON LOWER(BTRIM(event_control.label)) =
                 LOWER(BTRIM(COALESCE(c.event, '')))
            LEFT JOIN catalog_rarity_spawn_controls AS rarity_control
              ON rarity_control.rarity_key = LOWER(BTRIM(COALESCE(c.rarity, '')))
            WHERE LOWER(BTRIM(c.rarity)) = LOWER(BTRIM($1))
              AND COALESCE(rarity_control.spawn_enabled, TRUE) = TRUE
              AND (
                  SELECT COUNT(*)
                  FROM overlord_claims AS claim
                  WHERE claim.character_id = c.id
              ) < $2
              AND (
                  BTRIM(COALESCE(c.event, '')) = ''
                  OR COALESCE(event_control.spawn_enabled, TRUE) = TRUE
              )
            ORDER BY c.id ASC
            """,
            OVERLORD_RARITY,
            OVERLORD_UNIQUE_OWNER_CAP,
        )
    choices = [tuple(row) for row in rows]
    if not choices:
        return None
    if excluded_ids:
        preferred = [choice for choice in choices if int(choice[0]) not in excluded_ids]
        if preferred:
            choices = preferred
    return random.choice(choices)


async def _get_recent_spawn_ids(chat_id: int) -> list[int]:
    """Read the newest-first persistent anti-repeat queue for one group."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT recent_spawn_ids FROM groups WHERE chat_id = $1",
            chat_id,
        )
    if not row or not row["recent_spawn_ids"]:
        return []
    # Sanitize old/manual values defensively before applying the exclusion.
    return [
        int(character_id)
        for character_id in row["recent_spawn_ids"][:RECENT_SPAWN_EXCLUSION_COUNT]
        if isinstance(character_id, (int, str)) and str(character_id).lstrip("-").isdigit()
    ]


async def _get_catalog_character_for_spawn(character_id: int) -> tuple | None:
    """Fetch a specifically requested owner-spawn character with aliases."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, name, anime, rarity, rarity_score, image_url,
                   COALESCE(media_type, 'photo') AS media_type, aliases
            FROM characters
            WHERE id = $1
            """,
            character_id,
        )
    if not row:
        return None
    return (
        int(row["id"]),
        str(row["name"]),
        str(row["anime"]),
        str(row["rarity"]),
        int(row["rarity_score"]),
        str(row["image_url"]),
        str(row["media_type"] or "photo"),
        str(row["aliases"] or ""),
    )


async def _character_spawn_block_reason(
    character_id: int,
    *,
    conn=None,
    lock_controls: bool = False,
) -> str | None:
    """Return the exact active spawn-policy block for one stored card.

    ``event`` and ``rarity`` are intentionally separate controls. With
    ``lock_controls=True`` the per-rarity advisory lock and the existing event
    row lock remain held through Telegram delivery, so an owner pause either
    happens before a card can be posted or after that card is already visible.
    """
    async def _read(connection) -> str | None:
        character = await connection.fetchrow(
            """
            SELECT LOWER(BTRIM(COALESCE(rarity, ''))) AS rarity_key,
                   BTRIM(COALESCE(event, '')) AS event_label
            FROM characters
            WHERE id = $1
            """,
            character_id,
        )
        if not character:
            return "not_found"

        # This value is normalized by PostgreSQL, matching the durable table
        # key and the SQL cache joins even for non-ASCII catalog labels.
        rarity_key = str(character["rarity_key"] or "").strip()
        if rarity_key:
            if lock_controls:
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtext($1))",
                    f"rarity-spawn:{rarity_key}",
                )
            rarity_query = """
                SELECT spawn_enabled
                FROM catalog_rarity_spawn_controls
                WHERE rarity_key = $1
            """
            if lock_controls:
                rarity_query += " FOR UPDATE"
            rarity_control = await connection.fetchrow(rarity_query, rarity_key)
            if rarity_control is not None and not bool(rarity_control["spawn_enabled"]):
                return "rarity"

        event_label = str(character["event_label"] or "")
        if not event_label:
            return None
        event_query = """
            SELECT spawn_enabled
            FROM catalog_events
            WHERE LOWER(BTRIM(label)) = LOWER(BTRIM($1))
        """
        if lock_controls:
            event_query += " FOR UPDATE"
        event = await connection.fetchrow(event_query, event_label)
        # Keep historical/orphan event labels spawnable. Only an extant curated
        # event explicitly marked stopped suppresses its cards.
        if event is not None and not bool(event["spawn_enabled"]):
            return "event"
        return None

    if conn is not None:
        return await _read(conn)
    pool = get_pool()
    async with pool.acquire() as pooled_conn:
        return await _read(pooled_conn)


async def _is_character_spawn_enabled(
    character_id: int,
    *,
    conn=None,
    lock_controls: bool = False,
) -> bool:
    """Compatibility predicate for paths that only need enabled/disabled."""
    return await _character_spawn_block_reason(
        character_id,
        conn=conn,
        lock_controls=lock_controls,
    ) is None


async def _get_scheduled_op_spawn(chat_id: int) -> dict | None:
    """Return the one automatic OP card already announced for a group, if any."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT chat_id, character_id, claim_window, spawn_at
            FROM scheduled_op_spawns
            WHERE chat_id = $1
            """,
            chat_id,
        )
    return dict(row) if row else None


async def _schedule_op_spawn(
    chat_id: int,
    character_id: int,
    claim_window: int,
) -> bool:
    """Persist an enabled automatic OP card for its full one-minute delay."""
    spawn_at = time.time() + OP_SPAWN_ANNOUNCEMENT_DELAY_SECONDS
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Do not announce a stopped event or rarity after a stale cache
            # selection. Locking both controls serializes it with owner pauses.
            if not await _is_character_spawn_enabled(
                character_id,
                conn=conn,
                lock_controls=True,
            ):
                return False
            row = await conn.fetchrow(
                """
                INSERT INTO scheduled_op_spawns (chat_id, character_id, claim_window, spawn_at)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (chat_id) DO NOTHING
                RETURNING chat_id
                """,
                chat_id,
                character_id,
                claim_window,
                spawn_at,
            )
    return bool(row)


async def _remove_scheduled_op_spawn(chat_id: int, character_id: int) -> None:
    """Undo a never-announced queue entry when Telegram rejects the warning."""
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            DELETE FROM scheduled_op_spawns
            WHERE chat_id = $1 AND character_id = $2
            """,
            chat_id,
            character_id,
        )


async def _release_failed_scheduled_op_spawn(item: dict, lease_token: str) -> None:
    """Make a leased OP row quietly retryable after a temporary failure."""
    retry_at = time.time() + 5
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE scheduled_op_spawns
            SET processing_token = NULL,
                processing_until = NULL,
                spawn_at = $4
            WHERE chat_id = $1
              AND character_id = $2
              AND processing_token = $3
            """,
            int(item["chat_id"]),
            int(item["character_id"]),
            lease_token,
            retry_at,
        )


async def _discard_scheduled_op_spawn(item: dict, lease_token: str) -> None:
    """Discard an impossible queued card without affecting catalog records."""
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            DELETE FROM scheduled_op_spawns
            WHERE chat_id = $1
              AND character_id = $2
              AND processing_token = $3
            """,
            int(item["chat_id"]),
            int(item["character_id"]),
            lease_token,
        )


async def _select_character_for_spawn(
    chat_id: int,
    character_id: int | None = None,
    *,
    automatic: bool,
) -> tuple:
    """Choose a card for automatic spawning or validate an owner request."""
    recent_ids = set(await _get_recent_spawn_ids(chat_id))
    if character_id is None:
        if automatic:
            (
                main_overlord_progress,
                main_suspicious_progress,
                external_suspicious_progress,
            ) = await _get_special_spawn_progress(chat_id)
            if int(chat_id) == HENTACATCHER_GP_CHAT_ID:
                # The forced ranks are checked before weighted selection. Love
                # itself is absent from the cache and zero-weight as a second
                # defense, so it can never leak into a random group spawn.
                if main_overlord_progress >= OVERLORD_LOWER_SPAWNS_REQUIRED:
                    overlord = await _pick_available_overlord(recent_ids)
                    if overlord is not None:
                        return overlord
                if main_suspicious_progress >= MAIN_SUSPICIOUS_SPAWNS_REQUIRED:
                    suspicious = _pick_cached_rarity(SUSPICIOUS_RARITY, recent_ids)
                    if suspicious is not None:
                        return suspicious
                return _pick_weighted(recent_ids, rarity_weights=RARITY_WEIGHTS)

            if external_suspicious_progress >= SUSPICIOUS_EXTERNAL_LOWER_SPAWNS_REQUIRED:
                suspicious = _pick_cached_rarity(SUSPICIOUS_RARITY, recent_ids)
                if suspicious is not None:
                    return suspicious
            return _pick_weighted(recent_ids, rarity_weights=EXTERNAL_RARITY_WEIGHTS)

        return _pick_weighted(recent_ids, rarity_weights=FORCED_SPAWN_RARITY_WEIGHTS)

    char = await _get_catalog_character_for_spawn(character_id)
    if char is None:
        raise ForcedSpawnError("not_found", character_id)
    if is_love(char[3]):
        raise ForcedSpawnError("love_not_spawnable", character_id)
    if is_overlord(char[3]):
        if int(chat_id) != HENTACATCHER_GP_CHAT_ID:
            raise ForcedSpawnError("overlord_main_group_only", character_id)
        if await _overlord_claim_count(int(char[0])) >= OVERLORD_UNIQUE_OWNER_CAP:
            raise ForcedSpawnError("overlord_owner_cap_reached", character_id)
    block_reason = await _character_spawn_block_reason(int(char[0]))
    if block_reason == "not_found":
        raise ForcedSpawnError("not_found", character_id)
    if block_reason is not None:
        raise ForcedSpawnError(f"{block_reason}_spawn_stopped", character_id)
    # Explicit owner requests retain existing Eternal behavior. Event- and
    # rarity-level pauses plus OverLord's main-group/cap rules apply to every
    # spawn path.
    if int(char[0]) in recent_ids:
        raise ForcedSpawnError("recently_spawned", character_id)
    return char


def _spawn_caption(claim_window: int, rarity: str) -> str:
    sym = rarity_symbol(rarity)
    return (
        "✦ ─── 𝗡𝗘𝗪 𝗖𝗛𝗔𝗥𝗔𝗖𝗧𝗘𝗥 𝗦𝗣𝗔𝗪𝗡 ─── ✦\n"
        f"{sym} 𝗔 𝗻𝗲𝘄 𝗰𝗵𝗮𝗿𝗮𝗰𝘁𝗲𝗿 𝗵𝗮𝘀 𝗮𝗽𝗽𝗲𝗮𝗿𝗲𝗱!\n"
        "🔍 𝗨𝘀𝗲 `/catch` Name 𝘁𝗼 𝗰𝗹𝗮𝗶𝗺.\n"
        f"⏱️ 𝗬𝗼𝘂 𝗵𝗮𝘃𝗲 {claim_window} 𝘀𝗲𝗰𝗼𝗻𝗱𝘀 𝗯𝗲𝗳𝗼𝗿𝗲 𝗲𝘀𝗰𝗮𝗽𝗲!\n"
        "────────────────────────"
    )


async def _get_group_settings(chat_id: int) -> tuple[int, int]:
    """Fetch a group's message threshold and claim window.

    Spawn timing is intentionally not consulted: a character may appear only
    after the configured number of group messages.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT spawn_interval, claim_window FROM groups WHERE chat_id = $1",
            chat_id,
        )
    if row:
        return row["spawn_interval"], row["claim_window"]
    return default_spawn_interval_for_group(chat_id), settings.spawn_claim_window


async def _advance_durable_group_counter(
    chat_id: int,
    title: str,
    msg_threshold: int,
    claim_window: int,
) -> bool:
    """Atomically count one group message and return whether it reaches N.

    The old implementation kept this only in Redis. A Redis restart/eviction
    silently reset individual groups and could make active groups appear never
    to spawn. `groups.msg_counter` is an existing durable per-group field, so
    PostgreSQL now owns the counter while Redis remains an optional speed cache
    for active-spawn reads.
    """
    threshold = max(1, int(msg_threshold))
    pool = get_pool()
    async with pool.acquire() as conn:
        # ActivityMiddleware normally creates this row first. The upsert makes
        # direct service calls and a just-added group safe as well, without
        # overwriting any owner-selected threshold/window on an existing row.
        await conn.execute(
            """
            INSERT INTO groups (
                chat_id, title, spawn_interval, spawn_interval_custom,
                claim_window, msg_counter, updated_at
            )
            VALUES ($1, $2, $3, FALSE, $4, 0, NOW())
            ON CONFLICT (chat_id) DO NOTHING
            """,
            chat_id,
            title,
            threshold,
            max(1, int(claim_window)),
        )
        row = await conn.fetchrow(
            """
            UPDATE groups
            SET msg_counter = CASE
                    WHEN COALESCE(msg_counter, 0) + 1 >= $2 THEN 0
                    ELSE COALESCE(msg_counter, 0) + 1
                END,
                updated_at = NOW()
            WHERE chat_id = $1
            RETURNING msg_counter
            """,
            chat_id,
            threshold,
        )
    # In the controlled non-negative counter domain, a returned zero means the
    # threshold was reached (including the deliberate N=1 configuration).
    return bool(row and int(row["msg_counter"]) == 0)


async def _reset_durable_group_counter(chat_id: int) -> None:
    """Restart automatic cadence after a manual owner spawn."""
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE groups SET msg_counter = 0, updated_at = NOW() WHERE chat_id = $1",
            chat_id,
        )


async def _record_special_rank_spawn_progress(
    conn,
    *,
    chat_id: int,
    rarity: str,
    automatic: bool,
) -> bool:
    """Persist forced-rank cadence after a successfully visible automatic spawn.

    Every automatic Main Group emission advances *both* Main counters. A
    resulting Suspicious resets only the 40-spawn Suspicious cycle; a resulting
    OverLord resets only the 70-spawn OverLord cycle. External groups never
    auto-spawn OverLord and keep their separate 70-spawn Suspicious cycle.
    Manual owner spawns deliberately do not alter automatic cadence.
    """
    if not automatic:
        return False

    if int(chat_id) == HENTACATCHER_GP_CHAT_ID:
        if is_overlord(rarity):
            await conn.execute(
                """
                UPDATE groups
                SET overlord_lower_spawn_count = 0,
                    main_suspicious_spawn_count = LEAST(
                        COALESCE(main_suspicious_spawn_count, 0) + 1, $2
                    ),
                    overlord_countdown_announced = FALSE,
                    updated_at = NOW()
                WHERE chat_id = $1
                """,
                chat_id,
                MAIN_SUSPICIOUS_SPAWNS_REQUIRED,
            )
            return False

        if is_suspicious(rarity):
            row = await conn.fetchrow(
                """
                UPDATE groups
                SET overlord_lower_spawn_count = LEAST(
                        COALESCE(overlord_lower_spawn_count, 0) + 1, $2
                    ),
                    main_suspicious_spawn_count = 0,
                    overlord_countdown_announced = CASE
                        WHEN COALESCE(overlord_lower_spawn_count, 0) + 1 >= $3
                        THEN TRUE
                        ELSE COALESCE(overlord_countdown_announced, FALSE)
                    END,
                    updated_at = NOW()
                WHERE chat_id = $1
                RETURNING overlord_lower_spawn_count, overlord_countdown_announced
                """,
                chat_id,
                OVERLORD_LOWER_SPAWNS_REQUIRED,
                OVERLORD_NOTICE_AT_LOWER_SPAWNS,
            )
        else:
            row = await conn.fetchrow(
                """
                UPDATE groups
                SET overlord_lower_spawn_count = LEAST(
                        COALESCE(overlord_lower_spawn_count, 0) + 1, $2
                    ),
                    main_suspicious_spawn_count = LEAST(
                        COALESCE(main_suspicious_spawn_count, 0) + 1, $4
                    ),
                    overlord_countdown_announced = CASE
                        WHEN COALESCE(overlord_lower_spawn_count, 0) + 1 >= $3
                        THEN TRUE
                        ELSE COALESCE(overlord_countdown_announced, FALSE)
                    END,
                    updated_at = NOW()
                WHERE chat_id = $1
                RETURNING overlord_lower_spawn_count, overlord_countdown_announced
                """,
                chat_id,
                OVERLORD_LOWER_SPAWNS_REQUIRED,
                OVERLORD_NOTICE_AT_LOWER_SPAWNS,
                MAIN_SUSPICIOUS_SPAWNS_REQUIRED,
            )
        return bool(
            row
            and int(row["overlord_lower_spawn_count"] or 0) == OVERLORD_NOTICE_AT_LOWER_SPAWNS
            and bool(row["overlord_countdown_announced"])
        )

    if is_suspicious(rarity):
        await conn.execute(
            """
            UPDATE groups
            SET suspicious_lower_spawn_count = 0,
                updated_at = NOW()
            WHERE chat_id = $1
            """,
            chat_id,
        )
    else:
        await conn.execute(
            """
            UPDATE groups
            SET suspicious_lower_spawn_count = LEAST(
                    COALESCE(suspicious_lower_spawn_count, 0) + 1,
                    $2
                ),
                updated_at = NOW()
            WHERE chat_id = $1
            """,
            chat_id,
            SUSPICIOUS_EXTERNAL_LOWER_SPAWNS_REQUIRED,
        )
    return False


async def _clear_legacy_redis_counter(chat_id: int) -> None:
    """Best-effort cleanup for counters written by pre-durable releases."""
    try:
        await get_redis().delete(f"grp:{chat_id}:counter")
    except Exception as exc:
        log.warning("could not clear legacy Redis counter for chat %s: %s", chat_id, exc)


async def _evict_active_spawn_cache(chat_id: int) -> None:
    """Remove a previous active-card cache entry without touching PostgreSQL.

    A new card replaces the group-level active row. Evicting first ensures a
    healthy Redis instance never continues to advertise the older card while
    the newer one is being persisted. Database reads remain authoritative if
    this best-effort cache operation is unavailable.
    """
    try:
        await get_redis().delete(f"spawn:{chat_id}")
    except Exception as exc:
        log.warning("could not evict active-spawn cache for chat %s: %s", chat_id, exc)


async def _execute_spawn(
    bot: Bot,
    chat_id: int,
    claim_window: int,
    *,
    selected_character: tuple,
    scheduled_op_lease_token: str | None = None,
    automatic: bool = False,
) -> SpawnContext:
    """Post one selected card and persist its active spawn state.

    Selection is deliberately separate from emission so an announced OP card
    cannot be rerolled into a different character after its one-minute delay.
    If an OP scheduler lease is supplied, successful spawn persistence consumes
    that exact durable row in the same database transaction.

    The existing active row is locked across Telegram delivery and replacement.
    Thus a /catch that began on the former card cannot slip through during the
    short interval between the new visible message and its durable upsert: it
    resumes against the replacement and is rejected as stale instead.
    """
    char_id, name, anime, rarity, rarity_score, image_url, media_type, aliases = selected_character
    overlord_countdown_due = False
    pool = get_pool()

    async with pool.acquire() as conn:
        async with conn.transaction():
            # Love is catalog/Lovelist-only. Keep a final transactional guard
            # even if a stale cache or a malformed direct caller bypassed the
            # normal selection filters.
            if is_love(rarity):
                raise ForcedSpawnError("love_not_spawnable", int(char_id))
            # Re-check live event and rarity state after any cache selection.
            # Holding both controls through delivery makes /stopspawn and
            # /stoprarityspawn authoritative even when either races a selected
            # automatic/forced card already in memory.
            block_reason = await _character_spawn_block_reason(
                int(char_id),
                conn=conn,
                lock_controls=True,
            )
            if block_reason == "not_found":
                # A catalog admin may delete a selected card while an owner
                # force-spawn or automatic worker is waiting for its policy
                # locks. Preserve the ordinary missing-card error instead of
                # presenting it as a stopped policy.
                raise ForcedSpawnError("not_found", int(char_id))
            if block_reason is not None:
                raise ForcedSpawnError(f"{block_reason}_spawn_stopped", int(char_id))

            if is_overlord(rarity):
                if int(chat_id) != HENTACATCHER_GP_CHAT_ID:
                    raise ForcedSpawnError("overlord_main_group_only", int(char_id))
                # The same catalog-row lock is used by claim_character before
                # it inserts a ten-owner ledger row, so a just-capped card can
                # never be posted from stale cache/selection data.
                live_character = await conn.fetchrow(
                    "SELECT id FROM characters WHERE id = $1 FOR UPDATE",
                    int(char_id),
                )
                if not live_character:
                    raise ForcedSpawnError("not_found", int(char_id))
                if await _overlord_claim_count(int(char_id), conn=conn) >= OVERLORD_UNIQUE_OWNER_CAP:
                    raise ForcedSpawnError("overlord_owner_cap_reached", int(char_id))
                if automatic:
                    progress = await conn.fetchval(
                        "SELECT overlord_lower_spawn_count FROM groups WHERE chat_id = $1 FOR UPDATE",
                        chat_id,
                    )
                    if int(progress or 0) < OVERLORD_LOWER_SPAWNS_REQUIRED:
                        raise ForcedSpawnError("overlord_cadence_not_ready", int(char_id))

            if scheduled_op_lease_token is not None:
                # A concurrent /stopspawn deletes this row. Do not let a worker
                # that leased it moments earlier resurrect that cancelled OP
                # announcement, including after a quick later /restorespawn.
                scheduled = await conn.fetchrow(
                    """
                    SELECT chat_id
                    FROM scheduled_op_spawns
                    WHERE chat_id = $1
                      AND character_id = $2
                      AND processing_token = $3
                    FOR UPDATE
                    """,
                    chat_id,
                    int(char_id),
                    scheduled_op_lease_token,
                )
                if not scheduled:
                    raise ForcedSpawnError("scheduled_cancelled", int(char_id))

            # Serialize replacement with a concurrent /catch for the current
            # group card. No row is normal for a group's first-ever spawn.
            await conn.fetchrow(
                "SELECT chat_id FROM active_spawns WHERE chat_id = $1 FOR UPDATE",
                chat_id,
            )

            # Each group has exactly one current card. Before announcing a
            # newer one, remove the old Redis representation; the database
            # upsert below then makes the new card authoritative. Redis failure
            # cannot block the spawn.
            await _evict_active_spawn_cache(chat_id)

            media_sent = True
            try:
                if str(media_type).strip().casefold() == "video":
                    msg = await bot.send_video(
                        chat_id=chat_id,
                        video=image_url,
                        caption=_spawn_caption(claim_window, rarity),
                        supports_streaming=True,
                        parse_mode="HTML",
                        has_spoiler=True,
                    )
                else:
                    msg = await bot.send_photo(
                        chat_id=chat_id,
                        photo=image_url,
                        caption=_spawn_caption(claim_window, rarity),
                        parse_mode="HTML",
                        has_spoiler=True,
                    )
            except Exception as exc:
                logging.warning(
                    "spawn media failed for chat %s (character_id=%s): %s",
                    chat_id,
                    char_id,
                    exc,
                )
                media_sent = False
                msg = await bot.send_message(
                    chat_id=chat_id,
                    text=(
                        "✦ ─── 𝗡𝗘𝗪 𝗖𝗛𝗔𝗥𝗔𝗖𝗧𝗘𝗥 𝗦𝗣𝗔𝗪𝗡 ─── ✦\n"
                        f"{rarity_symbol(rarity)} 𝗔 𝗻𝗲𝘄 𝗰𝗵𝗮𝗿𝗮𝗰𝘁𝗲𝗿 𝗵𝗮𝘀 𝗮𝗽𝗽𝗲𝗮𝗿𝗲𝗱!\n"
                        "⚠️ <i>(image unavailable)</i>\n"
                        "🔍 𝗨𝘀𝗲 /catch Name 𝘁𝗼 𝗰𝗹𝗮𝗶𝗺.\n"
                        f"⏱️ 𝗬𝗼𝘂 𝗵𝗮𝘃𝗲 {claim_window} 𝘀𝗲𝗰𝗼𝗻𝗱𝘀 𝗯𝗲𝗳𝗼𝗿𝗲 𝗲𝘀𝗰𝗮𝗽𝗲!\n"
                        "────────────────────────"
                    ),
                    parse_mode="HTML",
                )

            # Start the four-second gate only after Telegram has accepted the
            # card message. This makes it a full four seconds from the visible
            # spawn, not from the beginning of a potentially slow upload.
            spawned_at = time.time()
            expires_at = int(spawned_at) + claim_window

            await conn.execute(
                """
                INSERT INTO active_spawns (
                    chat_id, character_id, message_id, spawned_at, expires_at,
                    superseded_character_id, superseded_at
                )
                VALUES ($1, $2, $3, $4, $5, NULL, NULL)
                ON CONFLICT (chat_id) DO UPDATE SET
                  character_id = EXCLUDED.character_id,
                  message_id   = EXCLUDED.message_id,
                  spawned_at   = EXCLUDED.spawned_at,
                  expires_at   = EXCLUDED.expires_at,
                  -- Preserve only an immediately replaced card that was still
                  -- catchable when this newer visible card was persisted.
                  superseded_character_id = CASE
                      WHEN active_spawns.expires_at >= EXCLUDED.spawned_at
                      THEN active_spawns.character_id
                      ELSE NULL
                  END,
                  superseded_at = CASE
                      WHEN active_spawns.expires_at >= EXCLUDED.spawned_at
                      THEN EXCLUDED.spawned_at
                      ELSE NULL
                  END
                """,
                chat_id,
                char_id,
                msg.message_id,
                spawned_at,
                expires_at,
            )
            # Newest first, capped at five. Persisting this on the group row
            # makes the anti-repeat rule survive cache reloads and restarts.
            # The upsert also ensures group_stats' foreign key has a parent if
            # a direct owner spawn occurs before ordinary activity registered it.
            await conn.execute(
                """
                INSERT INTO groups (
                    chat_id, spawn_interval, spawn_interval_custom,
                    recent_spawn_ids, updated_at
                )
                VALUES ($1, $3, FALSE, ARRAY[$2]::BIGINT[], NOW())
                ON CONFLICT (chat_id) DO UPDATE SET
                  recent_spawn_ids = (
                      ARRAY[$2]::BIGINT[] ||
                      COALESCE(groups.recent_spawn_ids, ARRAY[]::BIGINT[])
                  )[1:5],
                  updated_at = NOW()
                """,
                chat_id,
                char_id,
                default_spawn_interval_for_group(chat_id),
            )
            overlord_countdown_due = await _record_special_rank_spawn_progress(
                conn,
                chat_id=chat_id,
                rarity=str(rarity),
                automatic=automatic,
            )
            await conn.execute(
                "INSERT INTO group_stats (chat_id, total_spawns, total_claims) "
                "VALUES ($1, 1, 0) "
                "ON CONFLICT (chat_id) DO UPDATE SET total_spawns = group_stats.total_spawns + 1",
                chat_id,
            )
            if scheduled_op_lease_token is not None:
                await conn.execute(
                    """
                    DELETE FROM scheduled_op_spawns
                    WHERE chat_id = $1
                      AND character_id = $2
                      AND processing_token = $3
                    """,
                    chat_id,
                    char_id,
                    scheduled_op_lease_token,
                )

    if overlord_countdown_due:
        try:
            await bot.send_message(chat_id=chat_id, text=OVERLORD_COUNTDOWN_NOTICE)
        except Exception as exc:
            # The spawn/cadence transaction already committed. Missing a noisy
            # reminder must never make the card or its durable count disappear.
            log.warning("could not send OverLord countdown notice in %s: %s", chat_id, exc)

    spawn_data = {
        "character_id": char_id,
        "name": name,
        "anime": anime,
        "rarity": rarity,
        "rarity_score": rarity_score,
        "image_url": image_url,
        "media_type": media_type,
        "aliases": aliases,
        "message_id": msg.message_id,
        "spawned_at": spawned_at,
        "expires_at": expires_at,
        # Retain has_photo for old readers while exposing accurate media metadata.
        "has_photo": media_sent and str(media_type).strip().casefold() != "video",
        "has_media": media_sent,
    }
    # Redis is a fast cache only. The active-spawn row above is authoritative,
    # so a cache outage must never prevent a group from receiving or claiming a
    # successfully persisted spawn.
    try:
        redis = get_redis()
        await redis.set(f"spawn:{chat_id}", json.dumps(spawn_data), ex=claim_window + 10)
    except Exception as exc:
        log.warning("could not cache active spawn for chat %s: %s", chat_id, exc)

    return SpawnContext(
        chat_id=chat_id,
        character_id=char_id,
        name=name,
        anime=anime,
        rarity=rarity,
        rarity_score=rarity_score,
        image_url=image_url,
        media_type=str(media_type),
        aliases=aliases,
        message_id=msg.message_id,
        spawned_at=spawned_at,
        expires_at=expires_at,
    )

async def _start_automatic_selected_spawn(
    bot: Bot,
    chat_id: int,
    claim_window: int,
    selected_character: tuple,
) -> SpawnContext | None:
    """Emit a normal automatic card, or announce and queue an automatic OP."""
    character_id = int(selected_character[0])
    rarity = str(selected_character[3])
    if rarity.strip().casefold() != "op":
        return await _execute_spawn(
            bot,
            chat_id,
            claim_window,
            selected_character=selected_character,
            automatic=True,
        )

    if not await _schedule_op_spawn(chat_id, character_id, claim_window):
        # Another automatic message won a concurrent schedule race, or the
        # selected event was paused while an old cache entry was being used.
        return None
    if not await _is_character_spawn_enabled(character_id):
        # Avoid an orphaned announcement if /stopspawn or /stoprarityspawn ran
        # just after scheduling. The due-worker guard remains authoritative.
        await _remove_scheduled_op_spawn(chat_id, character_id)
        return None
    try:
        await bot.send_message(chat_id=chat_id, text=OP_SPAWN_ANNOUNCEMENT_TEXT)
    except Exception:
        await _remove_scheduled_op_spawn(chat_id, character_id)
        raise
    return None


async def maybe_spawn(bot: Bot, chat_id: int, title: str) -> SpawnContext | None:
    """Count every eligible group message and spawn exactly at its limit.

    A normal active card never pauses the durable message cadence. If its group
    reaches another threshold before it is caught or expires, the later card
    replaces it as the one and only active spawn. The existing one-minute
    announced-OP queue still pauses only while that promised OP card is pending,
    so it cannot be replaced before it appears.
    """
    if await _get_scheduled_op_spawn(chat_id):
        return None

    msg_threshold, claim_window = await _get_group_settings(chat_id)
    should_spawn = await _advance_durable_group_counter(
        chat_id,
        title,
        msg_threshold,
        claim_window,
    )
    if not should_spawn:
        return None

    # The Redis counter is no longer read, but clear an old deployment's key
    # when this group reaches its first durable threshold. Serialize the actual
    # selection/emission against another threshold burst in this same worker so
    # 40/70 special-rank cadence reads cannot race their own prior write.
    await _clear_legacy_redis_counter(chat_id)
    lock = await _automatic_spawn_lock_for(chat_id)
    async with lock:
        if await _get_scheduled_op_spawn(chat_id):
            return None
        selected_character = await _select_character_for_spawn(chat_id, automatic=True)
        return await _start_automatic_selected_spawn(bot, chat_id, claim_window, selected_character)


async def force_spawn(
    bot: Bot,
    chat_id: int,
    character_id: int | None = None,
) -> SpawnContext:
    """Immediately force a random or requested catalog character for an owner.

    This deliberately preserves prior owner semantics: manual OP and Eternal
    cards do not enter the automatic OP delay or automatic Eternal exclusion.
    """
    _, claim_window = await _get_group_settings(chat_id)
    selected_character = await _select_character_for_spawn(
        chat_id,
        character_id,
        automatic=False,
    )
    ctx = await _execute_spawn(
        bot,
        chat_id,
        claim_window,
        selected_character=selected_character,
    )

    # Manual cards remain outside automatic special-rank cadence: an owner
    # cannot reset or manufacture the next forced event by issuing /spawn.
    await _clear_legacy_redis_counter(chat_id)
    return ctx


async def process_due_op_spawns(bot: Bot, *, limit: int = SCHEDULED_OP_SPAWN_BATCH_SIZE) -> int:
    """Emit due, already-announced OP cards without losing restart-pending work.

    Each worker takes a short durable lease with ``FOR UPDATE SKIP LOCKED``.
    If it dies before emission, the lease expires and a later scheduler pass
    retries the same selected card. On a successful post, _execute_spawn
    consumes the matching lease in its active-spawn persistence transaction.
    """
    now = time.time()
    lease_token = uuid.uuid4().hex
    lease_until = now + OP_SPAWN_PROCESSING_LEASE_SECONDS
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            due_rows = await conn.fetch(
                """
                WITH due AS (
                    SELECT chat_id
                    FROM scheduled_op_spawns
                    WHERE spawn_at <= $1
                      AND (processing_until IS NULL OR processing_until <= $1)
                    ORDER BY spawn_at ASC, chat_id ASC
                    LIMIT $2
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE scheduled_op_spawns AS scheduled
                SET processing_token = $3,
                    processing_until = $4
                FROM due
                WHERE scheduled.chat_id = due.chat_id
                RETURNING scheduled.chat_id, scheduled.character_id,
                          scheduled.claim_window, scheduled.spawn_at
                """,
                now,
                max(1, min(int(limit), SCHEDULED_OP_SPAWN_BATCH_SIZE)),
                lease_token,
                lease_until,
            )

    emitted = 0
    for raw_item in due_rows:
        item = dict(raw_item)
        character_id = int(item["character_id"])
        try:
            character = await _get_catalog_character_for_spawn(character_id)
            if character is None:
                # The FK normally prevents this. Keep the guard for a manually
                # altered legacy database and do not resurrect a missing card.
                log.warning("discarding queued OP spawn for missing character %s", character_id)
                await _discard_scheduled_op_spawn(item, lease_token)
                continue
            block_reason = await _character_spawn_block_reason(character_id)
            if block_reason is not None:
                # Both pause commands delete unleased queued rows. This guard
                # covers an item leased by the scheduler just before its event
                # or rarity was paused.
                log.info(
                    "discarding queued %s-stopped character %s",
                    block_reason,
                    character_id,
                )
                await _discard_scheduled_op_spawn(item, lease_token)
                continue
            queued_rarity = str(character[3]).strip()
            if queued_rarity.casefold() == "eternal":
                # An owner may have changed a queued OP card to Eternal. Honor
                # the global Eternal-disable rule rather than posting it.
                log.info("discarding queued Eternal character %s", character_id)
                await _discard_scheduled_op_spawn(item, lease_token)
                continue
            if is_overlord(queued_rarity) or (
                is_suspicious(queued_rarity)
                and int(item["chat_id"]) != HENTACATCHER_GP_CHAT_ID
            ):
                # A card changed after its OP announcement must not bypass the
                # dedicated OverLord/main-group or external Suspicious cadence.
                log.info("discarding queued special-rank character %s", character_id)
                await _discard_scheduled_op_spawn(item, lease_token)
                continue
            await _execute_spawn(
                bot,
                int(item["chat_id"]),
                int(item["claim_window"]),
                selected_character=character,
                scheduled_op_lease_token=lease_token,
                automatic=True,
            )
            emitted += 1
        except ForcedSpawnError as exc:
            if exc.reason in {"event_spawn_stopped", "rarity_spawn_stopped", "scheduled_cancelled"}:
                log.info(
                    "discarding queued OP character %s because %s",
                    character_id,
                    exc.reason,
                )
                await _discard_scheduled_op_spawn(item, lease_token)
                continue
            log.warning(
                "queued OP spawn could not be emitted for character %s: %s",
                character_id,
                exc.reason,
            )
            await _release_failed_scheduled_op_spawn(item, lease_token)
        except Exception as exc:
            log.warning(
                "queued OP spawn failed for chat %s (character_id=%s): %s",
                item.get("chat_id"),
                character_id,
                exc,
            )
            await _release_failed_scheduled_op_spawn(item, lease_token)
    return emitted


def _cache_matches_active_spawn(cached: dict, authoritative: dict) -> bool:
    """Return whether a Redis value names the same durable spawn revision."""
    try:
        return (
            int(cached["character_id"]) == int(authoritative["character_id"])
            and int(cached["message_id"]) == int(authoritative["message_id"])
            and float(cached["spawned_at"]) == float(authoritative["spawned_at"])
        )
    except (KeyError, TypeError, ValueError):
        return False


async def _get_authoritative_active_spawn(chat_id: int) -> dict | None:
    """Read the one current card plus its immediate live predecessor from DB."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT s.*, c.name, c.anime, c.rarity, c.rarity_score, c.image_url,
                   COALESCE(c.media_type, 'photo') AS media_type, c.aliases,
                   old.name AS superseded_name,
                   old.aliases AS superseded_aliases
            FROM active_spawns s
            JOIN characters c ON c.id = s.character_id
            LEFT JOIN characters old ON old.id = s.superseded_character_id
            WHERE s.chat_id = $1
            """,
            chat_id,
        )
    return dict(row) if row else None


async def get_active_spawn(chat_id: int) -> dict | None:
    """Return the current authoritative card, never an older Redis value.

    Redis remains useful for photo metadata and fast cache storage, but /catch
    verifies its revision against PostgreSQL before relying on it. This matters
    when a fresh automatic card supersedes a still-live earlier card: a stale
    Redis value must never make the old answer claim the new card.
    """
    cached: dict | None = None
    try:
        raw = await get_redis().get(f"spawn:{chat_id}")
        if raw:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                cached = parsed
    except Exception as exc:
        # The database row is the durable authority. A cache problem must not
        # make /catch falsely report that a visible card cannot be found.
        log.warning("could not read active-spawn cache for chat %s: %s", chat_id, exc)

    authoritative = await _get_authoritative_active_spawn(chat_id)
    if authoritative is None:
        if cached is not None:
            # Do not leave a cache-only ghost catchable after a claim, expiry,
            # admin clear, or replacement.
            await _evict_active_spawn_cache(chat_id)
        return None

    if cached is not None and _cache_matches_active_spawn(cached, authoritative):
        # The database keeps names/aliases and predecessor details authoritative.
        # Carry over only cache-only presentation metadata when it is available.
        if "has_photo" in cached:
            authoritative["has_photo"] = cached["has_photo"]
        return authoritative

    if cached is not None:
        log.warning("discarding stale active-spawn cache for chat %s", chat_id)
        await _evict_active_spawn_cache(chat_id)
    return authoritative


async def clear_active_spawn(chat_id: int) -> None:
    """Remove the authoritative row and then its optional cache representation."""
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM active_spawns WHERE chat_id = $1", chat_id)
    await _evict_active_spawn_cache(chat_id)


async def clear_group_runtime_cache(chat_id: int) -> None:
    """Evict non-authoritative group cache keys after a deliberate bot leave.

    Database cleanup for a departed group happens transactionally in the owner
    tool. Redis contains only optional active-spawn/legacy-counter mirrors, so
    clear both best-effort keys afterwards without making the successful leave
    depend on Redis availability.
    """
    await _evict_active_spawn_cache(chat_id)
    await _clear_legacy_redis_counter(chat_id)
