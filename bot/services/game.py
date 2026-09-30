"""High-level game operations: claim, gift, trade, favorite."""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from decimal import Decimal

import asyncio

from bot.db.pool import get_pool
from bot.services.neko import (
    MAX_NEKO_BALANCE,
    NEKO_QUANTUM,
    _record_wallet_ledger,
)
from bot.services.love import LOVE_LEVEL_TARGETS, LOVE_RARITY, grant_love_state_in_transaction
from bot.services.special_ranks import (
    ETERNAL_RARITY,
    OVERLORD_RARITY,
    OVERLORD_UNIQUE_OWNER_CAP,
    SUSPICIOUS_RARITY,
    is_overlord,
)

RARITY_ORDER = [
    "Common", "Rare", "Legendary", "Mythical", "Sovereign", "OP",
    ETERNAL_RARITY, SUSPICIOUS_RARITY, OVERLORD_RARITY,
]

# Eternal and the ten-owner OverLord are deliberately non-burnable. Suspicious
# remains a normal burnable tier at the next fixed step above OP.
BURN_NEKO_REWARDS: dict[str, Decimal] = {
    "Common": Decimal("1.00"),
    "Rare": Decimal("2.00"),
    "Legendary": Decimal("5.00"),
    "Mythical": Decimal("10.00"),
    "Sovereign": Decimal("15.00"),
    "OP": Decimal("20.00"),
    SUSPICIOUS_RARITY: Decimal("25.00"),
}
ETERNAL_TRANSFER_WARNING = "You can't sell, gift, or trade an Eternal rank card."
OVERLORD_TRANSFER_WARNING = "You can't sell, gift, or trade an OverLord rank card."
OVERLORD_BURN_WARNING = "OverLord cards cannot be burned."
LOVE_TRANSFER_WARNING = "Love cards use the fixed-price /sell path and cannot be gifted or traded."
LOVE_BURN_WARNING = "Love cards cannot be burned; use /sell to list your active Love card."


def transfer_protection_reason(rarity: object) -> str | None:
    """Return the precise user-facing protection key for a locked card tier."""
    if is_overlord(rarity):
        return "overlord_protected"
    if str(rarity or "").strip().casefold() == ETERNAL_RARITY.casefold():
        return "eternal_protected"
    if str(rarity or "").strip().casefold() == LOVE_RARITY.casefold():
        return "love_protected"
    return None


def _rows_transfer_protection_reason(rows) -> str | None:
    """Find the first protected live card in a gift/trade row collection."""
    for row in rows:
        reason = transfer_protection_reason(row["rarity"])
        if reason is not None:
            return reason
    return None


# Successful /catch claims are limited globally, not per group. Window keys
# come from Unix time, so every player's 9-hour reset happens together.
CATCH_LIMIT = 25
CATCH_LIMIT_WINDOW_SECONDS = 9 * 60 * 60
# Every posted spawn, automatic or owner-forced, has a short universal head
# start guard. It is checked under the active-spawn row lock in claim_character.
CATCH_PROTECTION_SECONDS = 4
MAX_CATALOG_EVENT_ID = 9_223_372_036_854_775_807

# A /daily reward is available independently for each player exactly 24 hours
# after their last successful claim. The weights total 100; Mythical is the
# deliberately least-likely daily tier, while all four requested tiers remain
# possible regardless of how many cards each tier contains.
DAILY_REWARD_INTERVAL_SECONDS = 24 * 60 * 60
DAILY_RARITY_WEIGHTS: tuple[tuple[str, int], ...] = (
    ("Common", 60),
    ("Rare", 25),
    ("Legendary", 11),
    ("Mythical", 4),
)
DAILY_REWARD_RARITIES = tuple(rarity for rarity, _weight in DAILY_RARITY_WEIGHTS)


def _higher_rarity(a: str | None, b: str | None) -> str:
    """Return whichever rarity is higher. Handles None/unknown gracefully."""
    a_idx = RARITY_ORDER.index(a) if a in RARITY_ORDER else -1
    b_idx = RARITY_ORDER.index(b) if b in RARITY_ORDER else -1
    return a if a_idx >= b_idx else b


def _daily_random_below(upper_bound: int) -> int:
    """Use a cryptographically strong draw for the public daily tier roll."""
    return secrets.randbelow(upper_bound)


def _pick_daily_reward_rarity(available_rarities: set[str]) -> str | None:
    """Pick one available daily tier using the documented weighted policy."""
    available = {str(rarity or "").strip().casefold() for rarity in available_rarities}
    eligible = [
        (rarity, weight)
        for rarity, weight in DAILY_RARITY_WEIGHTS
        if weight > 0 and rarity.casefold() in available
    ]
    if not eligible:
        return None

    ticket = _daily_random_below(sum(weight for _rarity, weight in eligible))
    for rarity, weight in eligible:
        if ticket < weight:
            return rarity
        ticket -= weight
    # Defensive fallback for an impossible out-of-range random-provider result.
    return eligible[-1][0]


async def _recalculate_ranked_user_stats(conn, user_id: int) -> None:
    """Rebuild global ranking fields from ranked harem rows only.

    ``/sudo all -`` intentionally adds displayable collection rows without
    affecting global collector status.  Recalculation is used after ownership
    transfers/deletions where a mixed ranked and unranked inventory can change
    hands.  Group statistics retain their historical per-group semantics.
    """
    ranked_total = int(await conn.fetchval(
        """SELECT COUNT(*) FROM harem AS h
           JOIN characters AS c ON c.id = h.character_id
           WHERE h.user_id = $1 AND h.is_ranked = TRUE
             AND LOWER(BTRIM(c.rarity)) <> 'love'""",
        user_id,
    ) or 0)
    highest = await conn.fetchval(
        """
        SELECT c.rarity
        FROM harem AS h
        JOIN characters AS c ON c.id = h.character_id
        WHERE h.user_id = $1 AND h.is_ranked = TRUE
          AND LOWER(BTRIM(c.rarity)) <> 'love'
        ORDER BY c.rarity_score DESC, c.id ASC, h.id ASC
        LIMIT 1
        """,
        user_id,
    )
    await conn.execute(
        """
        UPDATE users
        SET total_claims = $2, highest_rarity = $3
        WHERE user_id = $1
        """,
        user_id,
        ranked_total,
        highest,
    )


@dataclass
class ClaimResult:
    success: bool
    reason: str = ""
    character_id: int = 0
    name: str = ""
    anime: str = ""
    rarity: str = ""
    rarity_score: int = 0
    catch_limit_count: int = 0
    catch_limit: int = CATCH_LIMIT
    catch_limit_reset_at: int = 0
    catch_available_at: float = 0.0


@dataclass(frozen=True)
class CatchLimitStatus:
    """One user's effective count in the current shared 9-hour window."""
    count: int
    limit: int
    reset_at: int

    @property
    def exhausted(self) -> bool:
        return self.count >= self.limit


@dataclass
class DailyRewardResult:
    """Outcome of a durable per-player rolling /daily claim."""
    success: bool
    reason: str = ""
    remaining_seconds: int = 0
    character_id: int = 0
    name: str = ""
    anime: str = ""
    rarity: str = ""
    rarity_score: int = 0
    image_url: str = ""
    media_type: str = "photo"
    event: str = ""


@dataclass
class HaremDeletionResult:
    """Outcome of a confirmed /deleteharem operation and its recovery snapshot."""
    deleted_count: int
    backup_id: int | None = None


@dataclass
class HaremCopyDeletionResult:
    """Outcome of deleting one confirmed harem copy through /doomcharacter <id>."""
    success: bool
    reason: str = ""
    user_id: int | None = None
    character_id: int | None = None
    character_name: str = ""
    anime: str = ""
    rarity: str = ""
    remaining_copies: int = 0


@dataclass
class BurnHaremCopyResult:
    """Outcome of permanently burning one owned non-Eternal Harem copy."""
    success: bool
    reason: str = ""
    harem_id: int | None = None
    character_id: int | None = None
    character_name: str = ""
    rarity: str = ""
    reward: Decimal = Decimal("0.00")
    balance_after: Decimal = Decimal("0.00")
    remaining_copies: int = 0
    total_claims: int = 0


@dataclass
class HaremRestoreResult:
    """Outcome of restoring one owner-created /deleteharem snapshot."""
    success: bool
    reason: str = ""
    user_id: int | None = None
    restored_count: int = 0
    backup_id: int | None = None


def catch_limit_window(now: int | None = None) -> tuple[int, int]:
    """Return the globally shared 9-hour window key and its next reset epoch."""
    current = int(time.time()) if now is None else int(now)
    key = current // CATCH_LIMIT_WINDOW_SECONDS
    return key, (key + 1) * CATCH_LIMIT_WINDOW_SECONDS


async def get_user_catch_limit_status(
    user_id: int,
    *,
    now: int | None = None,
) -> CatchLimitStatus:
    """Read a user's current global /catch allowance without consuming it."""
    window_key, reset_at = catch_limit_window(now)
    pool = get_pool()
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            """
            SELECT CASE
                WHEN catch_window_key = $2 THEN catch_window_claims
                ELSE 0
            END
            FROM users
            WHERE user_id = $1
            """,
            user_id,
            window_key,
        )
    return CatchLimitStatus(
        count=max(0, int(count or 0)),
        limit=CATCH_LIMIT,
        reset_at=reset_at,
    )


async def claim_daily_reward(
    *,
    user_id: int,
    username: str,
    full_name: str,
    chat_id: int,
) -> DailyRewardResult:
    """Atomically grant one rolling-24-hour Common-to-Mythical daily reward.

    The user row is locked before any tier roll or harem write. Two simultaneous
    /daily requests from the same player therefore cannot create two rewards;
    the second request observes the first one's timestamp. The reward is a
    normal ranked harem card and updates global collection totals, but it is not
    a catch and deliberately does not alter catch limits or group leaderboards.
    Paused curated-event cards use the same eligibility rule as automatic spawn.
    """
    if not isinstance(user_id, int) or user_id <= 0 or not isinstance(chat_id, int):
        return DailyRewardResult(success=False, reason="invalid_request")

    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Private /daily use may happen before group activity has created a
            # users row, so establish/update it inside the same transaction.
            await conn.execute(
                """
                INSERT INTO users (user_id, username, full_name, last_seen_at)
                VALUES ($1, $2, $3, NOW())
                ON CONFLICT (user_id) DO UPDATE SET
                  username = EXCLUDED.username,
                  full_name = EXCLUDED.full_name,
                  last_seen_at = NOW()
                """,
                user_id,
                username,
                full_name,
            )
            user = await conn.fetchrow(
                """
                SELECT
                    highest_rarity,
                    CASE
                        WHEN daily_claimed_at IS NOT NULL
                         AND daily_claimed_at + INTERVAL '24 hours' > NOW()
                        THEN CEIL(EXTRACT(EPOCH FROM (
                            daily_claimed_at + INTERVAL '24 hours' - NOW()
                        )))::BIGINT
                        ELSE 0
                    END AS remaining_seconds
                FROM users
                WHERE user_id = $1
                FOR UPDATE
                """,
                user_id,
            )
            if not user:
                return DailyRewardResult(success=False, reason="user_unavailable")

            remaining_seconds = max(0, int(user["remaining_seconds"] or 0))
            if remaining_seconds:
                return DailyRewardResult(
                    success=False,
                    reason="cooldown",
                    remaining_seconds=remaining_seconds,
                )

            # First discover which requested tiers currently have eligible cards.
            # A deleted or paused-only tier is removed from the weighted roll
            # rather than wasting or consuming the player's daily claim.
            available_rows = await conn.fetch(
                """
                SELECT DISTINCT c.rarity
                FROM characters AS c
                LEFT JOIN catalog_events AS event_control
                  ON LOWER(BTRIM(COALESCE(event_control.label, ''))) =
                     LOWER(BTRIM(COALESCE(c.event, '')))
                WHERE BTRIM(c.name) <> ''
                  AND LOWER(BTRIM(c.rarity)) = ANY($1::TEXT[])
                  AND (
                      BTRIM(COALESCE(c.event, '')) = ''
                      OR COALESCE(event_control.spawn_enabled, TRUE) = TRUE
                  )
                """,
                [rarity.casefold() for rarity in DAILY_REWARD_RARITIES],
            )
            tier = _pick_daily_reward_rarity({str(row["rarity"]) for row in available_rows})
            if tier is None:
                return DailyRewardResult(success=False, reason="catalog_unavailable")

            character = await conn.fetchrow(
                """
                SELECT c.id, c.name, c.anime, c.rarity, c.rarity_score,
                       c.image_url, COALESCE(c.media_type, 'photo') AS media_type, COALESCE(c.event, '') AS event
                FROM characters AS c
                LEFT JOIN catalog_events AS event_control
                  ON LOWER(BTRIM(COALESCE(event_control.label, ''))) =
                     LOWER(BTRIM(COALESCE(c.event, '')))
                WHERE BTRIM(c.name) <> ''
                  AND LOWER(BTRIM(c.rarity)) = LOWER(BTRIM($1))
                  AND (
                      BTRIM(COALESCE(c.event, '')) = ''
                      OR COALESCE(event_control.spawn_enabled, TRUE) = TRUE
                  )
                ORDER BY RANDOM()
                LIMIT 1
                """,
                tier,
            )
            if not character:
                # A rare concurrent catalog edit may remove the selected tier
                # after the availability read. No timestamp was written, so the
                # player can retry without losing their daily reward.
                return DailyRewardResult(success=False, reason="catalog_unavailable")

            await conn.execute(
                """
                INSERT INTO harem (user_id, chat_id, character_id, is_ranked)
                VALUES ($1, $2, $3, TRUE)
                """,
                user_id,
                chat_id,
                int(character["id"]),
            )

            # Daily cards are globally ranked collection rewards, just like a
            # collector-achievement reward, but intentionally have no group
            # claim/stat entry and consume none of the 25 catch-limit slots.
            new_highest = _higher_rarity(
                str(user["highest_rarity"]) if user["highest_rarity"] else None,
                str(character["rarity"]),
            )
            await conn.execute(
                """
                UPDATE users
                SET daily_claimed_at = NOW(),
                    total_claims = total_claims + 1,
                    highest_rarity = $2,
                    last_seen_at = NOW()
                WHERE user_id = $1
                """,
                user_id,
                new_highest,
            )

            return DailyRewardResult(
                success=True,
                character_id=int(character["id"]),
                name=str(character["name"]),
                anime=str(character["anime"]),
                rarity=str(character["rarity"]),
                rarity_score=int(character["rarity_score"] or 0),
                image_url=str(character["image_url"] or ""),
                media_type=str(character["media_type"] or "photo"),
                event=str(character["event"] or ""),
            )


async def claim_character(*, chat_id: int, user_id: int, username: str,
                          full_name: str, spawn: dict,
                          expected_character_id: int | None = None) -> ClaimResult:
    """Atomically claim a spawned character for a user.

    Uses SELECT ... FOR UPDATE on the active spawn row to prevent races
    when multiple users /guess at the same instant. The same lock makes the
    first-four-seconds catch protection authoritative for direct service calls
    as well as the /catch command UI. A caller may bind the claim to the card
    it displayed; if a newer automatic spawn replaced it meanwhile, the newer
    card is never granted for the older answer.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Lock the spawn row
            row = await conn.fetchrow(
                "SELECT character_id, spawned_at, expires_at FROM active_spawns "
                "WHERE chat_id = $1 FOR UPDATE",
                chat_id,
            )
            if not row:
                return ClaimResult(success=False, reason="no_active_spawn")

            # The command handler supplies the character it matched. If another
            # message threshold produced a newer card after that lookup, reject
            # this stale answer instead of accidentally granting the new card.
            if (
                expected_character_id is not None
                and int(row["character_id"]) != int(expected_character_id)
            ):
                return ClaimResult(
                    success=False,
                    reason="replaced",
                    character_id=int(row["character_id"]),
                )

            now = time.time()
            now_epoch = int(now)
            if row["expires_at"] < now_epoch:
                await conn.execute("DELETE FROM active_spawns WHERE chat_id = $1", chat_id)
                return ClaimResult(success=False, reason="expired")

            catch_available_at = float(row["spawned_at"]) + CATCH_PROTECTION_SECONDS
            if now < catch_available_at:
                # Do not create/update a user, consume a catch allowance, or
                # alter the active row. The card remains claimable after this
                # exact four-second gate elapses.
                return ClaimResult(
                    success=False,
                    reason="catch_protected",
                    catch_available_at=catch_available_at,
                )

            char_id = int(row["character_id"])
            # Lock the catalog row before the user/catch updates. Every
            # OverLord claim takes this same lock, making the ten-distinct-owner
            # cap race-safe even if a stale visible spawn somehow exists.
            char = await conn.fetchrow(
                """
                SELECT id, name, anime, rarity, rarity_score
                FROM characters
                WHERE id = $1
                FOR UPDATE
                """,
                char_id,
            )
            if not char:
                await conn.execute("DELETE FROM active_spawns WHERE chat_id = $1", chat_id)
                return ClaimResult(success=False, reason="no_active_spawn")
            if str(char["rarity"] or "").strip().casefold() == LOVE_RARITY.casefold():
                # Love must never be claimable even if a manually altered legacy
                # active_spawns row points at it. Remove the invalid spawn.
                await conn.execute("DELETE FROM active_spawns WHERE chat_id = $1", chat_id)
                return ClaimResult(
                    success=False,
                    reason="love_not_spawnable",
                    character_id=char_id,
                    name=str(char["name"] or ""),
                    rarity=LOVE_RARITY,
                )
            claiming_overlord = is_overlord(char["rarity"])

            # Upsert user before the durable OverLord ledger's user foreign key.
            await conn.execute(
                """
                INSERT INTO users (user_id, username, full_name, last_seen_at)
                VALUES ($1, $2, $3, NOW())
                ON CONFLICT (user_id) DO UPDATE SET
                  username = EXCLUDED.username,
                  full_name = EXCLUDED.full_name,
                  last_seen_at = NOW()
                """,
                user_id, username, full_name,
            )

            if claiming_overlord:
                already_claimed = await conn.fetchval(
                    """
                    SELECT 1
                    FROM overlord_claims
                    WHERE character_id = $1 AND user_id = $2
                    """,
                    char_id,
                    user_id,
                )
                if already_claimed:
                    # Keep the active card visible: another eligible player can
                    # still become one of this card's ten distinct owners.
                    return ClaimResult(
                        success=False,
                        reason="overlord_already_claimed",
                        character_id=char_id,
                        name=str(char["name"] or ""),
                        rarity=str(char["rarity"] or OVERLORD_RARITY),
                    )
                owner_count = int(await conn.fetchval(
                    "SELECT COUNT(*) FROM overlord_claims WHERE character_id = $1",
                    char_id,
                ) or 0)
                if owner_count >= OVERLORD_UNIQUE_OWNER_CAP:
                    # A stale pre-cap spawn must not linger once the durable cap
                    # has been reached elsewhere.
                    await conn.execute("DELETE FROM active_spawns WHERE chat_id = $1", chat_id)
                    return ClaimResult(
                        success=False,
                        reason="overlord_owner_cap_reached",
                        character_id=char_id,
                        name=str(char["name"] or ""),
                        rarity=str(char["rarity"] or OVERLORD_RARITY),
                    )

            # Reserve one globally limited /catch slot while the spawn row is
            # locked. The conditional UPDATE is the race-safe authority: two
            # simultaneous catches at 24/25 can produce only one 25th claim.
            catch_window_key, catch_reset_at = catch_limit_window(now_epoch)
            catch_counter = await conn.fetchrow(
                """
                UPDATE users
                SET catch_window_key = $2,
                    catch_window_claims = CASE
                        WHEN catch_window_key = $2 THEN catch_window_claims + 1
                        ELSE 1
                    END
                WHERE user_id = $1
                  AND (
                    catch_window_key <> $2
                    OR catch_window_claims < $3
                  )
                RETURNING catch_window_claims
                """,
                user_id,
                catch_window_key,
                CATCH_LIMIT,
            )
            if not catch_counter:
                return ClaimResult(
                    success=False,
                    reason="catch_limit",
                    catch_limit_count=CATCH_LIMIT,
                    catch_limit=CATCH_LIMIT,
                    catch_limit_reset_at=catch_reset_at,
                )
            catch_limit_count = int(catch_counter["catch_window_claims"])

            # Ordinary tiers still permit duplicate catches. OverLord is the
            # explicit exception: each person can become a holder of an exact
            # OverLord card once, preserving its ten-person collectible cap.
            await conn.execute(
                """
                INSERT INTO harem (user_id, chat_id, character_id)
                VALUES ($1, $2, $3)
                """,
                user_id, chat_id, char_id,
            )
            # Keep an exact catch-only rolling ledger independent from Harem
            # ownership. The event is written only by this successful current-
            # spawn claim path, so Daily, /sudo, Market, trade, gift, restore,
            # burn, and later deletion can never inflate `/catchtop`.
            await conn.execute(
                """
                DELETE FROM catch_events
                WHERE caught_at < NOW() - INTERVAL '8 days'
                """
            )
            await conn.execute(
                """
                INSERT INTO catch_events (user_id, chat_id, character_id)
                VALUES ($1, $2, $3)
                """,
                user_id, chat_id, char_id,
            )
            if claiming_overlord:
                first_claim = await conn.fetchval(
                    """
                    INSERT INTO overlord_claims (character_id, user_id)
                    VALUES ($1, $2)
                    ON CONFLICT (character_id, user_id) DO NOTHING
                    RETURNING user_id
                    """,
                    char_id,
                    user_id,
                )
                if first_claim is None:
                    # The character-row lock should make this impossible for
                    # normal code paths. Raising rolls the Harem/catch updates
                    # back rather than granting an untracked duplicate.
                    raise RuntimeError("OverLord claim ledger changed concurrently")

            # Update group+user aggregates
            await conn.execute(
                """
                INSERT INTO user_group_stats (user_id, chat_id, claims, rarity_score)
                VALUES ($1, $2, 1, $3)
                ON CONFLICT (user_id, chat_id) DO UPDATE SET
                  claims = user_group_stats.claims + 1,
                  rarity_score = user_group_stats.rarity_score + EXCLUDED.rarity_score
                """,
                user_id, chat_id, char["rarity_score"],
            )

            current = await conn.fetchval(
                "SELECT highest_rarity FROM users WHERE user_id = $1", user_id
            )
            new_highest = _higher_rarity(current, char["rarity"])

            await conn.execute(
                """
                UPDATE users SET
                  total_claims   = total_claims + 1,
                  highest_rarity = $2
                WHERE user_id = $1
                """,
                user_id, new_highest,
            )
            await conn.execute(
                "UPDATE group_stats SET total_claims = total_claims + 1 WHERE chat_id = $1",
                chat_id,
            )

            # Remove active spawn
            await conn.execute("DELETE FROM active_spawns WHERE chat_id = $1", chat_id)
            return ClaimResult(
                success=True,
                character_id=char_id,
                name=char["name"],
                anime=char["anime"],
                rarity=char["rarity"],
                rarity_score=char["rarity_score"],
                catch_limit_count=catch_limit_count,
                catch_limit=CATCH_LIMIT,
                catch_limit_reset_at=catch_reset_at,
            )


async def get_user_harem(user_id: int, limit: int = 10, offset: int = 0) -> list[dict]:
    """Return a global Harem page, including current art for owned Love levels."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                c.id, c.name, c.anime, c.rarity, c.rarity_score,
                COALESCE(level_media.image_url, c.image_url) AS image_url,
                COALESCE(level_media.media_type, c.media_type, 'photo') AS media_type,
                COALESCE(c.event, '') AS event,
                love_card.love_code AS love_code_number,
                love_state.level AS love_level,
                love_state.level_points AS love_level_points,
                COALESCE(love_state.is_active, FALSE) AS love_is_active,
                COUNT(*) AS qty,
                bool_or(h.is_favorite) AS is_favorite,
                MIN(h.acquired_at) AS acquired_at
            FROM harem AS h
            JOIN characters AS c ON c.id = h.character_id
            LEFT JOIN love_cards AS love_card ON love_card.character_id = c.id
            LEFT JOIN love_player_states AS love_state
              ON love_state.character_id = c.id
             AND love_state.user_id = h.user_id
             AND love_state.is_escrowed = FALSE
            LEFT JOIN love_card_levels AS level_media
              ON level_media.character_id = c.id
             AND level_media.level = love_state.level
            WHERE h.user_id = $1
            GROUP BY
                c.id, c.name, c.anime, c.rarity, c.rarity_score, c.image_url,
                c.media_type, c.event, love_card.love_code, love_state.level,
                love_state.level_points, love_state.is_active,
                level_media.image_url, level_media.media_type
            ORDER BY c.anime ASC, c.rarity_score DESC, c.id ASC
            LIMIT $2 OFFSET $3
            """,
            user_id, limit, offset,
        )
    output = [dict(row) for row in rows]
    for item in output:
        if item.get("love_code_number") is not None:
            item["love_code"] = f"L{int(item['love_code_number'])}"
            level = int(item.get("love_level") or 1)
            item["love_next_target"] = int(LOVE_LEVEL_TARGETS.get(level, 0))
    return output

async def count_user_harem(user_id: int) -> int:
    """Total copies owned (duplicates counted), across every group."""
    pool = get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT COUNT(*) FROM harem WHERE user_id = $1", user_id,
        )


async def count_user_harem_unique(user_id: int) -> int:
    """Distinct-character count, across every group — used for /harem pagination."""
    pool = get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT COUNT(DISTINCT character_id) FROM harem WHERE user_id = $1",
            user_id,
        )


async def get_user_harem_by_rarity(
    user_id: int,
    rarity: str,
    limit: int = 10,
    offset: int = 0,
) -> list[dict]:
    """Return one global rarity page, preserving the current Love-level media."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                c.id, c.name, c.anime, c.rarity, c.rarity_score,
                COALESCE(level_media.image_url, c.image_url) AS image_url,
                COALESCE(level_media.media_type, c.media_type, 'photo') AS media_type,
                COALESCE(c.event, '') AS event,
                love_card.love_code AS love_code_number,
                love_state.level AS love_level,
                love_state.level_points AS love_level_points,
                COALESCE(love_state.is_active, FALSE) AS love_is_active,
                COUNT(*) AS qty,
                bool_or(h.is_favorite) AS is_favorite,
                MIN(h.acquired_at) AS acquired_at
            FROM harem AS h
            JOIN characters AS c ON c.id = h.character_id
            LEFT JOIN love_cards AS love_card ON love_card.character_id = c.id
            LEFT JOIN love_player_states AS love_state
              ON love_state.character_id = c.id
             AND love_state.user_id = h.user_id
             AND love_state.is_escrowed = FALSE
            LEFT JOIN love_card_levels AS level_media
              ON level_media.character_id = c.id
             AND level_media.level = love_state.level
            WHERE h.user_id = $1 AND c.rarity = $2
            GROUP BY
                c.id, c.name, c.anime, c.rarity, c.rarity_score, c.image_url,
                c.media_type, c.event, love_card.love_code, love_state.level,
                love_state.level_points, love_state.is_active,
                level_media.image_url, level_media.media_type
            ORDER BY c.anime ASC, c.rarity_score DESC, c.id ASC
            LIMIT $3 OFFSET $4
            """,
            user_id,
            rarity,
            limit,
            offset,
        )
    output = [dict(row) for row in rows]
    for item in output:
        if item.get("love_code_number") is not None:
            item["love_code"] = f"L{int(item['love_code_number'])}"
            level = int(item.get("love_level") or 1)
            item["love_next_target"] = int(LOVE_LEVEL_TARGETS.get(level, 0))
    return output

async def count_user_harem_unique_by_rarity(user_id: int, rarity: str) -> int:
    """Count distinct globally owned art in a rarity tier for /henmode pages."""
    pool = get_pool()
    async with pool.acquire() as conn:
        return int(
            await conn.fetchval(
                """
                SELECT COUNT(DISTINCT h.character_id)
                FROM harem h
                JOIN characters c ON c.id = h.character_id
                WHERE h.user_id = $1 AND c.rarity = $2
                """,
                user_id,
                rarity,
            )
            or 0
        )


VALID_HAREM_DISPLAY_RARITIES = {
    "Common", "Rare", "Legendary", "Mythical", "Sovereign", "OP", "Eternal",
    SUSPICIOUS_RARITY, OVERLORD_RARITY, LOVE_RARITY,
}


async def get_harem_display_preference(user_id: int) -> tuple[str, str | None]:
    """Return a user's persistent /harem layout; Anime remains the fallback."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT display_mode, rarity
            FROM harem_display_preferences
            WHERE user_id = $1
            """,
            user_id,
        )
    if not row or row["display_mode"] != "rarity" or row["rarity"] not in VALID_HAREM_DISPLAY_RARITIES:
        return "anime", None
    return "rarity", str(row["rarity"])


async def set_harem_display_anime(user_id: int) -> None:
    """Persist Anime grouping as the user's normal /harem view."""
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO harem_display_preferences (user_id, display_mode, rarity, updated_at)
            VALUES ($1, 'anime', NULL, NOW())
            ON CONFLICT (user_id) DO UPDATE SET
              display_mode = 'anime',
              rarity = NULL,
              updated_at = NOW()
            """,
            user_id,
        )


async def set_harem_display_rarity(user_id: int, rarity: str) -> bool:
    """Persist one rarity-tier filter for the user's future /harem pages."""
    if rarity not in VALID_HAREM_DISPLAY_RARITIES:
        return False
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO harem_display_preferences (user_id, display_mode, rarity, updated_at)
            VALUES ($1, 'rarity', $2, NOW())
            ON CONFLICT (user_id) DO UPDATE SET
              display_mode = 'rarity',
              rarity = EXCLUDED.rarity,
              updated_at = NOW()
            """,
            user_id,
            rarity,
        )
    return True


async def user_owns_character(user_id: int, character_id: int) -> bool:
    """Check global ownership without changing the current favorite."""
    pool = get_pool()
    async with pool.acquire() as conn:
        return bool(
            await conn.fetchval(
                """
                SELECT 1 FROM harem
                WHERE user_id = $1 AND character_id = $2
                LIMIT 1
                """,
                user_id,
                character_id,
            )
        )


async def get_favorite_owned_media(
    user_id: int,
    *,
    include_love: bool = True,
) -> dict | None:
    """Return a favorite's catalog media, optionally excluding Love from profile art."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                COALESCE(level_media.image_url, c.image_url) AS image_url,
                COALESCE(level_media.media_type, c.media_type, 'photo') AS media_type
            FROM harem AS h
            JOIN characters AS c ON c.id = h.character_id
            LEFT JOIN love_player_states AS love_state
              ON love_state.user_id = h.user_id
             AND love_state.character_id = c.id
             AND love_state.is_escrowed = FALSE
            LEFT JOIN love_card_levels AS level_media
              ON level_media.character_id = c.id
             AND level_media.level = love_state.level
            WHERE h.user_id = $1 AND h.is_favorite = TRUE
              AND ($2::BOOLEAN OR LOWER(BTRIM(COALESCE(c.rarity, ''))) <> 'love')
            ORDER BY h.acquired_at DESC, h.id DESC
            LIMIT 1
            """,
            user_id,
            include_love,
        )
    return dict(row) if row else None


async def get_random_owned_media(
    user_id: int,
    *,
    include_love: bool = True,
) -> dict | None:
    """Return one owned medium, optionally excluding Love from profile art."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                COALESCE(level_media.image_url, c.image_url) AS image_url,
                COALESCE(level_media.media_type, c.media_type, 'photo') AS media_type
            FROM harem AS h
            JOIN characters AS c ON c.id = h.character_id
            LEFT JOIN love_player_states AS love_state
              ON love_state.user_id = h.user_id
             AND love_state.character_id = c.id
             AND love_state.is_escrowed = FALSE
            LEFT JOIN love_card_levels AS level_media
              ON level_media.character_id = c.id
             AND level_media.level = love_state.level
            WHERE h.user_id = $1
              AND ($2::BOOLEAN OR LOWER(BTRIM(COALESCE(c.rarity, ''))) <> 'love')
            ORDER BY random()
            LIMIT 1
            """,
            user_id,
            include_love,
        )
    return dict(row) if row else None


async def get_favorite_owned_image(user_id: int) -> str | None:
    """Legacy profile-style image accessor; Love never supplies profile art."""
    media = await get_favorite_owned_media(user_id, include_love=False)
    return str(media["image_url"] or "") if media is not None else None


async def get_random_owned_image(user_id: int) -> str | None:
    """Legacy profile-style image accessor; Love never supplies profile art."""
    media = await get_random_owned_media(user_id, include_love=False)
    return str(media["image_url"] or "") if media is not None else None

async def get_anime_totals(anime_names: list[str]) -> dict[str, int]:
    """Total distinct characters that exist in the DB for each anime."""
    if not anime_names:
        return {}
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT anime, COUNT(*) AS total FROM characters "
            "WHERE anime = ANY($1) GROUP BY anime",
            anime_names,
        )
        return {r["anime"]: r["total"] for r in rows}


async def get_user_anime_counts(user_id: int, anime_names: list[str]) -> dict[str, int]:
    """How many distinct characters this user owns (globally) per anime."""
    if not anime_names:
        return {}
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT c.anime, COUNT(DISTINCT c.id) AS owned
            FROM harem h
            JOIN characters c ON c.id = h.character_id
            WHERE h.user_id = $1 AND c.anime = ANY($2)
            GROUP BY c.anime
            """,
            user_id, anime_names,
        )
        return {r["anime"]: r["owned"] for r in rows}


async def get_user_display_name(user_id: int) -> str:
    pool = get_pool()
    async with pool.acquire() as conn:
        name = await conn.fetchval(
            "SELECT full_name FROM users WHERE user_id = $1", user_id
        )
        return name or f"User {user_id}"


async def get_character_by_id(character_id: int) -> dict | None:
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, name, anime, rarity, rarity_score, image_url, "
            "COALESCE(media_type, 'photo') AS media_type, "
            "COALESCE(event, '') AS event FROM characters WHERE id = $1",
            character_id,
        )
        return dict(row) if row else None


async def get_characters_by_ids(character_ids: list[int]) -> dict[int, dict]:
    """Fetch catalog cards in one query, keyed by their numeric IDs."""
    unique_ids = list(dict.fromkeys(character_ids))
    if not unique_ids:
        return {}
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, name, anime, rarity, rarity_score, image_url,
                   COALESCE(media_type, 'photo') AS media_type,
                   COALESCE(event, '') AS event
            FROM characters
            WHERE id = ANY($1::BIGINT[])
            """,
            unique_ids,
        )
    return {row["id"]: dict(row) for row in rows}


async def get_all_catalog_characters() -> list[dict]:
    """Return the current catalog in ID order for an owner /sudo all request."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, name, anime, rarity, rarity_score, image_url, COALESCE(media_type, 'photo') AS media_type, COALESCE(event, '') AS event
            FROM characters
            ORDER BY id ASC
            """
        )
    return [dict(row) for row in rows]


async def get_catalog_characters_by_rarity(rarity: str) -> list[dict]:
    """Return every exact rarity tier for the confirmation-safe /sudo form."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, name, anime, rarity, rarity_score, image_url, COALESCE(media_type, 'photo') AS media_type, COALESCE(event, '') AS event
            FROM characters
            WHERE LOWER(BTRIM(rarity)) = LOWER(BTRIM($1))
            ORDER BY id ASC
            """,
            rarity,
        )
    return [dict(row) for row in rows]


async def find_catalog_characters_by_name(name: str) -> list[dict]:
    """Find every art by exact character name or exact semicolon-separated alias.

    `/sudo raiden` should resolve the catalog's ``raiden`` alias for every
    Raiden Shogun art just as `/sudo Raiden Shogun` resolves its display name.
    The comparison remains exact after trimming/case-folding; it deliberately
    does not turn `/sudo` into a broad partial-text search.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, name, anime, rarity, rarity_score, image_url, COALESCE(media_type, 'photo') AS media_type, COALESCE(event, '') AS event
            FROM characters
            WHERE LOWER(BTRIM(name)) = LOWER(BTRIM($1))
               OR EXISTS (
                   SELECT 1
                   FROM unnest(string_to_array(COALESCE(aliases, ''), ';'))
                        AS aliases_split(alias_value)
                   WHERE LOWER(BTRIM(aliases_split.alias_value)) = LOWER(BTRIM($1))
               )
            ORDER BY rarity_score DESC, id ASC
            """,
            name,
        )
    return [dict(row) for row in rows]


async def find_catalog_characters_by_anime(anime: str) -> list[dict]:
    """Find every catalog art from an exact anime/game title, case-insensitively."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, name, anime, rarity, rarity_score, image_url, COALESCE(media_type, 'photo') AS media_type, COALESCE(event, '') AS event
            FROM characters
            WHERE LOWER(BTRIM(anime)) = LOWER(BTRIM($1))
            ORDER BY rarity_score DESC, id ASC
            """,
            anime,
        )
    return [dict(row) for row in rows]


VALID_SUDO_SELECTION_KINDS = {"ids", "name", "anime", "rarity", "all"}


async def create_sudo_grant(
    *,
    chat_id: int,
    owner_id: int,
    target_user_id: int,
    character_ids: list[int],
    selection_kind: str = "ids",
    is_ranked: bool = True,
) -> tuple[int | None, str]:
    """Persist a confirmation-safe /sudo proposal.

    Explicit ``/sudo 41 41`` selections retain every occurrence so the owner
    can deliberately grant multiple copies. Name, anime, rarity, and
    all-catalog selections keep their unique catalog lists and retain their
    skip-owned behavior on confirmation. ``is_ranked=False`` is used only by
    ``/sudo all -`` to create displayable inventory without score effects.
    """
    if selection_kind not in VALID_SUDO_SELECTION_KINDS:
        return None, "invalid_selection_kind"

    raw_ids = [int(character_id) for character_id in character_ids]
    if not raw_ids:
        return None, "no_characters"

    # Only explicit IDs represent requested quantities. The other selection
    # types are catalog sets, so deduplicate defensively before storing them.
    stored_ids = raw_ids if selection_kind == "ids" else list(dict.fromkeys(raw_ids))
    unique_catalog_ids = list(dict.fromkeys(stored_ids))

    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchval(
                "SELECT COUNT(*) FROM characters WHERE id = ANY($1::BIGINT[])",
                unique_catalog_ids,
            )
            if int(existing) != len(unique_catalog_ids):
                return None, "character_not_found"
            grant = await conn.fetchrow(
                """
                INSERT INTO sudo_grants (
                    chat_id, owner_id, target_user_id, character_ids, selection_kind, is_ranked
                )
                VALUES ($1, $2, $3, $4::BIGINT[], $5, $6)
                RETURNING id
                """,
                chat_id,
                owner_id,
                target_user_id,
                stored_ids,
                selection_kind,
                bool(is_ranked),
            )
            return int(grant["id"]), "ok"


async def accept_sudo_grant(
    grant_id: int,
    owner_id: int,
) -> tuple[bool, str, int | None, list[int], list[int]]:
    """Apply a pending owner grant atomically.

    Explicit numeric selections always grant the exact requested quantities,
    including repetitions and already-owned art.  ``name``, ``anime``, and
    ``all`` selections remain collection-fill operations and skip art already
    owned by the target.  Returns ``(ok, reason, target_id, added_ids,
    requested_ids)``.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Take the same target-user advisory lock used by /deleteharem before
            # locking the grant row. This consistent order avoids a deadlock
            # between a collection purge and a simultaneously accepted grant.
            target_user_id = await conn.fetchval(
                "SELECT target_user_id FROM sudo_grants "
                "WHERE id = $1 AND status = 'pending'",
                grant_id,
            )
            if target_user_id is None:
                return False, "not_found", None, [], []
            target_user_id = int(target_user_id)
            await conn.execute("SELECT pg_advisory_xact_lock($1)", target_user_id)

            grant = await conn.fetchrow(
                "SELECT * FROM sudo_grants WHERE id = $1 AND status = 'pending' FOR UPDATE",
                grant_id,
            )
            if not grant:
                return False, "not_found", None, [], []
            if grant["owner_id"] != owner_id:
                return False, "not_yours", int(grant["target_user_id"]), [], []

            requested_ids = [int(character_id) for character_id in grant["character_ids"]]
            unique_catalog_ids = list(dict.fromkeys(requested_ids))
            selection_kind = str(grant["selection_kind"] or "ids")
            if selection_kind not in VALID_SUDO_SELECTION_KINDS:
                selection_kind = "ids"
            grant_is_ranked = bool(grant["is_ranked"])

            catalog_rows = await conn.fetch(
                """
                SELECT id, rarity, rarity_score
                FROM characters
                WHERE id = ANY($1::BIGINT[])
                """,
                unique_catalog_ids,
            )
            catalog = {int(row["id"]): dict(row) for row in catalog_rows}
            if len(catalog) != len(unique_catalog_ids):
                await conn.execute(
                    "UPDATE sudo_grants SET status = 'failed', resolved_at = NOW() WHERE id = $1",
                    grant_id,
                )
                return False, "catalog_changed", target_user_id, [], requested_ids

            await conn.execute(
                """
                INSERT INTO users (user_id, last_seen_at)
                VALUES ($1, NOW())
                ON CONFLICT (user_id) DO UPDATE SET last_seen_at = NOW()
                """,
                target_user_id,
            )

            if selection_kind == "ids":
                # The sequence itself is the owner's requested quantity for
                # ordinary cards. Love is deliberately de-duplicated per exact
                # character because its persistent state is one card per owner.
                candidate_ids = requested_ids
            else:
                owned_rows = await conn.fetch(
                    """
                    SELECT DISTINCT character_id
                    FROM harem
                    WHERE user_id = $1 AND character_id = ANY($2::BIGINT[])
                    """,
                    target_user_id,
                    unique_catalog_ids,
                )
                owned_ids = {int(row["character_id"]) for row in owned_rows}
                candidate_ids = [
                    character_id
                    for character_id in unique_catalog_ids
                    if character_id not in owned_ids
                ]

            normal_candidate_ids = [
                character_id
                for character_id in candidate_ids
                if str(catalog[character_id]["rarity"] or "").casefold() != LOVE_RARITY.casefold()
            ]
            raw_love_candidate_ids = [
                character_id
                for character_id in candidate_ids
                if str(catalog[character_id]["rarity"] or "").casefold() == LOVE_RARITY.casefold()
            ]
            # Owner /sudo may give several *different* Love cards, but not two
            # independent states for the same L-code. Selection order stays
            # stable so the final listed Love becomes the active one.
            love_candidate_ids = list(dict.fromkeys(raw_love_candidate_ids))
            existing_love_rows = await conn.fetch(
                """
                SELECT character_id
                FROM love_player_states
                WHERE user_id = $1 AND character_id = ANY($2::BIGINT[])
                FOR UPDATE
                """,
                target_user_id,
                love_candidate_ids,
            ) if love_candidate_ids else []
            existing_love_ids = {int(row["character_id"]) for row in existing_love_rows}
            love_added_ids = [
                character_id for character_id in love_candidate_ids
                if character_id not in existing_love_ids
            ]
            # A pre-Love legacy Harem copy should also not receive a duplicate
            # state/card through a broad owner selection.
            if love_added_ids:
                legacy_love_harem_rows = await conn.fetch(
                    """
                    SELECT DISTINCT character_id
                    FROM harem
                    WHERE user_id = $1 AND character_id = ANY($2::BIGINT[])
                    """,
                    target_user_id,
                    love_added_ids,
                )
                legacy_love_harem_ids = {int(row["character_id"]) for row in legacy_love_harem_rows}
                love_added_ids = [
                    character_id for character_id in love_added_ids
                    if character_id not in legacy_love_harem_ids
                ]

            if normal_candidate_ids:
                await conn.executemany(
                    """
                    INSERT INTO harem (user_id, chat_id, character_id, is_ranked)
                    VALUES ($1, $2, $3, $4)
                    """,
                    [
                        (target_user_id, int(grant["chat_id"]), character_id, grant_is_ranked)
                        for character_id in normal_candidate_ids
                    ],
                )
            if love_added_ids:
                await conn.executemany(
                    """
                    INSERT INTO harem (user_id, chat_id, character_id, is_ranked)
                    VALUES ($1, $2, $3, FALSE)
                    """,
                    [
                        (target_user_id, int(grant["chat_id"]), character_id)
                        for character_id in love_added_ids
                    ],
                )
                confirmed_love_ids: list[int] = []
                for character_id in love_added_ids:
                    granted, _reason = await grant_love_state_in_transaction(
                        conn,
                        user_id=target_user_id,
                        character_id=character_id,
                    )
                    if granted:
                        confirmed_love_ids.append(character_id)
                    else:
                        # This is guarded by the target user lock. If a legacy
                        # inconsistency still appears, fail atomically rather
                        # than leaving a Harem copy without Love progression.
                        raise RuntimeError("Love sudo state could not be created")
                love_added_ids = confirmed_love_ids

            added_ids = [*normal_candidate_ids, *love_added_ids]
            ranked_added_ids = normal_candidate_ids if grant_is_ranked else []
            if ranked_added_ids:
                # Love never reaches this branch: no score, profile, group
                # leaderboard, or normal collection count changes are allowed.
                score_total = sum(
                    int(catalog[character_id]["rarity_score"])
                    for character_id in ranked_added_ids
                )
                await conn.execute(
                    """
                    INSERT INTO user_group_stats (user_id, chat_id, claims, rarity_score)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (user_id, chat_id) DO UPDATE SET
                      claims = user_group_stats.claims + EXCLUDED.claims,
                      rarity_score = user_group_stats.rarity_score + EXCLUDED.rarity_score
                    """,
                    target_user_id,
                    int(grant["chat_id"]),
                    len(ranked_added_ids),
                    score_total,
                )
                await conn.execute(
                    """
                    INSERT INTO group_stats (chat_id, total_claims)
                    VALUES ($1, $2)
                    ON CONFLICT (chat_id) DO UPDATE SET
                      total_claims = group_stats.total_claims + EXCLUDED.total_claims
                    """,
                    int(grant["chat_id"]),
                    len(ranked_added_ids),
                )

                current_highest = await conn.fetchval(
                    "SELECT highest_rarity FROM users WHERE user_id = $1",
                    target_user_id,
                )
                new_highest = current_highest
                for character_id in ranked_added_ids:
                    new_highest = _higher_rarity(new_highest, catalog[character_id]["rarity"])
                await conn.execute(
                    """
                    UPDATE users
                    SET total_claims = total_claims + $1,
                        highest_rarity = $2
                    WHERE user_id = $3
                    """,
                    len(ranked_added_ids),
                    new_highest,
                    target_user_id,
                )

            await conn.execute(
                "UPDATE sudo_grants SET status = 'accepted', resolved_at = NOW() WHERE id = $1",
                grant_id,
            )
            return (
                True,
                ("ok" if grant_is_ranked else "ok_unranked")
                if added_ids else "already_owned",
                target_user_id,
                added_ids,
                requested_ids,
            )


async def cancel_sudo_grant(grant_id: int, owner_id: int) -> tuple[bool, str]:
    """Cancel an unconfirmed /sudo proposal; only its issuing owner may do so."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE sudo_grants
            SET status = 'cancelled', resolved_at = NOW()
            WHERE id = $1 AND status = 'pending' AND owner_id = $2
            RETURNING id
            """,
            grant_id,
            owner_id,
        )
    return (True, "ok") if row else (False, "not_found_or_not_yours")


async def get_character_global_catch_count(character_id: int) -> int:
    """Total number of times this character has been caught, anywhere,
    including duplicate catches by the same user.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM harem
            WHERE character_id = $1 AND is_ranked = TRUE
            """,
            character_id
        )


async def get_character_top_catchers(character_id: int, limit: int = 10) -> list[dict]:
    """Top catchers of a single character, ranked by copies owned."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT h.user_id, COUNT(*) AS qty, u.username, u.full_name
            FROM harem h
            LEFT JOIN users u ON u.user_id = h.user_id
            WHERE h.character_id = $1 AND h.is_ranked = TRUE
            GROUP BY h.user_id, u.username, u.full_name
            ORDER BY qty DESC, h.user_id ASC
            LIMIT $2
            """,
            character_id, limit,
        )
        return [dict(r) for r in rows]


async def set_event_spawn_enabled(event_label: str, *, enabled: bool) -> dict | None:
    """Persist a full-owner pause/restore state for one reusable event.

    Stopping an event does not delete its label or cards. It also removes every
    not-yet-posted automatic OP row for that event, so a card announced before
    the command cannot appear after the event has been paused. The event row is
    locked first to serialize this action with event deletion and spawn checks.
    """
    label = event_label.strip()
    if not label:
        return None

    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            event = await conn.fetchrow(
                """
                SELECT id, label, spawn_enabled
                FROM catalog_events
                WHERE LOWER(BTRIM(label)) = LOWER(BTRIM($1))
                FOR UPDATE
                """,
                label,
            )
            if not event:
                return None

            was_spawn_enabled = bool(event["spawn_enabled"])
            if was_spawn_enabled != bool(enabled):
                await conn.execute(
                    "UPDATE catalog_events SET spawn_enabled = $2 WHERE id = $1",
                    event["id"],
                    bool(enabled),
                )

            cancelled_scheduled_op_count = 0
            if not enabled:
                cancelled = await conn.fetch(
                    """
                    DELETE FROM scheduled_op_spawns AS scheduled
                    USING characters AS character
                    WHERE scheduled.character_id = character.id
                      AND LOWER(BTRIM(COALESCE(character.event, ''))) = LOWER(BTRIM($1))
                    RETURNING scheduled.chat_id
                    """,
                    event["label"],
                )
                cancelled_scheduled_op_count = len(cancelled)

    return {
        "label": str(event["label"]),
        "spawn_enabled": bool(enabled),
        "was_spawn_enabled": was_spawn_enabled,
        "cancelled_scheduled_op_count": cancelled_scheduled_op_count,
    }


async def set_rarity_spawn_enabled(rarity_label: str, *, enabled: bool) -> dict | None:
    """Persist a full-owner global pause/restore for one catalog rarity tier.

    This is intentionally independent from named-event controls. The lowercased
    tier key is durable, so cards added later with the same rarity immediately
    follow the saved state. A transaction-scoped advisory lock serializes a
    tier update with final spawn emission checks, including queued automatic OP
    cards that were selected just before an owner pauses their rarity.
    """
    requested_label = " ".join((rarity_label or "").split())
    if not requested_label:
        return None

    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            canonical = await conn.fetchrow(
                """
                SELECT BTRIM(rarity) AS label,
                       LOWER(BTRIM(rarity)) AS rarity_key
                FROM characters
                WHERE LOWER(BTRIM(rarity)) = LOWER(BTRIM($1))
                ORDER BY id ASC
                LIMIT 1
                """,
                requested_label,
            )
            if not canonical:
                return None

            label = str(canonical["label"] or "").strip()
            if not label:
                return None
            # Love is intentionally catalog/Lovelist-only, so a spawn toggle
            # would imply behavior that no spawn path can ever provide.
            if label.casefold() == "love":
                return {"reason": "love_not_spawnable", "label": label}

            # Keep the stored key exactly aligned with PostgreSQL's LOWER()
            # expression used by cache joins and final spawn checks.
            rarity_key = str(canonical["rarity_key"] or "").strip()
            if not rarity_key:
                return None
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"rarity-spawn:{rarity_key}",
            )
            previous = await conn.fetchrow(
                """
                SELECT spawn_enabled
                FROM catalog_rarity_spawn_controls
                WHERE rarity_key = $1
                FOR UPDATE
                """,
                rarity_key,
            )
            was_spawn_enabled = bool(previous["spawn_enabled"]) if previous else True
            await conn.execute(
                """
                INSERT INTO catalog_rarity_spawn_controls (
                    rarity_key, label, spawn_enabled, updated_at
                )
                VALUES ($1, $2, $3, NOW())
                ON CONFLICT (rarity_key) DO UPDATE SET
                  label = EXCLUDED.label,
                  spawn_enabled = EXCLUDED.spawn_enabled,
                  updated_at = NOW()
                """,
                rarity_key,
                label,
                bool(enabled),
            )

            cancelled_scheduled_op_count = 0
            if not enabled:
                cancelled = await conn.fetch(
                    """
                    DELETE FROM scheduled_op_spawns AS scheduled
                    USING characters AS character
                    WHERE scheduled.character_id = character.id
                      AND LOWER(BTRIM(character.rarity)) = $1
                    RETURNING scheduled.chat_id
                    """,
                    rarity_key,
                )
                cancelled_scheduled_op_count = len(cancelled)

    return {
        "label": label,
        "rarity_key": rarity_key,
        "spawn_enabled": bool(enabled),
        "was_spawn_enabled": was_spawn_enabled,
        "cancelled_scheduled_op_count": cancelled_scheduled_op_count,
    }


async def get_event_character_codes(event_label: str) -> dict | None:
    """Return one curated event label and its currently linked card IDs.

    The lookup is case-insensitive and uses the reusable event table as the
    authority.  It intentionally returns an existing event with an empty code
    list so /ownercheck can distinguish "no cards assigned yet" from "no such
    event". Card IDs are ordered for an owner-friendly copy/paste response.
    """
    label = event_label.strip()
    if not label:
        return None

    pool = get_pool()
    async with pool.acquire() as conn:
        event = await conn.fetchrow(
            """
            SELECT id, label
            FROM catalog_events
            WHERE LOWER(BTRIM(label)) = LOWER(BTRIM($1))
            """,
            label,
        )
        if not event:
            return None
        rows = await conn.fetch(
            """
            SELECT id
            FROM characters
            WHERE LOWER(BTRIM(COALESCE(event, ''))) = LOWER(BTRIM($1))
            ORDER BY id ASC
            """,
            event["label"],
        )

    return {
        "label": str(event["label"]),
        "character_ids": [int(row["id"]) for row in rows],
    }


async def list_catalog_events_for_search() -> list[dict]:
    """Return every curated event with its current card count for /eventsearch."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT e.id, e.label, COUNT(c.id) AS character_count
            FROM catalog_events AS e
            LEFT JOIN characters AS c
              ON LOWER(BTRIM(COALESCE(c.event, ''))) = LOWER(BTRIM(e.label))
            GROUP BY e.id, e.label
            ORDER BY LOWER(BTRIM(e.label)) ASC, e.id ASC
            """
        )
    return [
        {
            "id": int(row["id"]),
            "label": str(row["label"]),
            "character_count": int(row["character_count"] or 0),
        }
        for row in rows
    ]


async def get_catalog_event_for_search(event_id: int) -> dict | None:
    """Resolve one event-selection callback without trusting its display text."""
    try:
        normalized_id = int(event_id)
    except (TypeError, ValueError):
        return None
    if not 0 < normalized_id <= MAX_CATALOG_EVENT_ID:
        return None

    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT e.id, e.label, COUNT(c.id) AS character_count
            FROM catalog_events AS e
            LEFT JOIN characters AS c
              ON LOWER(BTRIM(COALESCE(c.event, ''))) = LOWER(BTRIM(e.label))
            WHERE e.id = $1
            GROUP BY e.id, e.label
            """,
            normalized_id,
        )
    if not row:
        return None
    return {
        "id": int(row["id"]),
        "label": str(row["label"]),
        "character_count": int(row["character_count"] or 0),
    }


async def get_character_catch_report(character_id: int) -> dict | None:
    """Return a complete global catcher report for one catalog character.

    This is intentionally a single owner-facing data source for /ownercheck:
    the totals and every ranked catcher are read from the bot's live harem
    rows, never from a cached or per-group aggregate.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        summary = await conn.fetchrow(
            """
            SELECT
                c.id, c.name, c.anime, c.rarity, c.rarity_score,
                COUNT(h.id) AS total_catches,
                COUNT(DISTINCT h.user_id) AS total_unique_catchers
            FROM characters AS c
            LEFT JOIN harem AS h
              ON h.character_id = c.id AND h.is_ranked = TRUE
            WHERE c.id = $1
            GROUP BY c.id, c.name, c.anime, c.rarity, c.rarity_score
            """,
            character_id,
        )
        if not summary:
            return None

        catchers = await conn.fetch(
            """
            SELECT
                h.user_id,
                COALESCE(
                    NULLIF(BTRIM(u.full_name), ''),
                    NULLIF(BTRIM(u.username), ''),
                    'Unknown User'
                ) AS display_name,
                COUNT(*) AS catch_count
            FROM harem AS h
            LEFT JOIN users AS u ON u.user_id = h.user_id
            WHERE h.character_id = $1 AND h.is_ranked = TRUE
            GROUP BY h.user_id, u.full_name, u.username
            ORDER BY catch_count DESC, h.user_id ASC
            """,
            character_id,
        )

    report = dict(summary)
    report["total_catches"] = int(report["total_catches"] or 0)
    report["total_unique_catchers"] = int(report["total_unique_catchers"] or 0)
    report["catchers"] = [
        {
            "user_id": int(row["user_id"]),
            "display_name": str(row["display_name"] or "Unknown User"),
            "catch_count": int(row["catch_count"] or 0),
        }
        for row in catchers
    ]
    return report


async def search_characters(
    query: str,
    limit: int = 50,
    rarity: str | None = None,
    offset: int = 0,
    event_id: int | None = None,
) -> list[dict]:
    """Return one stable, offsettable catalog-search page for inline mode.

    Telegram accepts at most 50 inline results in one response. The inline
    handler asks for one extra row and uses ``offset``/``next_offset`` to let a
    player scroll through the complete catalog rather than seeing only an
    arbitrary first batch. Blank browse pages must therefore be deterministic
    (ID order), not ``ORDER BY random()``, otherwise later pages can repeat or
    hide cards. ``event_id`` is an optional server-side curated-event filter
    used by the /eventsearch panel; it never trusts a label from callback data.
    """
    if rarity is not None and rarity not in RARITY_ORDER:
        return []
    try:
        page_limit = max(1, int(limit))
        page_offset = max(0, int(offset))
        normalized_event_id = int(event_id) if event_id is not None else None
    except (TypeError, ValueError):
        return []
    if (
        normalized_event_id is not None
        and not 0 < normalized_event_id <= MAX_CATALOG_EVENT_ID
    ):
        return []

    pool = get_pool()
    async with pool.acquire() as conn:
        q = query.strip()
        if normalized_event_id is not None and not q and rarity is None:
            rows = await conn.fetch(
                """
                SELECT c.id, c.name, c.anime, c.rarity, c.rarity_score,
                       c.image_url, COALESCE(c.media_type, 'photo') AS media_type, COALESCE(c.event, '') AS event
                FROM characters AS c
                JOIN catalog_events AS event_filter
                  ON LOWER(BTRIM(COALESCE(c.event, ''))) = LOWER(BTRIM(event_filter.label))
                WHERE c.name != '' AND event_filter.id = $1
                ORDER BY c.id ASC
                LIMIT $2 OFFSET $3
                """,
                normalized_event_id,
                page_limit,
                page_offset,
            )
        elif normalized_event_id is not None and not q:
            rows = await conn.fetch(
                """
                SELECT c.id, c.name, c.anime, c.rarity, c.rarity_score,
                       c.image_url, COALESCE(c.media_type, 'photo') AS media_type, COALESCE(c.event, '') AS event
                FROM characters AS c
                JOIN catalog_events AS event_filter
                  ON LOWER(BTRIM(COALESCE(c.event, ''))) = LOWER(BTRIM(event_filter.label))
                WHERE c.name != '' AND event_filter.id = $1 AND c.rarity = $2
                ORDER BY c.id ASC
                LIMIT $3 OFFSET $4
                """,
                normalized_event_id,
                rarity,
                page_limit,
                page_offset,
            )
        elif normalized_event_id is not None and rarity is None:
            rows = await conn.fetch(
                """
                SELECT c.id, c.name, c.anime, c.rarity, c.rarity_score,
                       c.image_url, COALESCE(c.media_type, 'photo') AS media_type, COALESCE(c.event, '') AS event
                FROM characters AS c
                JOIN catalog_events AS event_filter
                  ON LOWER(BTRIM(COALESCE(c.event, ''))) = LOWER(BTRIM(event_filter.label))
                WHERE c.name != '' AND event_filter.id = $1
                  AND (c.name ILIKE '%' || $2 || '%'
                       OR c.anime ILIKE '%' || $2 || '%'
                       OR c.aliases ILIKE '%' || $2 || '%')
                ORDER BY c.rarity_score DESC, c.name ASC, c.id ASC
                LIMIT $3 OFFSET $4
                """,
                normalized_event_id,
                q,
                page_limit,
                page_offset,
            )
        elif normalized_event_id is not None:
            rows = await conn.fetch(
                """
                SELECT c.id, c.name, c.anime, c.rarity, c.rarity_score,
                       c.image_url, COALESCE(c.media_type, 'photo') AS media_type, COALESCE(c.event, '') AS event
                FROM characters AS c
                JOIN catalog_events AS event_filter
                  ON LOWER(BTRIM(COALESCE(c.event, ''))) = LOWER(BTRIM(event_filter.label))
                WHERE c.name != '' AND event_filter.id = $1 AND c.rarity = $2
                  AND (c.name ILIKE '%' || $3 || '%'
                       OR c.anime ILIKE '%' || $3 || '%'
                       OR c.aliases ILIKE '%' || $3 || '%')
                ORDER BY c.rarity_score DESC, c.name ASC, c.id ASC
                LIMIT $4 OFFSET $5
                """,
                normalized_event_id,
                rarity,
                q,
                page_limit,
                page_offset,
            )
        elif not q and rarity is None:
            rows = await conn.fetch(
                """
                SELECT id, name, anime, rarity, rarity_score, image_url, COALESCE(media_type, 'photo') AS media_type, COALESCE(event, '') AS event
                FROM characters
                WHERE name != ''
                ORDER BY id ASC
                LIMIT $1 OFFSET $2
                """,
                page_limit,
                page_offset,
            )
        elif not q:
            rows = await conn.fetch(
                """
                SELECT id, name, anime, rarity, rarity_score, image_url, COALESCE(media_type, 'photo') AS media_type, COALESCE(event, '') AS event
                FROM characters
                WHERE name != '' AND rarity = $1
                ORDER BY id ASC
                LIMIT $2 OFFSET $3
                """,
                rarity,
                page_limit,
                page_offset,
            )
        elif rarity is None:
            rows = await conn.fetch(
                """
                SELECT id, name, anime, rarity, rarity_score, image_url, COALESCE(media_type, 'photo') AS media_type, COALESCE(event, '') AS event
                FROM characters
                WHERE name != ''
                  AND (name ILIKE '%' || $1 || '%'
                       OR anime ILIKE '%' || $1 || '%'
                       OR aliases ILIKE '%' || $1 || '%')
                ORDER BY rarity_score DESC, name ASC, id ASC
                LIMIT $2 OFFSET $3
                """,
                q,
                page_limit,
                page_offset,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT id, name, anime, rarity, rarity_score, image_url, COALESCE(media_type, 'photo') AS media_type, COALESCE(event, '') AS event
                FROM characters
                WHERE name != '' AND rarity = $1
                  AND (name ILIKE '%' || $2 || '%'
                       OR anime ILIKE '%' || $2 || '%'
                       OR aliases ILIKE '%' || $2 || '%')
                ORDER BY rarity_score DESC, name ASC, id ASC
                LIMIT $3 OFFSET $4
                """,
                rarity,
                q,
                page_limit,
                page_offset,
            )
    return [dict(row) for row in rows]


async def set_favorite(user_id: int, character_id: int) -> bool:
    """Set exactly one global favorite for a user's full inventory.

    Ownership and the showcase are global, not scoped to the group where the
    command was sent.  The advisory lock serializes rapid double-clicks or
    commands from multiple chats for the same user.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1)", user_id)
            favorite_row = await conn.fetchrow(
                """
                SELECT id
                FROM harem
                WHERE user_id = $1 AND character_id = $2
                ORDER BY acquired_at ASC, id ASC
                LIMIT 1
                FOR UPDATE
                """,
                user_id,
                character_id,
            )
            if not favorite_row:
                return False

            await conn.execute(
                "UPDATE harem SET is_favorite = FALSE "
                "WHERE user_id = $1 AND is_favorite = TRUE",
                user_id,
            )
            await conn.execute(
                "UPDATE harem SET is_favorite = TRUE WHERE id = $1",
                favorite_row["id"],
            )
            return True


async def get_harem_copy_for_deletion(
    user_id: int,
    character_id: int,
) -> dict | None:
    """Find the one copy an owner would remove, preferring non-favorites.

    The collection is global.  Selecting a non-favorite, oldest copy makes a
    one-copy removal predictable and preserves the user's showcase whenever
    another copy of the requested character exists.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                h.id AS harem_id,
                h.user_id,
                h.chat_id,
                h.character_id,
                h.is_favorite,
                h.is_ranked,
                c.name,
                c.anime,
                c.rarity,
                c.rarity_score,
                c.aliases,
                c.image_url,
                COALESCE(c.media_type, 'photo') AS media_type,
                (
                    SELECT COUNT(*)
                    FROM harem AS copies
                    WHERE copies.user_id = h.user_id
                      AND copies.character_id = h.character_id
                ) AS copy_count
            FROM harem AS h
            JOIN characters AS c ON c.id = h.character_id
            WHERE h.user_id = $1 AND h.character_id = $2
            ORDER BY h.is_favorite ASC, h.acquired_at ASC, h.id ASC
            LIMIT 1
            """,
            user_id,
            character_id,
        )
    return dict(row) if row else None


async def delete_harem_copy(harem_id: int) -> HaremCopyDeletionResult:
    """Delete exactly one previously previewed global harem row atomically.

    The supplied row ID is selected during the confirmation preview.  It is
    re-read under the same per-user advisory lock used by favorite and full
    harem recovery operations, so a stale confirmation can never remove a
    different copy.  The related current-collection totals are reconciled;
    pending gifts/trades retain their own ownership check at acceptance time.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Resolve the owner before taking the user-scoped advisory lock.
            # This lock ordering avoids deadlocks with /deleteharem, /backharem, and
            # favorite changes, which all lock by user first.
            target_user_id = await conn.fetchval(
                "SELECT user_id FROM harem WHERE id = $1",
                harem_id,
            )
            if target_user_id is None:
                return HaremCopyDeletionResult(success=False, reason="not_found")
            target_user_id = int(target_user_id)
            await conn.execute("SELECT pg_advisory_xact_lock($1)", target_user_id)

            selected = await conn.fetchrow(
                """
                SELECT
                    h.id,
                    h.user_id,
                    h.chat_id,
                    h.character_id,
                    h.is_favorite,
                    h.is_ranked,
                    c.name,
                    c.anime,
                    c.rarity,
                    c.rarity_score
                FROM harem AS h
                JOIN characters AS c ON c.id = h.character_id
                WHERE h.id = $1 AND h.user_id = $2
                FOR UPDATE OF h
                """,
                harem_id,
                target_user_id,
            )
            if not selected:
                return HaremCopyDeletionResult(success=False, reason="not_found")
            if str(selected["rarity"] or "").strip().casefold() == LOVE_RARITY.casefold():
                return HaremCopyDeletionResult(
                    success=False,
                    reason="love_protected",
                    user_id=target_user_id,
                    character_id=int(selected["character_id"]),
                    character_name=str(selected["name"] or ""),
                    anime=str(selected["anime"] or ""),
                    rarity=LOVE_RARITY,
                )

            await conn.execute("DELETE FROM harem WHERE id = $1", harem_id)

            remaining_copies = int(await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM harem
                WHERE user_id = $1 AND character_id = $2
                """,
                target_user_id,
                selected["character_id"],
            ) or 0)
            # An unranked /sudo-all copy is deliberately inventory-only. Its
            # removal must not subtract a normal claim or disturb rankings.
            if bool(selected["is_ranked"]):
                await _recalculate_ranked_user_stats(conn, target_user_id)

                # A ranked harem row carries the group where it was gained.
                # Remove its contribution, then recompute that group's total
                # from per-user statistics so the global/group views align.
                await conn.execute(
                    """
                    UPDATE user_group_stats
                    SET claims = GREATEST(claims - 1, 0),
                        rarity_score = GREATEST(rarity_score - $3, 0)
                    WHERE user_id = $1 AND chat_id = $2
                    """,
                    target_user_id,
                    selected["chat_id"],
                    selected["rarity_score"],
                )
                await conn.execute(
                    """
                    DELETE FROM user_group_stats
                    WHERE user_id = $1 AND chat_id = $2 AND claims <= 0
                    """,
                    target_user_id,
                    selected["chat_id"],
                )
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
                    WHERE g.chat_id = $1
                    """,
                    selected["chat_id"],
                )

            return HaremCopyDeletionResult(
                success=True,
                reason="ok",
                user_id=target_user_id,
                character_id=int(selected["character_id"]),
                character_name=str(selected["name"]),
                anime=str(selected["anime"]),
                rarity=str(selected["rarity"]),
                remaining_copies=remaining_copies,
            )


async def burn_harem_copy(*, user_id: int, harem_id: int) -> BurnHaremCopyResult:
    """Permanently destroy one owned Harem copy and credit its fixed Neko value.

    The operation is bound to the exact Harem row shown in the signed Mini App,
    locks the owner's collection and wallet, and then updates all live ranked
    collection/group aggregates in the same transaction. It intentionally does
    *not* lower ``catch_window_claims``: burning cannot be used to bypass the
    separate strict 25-catch allowance.
    """
    if (
        not isinstance(user_id, int)
        or isinstance(user_id, bool)
        or user_id <= 0
        or not isinstance(harem_id, int)
        or isinstance(harem_id, bool)
        or harem_id <= 0
    ):
        return BurnHaremCopyResult(success=False, reason="invalid_arguments")

    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1)", user_id)
            selected = await conn.fetchrow(
                """
                SELECT
                    h.id AS harem_id,
                    h.user_id,
                    h.chat_id,
                    h.character_id,
                    h.is_ranked,
                    c.name,
                    c.rarity,
                    c.rarity_score
                FROM harem AS h
                JOIN characters AS c ON c.id = h.character_id
                WHERE h.id = $1 AND h.user_id = $2
                FOR UPDATE OF h
                """,
                harem_id,
                user_id,
            )
            if not selected:
                return BurnHaremCopyResult(success=False, reason="harem_copy_not_found")

            rarity = str(selected["rarity"] or "").strip()
            if rarity.casefold() == ETERNAL_RARITY.casefold():
                return BurnHaremCopyResult(
                    success=False,
                    reason="eternal_protected",
                    harem_id=harem_id,
                    character_id=int(selected["character_id"]),
                    character_name=str(selected["name"] or ""),
                    rarity=rarity,
                )
            if is_overlord(rarity):
                return BurnHaremCopyResult(
                    success=False,
                    reason="overlord_protected",
                    harem_id=harem_id,
                    character_id=int(selected["character_id"]),
                    character_name=str(selected["name"] or ""),
                    rarity=rarity,
                )
            if rarity.casefold() == LOVE_RARITY.casefold():
                return BurnHaremCopyResult(
                    success=False,
                    reason="love_protected",
                    harem_id=harem_id,
                    character_id=int(selected["character_id"]),
                    character_name=str(selected["name"] or ""),
                    rarity=rarity,
                )
            reward = BURN_NEKO_REWARDS.get(rarity)
            if reward is None:
                return BurnHaremCopyResult(
                    success=False,
                    reason="rarity_not_burnable",
                    harem_id=harem_id,
                    character_id=int(selected["character_id"]),
                    character_name=str(selected["name"] or ""),
                    rarity=rarity,
                )

            wallet = await conn.fetchrow(
                "SELECT neko_balance FROM users WHERE user_id = $1 FOR UPDATE",
                user_id,
            )
            if not wallet:
                raise RuntimeError("burn owner wallet row was not available")
            balance_before = Decimal(str(wallet["neko_balance"])).quantize(NEKO_QUANTUM)
            if balance_before > MAX_NEKO_BALANCE - reward:
                return BurnHaremCopyResult(
                    success=False,
                    reason="balance_limit",
                    harem_id=harem_id,
                    character_id=int(selected["character_id"]),
                    character_name=str(selected["name"] or ""),
                    rarity=rarity,
                    reward=reward,
                    balance_after=balance_before,
                )

            deleted = await conn.fetchval(
                "DELETE FROM harem WHERE id = $1 AND user_id = $2 RETURNING id",
                harem_id,
                user_id,
            )
            if deleted is None:
                return BurnHaremCopyResult(success=False, reason="harem_copy_not_found")

            if bool(selected["is_ranked"]):
                await _recalculate_ranked_user_stats(conn, user_id)
                # A burn really destroys the collection copy rather than merely
                # transferring it, so its originating group score/count is also
                # removed. Transfers retain group history by design.
                await conn.execute(
                    """
                    UPDATE user_group_stats
                    SET claims = GREATEST(claims - 1, 0),
                        rarity_score = GREATEST(rarity_score - $3, 0)
                    WHERE user_id = $1 AND chat_id = $2
                    """,
                    user_id,
                    int(selected["chat_id"]),
                    int(selected["rarity_score"] or 0),
                )
                await conn.execute(
                    """
                    DELETE FROM user_group_stats
                    WHERE user_id = $1 AND chat_id = $2 AND claims <= 0
                    """,
                    user_id,
                    int(selected["chat_id"]),
                )
                await conn.execute(
                    """
                    UPDATE group_stats AS g
                    SET total_claims = COALESCE(
                        (
                            SELECT SUM(stats.claims)
                            FROM user_group_stats AS stats
                            WHERE stats.chat_id = g.chat_id
                        ),
                        0
                    )
                    WHERE g.chat_id = $1
                    """,
                    int(selected["chat_id"]),
                )

            balance_after = (balance_before + reward).quantize(NEKO_QUANTUM)
            await conn.execute(
                """
                UPDATE users
                SET neko_balance = $2, last_seen_at = NOW()
                WHERE user_id = $1
                """,
                user_id,
                balance_after,
            )
            await _record_wallet_ledger(
                conn,
                user_id=user_id,
                delta=reward,
                balance_after=balance_after,
                reason="harem_burn",
                reference_id=harem_id,
            )
            remaining_copies = int(await conn.fetchval(
                """
                SELECT COUNT(*) FROM harem
                WHERE user_id = $1 AND character_id = $2
                """,
                user_id,
                int(selected["character_id"]),
            ) or 0)
            total_claims = int(await conn.fetchval(
                "SELECT total_claims FROM users WHERE user_id = $1",
                user_id,
            ) or 0)

    return BurnHaremCopyResult(
        success=True,
        reason="ok",
        harem_id=harem_id,
        character_id=int(selected["character_id"]),
        character_name=str(selected["name"] or ""),
        rarity=rarity,
        reward=reward,
        balance_after=balance_after,
        remaining_copies=remaining_copies,
        total_claims=total_claims,
    )


async def delete_user_harem(user_id: int, *, deleted_by: int) -> HaremDeletionResult:
    """Clear ranked/non-Love Harem inventory with an owner recovery snapshot.

    A confirmed ``/deleteharem`` removes ordinary live collection rows and
    their collection statistics immediately. Love is a separate persistent
    relationship state, so its live Harem rows/states deliberately survive and
    are never included in this generic recovery snapshot. Before ordinary
    removal, the exact non-Love rows, relevant group statistics, and global
    profile totals are saved for owner-only ``/backharem``.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # /deleteharem, /backharem, and favorite changes use the same per-user
            # lock.  This serializes a delete/restore pair for one collection.
            await conn.execute("SELECT pg_advisory_xact_lock($1)", user_id)

            user_snapshot = await conn.fetchrow(
                """
                SELECT total_claims, highest_rarity
                FROM users
                WHERE user_id = $1
                FOR UPDATE
                """,
                user_id,
            )
            harem_rows = await conn.fetch(
                """
                SELECT h.id, h.chat_id, h.character_id, h.is_favorite, h.is_ranked, h.acquired_at
                FROM harem AS h
                JOIN characters AS c ON c.id = h.character_id
                WHERE h.user_id = $1
                  AND LOWER(BTRIM(COALESCE(c.rarity, ''))) <> 'love'
                ORDER BY h.id ASC
                FOR UPDATE OF h
                """,
                user_id,
            )
            group_stat_rows = await conn.fetch(
                """
                SELECT chat_id, claims, rarity_score
                FROM user_group_stats
                WHERE user_id = $1
                FOR UPDATE
                """,
                user_id,
            )

            backup_id: int | None = None
            if harem_rows:
                original_total_claims = (
                    int(user_snapshot["total_claims"] or 0)
                    if user_snapshot else 0
                )
                original_highest_rarity = (
                    str(user_snapshot["highest_rarity"])
                    if user_snapshot and user_snapshot["highest_rarity"] else None
                )
                backup = await conn.fetchrow(
                    """
                    INSERT INTO harem_deletion_backups (
                        user_id, deleted_by, original_total_claims,
                        original_highest_rarity, record_count
                    )
                    VALUES ($1, $2, $3, $4, $5)
                    RETURNING id
                    """,
                    user_id,
                    deleted_by,
                    original_total_claims,
                    original_highest_rarity,
                    len(harem_rows),
                )
                backup_id = int(backup["id"])
                await conn.executemany(
                    """
                    INSERT INTO harem_deletion_backup_items (
                        backup_id, original_harem_id, chat_id, character_id,
                        is_favorite, is_ranked, acquired_at
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    """,
                    [
                        (
                            backup_id,
                            int(row["id"]),
                            int(row["chat_id"]),
                            int(row["character_id"]),
                            bool(row["is_favorite"]),
                            bool(row["is_ranked"]),
                            row["acquired_at"],
                        )
                        for row in harem_rows
                    ],
                )
                if group_stat_rows:
                    await conn.executemany(
                        """
                        INSERT INTO harem_deletion_backup_group_stats (
                            backup_id, chat_id, claims, rarity_score
                        )
                        VALUES ($1, $2, $3, $4)
                        """,
                        [
                            (
                                backup_id,
                                int(row["chat_id"]),
                                int(row["claims"]),
                                int(row["rarity_score"]),
                            )
                            for row in group_stat_rows
                        ],
                    )

            # Love cards are a separate persistent relationship state. They
            # are not score/profile inventory and cannot be removed by the
            # generic Harem deletion/restore workflow; /sell owns their only
            # player-facing removal path.
            deleted_rows = await conn.fetch(
                """
                DELETE FROM harem AS h
                USING characters AS c
                WHERE h.user_id = $1
                  AND c.id = h.character_id
                  AND LOWER(BTRIM(COALESCE(c.rarity, ''))) <> 'love'
                RETURNING h.id
                """,
                user_id,
            )
            await conn.execute(
                """
                UPDATE trades
                SET status = 'failed', resolved_at = NOW()
                WHERE status = 'pending'
                  AND (from_user_id = $1 OR to_user_id = $1)
                """,
                user_id,
            )
            await conn.execute(
                """
                UPDATE gifts
                SET status = 'failed', resolved_at = NOW()
                WHERE status = 'pending'
                  AND (from_user_id = $1 OR to_user_id = $1)
                """,
                user_id,
            )
            await conn.execute(
                """
                UPDATE sudo_grants
                SET status = 'failed', resolved_at = NOW()
                WHERE status = 'pending' AND target_user_id = $1
                """,
                user_id,
            )
            await conn.execute(
                """
                WITH affected_groups AS (
                    DELETE FROM user_group_stats
                    WHERE user_id = $1
                    RETURNING chat_id
                )
                UPDATE group_stats AS g
                SET total_claims = COALESCE(
                    (SELECT SUM(s.claims)
                     FROM user_group_stats AS s
                     WHERE s.chat_id = g.chat_id),
                    0
                )
                WHERE g.chat_id IN (SELECT chat_id FROM affected_groups)
                """,
                user_id,
            )
            await conn.execute(
                "UPDATE users SET total_claims = 0, highest_rarity = NULL "
                "WHERE user_id = $1",
                user_id,
            )
            return HaremDeletionResult(
                deleted_count=len(deleted_rows),
                backup_id=backup_id,
            )


async def get_latest_harem_deletion_backup(user_id: int) -> dict | None:
    """Return the most recent still-restorable /deleteharem snapshot for a user."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, user_id, deleted_by, record_count, deleted_at
            FROM harem_deletion_backups
            WHERE user_id = $1 AND restored_at IS NULL
            ORDER BY deleted_at DESC, id DESC
            LIMIT 1
            """,
            user_id,
        )
    return dict(row) if row else None


async def restore_harem_deletion_backup(
    backup_id: int,
    *,
    restored_by: int,
) -> HaremRestoreResult:
    """Restore one pending /deleteharem snapshot atomically and only once.

    New characters caught after deletion are retained.  The snapshot's harem
    and historical stats are added back; a newer favorite remains favorite so
    the one-global-favorite invariant is never violated.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Find the target before taking its advisory lock.  This mirrors
            # the /sudo target-lock ordering and avoids lock-order inversions.
            target_user_id = await conn.fetchval(
                """
                SELECT user_id
                FROM harem_deletion_backups
                WHERE id = $1 AND restored_at IS NULL
                """,
                backup_id,
            )
            if target_user_id is None:
                return HaremRestoreResult(
                    success=False,
                    reason="not_found_or_already_restored",
                    backup_id=backup_id,
                )
            target_user_id = int(target_user_id)
            await conn.execute("SELECT pg_advisory_xact_lock($1)", target_user_id)

            backup = await conn.fetchrow(
                """
                SELECT *
                FROM harem_deletion_backups
                WHERE id = $1 AND restored_at IS NULL
                FOR UPDATE
                """,
                backup_id,
            )
            if not backup:
                return HaremRestoreResult(
                    success=False,
                    reason="not_found_or_already_restored",
                    user_id=target_user_id,
                    backup_id=backup_id,
                )

            items = await conn.fetch(
                """
                SELECT original_harem_id, chat_id, character_id, is_favorite, is_ranked, acquired_at
                FROM harem_deletion_backup_items
                WHERE backup_id = $1
                ORDER BY acquired_at ASC, original_harem_id ASC
                """,
                backup_id,
            )
            if not items:
                # This should be impossible for snapshots created by
                # delete_user_harem, but resolve a manually-corrupted empty
                # snapshot instead of leaving an owner unable to dismiss it.
                await conn.execute(
                    """
                    UPDATE harem_deletion_backups
                    SET restored_by = $2, restored_at = NOW()
                    WHERE id = $1
                    """,
                    backup_id,
                    restored_by,
                )
                return HaremRestoreResult(
                    success=True,
                    reason="empty_backup",
                    user_id=target_user_id,
                    backup_id=backup_id,
                )

            await conn.execute(
                """
                INSERT INTO users (user_id, last_seen_at)
                VALUES ($1, NOW())
                ON CONFLICT (user_id) DO NOTHING
                """,
                target_user_id,
            )
            current_user = await conn.fetchrow(
                """
                SELECT total_claims, highest_rarity
                FROM users
                WHERE user_id = $1
                FOR UPDATE
                """,
                target_user_id,
            )
            current_favorite = bool(
                await conn.fetchval(
                    """
                    SELECT 1
                    FROM harem
                    WHERE user_id = $1 AND is_favorite = TRUE
                    LIMIT 1
                    """,
                    target_user_id,
                )
            )

            favorite_available = not current_favorite
            restore_rows: list[tuple] = []
            for item in items:
                restore_favorite = bool(item["is_favorite"]) and favorite_available
                if restore_favorite:
                    favorite_available = False
                restore_rows.append((
                    target_user_id,
                    int(item["chat_id"]),
                    int(item["character_id"]),
                    restore_favorite,
                    bool(item["is_ranked"]),
                    item["acquired_at"],
                ))
            await conn.executemany(
                """
                INSERT INTO harem (
                    user_id, chat_id, character_id, is_favorite, is_ranked, acquired_at
                )
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                restore_rows,
            )

            backup_group_stats = await conn.fetch(
                """
                SELECT chat_id, claims, rarity_score
                FROM harem_deletion_backup_group_stats
                WHERE backup_id = $1
                ORDER BY chat_id ASC
                """,
                backup_id,
            )
            affected_chat_ids = [int(row["chat_id"]) for row in backup_group_stats]
            if backup_group_stats:
                await conn.executemany(
                    """
                    INSERT INTO user_group_stats (user_id, chat_id, claims, rarity_score)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (user_id, chat_id) DO UPDATE SET
                      claims = user_group_stats.claims + EXCLUDED.claims,
                      rarity_score = user_group_stats.rarity_score + EXCLUDED.rarity_score
                    """,
                    [
                        (
                            target_user_id,
                            int(row["chat_id"]),
                            int(row["claims"]),
                            int(row["rarity_score"]),
                        )
                        for row in backup_group_stats
                    ],
                )
                # Recalculate only the affected groups from their live
                # per-user rows.  This preserves all other collectors' totals.
                await conn.execute(
                    """
                    INSERT INTO group_stats (chat_id, total_spawns, total_claims)
                    SELECT
                        g.chat_id,
                        COALESCE(existing.total_spawns, 0),
                        COALESCE((
                            SELECT SUM(s.claims)
                            FROM user_group_stats AS s
                            WHERE s.chat_id = g.chat_id
                        ), 0)
                    FROM groups AS g
                    LEFT JOIN group_stats AS existing ON existing.chat_id = g.chat_id
                    WHERE g.chat_id = ANY($1::BIGINT[])
                    ON CONFLICT (chat_id) DO UPDATE SET
                      total_claims = EXCLUDED.total_claims
                    """,
                    affected_chat_ids,
                )

            current_total_claims = int(current_user["total_claims"] or 0)
            original_total_claims = int(backup["original_total_claims"] or 0)
            current_highest = (
                str(current_user["highest_rarity"])
                if current_user["highest_rarity"] else None
            )
            original_highest = (
                str(backup["original_highest_rarity"])
                if backup["original_highest_rarity"] else None
            )
            await conn.execute(
                """
                UPDATE users
                SET total_claims = $2,
                    highest_rarity = $3
                WHERE user_id = $1
                """,
                target_user_id,
                current_total_claims + original_total_claims,
                _higher_rarity(current_highest, original_highest),
            )
            await conn.execute(
                """
                UPDATE harem_deletion_backups
                SET restored_by = $2, restored_at = NOW()
                WHERE id = $1 AND restored_at IS NULL
                """,
                backup_id,
                restored_by,
            )
            # A restored snapshot cannot be applied again. Its metadata stays
            # as an audit record, while its item rows are released so a later
            # owner-approved catalog deletion is not held by an obsolete FK.
            await conn.execute(
                "DELETE FROM harem_deletion_backup_group_stats WHERE backup_id = $1",
                backup_id,
            )
            await conn.execute(
                "DELETE FROM harem_deletion_backup_items WHERE backup_id = $1",
                backup_id,
            )
            return HaremRestoreResult(
                success=True,
                reason="ok",
                user_id=target_user_id,
                restored_count=len(restore_rows),
                backup_id=backup_id,
            )


async def get_harem_gift_summary(user_id: int) -> tuple[int, int]:
    """Return ``(copy_count, distinct_character_count)`` for /gift all."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT COUNT(*) AS copy_count,
                   COUNT(DISTINCT character_id) AS unique_count
            FROM harem
            WHERE user_id = $1
            """,
            user_id,
        )
    return int(row["copy_count"] or 0), int(row["unique_count"] or 0)


async def create_gift(
    *,
    chat_id: int,
    from_uid: int,
    to_uid: int,
    character_ids: list[int] | None = None,
    all_harem: bool = False,
) -> tuple[int | None, str]:
    """Create a deterministic pending gift from a global inventory.

    Normal selections may include a current favorite.  The selected source
    harem row IDs are saved with the proposal, which means confirmation moves
    exactly the previewed copies and preserves any per-copy ``is_ranked``
    marker.  ``all_harem=True`` snapshots every currently owned row, including
    favorites and duplicates, for the sender's explicit confirmation.
    """
    requested_ids = [int(character_id) for character_id in (character_ids or [])]
    if all_harem and requested_ids:
        return None, "invalid_all_selection"
    if not all_harem and not requested_ids:
        return None, "no_characters"

    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            if all_harem:
                selected_rows = await conn.fetch(
                    """
                    SELECT h.id, h.character_id, h.is_ranked, c.rarity
                    FROM harem AS h
                    JOIN characters AS c ON c.id = h.character_id
                    WHERE h.user_id = $1
                    ORDER BY h.acquired_at ASC, h.id ASC
                    """,
                    from_uid,
                )
                if not selected_rows:
                    return None, "sender_does_not_own"
                protection = _rows_transfer_protection_reason(selected_rows)
                if protection is not None:
                    return None, protection
                selected_character_ids = [int(row["character_id"]) for row in selected_rows]
            else:
                unique_requested_ids = list(dict.fromkeys(requested_ids))
                existing = await conn.fetchval(
                    "SELECT COUNT(*) FROM characters WHERE id = ANY($1::BIGINT[])",
                    unique_requested_ids,
                )
                if int(existing or 0) != len(unique_requested_ids):
                    return None, "character_not_found"

                # A favorite is transferable on purpose.  It is selected first
                # for a requested ID so gifting that art reliably clears the
                # sender's showcase instead of silently choosing another copy.
                available_rows = await conn.fetch(
                    """
                    SELECT h.id, h.character_id, h.is_ranked, c.rarity
                    FROM harem AS h
                    JOIN characters AS c ON c.id = h.character_id
                    WHERE h.user_id = $1
                      AND h.character_id = ANY($2::BIGINT[])
                    ORDER BY h.character_id ASC,
                             h.is_favorite DESC,
                             h.acquired_at ASC,
                             h.id ASC
                    """,
                    from_uid,
                    unique_requested_ids,
                )
                available_by_character: dict[int, list] = {}
                for row in available_rows:
                    available_by_character.setdefault(int(row["character_id"]), []).append(row)

                selected_rows = []
                for character_id in requested_ids:
                    candidates = available_by_character.get(character_id, [])
                    if not candidates:
                        return None, "sender_does_not_own"
                    selected_rows.append(candidates.pop(0))
                protection = _rows_transfer_protection_reason(selected_rows)
                if protection is not None:
                    return None, protection
                selected_character_ids = requested_ids

            source_harem_ids = [int(row["id"]) for row in selected_rows]
            gift = await conn.fetchrow(
                """
                INSERT INTO gifts (
                    chat_id, from_user_id, to_user_id, character_ids, source_harem_ids
                )
                VALUES ($1, $2, $3, $4::BIGINT[], $5::BIGINT[])
                RETURNING id
                """,
                chat_id,
                from_uid,
                to_uid,
                selected_character_ids,
                source_harem_ids,
            )
            return int(gift["id"]), "ok"


async def accept_gift(gift_id: int, accepting_user_id: int) -> tuple[bool, str, list[int]]:
    """Accept a pending gift; only its sender can confirm the transfer.

    New pending gifts carry exact harem row IDs.  A legacy pre-migration gift
    has no row-ID snapshot, so it falls back to selecting currently owned
    copies (including a card that became Favorite after proposal creation).
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Lock both inventories in a stable order before the gift record.
            # This aligns with full-harem delete/restore locks and prevents a
            # transfer from racing a collection purge or reverse-direction gift.
            pending = await conn.fetchrow(
                """
                SELECT from_user_id, to_user_id
                FROM gifts
                WHERE id = $1 AND status = 'pending'
                """,
                gift_id,
            )
            if not pending:
                return False, "not_found", []
            for user_id in sorted({int(pending["from_user_id"]), int(pending["to_user_id"])}):
                await conn.execute("SELECT pg_advisory_xact_lock($1)", user_id)

            gift = await conn.fetchrow(
                "SELECT * FROM gifts WHERE id = $1 AND status = 'pending' FOR UPDATE",
                gift_id,
            )
            if not gift:
                return False, "not_found", []
            if int(gift["from_user_id"]) != accepting_user_id:
                return False, "not_your_gift", []

            requested_ids = [int(character_id) for character_id in gift["character_ids"]]
            raw_source_ids = gift["source_harem_ids"]
            selected_rows: list = []

            if raw_source_ids is not None:
                source_harem_ids = [int(harem_id) for harem_id in raw_source_ids]
                if (
                    len(source_harem_ids) != len(requested_ids)
                    or len(set(source_harem_ids)) != len(source_harem_ids)
                ):
                    await conn.execute(
                        "UPDATE gifts SET status = 'failed', resolved_at = NOW() WHERE id = $1",
                        gift_id,
                    )
                    return False, "ownership_lost", []

                locked_rows = await conn.fetch(
                    """
                    SELECT h.id, h.character_id, h.is_ranked, c.rarity
                    FROM harem AS h
                    JOIN characters AS c ON c.id = h.character_id
                    WHERE h.user_id = $1 AND h.id = ANY($2::BIGINT[])
                    ORDER BY h.id ASC
                    FOR UPDATE OF h
                    """,
                    int(gift["from_user_id"]),
                    source_harem_ids,
                )
                rows_by_id = {int(row["id"]): row for row in locked_rows}
                for source_id, requested_id in zip(source_harem_ids, requested_ids):
                    row = rows_by_id.get(source_id)
                    if row is None or int(row["character_id"]) != requested_id:
                        await conn.execute(
                            "UPDATE gifts SET status = 'failed', resolved_at = NOW() WHERE id = $1",
                            gift_id,
                        )
                        return False, "ownership_lost", []
                    selected_rows.append(row)
            else:
                # Compatibility for any proposal created before source_harem_ids
                # existed.  This path deliberately no longer excludes Favorite.
                unique_requested_ids = list(dict.fromkeys(requested_ids))
                available_rows = await conn.fetch(
                    """
                    SELECT h.id, h.character_id, h.is_ranked, c.rarity
                    FROM harem AS h
                    JOIN characters AS c ON c.id = h.character_id
                    WHERE h.user_id = $1
                      AND h.character_id = ANY($2::BIGINT[])
                    ORDER BY h.character_id ASC,
                             h.is_favorite DESC,
                             h.acquired_at ASC,
                             h.id ASC
                    FOR UPDATE OF h
                    """,
                    int(gift["from_user_id"]),
                    unique_requested_ids,
                )
                available_by_character: dict[int, list] = {}
                for row in available_rows:
                    available_by_character.setdefault(int(row["character_id"]), []).append(row)
                for character_id in requested_ids:
                    candidates = available_by_character.get(character_id, [])
                    if not candidates:
                        await conn.execute(
                            "UPDATE gifts SET status = 'failed', resolved_at = NOW() WHERE id = $1",
                            gift_id,
                        )
                        return False, "ownership_lost", []
                    selected_rows.append(candidates.pop(0))

            # Pending requests created before protected high-rank rules remain
            # safe: inspect the row-locked live cards again before transfer.
            protection = _rows_transfer_protection_reason(selected_rows)
            if protection is not None:
                await conn.execute(
                    "UPDATE gifts SET status = 'failed', resolved_at = NOW() WHERE id = $1",
                    gift_id,
                )
                return False, protection, []

            await conn.execute(
                """
                INSERT INTO users (user_id, last_seen_at)
                VALUES ($1, NOW())
                ON CONFLICT (user_id) DO UPDATE SET last_seen_at = NOW()
                """,
                int(gift["to_user_id"]),
            )
            await conn.executemany(
                """
                INSERT INTO harem (user_id, chat_id, character_id, is_ranked)
                VALUES ($1, $2, $3, $4)
                """,
                [
                    (
                        int(gift["to_user_id"]),
                        int(gift["chat_id"]),
                        int(row["character_id"]),
                        bool(row["is_ranked"]),
                    )
                    for row in selected_rows
                ],
            )
            await conn.execute(
                "DELETE FROM harem WHERE id = ANY($1::BIGINT[])",
                [int(row["id"]) for row in selected_rows],
            )

            # Source/recipient rank fields always reflect only ranked copies.
            # This clears a transferred favorite naturally: no favorite flag is
            # copied, and the deleted source row removes the sender's flag.
            await _recalculate_ranked_user_stats(conn, int(gift["from_user_id"]))
            await _recalculate_ranked_user_stats(conn, int(gift["to_user_id"]))
            await conn.execute(
                "UPDATE gifts SET status = 'accepted', resolved_at = NOW() WHERE id = $1",
                gift_id,
            )
            return True, "ok", [int(row["character_id"]) for row in selected_rows]

async def cancel_gift(gift_id: int, cancelling_user_id: int) -> tuple[bool, str]:
    """Cancel a pending gift; only its sender may cancel it."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE gifts
            SET status = 'cancelled', resolved_at = NOW()
            WHERE id = $1 AND status = 'pending' AND from_user_id = $2
            RETURNING id
            """,
            gift_id,
            cancelling_user_id,
        )
    return (True, "ok") if row else (False, "not_found_or_not_yours")


async def create_trade(*, chat_id: int, from_uid: int, to_uid: int,
                       from_char: int, to_char: int) -> tuple[int | None, str]:
    """Create a pending trade between two global inventories.

    ``chat_id`` is retained as the chat where the proposal was made.  Both
    ownership checks intentionally ignore it, so characters collected in any
    group can be traded together.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        from_owns = await conn.fetchrow(
            """
            SELECT h.is_favorite, c.rarity
            FROM harem AS h
            JOIN characters AS c ON c.id = h.character_id
            WHERE h.user_id = $1 AND h.character_id = $2
            -- Prefer a transferable duplicate, but still inspect a sole
            -- Favorite so an Eternal request gets its required warning.
            ORDER BY h.is_favorite ASC, h.acquired_at ASC, h.id ASC
            LIMIT 1
            """,
            from_uid,
            from_char,
        )
        if not from_owns:
            return None, "sender_does_not_own"
        protection = transfer_protection_reason(from_owns["rarity"])
        if protection is not None:
            return None, protection
        if bool(from_owns["is_favorite"]):
            return None, "sender_does_not_own"

        to_owns = await conn.fetchrow(
            """
            SELECT h.is_favorite, c.rarity
            FROM harem AS h
            JOIN characters AS c ON c.id = h.character_id
            WHERE h.user_id = $1 AND h.character_id = $2
            -- Prefer a transferable duplicate, but still inspect a sole
            -- Favorite so an Eternal request gets its required warning.
            ORDER BY h.is_favorite ASC, h.acquired_at ASC, h.id ASC
            LIMIT 1
            """,
            to_uid,
            to_char,
        )
        if not to_owns:
            return None, "recipient_does_not_own"
        protection = transfer_protection_reason(to_owns["rarity"])
        if protection is not None:
            return None, protection
        if bool(to_owns["is_favorite"]):
            return None, "recipient_does_not_own"

        row = await conn.fetchrow(
            """
            INSERT INTO trades (chat_id, from_user_id, to_user_id,
                                from_character_id, to_character_id)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id
            """,
            chat_id,
            from_uid,
            to_uid,
            from_char,
            to_char,
        )
        return row["id"], "ok"


async def accept_trade(trade_id: int, accepting_user_id: int) -> tuple[bool, str]:
    """Accept a pending trade using globally owned, non-favorite copies.

    The unranked status belongs to the harem copy rather than to a user, so a
    card granted by ``/sudo all -`` stays unranked if it is traded later.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            pending = await conn.fetchrow(
                """
                SELECT from_user_id, to_user_id
                FROM trades
                WHERE id = $1 AND status = 'pending'
                """,
                trade_id,
            )
            if not pending:
                return False, "not_found"
            for user_id in sorted({int(pending["from_user_id"]), int(pending["to_user_id"])}):
                await conn.execute("SELECT pg_advisory_xact_lock($1)", user_id)

            trade = await conn.fetchrow(
                "SELECT * FROM trades WHERE id = $1 AND status = 'pending' FOR UPDATE",
                trade_id,
            )
            if not trade:
                return False, "not_found"

            if int(trade["to_user_id"]) != accepting_user_id:
                return False, "not_your_trade"

            # Lock one candidate copy from each global collection. The sort
            # prefers a transferable copy while still exposing a sole protected
            # high-rank Favorite to the required protection check below.
            from_own = await conn.fetchrow(
                """
                SELECT h.id, h.is_ranked, h.is_favorite, c.rarity
                FROM harem AS h
                JOIN characters AS c ON c.id = h.character_id
                WHERE h.user_id = $1 AND h.character_id = $2
                -- Lock a transferable duplicate first. If only a Favorite
                -- remains, inspect its rarity before resolving the proposal.
                ORDER BY h.is_favorite ASC, h.acquired_at ASC, h.id ASC
                LIMIT 1
                FOR UPDATE OF h
                """,
                int(trade["from_user_id"]),
                int(trade["from_character_id"]),
            )
            to_own = await conn.fetchrow(
                """
                SELECT h.id, h.is_ranked, h.is_favorite, c.rarity
                FROM harem AS h
                JOIN characters AS c ON c.id = h.character_id
                WHERE h.user_id = $1 AND h.character_id = $2
                -- Lock a transferable duplicate first. If only a Favorite
                -- remains, inspect its rarity before resolving the proposal.
                ORDER BY h.is_favorite ASC, h.acquired_at ASC, h.id ASC
                LIMIT 1
                FOR UPDATE OF h
                """,
                int(trade["to_user_id"]),
                int(trade["to_character_id"]),
            )
            if not (from_own and to_own):
                await conn.execute(
                    "UPDATE trades SET status = 'failed', resolved_at = NOW() WHERE id = $1",
                    trade_id,
                )
                return False, "ownership_lost"
            protection = _rows_transfer_protection_reason((from_own, to_own))
            if protection is not None:
                await conn.execute(
                    "UPDATE trades SET status = 'failed', resolved_at = NOW() WHERE id = $1",
                    trade_id,
                )
                return False, protection
            if bool(from_own["is_favorite"]) or bool(to_own["is_favorite"]):
                await conn.execute(
                    "UPDATE trades SET status = 'failed', resolved_at = NOW() WHERE id = $1",
                    trade_id,
                )
                return False, "ownership_lost"

            # Preserve the proposal chat as provenance while retaining each
            # source copy's ranked/unranked status.
            await conn.execute(
                """
                INSERT INTO harem (user_id, chat_id, character_id, is_ranked)
                VALUES ($1, $2, $3, $4)
                """,
                int(trade["to_user_id"]),
                int(trade["chat_id"]),
                int(trade["from_character_id"]),
                bool(from_own["is_ranked"]),
            )
            await conn.execute(
                """
                INSERT INTO harem (user_id, chat_id, character_id, is_ranked)
                VALUES ($1, $2, $3, $4)
                """,
                int(trade["from_user_id"]),
                int(trade["chat_id"]),
                int(trade["to_character_id"]),
                bool(to_own["is_ranked"]),
            )
            await conn.execute(
                "DELETE FROM harem WHERE id IN ($1, $2)",
                int(from_own["id"]),
                int(to_own["id"]),
            )
            await _recalculate_ranked_user_stats(conn, int(trade["from_user_id"]))
            await _recalculate_ranked_user_stats(conn, int(trade["to_user_id"]))
            await conn.execute(
                "UPDATE trades SET status = 'accepted', resolved_at = NOW() WHERE id = $1",
                trade_id,
            )
            return True, "ok"

async def decline_trade(trade_id: int, declining_user_id: int) -> tuple[bool, str]:
    """Decline a pending trade — only the recipient may decline."""
    pool = get_pool()
    async with pool.acquire() as conn:
        t = await conn.fetchrow(
            "SELECT to_user_id FROM trades WHERE id = $1 AND status = 'pending'",
            trade_id,
        )
        if not t:
            return False, "not_found"
        if t["to_user_id"] != declining_user_id:
            return False, "not_your_trade"
        await conn.execute(
            "UPDATE trades SET status='declined', resolved_at=NOW() WHERE id=$1",
            trade_id,
        )
        return True, "ok"


async def get_profile_data(user_id: int, chat_id: int | None) -> dict:
    """Return ranked collection totals, rarity progress, and profile ranks.

    Inventory-only /sudo all - copies remain visible in /harem but are
    intentionally excluded from every profile statistic and rank.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        totals = await conn.fetchrow(
            """
            SELECT
                (SELECT COUNT(*) FROM characters
                 WHERE LOWER(BTRIM(rarity)) <> 'love') AS total_characters,
                (SELECT COUNT(DISTINCT h.character_id)
                 FROM harem AS h
                 JOIN characters AS owned_character ON owned_character.id = h.character_id
                 WHERE h.user_id = $1 AND h.is_ranked = TRUE
                   AND LOWER(BTRIM(owned_character.rarity)) <> 'love') AS unique_owned
            """,
            user_id,
        )
        rarity_rows = await conn.fetch(
            """
            SELECT
                c.rarity,
                COUNT(DISTINCT c.id) AS total,
                COUNT(DISTINCT h.character_id) AS owned
            FROM characters AS c
            LEFT JOIN harem AS h
              ON h.character_id = c.id
             AND h.user_id = $1
             AND h.is_ranked = TRUE
            WHERE LOWER(BTRIM(c.rarity)) <> 'love'
            GROUP BY c.rarity
            """,
            user_id,
        )
        global_rank = await conn.fetchval(
            """
            WITH candidates AS (
                SELECT user_id FROM users
                UNION
                SELECT $1::BIGINT
            ),
            inventory AS (
                SELECT c.user_id, COUNT(DISTINCT owned_character.id) AS unique_owned
                FROM candidates AS c
                LEFT JOIN harem AS h
                  ON h.user_id = c.user_id AND h.is_ranked = TRUE
                LEFT JOIN characters AS owned_character
                  ON owned_character.id = h.character_id
                 AND LOWER(BTRIM(owned_character.rarity)) <> 'love'
                GROUP BY c.user_id
            ),
            ranked AS (
                SELECT user_id,
                       ROW_NUMBER() OVER (ORDER BY unique_owned DESC, user_id ASC) AS position
                FROM inventory
            )
            SELECT position FROM ranked WHERE user_id = $1
            """,
            user_id,
        )

        chat_rank = None
        if chat_id is not None:
            chat_rank = await conn.fetchval(
                """
                WITH members AS (
                    SELECT user_id
                    FROM user_group_stats
                    WHERE chat_id = $2
                    UNION
                    SELECT $1::BIGINT
                ),
                inventory AS (
                    SELECT m.user_id, COUNT(DISTINCT owned_character.id) AS unique_owned
                    FROM members AS m
                    LEFT JOIN harem AS h
                      ON h.user_id = m.user_id AND h.is_ranked = TRUE
                    LEFT JOIN characters AS owned_character
                      ON owned_character.id = h.character_id
                     AND LOWER(BTRIM(owned_character.rarity)) <> 'love'
                    GROUP BY m.user_id
                ),
                ranked AS (
                    SELECT user_id,
                           ROW_NUMBER() OVER (ORDER BY unique_owned DESC, user_id ASC) AS position
                    FROM inventory
                )
                SELECT position FROM ranked WHERE user_id = $1
                """,
                user_id,
                chat_id,
            )

    rarity_progress = {
        row["rarity"]: {"owned": int(row["owned"]), "total": int(row["total"])}
        for row in rarity_rows
    }
    return {
        "unique_owned": int(totals["unique_owned"]),
        "total_characters": int(totals["total_characters"]),
        "rarities": rarity_progress,
        "global_rank": int(global_rank) if global_rank is not None else None,
        "chat_rank": int(chat_rank) if chat_rank is not None else None,
    }


# `/catchtop` intentionally supports only the two player-visible rolling
# windows requested for the fixed Main Group board. Keeping this validation in
# the service protects any future API/admin caller as well as the command UI.
CATCH_TOP_SUPPORTED_WINDOWS = frozenset({1, 7})
CATCH_TOP_MAX_ROWS = 3


async def get_recent_catch_leaderboard(
    *,
    chat_id: int,
    window_days: int,
    limit: int = CATCH_TOP_MAX_ROWS,
) -> list[dict]:
    """Return up to three exact `/catch` leaders for one rolling time window.

    The source is ``catch_events``, not live Harem rows or aggregate group
    stats. Therefore a result remains catch-only even if a card is sold,
    burned, reset, deleted from Harem, gifted, or otherwise no longer owned.
    """
    if (
        not isinstance(chat_id, int)
        or isinstance(chat_id, bool)
        or not isinstance(window_days, int)
        or isinstance(window_days, bool)
        or window_days not in CATCH_TOP_SUPPORTED_WINDOWS
        or not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 1 <= limit <= CATCH_TOP_MAX_ROWS
    ):
        raise ValueError("unsupported catch leaderboard request")

    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                catch_event.user_id,
                COALESCE(
                    NULLIF(BTRIM(player.full_name), ''),
                    NULLIF(BTRIM(player.username), ''),
                    CONCAT('Player ', catch_event.user_id::TEXT)
                ) AS display_name,
                NULLIF(BTRIM(player.username), '') AS username,
                COUNT(*)::INT AS catches
            FROM catch_events AS catch_event
            LEFT JOIN users AS player ON player.user_id = catch_event.user_id
            WHERE catch_event.chat_id = $1
              AND catch_event.caught_at >= NOW() - ($2::INT * INTERVAL '1 day')
            GROUP BY catch_event.user_id, player.full_name, player.username
            ORDER BY COUNT(*) DESC, MIN(catch_event.caught_at) ASC, catch_event.user_id ASC
            LIMIT $3
            """,
            chat_id,
            window_days,
            limit,
        )
    return [dict(row) for row in rows]


async def get_leaderboard_global(limit: int = 10) -> list[dict]:
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT u.user_id, u.username, u.full_name,
                   u.total_claims, u.highest_rarity
            FROM users u
            -- Stable ties keep /topusers and the Mini App's global rank aligned.
            ORDER BY u.total_claims DESC, u.user_id ASC
            LIMIT $1
            """,
            limit,
        )
        return [dict(r) for r in rows]


async def get_leaderboard_chat(chat_id: int, limit: int = 10) -> list[dict]:
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT s.user_id, u.username, u.full_name, u.highest_rarity,
                   s.claims, s.rarity_score
            FROM user_group_stats s
            JOIN users u ON u.user_id = s.user_id
            WHERE s.chat_id = $1
            ORDER BY s.rarity_score DESC, s.claims DESC
            LIMIT $2
            """,
            chat_id, limit,
        )
        return [dict(r) for r in rows]


async def get_leaderboard_groups(limit: int = 10) -> list[dict]:
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT g.chat_id, g.title, g.username,
                   COALESCE(s.total_spawns, 0) AS spawns,
                   COALESCE(s.total_claims, 0) AS claims
            FROM groups g
            LEFT JOIN group_stats s ON s.chat_id = g.chat_id
            WHERE g.enabled = TRUE
            -- Stable ties keep /topgroups and owner /getgroups on the same rows.
            ORDER BY s.total_claims DESC NULLS LAST, g.chat_id ASC
            LIMIT $1
            """,
            limit,
        )
        return [dict(r) for r in rows]
