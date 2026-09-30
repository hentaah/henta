"""Persistent collection-achievement progress and rarity-reward operations.

Achievements are global across a player's ranked harem, matching the existing
/rarity chart.  Duplicate copies never increase a completion count: a collector
must own every distinct catalog card in a tier or anime.
"""
from __future__ import annotations

from dataclasses import dataclass

from bot.db.pool import get_pool


# The requested collector ladder ends at OP. Completing each tier below OP
# grants one selected card from the immediately following tier.
RARITY_COLLECTOR_TIERS = (
    "Common",
    "Rare",
    "Legendary",
    "Mythical",
    "Sovereign",
    "OP",
)
RARITY_REWARD_TARGETS: dict[str, str] = {
    "Common": "Rare",
    "Rare": "Legendary",
    "Legendary": "Mythical",
    "Mythical": "Sovereign",
    "Sovereign": "OP",
}
RARITY_RANK = {
    "Common": 0,
    "Rare": 1,
    "Legendary": 2,
    "Mythical": 3,
    "Sovereign": 4,
    "OP": 5,
    "Eternal": 6,
    "Suspicious": 7,
    "OverLord": 8,
}


@dataclass(frozen=True)
class RarityAchievementProgress:
    rarity: str
    owned: int
    total: int
    unlocked: bool
    reward_claimed: bool

    @property
    def complete_now(self) -> bool:
        return self.total > 0 and self.owned >= self.total


@dataclass(frozen=True)
class AnimeAchievementProgress:
    anime: str
    owned: int
    total: int
    unlocked: bool

    @property
    def complete_now(self) -> bool:
        return self.total > 0 and self.owned >= self.total


@dataclass(frozen=True)
class AchievementSnapshot:
    """All collector progress currently relevant to one player."""

    rarity_progress: tuple[RarityAchievementProgress, ...]
    anime_progress: tuple[AnimeAchievementProgress, ...]
    unlocked_count: int


@dataclass(frozen=True)
class RarityRewardStatus:
    source_rarity: str
    target_rarity: str | None
    unlocked: bool
    already_claimed: bool
    target_has_cards: bool

    @property
    def claimable(self) -> bool:
        return bool(
            self.unlocked
            and self.target_rarity is not None
            and self.target_has_cards
            and not self.already_claimed
        )


@dataclass(frozen=True)
class RarityRewardClaimResult:
    success: bool
    reason: str = ""
    required_rarity: str = ""
    actual_rarity: str = ""
    character_id: int = 0
    name: str = ""
    anime: str = ""
    rarity: str = ""
    image_url: str = ""
    media_type: str = "photo"


def canonical_collector_rarity(value: str | None) -> str | None:
    """Return a supported collector rarity regardless of input casing."""
    normalized = (value or "").strip().casefold()
    for rarity in RARITY_COLLECTOR_TIERS:
        if rarity.casefold() == normalized:
            return rarity
    return None


def rarity_achievement_key(rarity: str) -> str:
    return f"rarity:{rarity.casefold()}"


def anime_achievement_key(anime: str) -> str:
    return f"anime:{anime.strip().casefold()}"


def _higher_rarity(current: str | None, candidate: str) -> str:
    """Keep users.highest_rarity consistent when a reward joins the harem."""
    if not current:
        return candidate
    current_rank = RARITY_RANK.get(str(current), -1)
    candidate_rank = RARITY_RANK.get(str(candidate), -1)
    return str(current) if current_rank >= candidate_rank else candidate


async def _read_rarity_progress(conn, user_id: int) -> list[tuple[str, int, int]]:
    """Return distinct ranked ownership versus the catalog for each tier."""
    catalog_rows = await conn.fetch(
        """
        SELECT rarity, COUNT(DISTINCT id) AS total
        FROM characters
        WHERE rarity = ANY($1::TEXT[])
        GROUP BY rarity
        """,
        list(RARITY_COLLECTOR_TIERS),
    )
    owned_rows = await conn.fetch(
        """
        SELECT c.rarity, COUNT(DISTINCT c.id) AS owned
        FROM harem AS h
        JOIN characters AS c ON c.id = h.character_id
        WHERE h.user_id = $1
          AND h.is_ranked = TRUE
          AND c.rarity = ANY($2::TEXT[])
        GROUP BY c.rarity
        """,
        user_id,
        list(RARITY_COLLECTOR_TIERS),
    )
    total_by_rarity = {str(row["rarity"]): int(row["total"] or 0) for row in catalog_rows}
    owned_by_rarity = {str(row["rarity"]): int(row["owned"] or 0) for row in owned_rows}
    return [
        (rarity, owned_by_rarity.get(rarity, 0), total_by_rarity.get(rarity, 0))
        for rarity in RARITY_COLLECTOR_TIERS
    ]


async def _read_owned_anime_progress(conn, user_id: int) -> list[tuple[str, int, int]]:
    """Return only anime/game collections for which the player owns a card."""
    rows = await conn.fetch(
        """
        SELECT
            BTRIM(c.anime) AS anime,
            COUNT(DISTINCT c.id) AS total,
            COUNT(DISTINCT h.character_id) AS owned
        FROM characters AS c
        LEFT JOIN harem AS h
          ON h.character_id = c.id
         AND h.user_id = $1
         AND h.is_ranked = TRUE
        WHERE BTRIM(c.anime) <> ''
        GROUP BY BTRIM(c.anime)
        HAVING COUNT(DISTINCT h.character_id) > 0
        ORDER BY BTRIM(c.anime) ASC
        """,
        user_id,
    )
    return [
        (str(row["anime"]), int(row["owned"] or 0), int(row["total"] or 0))
        for row in rows
    ]


async def get_achievement_snapshot(user_id: int) -> AchievementSnapshot:
    """Calculate progress and permanently record newly completed collections.

    The snapshot intentionally uses ranked harem rows, the same meaning used by
    /rarity. Therefore inventory-only `/sudo all -` cards cannot unlock a
    collector reward, while normal catches and normal owner grants can.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            rarity_rows = await _read_rarity_progress(conn, user_id)
            anime_rows = await _read_owned_anime_progress(conn, user_id)

            newly_completed = [
                (user_id, rarity_achievement_key(rarity), "rarity", rarity, None)
                for rarity, owned, total in rarity_rows
                if total > 0 and owned >= total
            ]
            newly_completed.extend(
                (user_id, anime_achievement_key(anime), "anime", None, anime)
                for anime, owned, total in anime_rows
                if total > 0 and owned >= total
            )
            if newly_completed:
                await conn.executemany(
                    """
                    INSERT INTO user_achievements (
                        user_id, achievement_key, achievement_type, rarity, anime
                    )
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (user_id, achievement_key) DO NOTHING
                    """,
                    newly_completed,
                )

            unlocked_rows = await conn.fetch(
                """
                SELECT achievement_key, reward_claimed_at
                FROM user_achievements
                WHERE user_id = $1
                """,
                user_id,
            )

    unlocked = {
        str(row["achievement_key"]): row["reward_claimed_at"] is not None
        for row in unlocked_rows
    }
    rarity_progress = tuple(
        RarityAchievementProgress(
            rarity=rarity,
            owned=owned,
            total=total,
            unlocked=rarity_achievement_key(rarity) in unlocked,
            reward_claimed=bool(unlocked.get(rarity_achievement_key(rarity), False)),
        )
        for rarity, owned, total in rarity_rows
    )
    anime_progress = tuple(
        AnimeAchievementProgress(
            anime=anime,
            owned=owned,
            total=total,
            unlocked=anime_achievement_key(anime) in unlocked,
        )
        for anime, owned, total in anime_rows
    )
    return AchievementSnapshot(
        rarity_progress=rarity_progress,
        anime_progress=anime_progress,
        unlocked_count=len(unlocked),
    )


async def get_rarity_reward_status(user_id: int, source_rarity: str) -> RarityRewardStatus:
    """Return whether a completed rarity collector reward can be selected."""
    source = canonical_collector_rarity(source_rarity)
    if source is None:
        return RarityRewardStatus("", None, False, False, False)

    snapshot = await get_achievement_snapshot(user_id)
    source_progress = next(
        (item for item in snapshot.rarity_progress if item.rarity == source),
        None,
    )
    target = RARITY_REWARD_TARGETS.get(source)
    target_progress = next(
        (item for item in snapshot.rarity_progress if item.rarity == target),
        None,
    )
    return RarityRewardStatus(
        source_rarity=source,
        target_rarity=target,
        unlocked=bool(source_progress and source_progress.unlocked),
        already_claimed=bool(source_progress and source_progress.reward_claimed),
        target_has_cards=bool(target_progress and target_progress.total > 0),
    )


async def claim_rarity_achievement_reward(
    *,
    user_id: int,
    username: str,
    full_name: str,
    private_chat_id: int,
    source_rarity: str,
    character_id: int,
) -> RarityRewardClaimResult:
    """Atomically grant one eligible next-tier collector reward in private.

    A per-user advisory lock and the achievement row lock make a reward
    one-time-only even if the owner sends the same code twice simultaneously.
    Reward cards are normal ranked collection cards so they appear in /rarity
    and can contribute to later collector progress; they have no group claim
    statistics because the selection happens in the user's private chat.
    """
    source = canonical_collector_rarity(source_rarity)
    target = RARITY_REWARD_TARGETS.get(source or "")
    if source is None or target is None:
        return RarityRewardClaimResult(success=False, reason="not_claimable")
    if not isinstance(character_id, int) or character_id <= 0:
        return RarityRewardClaimResult(
            success=False,
            reason="not_found",
            required_rarity=target,
        )

    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1)", user_id)
            achievement = await conn.fetchrow(
                """
                SELECT reward_claimed_at
                FROM user_achievements
                WHERE user_id = $1
                  AND achievement_key = $2
                  AND achievement_type = 'rarity'
                  AND rarity = $3
                FOR UPDATE
                """,
                user_id,
                rarity_achievement_key(source),
                source,
            )
            if not achievement:
                return RarityRewardClaimResult(
                    success=False,
                    reason="not_unlocked",
                    required_rarity=target,
                )
            if achievement["reward_claimed_at"] is not None:
                return RarityRewardClaimResult(
                    success=False,
                    reason="already_claimed",
                    required_rarity=target,
                )

            character = await conn.fetchrow(
                """
                SELECT id, name, anime, rarity, rarity_score, image_url,
                       COALESCE(media_type, 'photo') AS media_type
                FROM characters
                WHERE id = $1
                FOR KEY SHARE
                """,
                character_id,
            )
            if not character:
                return RarityRewardClaimResult(
                    success=False,
                    reason="not_found",
                    required_rarity=target,
                )

            actual_rarity = str(character["rarity"])
            if actual_rarity.strip().casefold() != target.casefold():
                return RarityRewardClaimResult(
                    success=False,
                    reason="wrong_rarity",
                    required_rarity=target,
                    actual_rarity=actual_rarity,
                    character_id=character_id,
                )

            # Mark the reward claimed before adding the harem row. If any
            # subsequent query fails, the transaction rolls this marker back.
            claimed = await conn.fetchrow(
                """
                UPDATE user_achievements
                SET reward_claimed_at = NOW(), reward_character_id = $3
                WHERE user_id = $1
                  AND achievement_key = $2
                  AND reward_claimed_at IS NULL
                RETURNING achievement_key
                """,
                user_id,
                rarity_achievement_key(source),
                character_id,
            )
            if not claimed:
                return RarityRewardClaimResult(
                    success=False,
                    reason="already_claimed",
                    required_rarity=target,
                )

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
            await conn.execute(
                """
                INSERT INTO harem (user_id, chat_id, character_id)
                VALUES ($1, $2, $3)
                """,
                user_id,
                private_chat_id,
                character_id,
            )
            current_highest = await conn.fetchval(
                "SELECT highest_rarity FROM users WHERE user_id = $1",
                user_id,
            )
            await conn.execute(
                """
                UPDATE users
                SET total_claims = total_claims + 1,
                    highest_rarity = $2
                WHERE user_id = $1
                """,
                user_id,
                _higher_rarity(current_highest, actual_rarity),
            )

            return RarityRewardClaimResult(
                success=True,
                required_rarity=target,
                character_id=int(character["id"]),
                name=str(character["name"]),
                anime=str(character["anime"]),
                rarity=actual_rarity,
                image_url=str(character["image_url"] or ""),
                media_type=str(character["media_type"] or "photo"),
            )
