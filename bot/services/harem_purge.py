"""Durable Full Owner bulk removal of catalog codes from every player Harem.

This is intentionally narrower than catalog deletion: catalog rows, media, and
catalog-stock Market listings remain. It permanently removes existing player
inventory, player escrow, Love state, recovery-snapshot copies, and pending
transfers that could otherwise recreate one of the selected owned cards.
"""
from __future__ import annotations

from dataclasses import dataclass

from bot.db.pool import get_pool
from bot.services.game import _recalculate_ranked_user_stats

MAX_GLOBAL_HAREM_PURGE_CODES = 100
MAX_CATALOG_CHARACTER_ID = 9_223_372_036_854_775_807


@dataclass(frozen=True)
class GlobalHaremPurgeResult:
    """One terminal/visible result for a persisted bulk Harem purge job."""

    success: bool
    reason: str
    job_id: int
    requested_character_ids: tuple[int, ...] = ()
    missing_character_ids: tuple[int, ...] = ()
    harem_copies_removed: int = 0
    love_states_removed: int = 0
    player_listings_removed: int = 0
    backup_items_removed: int = 0
    pending_trades_failed: int = 0
    pending_gifts_failed: int = 0
    pending_sudo_grants_failed: int = 0


def _normalize_character_ids(character_ids: object) -> tuple[int, ...] | None:
    """Validate and deduplicate a finite ordered list of catalog BIGINT IDs."""
    if not isinstance(character_ids, (list, tuple)):
        return None
    normalized: list[int] = []
    seen: set[int] = set()
    for raw_id in character_ids:
        if (
            not isinstance(raw_id, int)
            or isinstance(raw_id, bool)
            or raw_id <= 0
            or raw_id > MAX_CATALOG_CHARACTER_ID
        ):
            return None
        if raw_id not in seen:
            seen.add(raw_id)
            normalized.append(raw_id)
    if not normalized or len(normalized) > MAX_GLOBAL_HAREM_PURGE_CODES:
        return None
    return tuple(normalized)


async def create_global_harem_purge_job(
    *,
    owner_id: int,
    character_ids: list[int] | tuple[int, ...],
) -> tuple[int | None, str, tuple[int, ...]]:
    """Persist one owner-bound confirmation job after validating catalog codes."""
    normalized = _normalize_character_ids(character_ids)
    if (
        normalized is None
        or not isinstance(owner_id, int)
        or isinstance(owner_id, bool)
        or owner_id <= 0
    ):
        return None, "invalid_arguments", ()

    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            catalog_rows = await conn.fetch(
                """
                SELECT id
                FROM characters
                WHERE id = ANY($1::BIGINT[])
                FOR KEY SHARE
                """,
                list(normalized),
            )
            found_ids = {int(row["id"]) for row in catalog_rows}
            missing_ids = tuple(character_id for character_id in normalized if character_id not in found_ids)
            if missing_ids:
                return None, "character_not_found", missing_ids
            job = await conn.fetchrow(
                """
                INSERT INTO global_harem_purge_jobs (owner_id, character_ids)
                VALUES ($1, $2::BIGINT[])
                RETURNING id
                """,
                owner_id,
                list(normalized),
            )
    if not job:
        raise RuntimeError("global Harem purge job was not created")
    return int(job["id"]), "ok", normalized


async def cancel_global_harem_purge_job(*, job_id: int, owner_id: int) -> tuple[bool, str]:
    """Cancel exactly one still-pending confirmation job owned by its creator."""
    if (
        not isinstance(job_id, int)
        or isinstance(job_id, bool)
        or job_id <= 0
        or not isinstance(owner_id, int)
        or isinstance(owner_id, bool)
        or owner_id <= 0
    ):
        return False, "invalid_arguments"
    pool = get_pool()
    async with pool.acquire() as conn:
        changed = await conn.fetchval(
            """
            UPDATE global_harem_purge_jobs
            SET status = 'cancelled', resolved_at = NOW()
            WHERE id = $1 AND owner_id = $2 AND status = 'pending'
            RETURNING id
            """,
            job_id,
            owner_id,
        )
    return (True, "ok") if changed is not None else (False, "not_pending_or_not_owner")


async def _adjust_recovery_backups_after_purge(
    conn,
    *,
    backup_rows,
    rarity_scores: dict[int, int],
) -> int:
    """Remove selected cards from old `/deleteharem` snapshots permanently."""
    if not backup_rows:
        return 0

    affected_backup_ids = sorted({int(row["backup_id"]) for row in backup_rows})
    ranked_removals: dict[tuple[int, int], tuple[int, int]] = {}
    for row in backup_rows:
        if not bool(row["is_ranked"]):
            continue
        key = (int(row["backup_id"]), int(row["chat_id"]))
        old_count, old_score = ranked_removals.get(key, (0, 0))
        rarity_score = max(0, int(rarity_scores.get(int(row["character_id"]), 0)))
        ranked_removals[key] = (old_count + 1, old_score + rarity_score)

    if ranked_removals:
        await conn.executemany(
            """
            UPDATE harem_deletion_backup_group_stats
            SET claims = GREATEST(claims - $3, 0),
                rarity_score = GREATEST(rarity_score - $4, 0)
            WHERE backup_id = $1 AND chat_id = $2
            """,
            [
                (backup_id, chat_id, count, score)
                for (backup_id, chat_id), (count, score) in sorted(ranked_removals.items())
            ],
        )
        await conn.executemany(
            """
            DELETE FROM harem_deletion_backup_group_stats
            WHERE backup_id = $1 AND chat_id = $2 AND claims <= 0
            """,
            [(backup_id, chat_id) for backup_id, chat_id in sorted(ranked_removals)],
        )

    # Rebuild each surviving snapshot header from its exact remaining items.
    # Love rows should not normally be present in v19 backups, but filtering
    # them defensively preserves the same profile accounting invariant.
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
                JOIN characters AS character ON character.id = item.character_id
                WHERE item.backup_id = backup.id
                  AND item.is_ranked = TRUE
                  AND LOWER(BTRIM(COALESCE(character.rarity, ''))) <> 'love'
            ),
            original_highest_rarity = (
                SELECT character.rarity
                FROM harem_deletion_backup_items AS item
                JOIN characters AS character ON character.id = item.character_id
                WHERE item.backup_id = backup.id
                  AND item.is_ranked = TRUE
                  AND LOWER(BTRIM(COALESCE(character.rarity, ''))) <> 'love'
                ORDER BY character.rarity_score DESC, character.id ASC, item.original_harem_id ASC
                LIMIT 1
            )
        WHERE backup.id = ANY($1::BIGINT[])
          AND backup.restored_at IS NULL
        """,
        affected_backup_ids,
    )
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
    return len(backup_rows)


async def execute_global_harem_purge_job(
    *,
    job_id: int,
    owner_id: int,
) -> GlobalHaremPurgeResult:
    """Atomically apply a confirmed permanent multi-code player-inventory purge.

    The durable job row is locked before touching inventory. A duplicate
    callback cannot run the destructive action twice. Catalog rows and official
    catalog-stock Market listings remain intentionally untouched; this command
    removes only already player-owned/escrowed state and paths that would revive
    that ownership.
    """
    safe_job_id = job_id if isinstance(job_id, int) and not isinstance(job_id, bool) else 0
    if (
        safe_job_id <= 0
        or not isinstance(owner_id, int)
        or isinstance(owner_id, bool)
        or owner_id <= 0
    ):
        return GlobalHaremPurgeResult(False, "invalid_arguments", safe_job_id)

    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Serialize two overlapping bulk jobs before their individual job
            # rows or catalog cards are locked in potentially different orders.
            await conn.execute("SELECT pg_advisory_xact_lock($1)", -704_188_207_401)
            job = await conn.fetchrow(
                """
                SELECT id, owner_id, character_ids, status
                FROM global_harem_purge_jobs
                WHERE id = $1
                FOR UPDATE
                """,
                job_id,
            )
            if not job:
                return GlobalHaremPurgeResult(False, "not_found", job_id)
            if int(job["owner_id"]) != owner_id:
                return GlobalHaremPurgeResult(False, "not_owner", job_id)
            if str(job["status"] or "") != "pending":
                return GlobalHaremPurgeResult(False, "not_pending", job_id)

            character_ids = _normalize_character_ids(list(job["character_ids"] or []))
            if character_ids is None:
                raise RuntimeError("stored global Harem purge job is invalid")
            ids = list(character_ids)

            # A row lock on every live catalog card serializes this purge with
            # catches, owner grants, and Market ownership inserts for these IDs.
            catalog_rows = await conn.fetch(
                """
                SELECT id, rarity_score
                FROM characters
                WHERE id = ANY($1::BIGINT[])
                FOR UPDATE
                """,
                ids,
            )
            rarity_scores = {
                int(row["id"]): max(0, int(row["rarity_score"] or 0))
                for row in catalog_rows
            }
            missing_ids = tuple(character_id for character_id in character_ids if character_id not in rarity_scores)
            live_ids = sorted(rarity_scores)

            await conn.execute(
                """
                UPDATE global_harem_purge_jobs
                SET status = 'running', confirmed_at = NOW()
                WHERE id = $1
                """,
                job_id,
            )

            if live_ids:
                # Player listings are ownership escrow, not catalog stock. They
                # must disappear without restoration so a later cancellation or
                # buyer cannot bring a purged player copy back.
                player_listing_rows = await conn.fetch(
                    """
                    DELETE FROM market_listings
                    WHERE listing_kind = 'player' AND character_id = ANY($1::BIGINT[])
                    RETURNING id
                    """,
                    live_ids,
                )
                love_state_rows = await conn.fetch(
                    """
                    DELETE FROM love_player_states
                    WHERE character_id = ANY($1::BIGINT[])
                    RETURNING user_id, character_id
                    """,
                    live_ids,
                )
                harem_rows = await conn.fetch(
                    """
                    DELETE FROM harem
                    WHERE character_id = ANY($1::BIGINT[])
                    RETURNING user_id, chat_id, character_id, is_ranked
                    """,
                    live_ids,
                )
            else:
                player_listing_rows = []
                love_state_rows = []
                harem_rows = []

            ranked_user_ids: set[int] = set()
            group_adjustments: dict[tuple[int, int], tuple[int, int]] = {}
            for row in harem_rows:
                if not bool(row["is_ranked"]):
                    continue
                user_id = int(row["user_id"])
                chat_id = int(row["chat_id"])
                ranked_user_ids.add(user_id)
                key = (user_id, chat_id)
                old_count, old_score = group_adjustments.get(key, (0, 0))
                score = max(0, int(rarity_scores.get(int(row["character_id"]), 0)))
                group_adjustments[key] = (old_count + 1, old_score + score)

            if group_adjustments:
                await conn.executemany(
                    """
                    UPDATE user_group_stats
                    SET claims = GREATEST(claims - $3, 0),
                        rarity_score = GREATEST(rarity_score - $4, 0)
                    WHERE user_id = $1 AND chat_id = $2
                    """,
                    [
                        (user_id, chat_id, count, score)
                        for (user_id, chat_id), (count, score) in sorted(group_adjustments.items())
                    ],
                )
                await conn.executemany(
                    """
                    DELETE FROM user_group_stats
                    WHERE user_id = $1 AND chat_id = $2 AND claims <= 0
                    """,
                    [(user_id, chat_id) for user_id, chat_id in sorted(group_adjustments)],
                )
                affected_chat_ids = sorted({chat_id for _user_id, chat_id in group_adjustments})
                await conn.execute(
                    """
                    UPDATE group_stats AS group_stat
                    SET total_claims = COALESCE(
                        (
                            SELECT SUM(stats.claims)
                            FROM user_group_stats AS stats
                            WHERE stats.chat_id = group_stat.chat_id
                        ),
                        0
                    )
                    WHERE group_stat.chat_id = ANY($1::BIGINT[])
                    """,
                    affected_chat_ids,
                )

            for user_id in sorted(ranked_user_ids):
                await _recalculate_ranked_user_stats(conn, user_id)

            if live_ids:
                backup_rows = await conn.fetch(
                    """
                    DELETE FROM harem_deletion_backup_items
                    WHERE character_id = ANY($1::BIGINT[])
                    RETURNING backup_id, chat_id, character_id, is_ranked
                    """,
                    live_ids,
                )
                backup_items_removed = await _adjust_recovery_backups_after_purge(
                    conn,
                    backup_rows=backup_rows,
                    rarity_scores=rarity_scores,
                )
                pending_trade_rows = await conn.fetch(
                    """
                    UPDATE trades
                    SET status = 'failed', resolved_at = NOW()
                    WHERE status = 'pending'
                      AND (
                          from_character_id = ANY($1::BIGINT[])
                          OR to_character_id = ANY($1::BIGINT[])
                      )
                    RETURNING id
                    """,
                    live_ids,
                )
                pending_gift_rows = await conn.fetch(
                    """
                    UPDATE gifts
                    SET status = 'failed', resolved_at = NOW()
                    WHERE status = 'pending'
                      AND character_ids && $1::BIGINT[]
                    RETURNING id
                    """,
                    live_ids,
                )
                pending_sudo_rows = await conn.fetch(
                    """
                    UPDATE sudo_grants
                    SET status = 'failed', resolved_at = NOW()
                    WHERE status = 'pending'
                      AND character_ids && $1::BIGINT[]
                    RETURNING id
                    """,
                    live_ids,
                )
            else:
                backup_items_removed = 0
                pending_trade_rows = []
                pending_gift_rows = []
                pending_sudo_rows = []

            result = GlobalHaremPurgeResult(
                success=True,
                reason="ok",
                job_id=job_id,
                requested_character_ids=character_ids,
                missing_character_ids=missing_ids,
                harem_copies_removed=len(harem_rows),
                love_states_removed=len(love_state_rows),
                player_listings_removed=len(player_listing_rows),
                backup_items_removed=backup_items_removed,
                pending_trades_failed=len(pending_trade_rows),
                pending_gifts_failed=len(pending_gift_rows),
                pending_sudo_grants_failed=len(pending_sudo_rows),
            )
            await conn.execute(
                """
                UPDATE global_harem_purge_jobs
                SET status = 'completed',
                    resolved_at = NOW(),
                    harem_copies_removed = $2,
                    love_states_removed = $3,
                    player_listings_removed = $4,
                    backup_items_removed = $5,
                    pending_trades_failed = $6,
                    pending_gifts_failed = $7,
                    pending_sudo_grants_failed = $8
                WHERE id = $1
                """,
                job_id,
                result.harem_copies_removed,
                result.love_states_removed,
                result.player_listings_removed,
                result.backup_items_removed,
                result.pending_trades_failed,
                result.pending_gifts_failed,
                result.pending_sudo_grants_failed,
            )
            return result
