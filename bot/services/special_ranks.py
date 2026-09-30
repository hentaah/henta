"""Canonical rules for the two high-rank catalog tiers.

The values here are deliberately small, dependency-free constants so spawn,
claim, Market, and UI-facing services enforce the same exact rank policy.
"""
from __future__ import annotations

SUSPICIOUS_RARITY = "Suspicious"
OVERLORD_RARITY = "OverLord"
ETERNAL_RARITY = "Eternal"
# Love is catalog/Lovelist-only and therefore intentionally absent from every
# automatic spawn pool. It has a controlled fixed-price sale path of its own.
LOVE_RARITY = "Love"

# Every OverLord card is a limited ten-*person* collectible. This is lifetime
# claim history, not a count of current Harem rows, so burning/resetting a copy
# cannot reopen a fully claimed card's future spawn pool.
OVERLORD_UNIQUE_OWNER_CAP = 10

# Main-group cadence: every automatic Main Group spawn advances this counter;
# once 70 cards have emitted, the following eligible automatic spawn is forced
# to OverLord. Notify the group once when only five remain.
OVERLORD_LOWER_SPAWNS_REQUIRED = 70
OVERLORD_COUNTDOWN_REMAINING = 5
OVERLORD_NOTICE_AT_LOWER_SPAWNS = (
    OVERLORD_LOWER_SPAWNS_REQUIRED - OVERLORD_COUNTDOWN_REMAINING
)

# Outside the flagship group, Suspicious is a guaranteed cadence event rather
# than a random selection: 70 lower-rank automatic cards, then Suspicious.
SUSPICIOUS_EXTERNAL_LOWER_SPAWNS_REQUIRED = 70

# Suspicious is cadence-only everywhere: Main forces it after 40 automatic
# Main Group spawns, and every other group forces it after 70. It must never be
# selected by an ordinary weighted roll.
SUSPICIOUS_MAIN_GROUP_WEIGHT = 0
MAIN_SUSPICIOUS_SPAWNS_REQUIRED = 40

# Eternal and OverLord cards never enter sale/gift/trade flows. Eternal retains
# its historic no-burn rule. OverLord is also protected from burn in game.py so
# the deliberately ten-owner collectible cannot be destroyed accidentally.
NON_TRANSFERABLE_RARITIES = frozenset({ETERNAL_RARITY.casefold(), OVERLORD_RARITY.casefold()})


def normalized_rarity(value: object) -> str:
    """Normalize a controlled catalog rarity for safe policy comparisons."""
    return str(value or "").strip().casefold()


def is_overlord(value: object) -> bool:
    return normalized_rarity(value) == OVERLORD_RARITY.casefold()


def is_suspicious(value: object) -> bool:
    return normalized_rarity(value) == SUSPICIOUS_RARITY.casefold()


def is_love(value: object) -> bool:
    return normalized_rarity(value) == LOVE_RARITY.casefold()


def is_non_transferable(value: object) -> bool:
    return normalized_rarity(value) in NON_TRANSFERABLE_RARITIES
