"""Atomic destructive player-state reset used only by root-confirmed /deletedatabase.

The catalog itself deliberately does not appear in any DELETE/UPDATE statement
here. Character rows, curated events, group configuration, owner/admin roles,
and runtime configuration survive intact.
"""
from __future__ import annotations

from dataclasses import dataclass

from bot.db.pool import get_pool


@dataclass(frozen=True)
class PlayerDatabaseResetResult:
    """A small truthful summary for the root owner's completion card."""

    harem_copies_removed: int
    player_market_listings_removed: int
    active_spawns_removed: int
    scheduled_op_spawns_removed: int
    users_reset: int


async def reset_all_player_game_state() -> PlayerDatabaseResetResult:
    """Irreversibly clear player collection/economy/progress state in one transaction.

    This intentionally creates no harem or wallet backup. An ACCESS EXCLUSIVE
    lock set makes the reset a clear before/after boundary: a concurrent catch,
    trade, market purchase, daily claim, or casino action either completes
    before the reset and is wiped, or waits and begins against clean state.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Keep the comma-separated lock list in dependency-safe order and
            # include every mutable player-state table touched below. Catalog
            # tables (`characters`, `catalog_events`) and group configuration
            # deliberately remain unlocked/unmodified.
            await conn.execute(
                """
                LOCK TABLE
                    active_spawns,
                    scheduled_op_spawns,
                    trades,
                    gifts,
                    sudo_grants,
                    market_listings,
                    neko_transfer_requests,
                    neko_casino_input_sessions,
                    neko_dice_bets,
                    neko_casino_games,
                    neko_wallet_ledger,
                    neko_daily_streaks,
                    neko_weekly_quests,
                    neko_wallet_deletion_backups,
                    neko_pass_grants,
                    harem_deletion_backups,
                    harem_deletion_backup_items,
                    harem_deletion_backup_group_stats,
                    harem_display_preferences,
                    love_player_states,
                    user_achievements,
                    overlord_claims,
                    catch_events,
                    harem,
                    user_group_stats,
                    group_stats,
                    users
                IN ACCESS EXCLUSIVE MODE
                """
            )

            harem_copies_removed = int(await conn.fetchval("SELECT COUNT(*) FROM harem") or 0)
            player_market_listings_removed = int(await conn.fetchval(
                "SELECT COUNT(*) FROM market_listings WHERE listing_kind = 'player'"
            ) or 0)
            active_spawns_removed = int(await conn.fetchval(
                "SELECT COUNT(*) FROM active_spawns"
            ) or 0)
            scheduled_op_spawns_removed = int(await conn.fetchval(
                "SELECT COUNT(*) FROM scheduled_op_spawns"
            ) or 0)
            users_reset = int(await conn.fetchval("SELECT COUNT(*) FROM users") or 0)

            # Pending/in-flight collection changes must not be able to restore
            # cards after the wipe. Historic rows are also erased so this is a
            # true no-backup reset rather than a hidden restore path.
            await conn.execute("DELETE FROM trades")
            await conn.execute("DELETE FROM gifts")
            await conn.execute("DELETE FROM sudo_grants")
            # Official catalog stock is not player-owned inventory and remains
            # listed; player listings are escrowed Harem copies and are removed.
            await conn.execute("DELETE FROM market_listings WHERE listing_kind = 'player'")

            # Reset the entire Neko wallet/game ecosystem, including rolling
            # Daily Neko/streak/quest state and every restoration/audit record.
            await conn.execute("DELETE FROM neko_transfer_requests")
            await conn.execute("DELETE FROM neko_casino_input_sessions")
            await conn.execute("DELETE FROM neko_dice_bets")
            await conn.execute("DELETE FROM neko_casino_games")
            await conn.execute("DELETE FROM neko_wallet_ledger")
            await conn.execute("DELETE FROM neko_daily_streaks")
            await conn.execute("DELETE FROM neko_weekly_quests")
            await conn.execute("DELETE FROM neko_wallet_deletion_backups")
            await conn.execute("DELETE FROM neko_pass_grants")

            # Delete backup headers (items/group stats cascade) so /backharem
            # can never recover a collection that this command wiped.
            await conn.execute("DELETE FROM harem_deletion_backups")
            # A saved Anime/Rarity display mode is Harem state rather than an
            # account/moderation setting, so the fresh collection starts clean.
            await conn.execute("DELETE FROM harem_display_preferences")
            # Love definitions and their L-codes are catalog data, but every
            # player's selected card, level, points, escrow and cooldowns are
            # player progress and must be irreversibly cleared by /deletedatabase.
            await conn.execute("DELETE FROM love_player_states")
            await conn.execute("DELETE FROM user_achievements")
            # `/catchtop` is exact player catch history, not a derived Harem
            # view, so a root player-database wipe must clear its rolling ledger.
            await conn.execute("DELETE FROM catch_events")
            # OverLord's ten-owner ledger is player claim state. A full player
            # database wipe deliberately restarts this cap while preserving the
            # catalog character row itself.
            await conn.execute("DELETE FROM overlord_claims")
            await conn.execute("DELETE FROM harem")
            await conn.execute("DELETE FROM user_group_stats")
            # Retain each group's historical spawn total, but remove all card
            # catch totals so leaderboards match empty Harems immediately.
            await conn.execute("UPDATE group_stats SET total_claims = 0")
            # Active cards and already-announced future OP cards are invalidated
            # so neither a visible pre-reset card nor a queued pre-reset card can
            # reintroduce game activity after completion. Group configuration and
            # event/catalog definitions remain untouched.
            await conn.execute("DELETE FROM active_spawns")
            await conn.execute("DELETE FROM scheduled_op_spawns")

            await conn.execute(
                """
                UPDATE users
                SET total_claims = 0,
                    highest_rarity = NULL,
                    catch_window_key = -1,
                    catch_window_claims = 0,
                    daily_claimed_at = NULL,
                    daily_neko_claimed_at = NULL,
                    neko_balance = 0.00,
                    neko_game_window_started_at = NULL,
                    neko_game_uses = 0,
                    neko_pass_expires_at = NULL,
                    casino_starts_since_cooldown = 0,
                    casino_cooldown_until = NULL,
                    casino_cooldown_version = 2
                """
            )

    return PlayerDatabaseResetResult(
        harem_copies_removed=harem_copies_removed,
        player_market_listings_removed=player_market_listings_removed,
        active_spawns_removed=active_spawns_removed,
        scheduled_op_spawns_removed=scheduled_op_spawns_removed,
        users_reset=users_reset,
    )

RESET_CONFIRMATION_TTL_SECONDS = 15 * 60


async def create_player_database_reset_job(owner_id: int) -> int:
    """Create one persisted, single-use root confirmation prompt."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO player_database_reset_jobs (owner_id)
            VALUES ($1)
            RETURNING id
            """,
            owner_id,
        )
    return int(row["id"])


async def claim_player_database_reset_job(job_id: int, owner_id: int) -> bool:
    """Atomically consume a fresh pending confirmation before the destructive work."""
    pool = get_pool()
    async with pool.acquire() as conn:
        claimed = await conn.fetchval(
            """
            UPDATE player_database_reset_jobs
            SET status = 'running', confirmed_at = NOW()
            WHERE id = $1
              AND owner_id = $2
              AND status = 'pending'
              AND created_at >= NOW() - ($3::INT * INTERVAL '1 second')
            RETURNING 1
            """,
            job_id,
            owner_id,
            RESET_CONFIRMATION_TTL_SECONDS,
        )
    return bool(claimed)


async def cancel_player_database_reset_job(job_id: int, owner_id: int) -> bool:
    """Cancel an unconfirmed reset prompt; completed/running jobs stay immutable."""
    pool = get_pool()
    async with pool.acquire() as conn:
        cancelled = await conn.fetchval(
            """
            UPDATE player_database_reset_jobs
            SET status = 'cancelled', resolved_at = NOW()
            WHERE id = $1 AND owner_id = $2 AND status = 'pending'
            RETURNING 1
            """,
            job_id,
            owner_id,
        )
    return bool(cancelled)


async def finish_player_database_reset_job(
    job_id: int,
    owner_id: int,
    *,
    success: bool,
    error_detail: str | None = None,
) -> None:
    """Mark the already-claimed confirmation terminal without retaining player data."""
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE player_database_reset_jobs
            SET status = $3,
                resolved_at = NOW(),
                error_detail = $4
            WHERE id = $1 AND owner_id = $2 AND status = 'running'
            """,
            job_id,
            owner_id,
            "completed" if success else "failed",
            (error_detail or "")[:1000] if not success else None,
        )
