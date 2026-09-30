"""Initialize schema migrations and import the current character CSV.

Normal, safe command (use this for ordinary deployments/updates):
    python -m bot.db.init_db

It applies every schema migration — including ``users.is_banned`` and global
Favorite support — then UPSERTs the current CSV without deleting users,
harems, trades, or old characters.

Destructive reset, only when explicitly requested:
    python -m bot.db.init_db --reset

The reset mode clears old game catalog/collection data before importing the
CSV. Stop the bot and back up PostgreSQL before using it.

CSV accepts the current event/media-aware layouts:
  id,name,anime,rarity,aliases,image_url,event                            (7 columns)
  id,name,anime,rarity,rarity_score,aliases,image_url,event                (8 columns)
  id,name,anime,rarity,aliases,image_url,media_type,event                  (8 columns)
  id,name,anime,rarity,rarity_score,aliases,image_url,media_type,event     (9 columns)

Legacy CSVs without ``event`` or ``media_type`` remain importable. If
rarity_score is omitted, it is auto-derived from the rarity column below. An
empty CSV event does not wipe an event assigned live through /eventchange; use
/eventchange None to clear it. A CSV without media_type preserves an existing live video's complete media
record (kind and Telegram file ID) and defaults a new row to a photo.
"""
from __future__ import annotations

import asyncio
import csv
import sys
from pathlib import Path

from bot.db.pool import close_pool, get_pool, init_pool
from bot.db.schema import SCHEMA_SQL
from bot.services.redis_client import close_redis, get_redis, init_redis

CSV_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "waifu_characters.csv"
# ``grp:*:lastspawn`` is a legacy cleanup pattern only. ``grp:*:counter`` is
# also cleared on a destructive reset for older deployments, but the current
# message-only spawn counter is durable in PostgreSQL groups.msg_counter.
_REDIS_PATTERNS = ("spawn:*", "grp:*:counter", "grp:*:lastspawn", "spam:*")

# Default score per rarity tier, used when the CSV has no rarity_score column.
DEFAULT_RARITY_SCORE = {
    "Common":    10,
    "Rare":      25,
    "Legendary": 65,
    "Mythical":  80,
    "Sovereign": 92,
    "OP":        100,
    "Eternal":   110,
    "Suspicious": 120,
    "OverLord":  130,
}


async def apply_schema(conn) -> None:
    """Apply all idempotent tables, indexes, and migrations."""
    await conn.execute(SCHEMA_SQL)
    print("✓ Schema applied")


async def import_characters(conn) -> int:
    """UPSERT the current CSV while preserving all existing game data."""
    if not CSV_PATH.exists():
        print(f"✗ CSV not found at {CSV_PATH}")
        return 0

    rows = []
    skipped_empty = 0
    with CSV_PATH.open("r", encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)
        has_score_col = reader.fieldnames and "rarity_score" in reader.fieldnames
        has_event_col = reader.fieldnames and "event" in reader.fieldnames
        has_media_type_col = reader.fieldnames and "media_type" in reader.fieldnames

        for record in reader:
            name = (record.get("name") or "").strip()
            if not name:
                skipped_empty += 1
                continue

            rarity = (record.get("rarity") or "Common").strip()
            if has_score_col and (record.get("rarity_score") or "").strip():
                score = int(record["rarity_score"])
            else:
                score = DEFAULT_RARITY_SCORE.get(rarity, 10)

            row = (
                int(record["id"]),
                name,
                (record.get("anime") or "").strip(),
                rarity,
                score,
                (record.get("aliases") or "").strip(),
                (record.get("image_url") or "").strip(),
                (record.get("event") or "").strip() if has_event_col else "",
            )
            if has_media_type_col:
                media_type = (record.get("media_type") or "").strip().casefold()
                # A malformed or blank exported value is safe to treat as a
                # photo; only bot-created Telegram videos opt into video media.
                row = (*row[:7], "video" if media_type == "video" else "photo", row[7])
            rows.append(row)

    if not rows:
        raise RuntimeError(
            f"No completed character rows found in {CSV_PATH} "
            f"({skipped_empty} empty rows skipped)."
        )

    if has_media_type_col:
        await conn.executemany(
            """
            INSERT INTO characters (
              id, name, anime, rarity, rarity_score, aliases, image_url, media_type, event
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT (id) DO UPDATE SET
              name         = EXCLUDED.name,
              anime        = EXCLUDED.anime,
              rarity       = EXCLUDED.rarity,
              rarity_score = EXCLUDED.rarity_score,
              aliases      = EXCLUDED.aliases,
              image_url    = EXCLUDED.image_url,
              media_type   = EXCLUDED.media_type,
              -- Live event assignment is intentionally authoritative when the
              -- catalog file carries its normal blank event column.
              event        = CASE
                               WHEN BTRIM(EXCLUDED.event) <> '' THEN EXCLUDED.event
                               ELSE characters.event
                             END
            """,
            rows,
        )
    else:
        # A historic photo-only CSV cannot safely describe a bot-added video:
        # preserving only its media_type while replacing its file ID would make
        # a photo file ID get sent through Telegram's video API. Preserve the
        # complete existing video record unless an explicit media_type column is
        # supplied. Newly inserted rows still use the schema's photo default.
        await conn.executemany(
            """
            INSERT INTO characters (
              id, name, anime, rarity, rarity_score, aliases, image_url, event
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (id) DO UPDATE SET
              name         = EXCLUDED.name,
              anime        = EXCLUDED.anime,
              rarity       = EXCLUDED.rarity,
              rarity_score = EXCLUDED.rarity_score,
              aliases      = EXCLUDED.aliases,
              image_url    = CASE
                               WHEN COALESCE(characters.media_type, 'photo') = 'video'
                               THEN characters.image_url
                               ELSE EXCLUDED.image_url
                             END,
              event        = CASE
                               WHEN BTRIM(EXCLUDED.event) <> '' THEN EXCLUDED.event
                               ELSE characters.event
                             END
            """,
            rows,
        )
    print(f"✓ Upserted {len(rows)} characters (existing user data preserved)")
    if skipped_empty:
        print(f"  ({skipped_empty} empty CSV row(s) skipped)")
    return len(rows)


async def _clear_bot_redis_keys() -> int:
    """Remove only bot spawn/counter keys; never flush a shared Redis database."""
    redis = await init_redis()
    keys: list[str] = []
    for pattern in _REDIS_PATTERNS:
        keys.extend([key async for key in redis.scan_iter(match=pattern, count=200)])

    unique_keys = list(dict.fromkeys(keys))
    deleted = 0
    for start in range(0, len(unique_keys), 500):
        deleted += await redis.delete(*unique_keys[start:start + 500])
    return deleted


async def reset_catalog() -> int:
    """Explicitly reset game catalog/collection data, then import the CSV."""
    await init_pool()
    try:
        pool = get_pool()
        async with pool.acquire() as conn:
            await apply_schema(conn)
            async with conn.transaction():
                # Child references must be removed before the catalog because
                # these tables have foreign keys pointing at characters.
                await conn.execute("DELETE FROM active_spawns")
                await conn.execute("DELETE FROM scheduled_op_spawns")
                # Rolling catch-only leaderboard events reference the old
                # catalog/user game state and must not survive a full reset.
                await conn.execute("DELETE FROM catch_events")
                await conn.execute("DELETE FROM overlord_claims")
                await conn.execute("DELETE FROM trades")
                await conn.execute("DELETE FROM gifts")
                # Currency belongs to the reset game state as well: do not
                # leave old balances, passes, transfer prompts, or recovery
                # records attached to an otherwise brand-new catalog.
                await conn.execute("DELETE FROM neko_transfer_requests")
                await conn.execute("DELETE FROM neko_casino_input_sessions")
                await conn.execute("DELETE FROM neko_dice_bets")
                await conn.execute("DELETE FROM neko_casino_games")
                await conn.execute("DELETE FROM neko_wallet_ledger")
                await conn.execute("DELETE FROM neko_daily_streaks")
                await conn.execute("DELETE FROM neko_weekly_quests")
                await conn.execute("DELETE FROM neko_wallet_deletion_backups")
                await conn.execute("DELETE FROM neko_pass_grants")
                await conn.execute("DELETE FROM sudo_grants")
                # Active market listings reference catalog cards and must be
                # cleared before a destructive catalog reset removes characters.
                await conn.execute("DELETE FROM market_listings")
                # Achievements are catalog-specific milestones; preserving them
                # through a destructive catalog reset would leave old rewards
                # claimable against a brand-new collection.
                await conn.execute("DELETE FROM user_achievements")
                # Recovery snapshots reference catalog IDs, so a deliberate
                # full reset must clear them before deleting characters.
                await conn.execute("DELETE FROM harem_deletion_backups")
                # Love state is player progress. Its catalog definitions/level
                # media cascade with `characters` below, but clear state first
                # so this reset is explicit and safe on legacy FK layouts.
                await conn.execute("DELETE FROM love_player_states")
                await conn.execute("DELETE FROM harem")
                await conn.execute("DELETE FROM user_group_stats")
                await conn.execute("DELETE FROM group_stats")
                await conn.execute(
                    "UPDATE groups SET msg_counter = 0, "
                    "recent_spawn_ids = ARRAY[]::BIGINT[], "
                    "overlord_lower_spawn_count = 0, "
                    "overlord_countdown_announced = FALSE, "
                    "suspicious_lower_spawn_count = 0, "
                    "main_suspicious_spawn_count = 0"
                )
                await conn.execute(
                    "UPDATE users SET total_claims = 0, highest_rarity = NULL, "
                    "spam_banned_until = NULL, manual_muted_until = NULL, "
                    "catch_window_key = -1, catch_window_claims = 0, "
                    "daily_claimed_at = NULL, daily_neko_claimed_at = NULL, "
                    "neko_balance = 0.00, neko_game_window_started_at = NULL, "
                    "neko_game_uses = 0, neko_pass_expires_at = NULL, "
                    "casino_starts_since_cooldown = 0, casino_cooldown_until = NULL, "
                    "casino_cooldown_version = 2"
                )
                await conn.execute("DELETE FROM characters")
                # A destructive catalog reset creates a new Love catalog, so
                # restart its independent public L-code counter as well.
                await conn.execute("ALTER SEQUENCE love_card_code_sequence RESTART WITH 1")
                imported = await import_characters(conn)

        redis_deleted = await _clear_bot_redis_keys()
        print(
            f"✓ Database reset complete: imported {imported} characters; "
            f"cleared {redis_deleted} bot Redis key(s)."
        )
        return imported
    finally:
        await close_pool()
        await close_redis()


async def main() -> None:
    if "--reset" in sys.argv[1:]:
        await reset_catalog()
        return

    await init_pool()
    try:
        pool = get_pool()
        async with pool.acquire() as conn:
            await apply_schema(conn)
            await import_characters(conn)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
