"""Durable decimal Neko wallets, transfers, bonus game, Pass, and Neko Mines.

Every balance-changing path uses a PostgreSQL transaction and locks its wallet
row(s) before calculating the next amount.  Telegram callback data carries only
server IDs/choices; it never contains a balance, payout, or casino bomb layout.
"""
from __future__ import annotations

import re
import secrets
from math import ceil
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from bot.db.pool import get_pool
from bot.services.neko_quests import (
    WEEKLY_QUEST_CLAIM_COLUMNS,
    WEEKLY_QUEST_COMPLETION,
    WEEKLY_QUEST_ORDER,
    WEEKLY_QUEST_REWARDS,
    get_weekly_quest_row_for_update,
    utc_now,
    utc_week_start,
    weekly_quest_snapshot_from_row,
)

# Two decimal places preserve payable casino cents from fixed Mines multiplier
# tables. NUMERIC(21, 2) allows 19 integral digits, safely below PostgreSQL
# BIGINT-domain user IDs while preserving all prior integer balances.
NEKO_QUANTUM = Decimal("0.01")
ZERO_NEKO = Decimal("0.00")
MAX_NEKO_BALANCE = Decimal("9000000000000000000.00")
MAX_NEKO_AMOUNT = MAX_NEKO_BALANCE
MAX_NEKO_PASS_DAYS = 3_650

NEKO_GAME_WINDOW_SECONDS = 24 * 60 * 60
NEKO_GAME_STANDARD_LIMIT = 3
NEKO_GAME_PASS_LIMIT = 5
NEKO_GAME_DICE_SIDES = 6
NEKO_PER_DICE_POINT = 5

# `/dailyneko` is independent of the character `/daily` reward: one secure
# whole-Neko reward from 1–49 every personal rolling 24 hours.
DAILY_NEKO_REWARD_INTERVAL_SECONDS = 24 * 60 * 60
DAILY_NEKO_MIN_REWARD = 1
DAILY_NEKO_MAX_REWARD = 49

# Retired streak constants remain only to preserve historic data helpers; new
# Daily Neko claims no longer advance streaks or award their milestones.
NEKO_STREAK_DAY3 = 3
NEKO_STREAK_DAY7 = 7
NEKO_STREAK_DAY30 = 30
NEKO_STREAK_DAY3_REWARD = Decimal("10.00")
NEKO_STREAK_DAY7_REWARD = Decimal("35.00")
NEKO_STREAK_DAY30_BADGE_KEY = "neko_streak_30_day"
NEKO_STREAK_DAY30_BADGE_NAME = "30-Day Neko Streak"

# New Neko Mines boards use a 3×3 layout with one bomb.  Payout versions
# are immutable per board so a deployed schedule change never reprices a
# player who already reserved a stake.
CASINO_GRID_COLUMNS = 3
CASINO_GRID_SIZE = CASINO_GRID_COLUMNS * CASINO_GRID_COLUMNS
CASINO_BOMB_COUNT = 1
CASINO_MAX_SAFE_PICKS = CASINO_GRID_SIZE - CASINO_BOMB_COUNT

# Version 1 is the historic linear x1.000 + x0.375 schedule through x4.
# Version 2 is the historic v15–v17 balanced table. Version 3 is the current
# gradual table: the first safe tile only protects the stake (x1.000), then
# payout rises slowly to x3.000 after all eight safe tiles.
CASINO_PAYOUT_VERSION_LINEAR = 1
CASINO_PAYOUT_VERSION_BALANCED = 2
CASINO_PAYOUT_VERSION_GRADUAL = 3
CASINO_CURRENT_PAYOUT_VERSION = CASINO_PAYOUT_VERSION_GRADUAL
CASINO_LEGACY_LINEAR_MAX_MULTIPLIER = Decimal("4.000")
CASINO_MAX_MULTIPLIER = Decimal("3.000")
# Kept as a version-1 compatibility value. Version 3 uses its exact table,
# rather than calculating a linear step from this value.
CASINO_SAFE_PICK_INCREMENT = (
    (CASINO_LEGACY_LINEAR_MAX_MULTIPLIER - Decimal("1.000"))
    / Decimal(CASINO_MAX_SAFE_PICKS)
)
CASINO_BALANCED_MULTIPLIERS = (
    Decimal("1.000"),  # zero safe picks: return the reserved stake unchanged
    Decimal("1.030"),
    Decimal("1.180"),
    Decimal("1.380"),
    Decimal("1.650"),
    Decimal("2.050"),
    Decimal("2.700"),
    Decimal("3.400"),
    Decimal("4.000"),
)
CASINO_GRADUAL_MULTIPLIERS = (
    Decimal("1.000"),  # no safe tiles: Get returns only the reserved stake
    Decimal("1.000"),  # first safe tile: no immediate profit
    Decimal("1.100"),
    Decimal("1.300"),
    Decimal("1.550"),
    Decimal("1.850"),
    Decimal("2.200"),
    Decimal("2.600"),
    Decimal("3.000"),  # all eight safe tiles: maximum x3 payout
)
# Every successful board start sets this user's next-start cooldown. There is
# no three-start batch and no five-minute pause.
CASINO_COOLDOWN_SECONDS = 70
CASINO_COOLDOWN_VERSION = 2
CASINO_MIN_STAKE = Decimal("50.00")
CASINO_MAX_STAKE = Decimal("2000.00")
DICE_ODD_EVEN_MULTIPLIER = Decimal("1.50")
DICE_EXACT_MULTIPLIER = Decimal("2.50")
DICE_CHOICES = frozenset({"odd", "even", "1", "2", "3", "4", "5", "6"})

# Decimal strings must be conventional, positive Neko values with at most two
# fractional digits. Scientific notation, signs, separators, and floats are
# rejected so money values cannot be accidentally rounded by binary float math.
_NEKO_AMOUNT_RE = re.compile(r"[0-9]{1,19}(?:\.[0-9]{1,2})?")


def _coerce_neko(value: object) -> Decimal | None:
    """Return an exact two-decimal Decimal or ``None`` for an invalid value."""
    if isinstance(value, bool):
        return None
    try:
        if isinstance(value, Decimal):
            parsed = value
        elif isinstance(value, int):
            parsed = Decimal(value)
        elif isinstance(value, str) and _NEKO_AMOUNT_RE.fullmatch(value.strip()):
            parsed = Decimal(value.strip())
        else:
            return None
    except (InvalidOperation, ValueError):
        return None

    if not parsed.is_finite():
        return None
    quantized = parsed.quantize(NEKO_QUANTUM, rounding=ROUND_HALF_UP)
    # Never silently alter caller input: a value with more than two meaningful
    # fractional digits is invalid rather than being unexpectedly rounded.
    if parsed != quantized:
        return None
    return quantized


def parse_neko_amount(value: object) -> Decimal | None:
    """Validate one positive amount that can safely be stored in a Neko wallet."""
    amount = _coerce_neko(value)
    if amount is None or amount <= ZERO_NEKO or amount > MAX_NEKO_AMOUNT:
        return None
    return amount


def valid_neko_amount(amount: object) -> bool:
    """Return whether an amount is positive, two-decimal, and safely storable."""
    return parse_neko_amount(amount) is not None


def parse_casino_stake(value: object) -> Decimal | None:
    """Accept only the requested 50–2,000 Neko Casino/Dice stake range."""
    stake = parse_neko_amount(value)
    if stake is None or not CASINO_MIN_STAKE <= stake <= CASINO_MAX_STAKE:
        return None
    return stake


def valid_neko_pass_days(days: int) -> bool:
    """Keep custom pass durations positive and bounded to ten years."""
    return (
        isinstance(days, int)
        and not isinstance(days, bool)
        and 0 < days <= MAX_NEKO_PASS_DAYS
    )


def format_neko_amount(value: object) -> str:
    """Render whole Neko compactly and retain meaningful decimal cents."""
    amount = _coerce_neko(value)
    if amount is None:
        amount = ZERO_NEKO
    rendered = f"{amount:.2f}"
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def _utc(value: datetime | None) -> datetime | None:
    """Normalize an asyncpg timestamp defensively for rolling-window checks."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _pass_is_active(expires_at: datetime | None, now: datetime) -> bool:
    expires = _utc(expires_at)
    return bool(expires and expires > now)


def _balance_from_row(row) -> Decimal:
    """Read a non-negative decimal balance even if a legacy row contains NULL."""
    amount = _coerce_neko(row["neko_balance"])
    return max(ZERO_NEKO, amount or ZERO_NEKO)


async def _ensure_wallet_user(
    conn,
    *,
    user_id: int,
    username: str | None = None,
    full_name: str | None = None,
) -> None:
    """Create a user row when needed and refresh a usable visible identity."""
    if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
        raise ValueError("invalid user id")

    supplied_name = (full_name or "").strip()
    # A target seen only as a numeric ID needs a NOT NULL insert value, but that
    # fallback must never overwrite an existing Telegram display name.
    insert_name = supplied_name or f"User {user_id}"
    visible_username = (username or "").strip() or None
    await conn.execute(
        """
        INSERT INTO users (user_id, username, full_name, last_seen_at)
        VALUES ($1, $2, $3, NOW())
        ON CONFLICT (user_id) DO UPDATE SET
            username = COALESCE(EXCLUDED.username, users.username),
            full_name = CASE
                WHEN BTRIM($4) <> '' THEN $4
                ELSE users.full_name
            END,
            last_seen_at = NOW()
        """,
        user_id,
        visible_username,
        insert_name,
        supplied_name,
    )


async def _record_wallet_ledger(
    conn,
    *,
    user_id: int,
    delta: Decimal,
    balance_after: Decimal,
    reason: str,
    related_user_id: int | None = None,
    reference_id: int | None = None,
) -> None:
    """Keep one compact audit row for every committed wallet balance mutation."""
    await conn.execute(
        """
        INSERT INTO neko_wallet_ledger (
            user_id, delta, balance_after, reason, related_user_id, reference_id
        )
        VALUES ($1, $2, $3, $4, $5, $6)
        """,
        user_id,
        delta,
        balance_after,
        reason,
        related_user_id,
        reference_id,
    )


def _as_utc_date(value: object) -> date | None:
    """Normalize PostgreSQL DATE/timestamp values without trusting a local zone."""
    if isinstance(value, datetime):
        return _utc(value).date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


async def _get_neko_streak_row_for_update(conn, *, user_id: int):
    """Create and row-lock one player's durable UTC streak state."""
    await conn.execute(
        """
        INSERT INTO neko_daily_streaks (user_id)
        VALUES ($1)
        ON CONFLICT (user_id) DO NOTHING
        """,
        user_id,
    )
    row = await conn.fetchrow(
        """
        SELECT
            streak_count,
            last_claim_date,
            day3_rewarded_at,
            day7_rewarded_at,
            day30_badge_unlocked_at
        FROM neko_daily_streaks
        WHERE user_id = $1
        FOR UPDATE
        """,
        user_id,
    )
    if not row:
        raise RuntimeError("daily Neko streak row was not created")
    return row


def _current_utc_streak_count(
    *,
    stored_count: object,
    last_claim_date: object,
    current_date: date,
) -> int:
    """Return zero once one complete UTC calendar date was missed."""
    try:
        count = max(0, int(stored_count or 0))
    except (TypeError, ValueError):
        count = 0
    last_date = _as_utc_date(last_claim_date)
    if last_date is None:
        return 0
    # A claim today or yesterday remains live. A future date is defensive
    # malformed-data handling: preserve the stored count rather than grant a
    # duplicate milestone because of a clock/admin anomaly.
    return count if last_date >= current_date - timedelta(days=1) else 0


def _prepare_neko_daily_streak_update(streak, *, current_time: datetime) -> dict:
    """Calculate a row-locked successful claim's next state without writing it."""
    current_date = current_time.date()
    last_date = _as_utc_date(streak["last_claim_date"])
    stored_count = max(0, int(streak["streak_count"] or 0))
    if last_date == current_date:
        # The normal 24-hour cooldown makes this unreachable, but retaining the
        # count is safer than treating a malformed direct call as a new day.
        new_count = stored_count
    elif last_date == current_date - timedelta(days=1):
        new_count = stored_count + 1
    else:
        new_count = 1

    day3_already_rewarded = streak["day3_rewarded_at"] is not None
    day7_already_rewarded = streak["day7_rewarded_at"] is not None
    badge_already_unlocked = streak["day30_badge_unlocked_at"] is not None
    bonus = ZERO_NEKO
    bonus_day: int | None = None
    if new_count == NEKO_STREAK_DAY3 and not day3_already_rewarded:
        bonus = NEKO_STREAK_DAY3_REWARD
        bonus_day = NEKO_STREAK_DAY3
    elif new_count == NEKO_STREAK_DAY7 and not day7_already_rewarded:
        bonus = NEKO_STREAK_DAY7_REWARD
        bonus_day = NEKO_STREAK_DAY7

    badge_unlocked = new_count == NEKO_STREAK_DAY30 and not badge_already_unlocked
    return {
        "streak_count": new_count,
        "last_claim_date": current_date,
        "bonus": bonus,
        "bonus_day": bonus_day,
        "badge_unlocked": badge_unlocked,
        "badge_key": NEKO_STREAK_DAY30_BADGE_KEY if badge_unlocked else None,
        "day3_rewarded_at": (
            current_time if bonus_day == NEKO_STREAK_DAY3 else streak["day3_rewarded_at"]
        ),
        "day7_rewarded_at": (
            current_time if bonus_day == NEKO_STREAK_DAY7 else streak["day7_rewarded_at"]
        ),
        "day30_badge_unlocked_at": (
            current_time if badge_unlocked else streak["day30_badge_unlocked_at"]
        ),
    }


async def _save_neko_daily_streak_update(
    conn,
    *,
    user_id: int,
    update: dict,
    current_time: datetime,
) -> None:
    """Persist a prepared streak result while the user/streak rows are locked."""
    await conn.execute(
        """
        UPDATE neko_daily_streaks
        SET streak_count = $2,
            last_claim_date = $3,
            day3_rewarded_at = $4,
            day7_rewarded_at = $5,
            day30_badge_unlocked_at = $6,
            updated_at = $7
        WHERE user_id = $1
        """,
        user_id,
        int(update["streak_count"]),
        update["last_claim_date"],
        update["day3_rewarded_at"],
        update["day7_rewarded_at"],
        update["day30_badge_unlocked_at"],
        current_time,
    )


async def _advance_neko_daily_streak_in_transaction(
    conn,
    *,
    user_id: int,
    current_time: datetime,
    streak_row=None,
    prepared_update: dict | None = None,
) -> dict:
    """Advance only a successful `/dailyneko` claim's UTC-date streak.

    The optional pre-locked/prepared arguments let the caller validate that a
    milestone bonus fits the wallet before changing either durable state.
    """
    row = streak_row if streak_row is not None else await _get_neko_streak_row_for_update(
        conn,
        user_id=user_id,
    )
    update = prepared_update or _prepare_neko_daily_streak_update(
        row,
        current_time=current_time,
    )
    await _save_neko_daily_streak_update(
        conn,
        user_id=user_id,
        update=update,
        current_time=current_time,
    )
    return update

def _neko_streak_snapshot_from_row(row, *, current_date: date) -> dict:
    """Render durable streak state without retaining a missed UTC day."""
    if not row:
        raw_count = 0
        last_date = None
        day3_rewarded = False
        day7_rewarded = False
        badge_unlocked = False
    else:
        raw_count = max(0, int(row["streak_count"] or 0))
        last_date = _as_utc_date(row["last_claim_date"])
        day3_rewarded = row["day3_rewarded_at"] is not None
        day7_rewarded = row["day7_rewarded_at"] is not None
        badge_unlocked = row["day30_badge_unlocked_at"] is not None
    current_count = _current_utc_streak_count(
        stored_count=raw_count,
        last_claim_date=last_date,
        current_date=current_date,
    )
    milestones = {
        "day3": {
            "day": NEKO_STREAK_DAY3,
            "reward": NEKO_STREAK_DAY3_REWARD,
            "claimed": day3_rewarded,
        },
        "day7": {
            "day": NEKO_STREAK_DAY7,
            "reward": NEKO_STREAK_DAY7_REWARD,
            "claimed": day7_rewarded,
        },
        "day30": {
            "day": NEKO_STREAK_DAY30,
            "badge_key": NEKO_STREAK_DAY30_BADGE_KEY,
            "badge_name": NEKO_STREAK_DAY30_BADGE_NAME,
            "claimed": badge_unlocked,
        },
    }
    next_milestone = next((
        data for key, data in milestones.items()
        if not data["claimed"]
    ), None)
    return {
        "streak_count": current_count,
        "last_claim_date": last_date,
        "streak_was_reset": bool(raw_count and current_count == 0),
        "milestones": milestones,
        "next_milestone": next_milestone,
        "badge_unlocked": badge_unlocked,
    }


async def get_neko_streak_snapshot(
    *,
    user_id: int,
    username: str | None = None,
    full_name: str | None = None,
    now: datetime | None = None,
) -> dict:
    """Return one player's effective current UTC daily-Neko streak and badge."""
    current_time = utc_now(now)
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _ensure_wallet_user(
                conn,
                user_id=user_id,
                username=username,
                full_name=full_name,
            )
            row = await conn.fetchrow(
                """
                SELECT streak_count, last_claim_date, day3_rewarded_at,
                       day7_rewarded_at, day30_badge_unlocked_at
                FROM neko_daily_streaks
                WHERE user_id = $1
                """,
                user_id,
            )
    return _neko_streak_snapshot_from_row(row, current_date=current_time.date())


async def get_neko_weekly_quest_snapshot(
    *,
    user_id: int,
    username: str | None = None,
    full_name: str | None = None,
    now: datetime | None = None,
) -> dict:
    """Return/create one user's Monday-to-Monday UTC Neko-quest panel state."""
    current_time = utc_now(now)
    week_start = utc_week_start(current_time)
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _ensure_wallet_user(
                conn,
                user_id=user_id,
                username=username,
                full_name=full_name,
            )
            row = await get_weekly_quest_row_for_update(
                conn,
                user_id=user_id,
                week_start=week_start,
            )
    return weekly_quest_snapshot_from_row(row)


async def claim_weekly_neko_quest_reward(
    *,
    user_id: int,
    quest_key: str,
    week_start: date,
    username: str | None = None,
    full_name: str | None = None,
    now: datetime | None = None,
) -> dict:
    """Claim exactly one earned current-week quest reward, wallet-lock safely."""
    if quest_key not in {*WEEKLY_QUEST_ORDER, WEEKLY_QUEST_COMPLETION}:
        return {"success": False, "reason": "invalid_quest"}
    if isinstance(week_start, datetime) or not isinstance(week_start, date):
        return {"success": False, "reason": "invalid_week"}

    current_time = utc_now(now)
    current_week_start = utc_week_start(current_time)
    if week_start != current_week_start:
        return {"success": False, "reason": "expired_week", "week_start": current_week_start}

    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _ensure_wallet_user(
                conn,
                user_id=user_id,
                username=username,
                full_name=full_name,
            )
            wallet = await conn.fetchrow(
                "SELECT neko_balance FROM users WHERE user_id = $1 FOR UPDATE",
                user_id,
            )
            if not wallet:
                raise RuntimeError("wallet user row was not created")
            quest_row = await get_weekly_quest_row_for_update(
                conn,
                user_id=user_id,
                week_start=current_week_start,
            )
            snapshot = weekly_quest_snapshot_from_row(quest_row)
            completed = (
                snapshot["all_completed"]
                if quest_key == WEEKLY_QUEST_COMPLETION
                else snapshot["completed"][quest_key]
            )
            if not completed:
                return {"success": False, "reason": "incomplete", "snapshot": snapshot}
            if snapshot["claimed"][quest_key]:
                return {"success": False, "reason": "already_claimed", "snapshot": snapshot}

            prize = Decimal(WEEKLY_QUEST_REWARDS[quest_key]).quantize(NEKO_QUANTUM)
            balance_before = _balance_from_row(wallet)
            if balance_before > MAX_NEKO_BALANCE - prize:
                return {
                    "success": False,
                    "reason": "balance_limit",
                    "balance": balance_before,
                    "snapshot": snapshot,
                }
            balance_after = balance_before + prize
            await conn.execute(
                "UPDATE users SET neko_balance = $2, last_seen_at = NOW() WHERE user_id = $1",
                user_id,
                balance_after,
            )
            claim_column = WEEKLY_QUEST_CLAIM_COLUMNS[quest_key]
            # This is a closed constant mapping, never a callback-provided SQL name.
            await conn.execute(
                f"""
                UPDATE neko_weekly_quests
                SET {claim_column} = $3,
                    updated_at = $3
                WHERE user_id = $1 AND week_start = $2
                """,
                user_id,
                current_week_start,
                current_time,
            )
            await _record_wallet_ledger(
                conn,
                user_id=user_id,
                delta=prize,
                balance_after=balance_after,
                reason=f"weekly_quest_{quest_key}_reward",
            )
            return {
                "success": True,
                "reason": "ok",
                "quest_key": quest_key,
                "reward": prize,
                "balance": balance_after,
                "week_start": current_week_start,
            }


async def get_neko_wallet(
    *,
    user_id: int,
    username: str | None = None,
    full_name: str | None = None,
) -> dict:
    """Return one user's persisted wallet plus a deterministic balance rank."""
    now = datetime.now(timezone.utc)
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _ensure_wallet_user(
                conn,
                user_id=user_id,
                username=username,
                full_name=full_name,
            )
            row = await conn.fetchrow(
                """
                SELECT
                    wallet.user_id,
                    wallet.username,
                    wallet.full_name,
                    wallet.neko_balance,
                    wallet.neko_pass_expires_at,
                    1 + (
                        SELECT COUNT(*)
                        FROM users AS ranked
                        WHERE ranked.neko_balance > wallet.neko_balance
                           OR (
                                ranked.neko_balance = wallet.neko_balance
                                AND ranked.user_id < wallet.user_id
                           )
                    ) AS wallet_rank
                FROM users AS wallet
                WHERE wallet.user_id = $1
                """,
                user_id,
            )

    if not row:  # The UPSERT above makes this a defensive unreachable guard.
        raise RuntimeError("wallet user row was not created")
    expires_at = _utc(row["neko_pass_expires_at"])
    return {
        "user_id": int(row["user_id"]),
        "username": str(row["username"] or ""),
        "full_name": str(row["full_name"] or f"User {user_id}"),
        "balance": _balance_from_row(row),
        "rank": int(row["wallet_rank"] or 1),
        "pass_active": _pass_is_active(expires_at, now),
        "pass_expires_at": expires_at,
    }


async def play_neko_game(
    *,
    user_id: int,
    username: str | None = None,
    full_name: str | None = None,
    dice_result: int | None = None,
) -> dict:
    """Legacy internal Neko-roll helper retained for historic compatibility.

    It has no public command registration. ``dice_result`` is internal/test-only
    and keeps offline arithmetic validation deterministic.
    """
    if dice_result is not None and (
        not isinstance(dice_result, int)
        or isinstance(dice_result, bool)
        or not 1 <= dice_result <= NEKO_GAME_DICE_SIDES
    ):
        raise ValueError("invalid dice result")

    now = datetime.now(timezone.utc)
    window = timedelta(seconds=NEKO_GAME_WINDOW_SECONDS)
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _ensure_wallet_user(
                conn,
                user_id=user_id,
                username=username,
                full_name=full_name,
            )
            row = await conn.fetchrow(
                """
                SELECT neko_balance, neko_game_window_started_at, neko_game_uses,
                       neko_pass_expires_at
                FROM users
                WHERE user_id = $1
                FOR UPDATE
                """,
                user_id,
            )
            if not row:
                raise RuntimeError("wallet user row was not created")

            window_started_at = _utc(row["neko_game_window_started_at"])
            current_uses = max(0, int(row["neko_game_uses"] or 0))
            if window_started_at is None or now >= window_started_at + window:
                window_started_at = now
                current_uses = 0

            pass_active = _pass_is_active(_utc(row["neko_pass_expires_at"]), now)
            use_limit = NEKO_GAME_PASS_LIMIT if pass_active else NEKO_GAME_STANDARD_LIMIT
            if current_uses >= use_limit:
                reset_at = window_started_at + window
                remaining_seconds = max(0, int((reset_at - now).total_seconds()))
                return {
                    "success": False,
                    "reason": "limit_reached",
                    "balance": _balance_from_row(row),
                    "remaining_uses": 0,
                    "use_limit": use_limit,
                    "remaining_seconds": remaining_seconds,
                    "pass_active": pass_active,
                }

            roll = dice_result if dice_result is not None else secrets.randbelow(NEKO_GAME_DICE_SIDES) + 1
            prize = Decimal(roll * NEKO_PER_DICE_POINT).quantize(NEKO_QUANTUM)
            old_balance = _balance_from_row(row)
            if old_balance > MAX_NEKO_BALANCE - prize:
                return {
                    "success": False,
                    "reason": "balance_limit",
                    "balance": old_balance,
                    "remaining_uses": use_limit - current_uses,
                    "use_limit": use_limit,
                    "remaining_seconds": 0,
                    "pass_active": pass_active,
                }

            new_balance = old_balance + prize
            new_uses = current_uses + 1
            await conn.execute(
                """
                UPDATE users
                SET neko_balance = $2,
                    neko_game_window_started_at = $3,
                    neko_game_uses = $4,
                    last_seen_at = NOW()
                WHERE user_id = $1
                """,
                user_id,
                new_balance,
                window_started_at,
                new_uses,
            )
            await _record_wallet_ledger(
                conn,
                user_id=user_id,
                delta=prize,
                balance_after=new_balance,
                reason="game_bonus",
            )
            return {
                "success": True,
                "reason": "ok",
                "roll": roll,
                "prize": prize,
                "balance": new_balance,
                "remaining_uses": use_limit - new_uses,
                "use_limit": use_limit,
                "remaining_seconds": 0,
                "pass_active": pass_active,
            }


async def claim_daily_neko_reward(
    *,
    user_id: int,
    username: str | None = None,
    full_name: str | None = None,
    now: datetime | None = None,
    reward: int | None = None,
) -> dict:
    """Credit one standalone secure 1–49 Neko reward per rolling 24 hours.

    The retired `/game`, `/nekostreak`, and `/nekoquests` paths no longer add
    side rewards or progress here. A successful claim changes only the wallet,
    its daily cooldown timestamp, and one auditable ledger row. ``now`` and
    ``reward`` are deterministic-test hooks; Telegram users cannot supply them.
    """
    if reward is not None and (
        not isinstance(reward, int)
        or isinstance(reward, bool)
        or not DAILY_NEKO_MIN_REWARD <= reward <= DAILY_NEKO_MAX_REWARD
    ):
        raise ValueError("invalid daily Neko reward")

    current_time = _utc(now) or datetime.now(timezone.utc)
    cooldown_window = timedelta(seconds=DAILY_NEKO_REWARD_INTERVAL_SECONDS)
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _ensure_wallet_user(
                conn,
                user_id=user_id,
                username=username,
                full_name=full_name,
            )
            wallet = await conn.fetchrow(
                """
                SELECT neko_balance, daily_neko_claimed_at
                FROM users
                WHERE user_id = $1
                FOR UPDATE
                """,
                user_id,
            )
            if not wallet:
                raise RuntimeError("wallet user row was not created")

            claimed_at = _utc(wallet["daily_neko_claimed_at"])
            if claimed_at is not None:
                next_available_at = claimed_at + cooldown_window
                if current_time < next_available_at:
                    return {
                        "success": False,
                        "reason": "cooldown",
                        "balance": _balance_from_row(wallet),
                        "remaining_seconds": max(
                            1,
                            ceil((next_available_at - current_time).total_seconds()),
                        ),
                        "next_available_at": next_available_at,
                    }

            reward_units = (
                reward
                if reward is not None
                else secrets.randbelow(DAILY_NEKO_MAX_REWARD) + DAILY_NEKO_MIN_REWARD
            )
            prize = Decimal(reward_units).quantize(NEKO_QUANTUM)
            balance_before = _balance_from_row(wallet)
            if balance_before > MAX_NEKO_BALANCE - prize:
                # The cooldown remains unused so the player can retry once
                # their wallet has room for the entire base daily reward.
                return {
                    "success": False,
                    "reason": "balance_limit",
                    "balance": balance_before,
                }

            balance_after = balance_before + prize
            await conn.execute(
                """
                UPDATE users
                SET neko_balance = $2,
                    daily_neko_claimed_at = $3,
                    last_seen_at = NOW()
                WHERE user_id = $1
                """,
                user_id,
                balance_after,
                current_time,
            )
            await _record_wallet_ledger(
                conn,
                user_id=user_id,
                delta=prize,
                balance_after=balance_after,
                reason="daily_neko_reward",
            )
            return {
                "success": True,
                "reason": "ok",
                "reward": prize,
                "balance": balance_after,
                "next_available_at": current_time + cooldown_window,
            }


async def create_neko_transfer(
    *,
    chat_id: int,
    sender_user_id: int,
    sender_username: str | None,
    sender_full_name: str | None,
    recipient_user_id: int,
    recipient_username: str | None,
    recipient_full_name: str | None,
    amount: object,
) -> dict:
    """Create a sender-confirmed Neko transfer after an available-balance check."""
    transfer_amount = parse_neko_amount(amount)
    if transfer_amount is None:
        return {"success": False, "reason": "invalid_amount"}
    if sender_user_id == recipient_user_id:
        return {"success": False, "reason": "self_transfer"}

    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Both UPSERTs may lock existing user rows. Use numeric order so
            # simultaneous opposite-direction requests cannot deadlock before
            # their pending transfer has been created.
            identities = {
                sender_user_id: (sender_username, sender_full_name),
                recipient_user_id: (recipient_username, recipient_full_name),
            }
            for target_user_id in sorted(identities):
                username, full_name = identities[target_user_id]
                await _ensure_wallet_user(
                    conn,
                    user_id=target_user_id,
                    username=username,
                    full_name=full_name,
                )
            sender = await conn.fetchrow(
                "SELECT neko_balance FROM users WHERE user_id = $1 FOR UPDATE",
                sender_user_id,
            )
            sender_balance = _balance_from_row(sender)
            if sender_balance < transfer_amount:
                return {
                    "success": False,
                    "reason": "insufficient_balance",
                    "balance": sender_balance,
                }
            request = await conn.fetchrow(
                """
                INSERT INTO neko_transfer_requests (
                    chat_id, from_user_id, to_user_id, amount
                )
                VALUES ($1, $2, $3, $4)
                RETURNING id
                """,
                chat_id,
                sender_user_id,
                recipient_user_id,
                transfer_amount,
            )
            return {
                "success": True,
                "reason": "ok",
                "transfer_id": int(request["id"]),
                "balance": sender_balance,
            }


async def confirm_neko_transfer(*, transfer_id: int, sender_user_id: int) -> dict:
    """Atomically re-check and apply the sender-bound pending transfer."""
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            transfer = await conn.fetchrow(
                "SELECT * FROM neko_transfer_requests WHERE id = $1 FOR UPDATE",
                transfer_id,
            )
            if not transfer:
                return {"success": False, "reason": "not_found"}
            if int(transfer["from_user_id"]) != sender_user_id:
                return {"success": False, "reason": "not_yours"}
            if str(transfer["status"]) != "pending":
                return {"success": False, "reason": "not_pending"}

            recipient_user_id = int(transfer["to_user_id"])
            transfer_amount = parse_neko_amount(transfer["amount"])
            if transfer_amount is None:
                await conn.execute(
                    "UPDATE neko_transfer_requests SET status = 'failed', resolved_at = NOW() "
                    "WHERE id = $1",
                    transfer_id,
                )
                return {"success": False, "reason": "invalid_amount"}

            # Lock wallet rows in numeric order, so two opposite-direction
            # confirmations cannot deadlock each other.
            locked_rows: dict[int, object] = {}
            for target_user_id in sorted((sender_user_id, recipient_user_id)):
                row = await conn.fetchrow(
                    "SELECT neko_balance FROM users WHERE user_id = $1 FOR UPDATE",
                    target_user_id,
                )
                if not row:
                    await conn.execute(
                        "UPDATE neko_transfer_requests SET status = 'failed', resolved_at = NOW() "
                        "WHERE id = $1",
                        transfer_id,
                    )
                    return {"success": False, "reason": "wallet_missing"}
                locked_rows[target_user_id] = row

            sender_balance = _balance_from_row(locked_rows[sender_user_id])
            recipient_balance = _balance_from_row(locked_rows[recipient_user_id])
            if sender_balance < transfer_amount:
                await conn.execute(
                    "UPDATE neko_transfer_requests SET status = 'failed', resolved_at = NOW() "
                    "WHERE id = $1",
                    transfer_id,
                )
                return {
                    "success": False,
                    "reason": "insufficient_balance",
                    "balance": sender_balance,
                }
            if recipient_balance > MAX_NEKO_BALANCE - transfer_amount:
                await conn.execute(
                    "UPDATE neko_transfer_requests SET status = 'failed', resolved_at = NOW() "
                    "WHERE id = $1",
                    transfer_id,
                )
                return {"success": False, "reason": "recipient_balance_limit"}

            sender_after = sender_balance - transfer_amount
            recipient_after = recipient_balance + transfer_amount
            await conn.execute(
                "UPDATE users SET neko_balance = $2, last_seen_at = NOW() WHERE user_id = $1",
                sender_user_id,
                sender_after,
            )
            await conn.execute(
                "UPDATE users SET neko_balance = $2, last_seen_at = NOW() WHERE user_id = $1",
                recipient_user_id,
                recipient_after,
            )
            await _record_wallet_ledger(
                conn,
                user_id=sender_user_id,
                delta=-transfer_amount,
                balance_after=sender_after,
                reason="transfer_sent",
                related_user_id=recipient_user_id,
                reference_id=transfer_id,
            )
            await _record_wallet_ledger(
                conn,
                user_id=recipient_user_id,
                delta=transfer_amount,
                balance_after=recipient_after,
                reason="transfer_received",
                related_user_id=sender_user_id,
                reference_id=transfer_id,
            )
            await conn.execute(
                "UPDATE neko_transfer_requests SET status = 'accepted', resolved_at = NOW() "
                "WHERE id = $1",
                transfer_id,
            )
            return {
                "success": True,
                "reason": "ok",
                "amount": transfer_amount,
                "sender_balance": sender_after,
                "recipient_balance": recipient_after,
                "recipient_user_id": recipient_user_id,
            }


async def cancel_neko_transfer(*, transfer_id: int, sender_user_id: int) -> bool:
    """Cancel only a still-pending request created by the user who pressed it."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE neko_transfer_requests
            SET status = 'cancelled', resolved_at = NOW()
            WHERE id = $1 AND from_user_id = $2 AND status = 'pending'
            RETURNING id
            """,
            transfer_id,
            sender_user_id,
        )
    return bool(row)


async def get_latest_neko_wallet_backup(*, user_id: int) -> dict | None:
    """Return the newest unused /deletewallet balance snapshot for a user."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, user_id, deleted_by, original_balance, deleted_at
            FROM neko_wallet_deletion_backups
            WHERE user_id = $1 AND restored_at IS NULL
            ORDER BY deleted_at DESC, id DESC
            LIMIT 1
            """,
            user_id,
        )
    return dict(row) if row else None


async def reset_neko_wallet(*, user_id: int, reset_by: int) -> dict:
    """Save the available balance, cancel active Casino state, and reset wallet."""
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _ensure_wallet_user(conn, user_id=user_id)
            row = await conn.fetchrow(
                "SELECT neko_balance FROM users WHERE user_id = $1 FOR UPDATE",
                user_id,
            )
            old_balance = _balance_from_row(row)
            # Casino stake was already removed from the available wallet at
            # game creation. A full wallet reset must not leave a stale Get
            # button that could later restore money, so active games for this
            # user are forfeited in the same user-lock transaction.
            cancelled_games = await conn.fetch(
                """
                UPDATE neko_casino_games
                SET status = 'cancelled', resolved_at = NOW(), payout = NULL
                WHERE user_id = $1 AND status = 'active'
                RETURNING id
                """,
                user_id,
            )
            cancelled_dice_bets = await conn.fetch(
                """
                UPDATE neko_dice_bets
                SET status = 'cancelled', resolved_at = NOW(), payout = NULL
                WHERE user_id = $1 AND status IN ('selecting', 'awaiting_roll')
                RETURNING id
                """,
                user_id,
            )
            await conn.execute(
                "DELETE FROM neko_casino_input_sessions WHERE user_id = $1",
                user_id,
            )
            backup = await conn.fetchrow(
                """
                INSERT INTO neko_wallet_deletion_backups (
                    user_id, deleted_by, original_balance
                )
                VALUES ($1, $2, $3)
                RETURNING id
                """,
                user_id,
                reset_by,
                old_balance,
            )
            await conn.execute(
                "UPDATE users SET neko_balance = 0.00, last_seen_at = NOW() WHERE user_id = $1",
                user_id,
            )
            await _record_wallet_ledger(
                conn,
                user_id=user_id,
                delta=-old_balance,
                balance_after=ZERO_NEKO,
                reason="owner_wallet_reset",
                related_user_id=reset_by,
                reference_id=int(backup["id"]),
            )
            return {
                "backup_id": int(backup["id"]),
                "old_balance": old_balance,
                "new_balance": ZERO_NEKO,
                "cancelled_casino_games": len(cancelled_games),
                "cancelled_dice_bets": len(cancelled_dice_bets),
            }


async def reset_all_neko_wallets(*, reset_by: int) -> dict:
    """Zero every funded Neko wallet and cancel active Mines/Dice Casino state.

    The action is deliberately owner-driven and global.  It records one ledger
    entry per non-zero wallet so the balance mutation remains auditable, but it
    does not create per-player restore backups: a global reset is an explicit,
    separately confirmed economy action.
    """
    if not isinstance(reset_by, int) or isinstance(reset_by, bool) or reset_by <= 0:
        raise ValueError("invalid reset owner")

    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            summary = await conn.fetchrow(
                """
                WITH locked_wallets AS (
                    -- Lock every wallet, including a zero-balance player with
                    -- a pending Casino stake. That makes this root action a
                    -- clean boundary against a concurrent Dice/Mines payout.
                    SELECT user_id, neko_balance AS old_balance
                    FROM users
                    ORDER BY user_id
                    FOR UPDATE
                ),
                reset_wallets AS (
                    UPDATE users AS wallet
                    SET neko_balance = 0.00
                    FROM locked_wallets AS locked
                    WHERE wallet.user_id = locked.user_id
                      AND locked.old_balance > 0.00
                    RETURNING wallet.user_id
                ),
                ledger_entries AS (
                    INSERT INTO neko_wallet_ledger (
                        user_id, delta, balance_after, reason, related_user_id
                    )
                    SELECT
                        locked.user_id,
                        -locked.old_balance,
                        0.00,
                        'owner_global_neko_reset',
                        $1
                    FROM locked_wallets AS locked
                    JOIN reset_wallets AS reset ON reset.user_id = locked.user_id
                )
                SELECT
                    COUNT(*)::INT AS reset_user_count,
                    COALESCE(SUM(old_balance), 0.00) AS removed_balance
                FROM locked_wallets
                """,
                reset_by,
            )
            cancelled_games = await conn.fetch(
                """
                UPDATE neko_casino_games
                SET status = 'cancelled', resolved_at = NOW(), payout = NULL
                WHERE status = 'active'
                RETURNING id
                """
            )
            cancelled_dice_bets = await conn.fetch(
                """
                UPDATE neko_dice_bets
                SET status = 'cancelled', resolved_at = NOW(), payout = NULL
                WHERE status IN ('selecting', 'awaiting_roll')
                RETURNING id
                """
            )
            await conn.execute("DELETE FROM neko_casino_input_sessions")

    return {
        "reset_user_count": int(summary["reset_user_count"] or 0) if summary else 0,
        "removed_balance": (_coerce_neko(summary["removed_balance"]) or ZERO_NEKO)
        if summary else ZERO_NEKO,
        "cancelled_casino_games": len(cancelled_games),
        "cancelled_dice_bets": len(cancelled_dice_bets),
    }


async def restore_neko_wallet_backup(*, backup_id: int, restored_by: int) -> dict:
    """Restore one unused snapshot by replacing, never adding to, a wallet."""
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            backup = await conn.fetchrow(
                """
                SELECT id, user_id, original_balance
                FROM neko_wallet_deletion_backups
                WHERE id = $1 AND restored_at IS NULL
                FOR UPDATE
                """,
                backup_id,
            )
            if not backup:
                return {"success": False, "reason": "not_found_or_restored"}

            user_id = int(backup["user_id"])
            wallet = await conn.fetchrow(
                "SELECT neko_balance FROM users WHERE user_id = $1 FOR UPDATE",
                user_id,
            )
            if not wallet:
                return {"success": False, "reason": "wallet_missing"}

            old_balance = _balance_from_row(wallet)
            restored_balance = _coerce_neko(backup["original_balance"])
            if restored_balance is None or restored_balance < ZERO_NEKO:
                return {"success": False, "reason": "invalid_backup"}
            await conn.execute(
                "UPDATE users SET neko_balance = $2, last_seen_at = NOW() WHERE user_id = $1",
                user_id,
                restored_balance,
            )
            await conn.execute(
                """
                UPDATE neko_wallet_deletion_backups
                SET restored_by = $2, restored_at = NOW()
                WHERE id = $1
                """,
                backup_id,
                restored_by,
            )
            await _record_wallet_ledger(
                conn,
                user_id=user_id,
                delta=restored_balance - old_balance,
                balance_after=restored_balance,
                reason="owner_wallet_restore",
                related_user_id=restored_by,
                reference_id=backup_id,
            )
            return {
                "success": True,
                "reason": "ok",
                "user_id": user_id,
                "old_balance": old_balance,
                "restored_balance": restored_balance,
            }


async def adjust_neko_wallet(
    *,
    user_id: int,
    username: str | None,
    full_name: str | None,
    amount: object,
    adjusted_by: int,
    add: bool,
) -> dict:
    """Owner-only exact grant or floor-at-zero deduction for a target wallet."""
    adjustment = parse_neko_amount(amount)
    if adjustment is None:
        return {"success": False, "reason": "invalid_amount"}

    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _ensure_wallet_user(
                conn,
                user_id=user_id,
                username=username,
                full_name=full_name,
            )
            row = await conn.fetchrow(
                "SELECT neko_balance FROM users WHERE user_id = $1 FOR UPDATE",
                user_id,
            )
            old_balance = _balance_from_row(row)
            if add:
                if old_balance > MAX_NEKO_BALANCE - adjustment:
                    return {
                        "success": False,
                        "reason": "balance_limit",
                        "balance": old_balance,
                    }
                new_balance = old_balance + adjustment
                delta = adjustment
                reason = "owner_neko_grant"
            else:
                new_balance = max(ZERO_NEKO, old_balance - adjustment)
                delta = new_balance - old_balance
                reason = "owner_neko_deduction"

            await conn.execute(
                "UPDATE users SET neko_balance = $2, last_seen_at = NOW() WHERE user_id = $1",
                user_id,
                new_balance,
            )
            await _record_wallet_ledger(
                conn,
                user_id=user_id,
                delta=delta,
                balance_after=new_balance,
                reason=reason,
                related_user_id=adjusted_by,
            )
            return {
                "success": True,
                "reason": "ok",
                "old_balance": old_balance,
                "new_balance": new_balance,
                # For an /doomneko request larger than the wallet, this exposes
                # the real deduction rather than falsely reporting its input.
                "changed_amount": abs(delta),
            }


async def grant_neko_pass(
    *,
    user_id: int,
    username: str | None,
    full_name: str | None,
    days: int,
    granted_by: int,
) -> dict:
    """Activate or extend a Neko Pass from its current active expiry."""
    if not valid_neko_pass_days(days):
        return {"success": False, "reason": "invalid_days"}

    now = datetime.now(timezone.utc)
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _ensure_wallet_user(
                conn,
                user_id=user_id,
                username=username,
                full_name=full_name,
            )
            row = await conn.fetchrow(
                "SELECT neko_pass_expires_at FROM users WHERE user_id = $1 FOR UPDATE",
                user_id,
            )
            previous_expiry = _utc(row["neko_pass_expires_at"])
            was_active = _pass_is_active(previous_expiry, now)
            base = previous_expiry if was_active and previous_expiry else now
            expires_at = base + timedelta(days=days)
            await conn.execute(
                """
                UPDATE users
                SET neko_pass_expires_at = $2, last_seen_at = NOW()
                WHERE user_id = $1
                """,
                user_id,
                expires_at,
            )
            await conn.execute(
                """
                INSERT INTO neko_pass_grants (
                    user_id, granted_by, days, previous_expires_at, expires_at
                )
                VALUES ($1, $2, $3, $4, $5)
                """,
                user_id,
                granted_by,
                days,
                previous_expiry,
                expires_at,
            )
            return {
                "success": True,
                "reason": "ok",
                "days": days,
                "was_active": was_active,
                "expires_at": expires_at,
                "granted_by": granted_by,
            }


async def start_neko_casino_input_session(
    *,
    user_id: int,
    username: str | None,
    full_name: str | None,
    chat_id: int,
    prompt_message_id: int,
    mode: str,
) -> None:
    """Persist one reply-bound Mine or Dice value-entry prompt."""
    if mode not in {"mine", "dice"}:
        raise ValueError("invalid casino input mode")
    if prompt_message_id <= 0:
        raise ValueError("invalid casino prompt message id")

    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _ensure_wallet_user(
                conn,
                user_id=user_id,
                username=username,
                full_name=full_name,
            )
            await conn.execute(
                """
                INSERT INTO neko_casino_input_sessions (
                    user_id, chat_id, prompt_message_id, mode
                )
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (user_id) DO UPDATE SET
                    chat_id = EXCLUDED.chat_id,
                    prompt_message_id = EXCLUDED.prompt_message_id,
                    mode = EXCLUDED.mode,
                    created_at = NOW()
                """,
                user_id,
                chat_id,
                prompt_message_id,
                mode,
            )


async def get_neko_casino_input_session(
    *,
    user_id: int,
    chat_id: int,
    prompt_message_id: int,
) -> str | None:
    """Return the mode only for the exact player/chat/prompt reply target."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT mode
            FROM neko_casino_input_sessions
            WHERE user_id = $1 AND chat_id = $2 AND prompt_message_id = $3
            """,
            user_id,
            chat_id,
            prompt_message_id,
        )
    return str(row["mode"]) if row else None


async def clear_neko_casino_input_session(
    *,
    user_id: int,
    chat_id: int,
    prompt_message_id: int,
) -> None:
    """Consume one completed Mine/Dice amount-entry session exactly once."""
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            DELETE FROM neko_casino_input_sessions
            WHERE user_id = $1 AND chat_id = $2 AND prompt_message_id = $3
            """,
            user_id,
            chat_id,
            prompt_message_id,
        )


def _casino_cooldown_remaining(wallet: object, now: datetime) -> int:
    """Read the current 70-second start cooldown from a locked wallet row."""
    version = int(wallet["casino_cooldown_version"] or 1)
    until = (
        _utc(wallet["casino_cooldown_until"])
        if version == CASINO_COOLDOWN_VERSION
        else None
    )
    if until and now < until:
        return max(1, ceil((until - now).total_seconds()))
    return 0


async def create_neko_dice_bet(
    *,
    user_id: int,
    username: str | None,
    full_name: str | None,
    chat_id: int,
    stake: object,
) -> dict:
    """Store a requested Dice stake before the player selects a prediction.

    No Neko is reserved at this point.  It is reserved atomically only when an
    Odd/Even/face button is selected, so an abandoned choice cannot lock money.
    """
    wager = parse_casino_stake(stake)
    if wager is None:
        return {"success": False, "reason": "invalid_amount"}

    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _ensure_wallet_user(
                conn,
                user_id=user_id,
                username=username,
                full_name=full_name,
            )
            await conn.fetchrow(
                "SELECT neko_balance FROM users WHERE user_id = $1 FOR UPDATE",
                user_id,
            )
            active_mines = await conn.fetchrow(
                """
                SELECT id FROM neko_casino_games
                WHERE user_id = $1 AND status = 'active'
                FOR UPDATE
                """,
                user_id,
            )
            if active_mines:
                return {"success": False, "reason": "active_game"}
            active_dice = await conn.fetchrow(
                """
                SELECT id, status FROM neko_dice_bets
                WHERE user_id = $1 AND status IN ('selecting', 'awaiting_roll')
                FOR UPDATE
                """,
                user_id,
            )
            if active_dice and str(active_dice["status"]) == "awaiting_roll":
                return {"success": False, "reason": "active_dice"}
            if active_dice:
                # A pre-choice record has no reserved Neko.  Replacing it lets
                # a player recover from an abandoned button menu without an
                # owner reset, while making stale old buttons harmless.
                await conn.execute(
                    """
                    UPDATE neko_dice_bets
                    SET status = 'cancelled', resolved_at = NOW(), payout = NULL
                    WHERE id = $1 AND status = 'selecting'
                    """,
                    int(active_dice["id"]),
                )
            bet = await conn.fetchrow(
                """
                INSERT INTO neko_dice_bets (user_id, chat_id, stake)
                VALUES ($1, $2, $3)
                RETURNING id
                """,
                user_id,
                chat_id,
                wager,
            )
            return {
                "success": True,
                "reason": "ok",
                "bet_id": int(bet["id"]),
                "stake": wager,
            }


async def select_neko_dice_bet(
    *,
    bet_id: int,
    user_id: int,
    chat_id: int,
    choice: str,
    roll_prompt_message_id: int,
    now: datetime | None = None,
) -> dict:
    """Reserve a Dice stake and bind its selected prediction to one prompt."""
    if choice not in DICE_CHOICES:
        return {"success": False, "reason": "invalid_choice"}
    if not isinstance(chat_id, int) or isinstance(chat_id, bool):
        return {"success": False, "reason": "invalid_chat"}
    if not isinstance(roll_prompt_message_id, int) or roll_prompt_message_id <= 0:
        return {"success": False, "reason": "invalid_prompt"}

    current_time = _utc(now) or datetime.now(timezone.utc)
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            wallet = await conn.fetchrow(
                """
                SELECT neko_balance, casino_cooldown_until, casino_cooldown_version
                FROM users
                WHERE user_id = $1
                FOR UPDATE
                """,
                user_id,
            )
            if not wallet:
                return {"success": False, "reason": "wallet_missing"}
            bet = await conn.fetchrow(
                """
                SELECT id, user_id, stake, status
                FROM neko_dice_bets
                WHERE id = $1 AND user_id = $2 AND chat_id = $3
                FOR UPDATE
                """,
                bet_id,
                user_id,
                chat_id,
            )
            if not bet:
                return {"success": False, "reason": "not_found"}
            if str(bet["status"]) != "selecting":
                return {"success": False, "reason": "not_selecting"}
            active_mines = await conn.fetchrow(
                """
                SELECT id FROM neko_casino_games
                WHERE user_id = $1 AND status = 'active'
                FOR UPDATE
                """,
                user_id,
            )
            if active_mines:
                return {"success": False, "reason": "active_game"}

            cooldown_remaining = _casino_cooldown_remaining(wallet, current_time)
            available_balance = _balance_from_row(wallet)
            if cooldown_remaining:
                return {
                    "success": False,
                    "reason": "cooldown",
                    "remaining_seconds": cooldown_remaining,
                    "balance": available_balance,
                }

            wager = _coerce_neko(bet["stake"])
            if wager is None or not CASINO_MIN_STAKE <= wager <= CASINO_MAX_STAKE:
                return {"success": False, "reason": "invalid_amount"}
            if available_balance < wager:
                return {
                    "success": False,
                    "reason": "insufficient_balance",
                    "balance": available_balance,
                }

            multiplier = DICE_EXACT_MULTIPLIER if choice.isdigit() else DICE_ODD_EVEN_MULTIPLIER
            potential_payout = (wager * multiplier).quantize(NEKO_QUANTUM, rounding=ROUND_HALF_UP)
            available_after = available_balance - wager
            if available_after > MAX_NEKO_BALANCE - potential_payout:
                return {
                    "success": False,
                    "reason": "balance_limit",
                    "balance": available_balance,
                }

            cooldown_until = current_time + timedelta(seconds=CASINO_COOLDOWN_SECONDS)
            await conn.execute(
                """
                UPDATE users
                SET neko_balance = $2,
                    casino_starts_since_cooldown = 0,
                    casino_cooldown_until = $3,
                    casino_cooldown_version = $4,
                    last_seen_at = NOW()
                WHERE user_id = $1
                """,
                user_id,
                available_after,
                cooldown_until,
                CASINO_COOLDOWN_VERSION,
            )
            await conn.execute(
                """
                UPDATE neko_dice_bets
                SET choice = $3,
                    status = 'awaiting_roll',
                    roll_prompt_message_id = $4,
                    selected_at = NOW()
                WHERE id = $1 AND user_id = $2 AND status = 'selecting'
                """,
                bet_id,
                user_id,
                choice,
                roll_prompt_message_id,
            )
            await _record_wallet_ledger(
                conn,
                user_id=user_id,
                delta=-wager,
                balance_after=available_after,
                reason="casino_dice_stake",
                reference_id=bet_id,
            )
            return {
                "success": True,
                "reason": "ok",
                "stake": wager,
                "choice": choice,
                "balance": available_after,
            }


async def settle_neko_dice_bet(
    *,
    user_id: int,
    chat_id: int,
    roll_prompt_message_id: int,
    dice_value: int,
) -> dict:
    """Settle one reply-bound Telegram Dice result against its stored choice."""
    if not isinstance(dice_value, int) or isinstance(dice_value, bool) or not 1 <= dice_value <= 6:
        return {"success": False, "reason": "invalid_dice"}

    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            wallet = await conn.fetchrow(
                "SELECT neko_balance FROM users WHERE user_id = $1 FOR UPDATE",
                user_id,
            )
            if not wallet:
                return {"success": False, "reason": "wallet_missing"}
            bet = await conn.fetchrow(
                """
                SELECT id, stake, choice
                FROM neko_dice_bets
                WHERE user_id = $1
                  AND chat_id = $2
                  AND roll_prompt_message_id = $3
                  AND status = 'awaiting_roll'
                FOR UPDATE
                """,
                user_id,
                chat_id,
                roll_prompt_message_id,
            )
            if not bet:
                return {"success": False, "reason": "not_found"}

            wager = _coerce_neko(bet["stake"])
            choice = str(bet["choice"] or "")
            if wager is None or choice not in DICE_CHOICES:
                return {"success": False, "reason": "invalid_bet"}

            won = (
                (choice == "odd" and dice_value % 2 == 1)
                or (choice == "even" and dice_value % 2 == 0)
                or (choice.isdigit() and int(choice) == dice_value)
            )
            multiplier = DICE_EXACT_MULTIPLIER if choice.isdigit() else DICE_ODD_EVEN_MULTIPLIER
            payout = (
                (wager * multiplier).quantize(NEKO_QUANTUM, rounding=ROUND_HALF_UP)
                if won else ZERO_NEKO
            )
            wallet_balance = _balance_from_row(wallet)
            balance_after = wallet_balance + payout
            if balance_after > MAX_NEKO_BALANCE:
                return {"success": False, "reason": "balance_limit"}

            status = "won" if won else "lost"
            await conn.execute(
                """
                UPDATE neko_dice_bets
                SET status = $2,
                    dice_value = $3,
                    payout = $4,
                    resolved_at = NOW()
                WHERE id = $1 AND status = 'awaiting_roll'
                """,
                int(bet["id"]),
                status,
                dice_value,
                payout,
            )
            if payout > ZERO_NEKO:
                await conn.execute(
                    "UPDATE users SET neko_balance = $2, last_seen_at = NOW() WHERE user_id = $1",
                    user_id,
                    balance_after,
                )
                await _record_wallet_ledger(
                    conn,
                    user_id=user_id,
                    delta=payout,
                    balance_after=balance_after,
                    reason="casino_dice_payout",
                    reference_id=int(bet["id"]),
                )
            return {
                "success": True,
                "reason": "ok",
                "won": won,
                "choice": choice,
                "stake": wager,
                "dice_value": dice_value,
                "payout": payout,
                "balance": balance_after,
                "multiplier": multiplier,
            }


# ─── Neko Mines /casino ────────────────────────────────────────────────────

# Boards that were already active before the 3×3/one-bomb release remain
# safely playable after a restart or deployment. Their three stored bombs are
# enough to identify the legacy 4×4/x0.25 rule set. The payout-version column
# additionally preserves the temporary v15–v17 balanced 3×3 table for boards
# created while it was active; current boards use the gradual version-3 table.
_LEGACY_CASINO_GRID_COLUMNS = 4
_LEGACY_CASINO_GRID_SIZE = _LEGACY_CASINO_GRID_COLUMNS * _LEGACY_CASINO_GRID_COLUMNS
_LEGACY_CASINO_BOMB_COUNT = 3
_LEGACY_CASINO_MAX_SAFE_PICKS = _LEGACY_CASINO_GRID_SIZE - _LEGACY_CASINO_BOMB_COUNT
_LEGACY_CASINO_SAFE_PICK_INCREMENT = Decimal("0.25")


def _casino_rules_for_bombs(bomb_positions: list[int]) -> tuple[int, int, int, Decimal]:
    """Infer immutable board geometry from a stored game's server-side bombs."""
    if len(bomb_positions) == _LEGACY_CASINO_BOMB_COUNT:
        return (
            _LEGACY_CASINO_GRID_COLUMNS,
            _LEGACY_CASINO_GRID_SIZE,
            _LEGACY_CASINO_MAX_SAFE_PICKS,
            _LEGACY_CASINO_SAFE_PICK_INCREMENT,
        )
    return (
        CASINO_GRID_COLUMNS,
        CASINO_GRID_SIZE,
        CASINO_MAX_SAFE_PICKS,
        CASINO_SAFE_PICK_INCREMENT,
    )


def _casino_payout_version_from_row(values: dict, bomb_positions: list[int]) -> int:
    """Read a durable payout version without ever repricing legacy boards."""
    # A historical 4×4 board always follows its own linear schedule, even if
    # somebody manually altered its new compatibility field.
    if len(bomb_positions) == _LEGACY_CASINO_BOMB_COUNT:
        return CASINO_PAYOUT_VERSION_LINEAR
    try:
        version = int(values.get("payout_version") or CASINO_PAYOUT_VERSION_LINEAR)
    except (TypeError, ValueError):
        version = CASINO_PAYOUT_VERSION_LINEAR
    return (
        version
        if version in {
            CASINO_PAYOUT_VERSION_LINEAR,
            CASINO_PAYOUT_VERSION_BALANCED,
            CASINO_PAYOUT_VERSION_GRADUAL,
        }
        else CASINO_PAYOUT_VERSION_LINEAR
    )


def casino_multiplier(
    safe_picks: int,
    *,
    max_safe_picks: int = CASINO_MAX_SAFE_PICKS,
    safe_pick_increment: Decimal = CASINO_SAFE_PICK_INCREMENT,
    payout_version: int = CASINO_CURRENT_PAYOUT_VERSION,
) -> Decimal:
    """Return the stored game's multiplier without changing an old wager's rules."""
    safe_count = max(0, min(max(0, int(max_safe_picks)), int(safe_picks)))
    if int(max_safe_picks) == CASINO_MAX_SAFE_PICKS:
        if int(payout_version) == CASINO_PAYOUT_VERSION_GRADUAL:
            return CASINO_GRADUAL_MULTIPLIERS[safe_count]
        if int(payout_version) == CASINO_PAYOUT_VERSION_BALANCED:
            return CASINO_BALANCED_MULTIPLIERS[safe_count]
    # Historic version-1 and 4×4 games retain their stored linear rules.
    return Decimal("1.000") + Decimal(safe_pick_increment) * safe_count


def casino_payout(
    stake: object,
    safe_picks: int,
    *,
    max_safe_picks: int = CASINO_MAX_SAFE_PICKS,
    safe_pick_increment: Decimal = CASINO_SAFE_PICK_INCREMENT,
    payout_version: int = CASINO_CURRENT_PAYOUT_VERSION,
) -> Decimal:
    """Calculate the two-decimal Get payout, including the reserved stake."""
    normalized_stake = parse_neko_amount(stake)
    if normalized_stake is None:
        raise ValueError("invalid casino stake")
    return (
        normalized_stake * casino_multiplier(
            safe_picks,
            max_safe_picks=max_safe_picks,
            safe_pick_increment=safe_pick_increment,
            payout_version=payout_version,
        )
    ).quantize(NEKO_QUANTUM, rounding=ROUND_HALF_UP)


def _casino_game_from_row(row, *, wallet_balance: Decimal | None = None) -> dict:
    """Normalize a stored game and preserve its geometry and payout schedule."""
    values = dict(row)
    raw_bombs = [int(value) for value in (values["bomb_positions"] or [])]
    grid_columns, grid_size, max_safe_picks, safe_pick_increment = _casino_rules_for_bombs(raw_bombs)
    payout_version = _casino_payout_version_from_row(values, raw_bombs)
    raw_payout = values.get("payout")
    payout = _coerce_neko(raw_payout) if raw_payout is not None else None
    return {
        "id": int(values["id"]),
        "user_id": int(values["user_id"]),
        "chat_id": int(values["chat_id"]),
        "stake": parse_neko_amount(values["stake"]),
        "bomb_positions": sorted(set(raw_bombs)),
        "safe_positions": sorted({int(value) for value in (values["safe_positions"] or [])}),
        "grid_columns": grid_columns,
        "grid_size": grid_size,
        "max_safe_picks": max_safe_picks,
        "safe_pick_increment": safe_pick_increment,
        "payout_version": payout_version,
        "status": str(values["status"]),
        "payout": payout,
        "wallet_balance": wallet_balance,
    }


async def get_active_neko_casino_game(*, user_id: int) -> dict | None:
    """Return only this player's resumable active Mines board, if any."""
    pool = get_pool()
    async with pool.acquire() as conn:
        game = await conn.fetchrow(
            """
            SELECT id, user_id, chat_id, stake, bomb_positions, safe_positions, status, payout, payout_version
            FROM neko_casino_games
            WHERE user_id = $1 AND status = 'active'
            ORDER BY id DESC
            LIMIT 1
            """,
            user_id,
        )
        if not game:
            return None
        wallet = await conn.fetchrow(
            "SELECT neko_balance FROM users WHERE user_id = $1",
            user_id,
        )
    if not wallet:
        return None
    return _casino_game_from_row(game, wallet_balance=_balance_from_row(wallet))


async def get_neko_casino_game_audit(*, game_id: int) -> dict | None:
    """Return one immutable Mines board for a Full Owner's private audit view.

    This read-only service intentionally includes an active board's bomb layout.
    Its Telegram caller is private-chat and Full-Owner gated; normal players
    continue receiving only their existing masked active-board UI.
    """
    if not isinstance(game_id, int) or isinstance(game_id, bool) or game_id <= 0:
        return None
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT game.id, game.user_id, game.chat_id, game.stake,
                   game.bomb_positions, game.safe_positions, game.status,
                   game.payout, game.payout_version, game.created_at,
                   game.resolved_at, player.username, player.full_name
            FROM neko_casino_games AS game
            JOIN users AS player ON player.user_id = game.user_id
            WHERE game.id = $1
            """,
            game_id,
        )
    if not row:
        return None
    audit = _casino_game_from_row(row)
    values = dict(row)
    audit.update({
        "created_at": _utc(values.get("created_at")),
        "resolved_at": _utc(values.get("resolved_at")),
        "player_username": str(values.get("username") or ""),
        "player_full_name": str(values.get("full_name") or ""),
    })
    return audit


async def create_neko_casino_game(
    *,
    user_id: int,
    username: str | None,
    full_name: str | None,
    chat_id: int,
    stake: object,
    now: datetime | None = None,
) -> dict:
    """Reserve a wager and create one player's independent 3×3 Mines board.

    The locked wallet row makes the 70-second start cooldown personal and race
    safe. No query ever selects or changes another user's active game. ``now``
    is internal/test-only and is not supplied by the Telegram handler.
    """
    wager = parse_casino_stake(stake)
    if wager is None:
        return {"success": False, "reason": "invalid_amount"}
    current_time = _utc(now) or datetime.now(timezone.utc)

    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _ensure_wallet_user(
                conn,
                user_id=user_id,
                username=username,
                full_name=full_name,
            )
            # User-first lock ordering is used by creation, tile reveals,
            # cashouts, and /deletewallet cancellation. It permits different users
            # to start independently at the same time while serializing only
            # one user's own wallet/game actions.
            wallet = await conn.fetchrow(
                """
                SELECT neko_balance, casino_starts_since_cooldown, casino_cooldown_until,
                       casino_cooldown_version
                FROM users
                WHERE user_id = $1
                FOR UPDATE
                """,
                user_id,
            )
            if not wallet:
                raise RuntimeError("wallet user row was not created")
            available_balance = _balance_from_row(wallet)
            active = await conn.fetchrow(
                """
                SELECT id, user_id, chat_id, stake, bomb_positions, safe_positions, status, payout, payout_version
                FROM neko_casino_games
                WHERE user_id = $1 AND status = 'active'
                FOR UPDATE
                """,
                user_id,
            )
            if active:
                return {
                    "success": False,
                    "reason": "active_game",
                    "game": _casino_game_from_row(
                        active,
                        wallet_balance=available_balance,
                    ),
                }
            active_dice_bet = await conn.fetchrow(
                """
                SELECT id
                FROM neko_dice_bets
                WHERE user_id = $1 AND status = 'awaiting_roll'
                FOR UPDATE
                """,
                user_id,
            )
            if active_dice_bet:
                return {"success": False, "reason": "active_dice"}

            # v8 used a three-start/five-minute batch. A version-1 row may
            # still carry that old deadline when this package is first deployed;
            # ignore it rather than making a player complete an obsolete pause.
            cooldown_version = int(wallet["casino_cooldown_version"] or 1)
            cooldown_until = (
                _utc(wallet["casino_cooldown_until"])
                if cooldown_version == CASINO_COOLDOWN_VERSION
                else None
            )
            if cooldown_until and current_time < cooldown_until:
                return {
                    "success": False,
                    "reason": "cooldown",
                    "balance": available_balance,
                    "remaining_seconds": max(
                        1,
                        ceil((cooldown_until - current_time).total_seconds()),
                    ),
                    "cooldown_until": cooldown_until,
                }

            if available_balance < wager:
                return {
                    "success": False,
                    "reason": "insufficient_balance",
                    "balance": available_balance,
                }

            # Start the new personal 70-second countdown immediately with
            # every successful board reservation, independent of board outcome.
            stored_cooldown_until = current_time + timedelta(seconds=CASINO_COOLDOWN_SECONDS)
            bomb_positions = sorted(
                secrets.SystemRandom().sample(range(CASINO_GRID_SIZE), CASINO_BOMB_COUNT)
            )
            game_row = await conn.fetchrow(
                """
                INSERT INTO neko_casino_games (
                    user_id, chat_id, stake, bomb_positions, payout_version
                )
                VALUES ($1, $2, $3, $4::SMALLINT[], $5)
                RETURNING id
                """,
                user_id,
                chat_id,
                wager,
                bomb_positions,
                CASINO_CURRENT_PAYOUT_VERSION,
            )
            available_after = available_balance - wager
            await conn.execute(
                """
                UPDATE users
                SET neko_balance = $2,
                    casino_starts_since_cooldown = 0,
                    casino_cooldown_until = $3,
                    casino_cooldown_version = $4,
                    last_seen_at = NOW()
                WHERE user_id = $1
                """,
                user_id,
                available_after,
                stored_cooldown_until,
                CASINO_COOLDOWN_VERSION,
            )
            await _record_wallet_ledger(
                conn,
                user_id=user_id,
                delta=-wager,
                balance_after=available_after,
                reason="casino_stake",
                reference_id=int(game_row["id"]),
            )
            return {
                "success": True,
                "reason": "ok",
                "cooldown_until": stored_cooldown_until,
                "game": {
                    "id": int(game_row["id"]),
                    "user_id": user_id,
                    "chat_id": chat_id,
                    "stake": wager,
                    "bomb_positions": bomb_positions,
                    "safe_positions": [],
                    "grid_columns": CASINO_GRID_COLUMNS,
                    "grid_size": CASINO_GRID_SIZE,
                    "max_safe_picks": CASINO_MAX_SAFE_PICKS,
                    "safe_pick_increment": CASINO_SAFE_PICK_INCREMENT,
                    "payout_version": CASINO_CURRENT_PAYOUT_VERSION,
                    "status": "active",
                    "payout": None,
                    "wallet_balance": available_after,
                },
            }


async def _lock_casino_game_for_user(conn, *, game_id: int, user_id: int) -> tuple[dict | None, Decimal | None, str | None]:
    """Verify ownership, then lock wallet before game for a consistent order."""
    probe = await conn.fetchrow(
        "SELECT user_id FROM neko_casino_games WHERE id = $1",
        game_id,
    )
    if not probe:
        return None, None, "not_found"
    if int(probe["user_id"]) != user_id:
        return None, None, "not_yours"

    wallet = await conn.fetchrow(
        "SELECT neko_balance FROM users WHERE user_id = $1 FOR UPDATE",
        user_id,
    )
    if not wallet:
        return None, None, "wallet_missing"
    game_row = await conn.fetchrow(
        """
        SELECT id, user_id, chat_id, stake, bomb_positions, safe_positions, status, payout, payout_version
        FROM neko_casino_games
        WHERE id = $1 AND user_id = $2
        FOR UPDATE
        """,
        game_id,
        user_id,
    )
    if not game_row:
        return None, _balance_from_row(wallet), "not_found"

    wallet_balance = _balance_from_row(wallet)
    game = _casino_game_from_row(game_row, wallet_balance=wallet_balance)
    if game["status"] != "active":
        return game, wallet_balance, "not_active"
    if game["stake"] is None:
        return game, wallet_balance, "invalid_stake"
    return game, wallet_balance, None


async def _cash_out_locked_casino_game(
    conn,
    *,
    game: dict,
    wallet_balance: Decimal,
) -> dict:
    """Settle a locked active game after its current safe-tile count."""
    payout = casino_payout(
        game["stake"],
        len(game["safe_positions"]),
        max_safe_picks=int(game["max_safe_picks"]),
        safe_pick_increment=Decimal(game["safe_pick_increment"]),
        payout_version=int(game["payout_version"]),
    )
    if wallet_balance > MAX_NEKO_BALANCE - payout:
        return {"success": False, "reason": "balance_limit", "game": game}

    balance_after = wallet_balance + payout
    await conn.execute(
        "UPDATE users SET neko_balance = $2, last_seen_at = NOW() WHERE user_id = $1",
        game["user_id"],
        balance_after,
    )
    await conn.execute(
        """
        UPDATE neko_casino_games
        SET status = 'cashed_out',
            safe_positions = $2::SMALLINT[],
            payout = $3,
            resolved_at = NOW()
        WHERE id = $1 AND status = 'active'
        """,
        game["id"],
        game["safe_positions"],
        payout,
    )
    await _record_wallet_ledger(
        conn,
        user_id=game["user_id"],
        delta=payout,
        balance_after=balance_after,
        reason="casino_cashout",
        reference_id=game["id"],
    )
    game["status"] = "cashed_out"
    game["payout"] = payout
    game["wallet_balance"] = balance_after
    return {
        "success": True,
        "reason": "ok",
        "outcome": "cashed_out",
        "game": game,
    }


async def reveal_neko_casino_tile(
    *,
    game_id: int,
    user_id: int,
    tile_index: int,
) -> dict:
    """Reveal one server-verified tile; bombs burn escrow and safe tiles grow Get."""
    if not isinstance(tile_index, int) or isinstance(tile_index, bool) or tile_index < 0:
        return {"success": False, "reason": "invalid_tile"}

    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            game, wallet_balance, failure = await _lock_casino_game_for_user(
                conn,
                game_id=game_id,
                user_id=user_id,
            )
            if failure:
                return {
                    "success": False,
                    "reason": failure,
                    "game": game,
                }
            assert game is not None and wallet_balance is not None
            if tile_index >= int(game["grid_size"]):
                return {"success": False, "reason": "invalid_tile", "game": game}
            if tile_index in game["safe_positions"]:
                return {"success": False, "reason": "already_revealed", "game": game}
            if tile_index in game["bomb_positions"]:
                await conn.execute(
                    """
                    UPDATE neko_casino_games
                    SET status = 'lost', payout = 0.00, resolved_at = NOW()
                    WHERE id = $1 AND status = 'active'
                    """,
                    game_id,
                )
                game["status"] = "lost"
                game["payout"] = ZERO_NEKO
                return {
                    "success": True,
                    "reason": "ok",
                    "outcome": "lost",
                    "game": game,
                }

            safe_positions = sorted({*game["safe_positions"], tile_index})
            game["safe_positions"] = safe_positions
            if len(safe_positions) >= int(game["max_safe_picks"]):
                # Every non-bomb tile has been revealed: auto-cashout prevents
                # a no-choice terminal board from leaving reserved Neko stuck.
                return await _cash_out_locked_casino_game(
                    conn,
                    game=game,
                    wallet_balance=wallet_balance,
                )

            await conn.execute(
                """
                UPDATE neko_casino_games
                SET safe_positions = $2::SMALLINT[]
                WHERE id = $1 AND status = 'active'
                """,
                game_id,
                safe_positions,
            )
            return {
                "success": True,
                "reason": "ok",
                "outcome": "safe",
                "game": game,
            }


async def cash_out_neko_casino(*, game_id: int, user_id: int) -> dict:
    """Pay the current safe-tile multiplier only to the game's owning player."""
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            game, wallet_balance, failure = await _lock_casino_game_for_user(
                conn,
                game_id=game_id,
                user_id=user_id,
            )
            if failure:
                return {
                    "success": False,
                    "reason": failure,
                    "game": game,
                }
            assert game is not None and wallet_balance is not None
            return await _cash_out_locked_casino_game(
                conn,
                game=game,
                wallet_balance=wallet_balance,
            )

