"""UTC weekly Neko-quest progress primitives shared by game services.

This module deliberately contains no Telegram/UI code and no wallet mutation.
Callers record a completed game action inside their already-open transaction;
claiming an earned Neko reward remains in :mod:`bot.services.neko`, where the
wallet row and ledger are authoritatively locked and updated.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

# Stable server-side keys. Callback data carries only one of these keys and a
# week token; reward values and completion are always recalculated from storage.
WEEKLY_QUEST_CATCH = "catch"
WEEKLY_QUEST_DAILY_NEKO = "dailyneko"
WEEKLY_QUEST_GAME = "game"
WEEKLY_QUEST_GIFT = "gift"
WEEKLY_QUEST_COMPLETION = "completion"
WEEKLY_QUEST_ORDER = (
    WEEKLY_QUEST_CATCH,
    WEEKLY_QUEST_DAILY_NEKO,
    WEEKLY_QUEST_GAME,
    WEEKLY_QUEST_GIFT,
)
WEEKLY_QUEST_TARGETS = {
    WEEKLY_QUEST_CATCH: 10,
    WEEKLY_QUEST_DAILY_NEKO: 4,
    WEEKLY_QUEST_GAME: 3,
    WEEKLY_QUEST_GIFT: 1,
}
WEEKLY_QUEST_REWARDS = {
    WEEKLY_QUEST_CATCH: 20,
    WEEKLY_QUEST_DAILY_NEKO: 15,
    WEEKLY_QUEST_GAME: 10,
    WEEKLY_QUEST_GIFT: 15,
    WEEKLY_QUEST_COMPLETION: 25,
}
WEEKLY_QUEST_CLAIM_COLUMNS = {
    WEEKLY_QUEST_CATCH: "catch_reward_claimed_at",
    WEEKLY_QUEST_DAILY_NEKO: "daily_neko_reward_claimed_at",
    WEEKLY_QUEST_GAME: "game_reward_claimed_at",
    WEEKLY_QUEST_GIFT: "gift_reward_claimed_at",
    WEEKLY_QUEST_COMPLETION: "completion_reward_claimed_at",
}


def utc_now(value: datetime | None = None) -> datetime:
    """Return an aware UTC time, including for deterministic service tests."""
    current = value or datetime.now(timezone.utc)
    return current.replace(tzinfo=timezone.utc) if current.tzinfo is None else current.astimezone(timezone.utc)


def utc_week_start(value: datetime | None = None) -> date:
    """Return Monday 00:00 UTC's calendar date for the supplied moment."""
    current_date = utc_now(value).date()
    return current_date - timedelta(days=current_date.weekday())


def week_start_token(value: date) -> str:
    """Encode a Monday week start compactly for callback data."""
    if isinstance(value, datetime) or not isinstance(value, date):
        raise ValueError("invalid week start")
    return value.strftime("%Y%m%d")


def parse_week_start_token(value: object) -> date | None:
    """Parse only the fixed eight-digit callback week token."""
    if not isinstance(value, str) or len(value) != 8 or not value.isascii() or not value.isdigit():
        return None
    try:
        parsed = datetime.strptime(value, "%Y%m%d").date()
    except ValueError:
        return None
    return parsed if parsed.weekday() == 0 else None


def _as_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return utc_now(value).date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def _int_at_least_zero(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def weekly_quest_snapshot_from_row(row) -> dict:
    """Normalize a durable weekly row into a UI/service-safe snapshot."""
    stored_week_start = _as_date(row["week_start"])
    if stored_week_start is None:
        raise ValueError("weekly quest row has an invalid week start")
    stored_dates = row["daily_neko_dates"] or []
    daily_dates = sorted({parsed for raw in stored_dates if (parsed := _as_date(raw)) is not None})
    progress = {
        WEEKLY_QUEST_CATCH: _int_at_least_zero(row["catch_count"]),
        WEEKLY_QUEST_DAILY_NEKO: len(daily_dates),
        WEEKLY_QUEST_GAME: _int_at_least_zero(row["game_count"]),
        WEEKLY_QUEST_GIFT: _int_at_least_zero(row["gift_count"]),
    }
    claimed = {
        key: row[column] is not None
        for key, column in WEEKLY_QUEST_CLAIM_COLUMNS.items()
    }
    completed = {
        key: progress[key] >= WEEKLY_QUEST_TARGETS[key]
        for key in WEEKLY_QUEST_ORDER
    }
    return {
        "week_start": stored_week_start,
        "week_end": stored_week_start + timedelta(days=7),
        "progress": progress,
        "daily_neko_dates": daily_dates,
        "completed": completed,
        "claimed": claimed,
        "completed_count": sum(completed.values()),
        "all_completed": all(completed.values()),
    }


async def ensure_weekly_quest_row(
    conn,
    *,
    user_id: int,
    week_start: date,
) -> None:
    """Create a current-week row idempotently after the caller established user."""
    await conn.execute(
        """
        INSERT INTO neko_weekly_quests (user_id, week_start)
        VALUES ($1, $2)
        ON CONFLICT (user_id, week_start) DO NOTHING
        """,
        user_id,
        week_start,
    )


async def get_weekly_quest_row_for_update(
    conn,
    *,
    user_id: int,
    week_start: date,
):
    """Create then row-lock exactly one user's current UTC-week progress."""
    await ensure_weekly_quest_row(conn, user_id=user_id, week_start=week_start)
    row = await conn.fetchrow(
        """
        SELECT
            user_id,
            week_start,
            catch_count,
            daily_neko_dates,
            game_count,
            gift_count,
            catch_reward_claimed_at,
            daily_neko_reward_claimed_at,
            game_reward_claimed_at,
            gift_reward_claimed_at,
            completion_reward_claimed_at
        FROM neko_weekly_quests
        WHERE user_id = $1 AND week_start = $2
        FOR UPDATE
        """,
        user_id,
        week_start,
    )
    if not row:
        raise RuntimeError("weekly Neko quest row was not created")
    return row


async def record_neko_weekly_quest_activity_in_transaction(
    conn,
    *,
    user_id: int,
    activity: str,
    now: datetime | None = None,
    units: int = 1,
) -> None:
    """Record one already-successful player action in its UTC-week row.

    The caller must be inside the transaction that committed the action itself.
    `/dailyneko` saves at most one distinct UTC date; all other counters retain
    their real successful action count, even after their visible quest target.
    """
    if activity not in WEEKLY_QUEST_ORDER:
        raise ValueError("invalid weekly Neko quest activity")
    if not isinstance(units, int) or isinstance(units, bool) or units <= 0:
        raise ValueError("invalid weekly Neko quest units")

    current = utc_now(now)
    week_start = utc_week_start(current)
    row = await get_weekly_quest_row_for_update(
        conn,
        user_id=user_id,
        week_start=week_start,
    )

    if activity == WEEKLY_QUEST_DAILY_NEKO:
        current_date = current.date()
        existing_dates = {
            parsed
            for raw in (row["daily_neko_dates"] or [])
            if (parsed := _as_date(raw)) is not None
        }
        if current_date not in existing_dates:
            existing_dates.add(current_date)
            await conn.execute(
                """
                UPDATE neko_weekly_quests
                SET daily_neko_dates = $3::DATE[],
                    updated_at = $4
                WHERE user_id = $1 AND week_start = $2
                """,
                user_id,
                week_start,
                sorted(existing_dates),
                current,
            )
        return

    column = {
        WEEKLY_QUEST_CATCH: "catch_count",
        WEEKLY_QUEST_GAME: "game_count",
        WEEKLY_QUEST_GIFT: "gift_count",
    }[activity]
    # ``column`` is selected only from this closed mapping, never Telegram input.
    await conn.execute(
        f"""
        UPDATE neko_weekly_quests
        SET {column} = {column} + $3,
            updated_at = $4
        WHERE user_id = $1 AND week_start = $2
        """,
        user_id,
        week_start,
        units,
        current,
    )
