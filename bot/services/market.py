"""Durable Mini App marketplace operations.

There are two deliberately distinct inventory sources:

* ``catalog`` listings are the existing Full Owner catalog-stock listings.
* ``player`` listings escrow exactly one already-owned Harem copy.  That copy
  leaves the seller's Harem while listed and is restored on cancellation or
  transferred atomically to the buyer on purchase.

All prices use exact two-decimal Neko values.  Every destructive/financial
change happens in one PostgreSQL transaction; browser-provided character IDs,
prices, and ownership claims are never trusted on their own.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from bot.db.pool import get_pool
from bot.services.game import _higher_rarity, _recalculate_ranked_user_stats
from bot.services.love import (
    LOVE_RARITY,
    restore_love_state_after_market_cancel,
    transfer_love_state_after_market_purchase,
)
from bot.services.neko import (
    MAX_NEKO_AMOUNT,
    MAX_NEKO_BALANCE,
    NEKO_QUANTUM,
    ZERO_NEKO,
    _record_wallet_ledger,
)
from bot.services.special_ranks import ETERNAL_RARITY, is_overlord

# A marketplace transfer is global rather than tied to any Telegram group.
# It therefore never rewrites historical per-group catch statistics.
MARKET_CHAT_ID = 0
MARKET_LISTING_CATALOG = "catalog"
MARKET_LISTING_PLAYER = "player"
_MARKET_PRICE_RE = re.compile(r"([0-9]{1,19}(?:\.[0-9]{1,2})?)\s*(?:neko)?", re.IGNORECASE)


def parse_market_price(value: object) -> Decimal | None:
    """Parse ``4000neko`` / ``4000 Neko`` / ``4000`` as a positive price."""
    if not isinstance(value, str):
        return None
    match = _MARKET_PRICE_RE.fullmatch(value.strip())
    if not match:
        return None
    try:
        price = Decimal(match.group(1))
    except InvalidOperation:
        return None
    if not price.is_finite():
        return None
    price = price.quantize(NEKO_QUANTUM, rounding=ROUND_HALF_UP)
    if price <= ZERO_NEKO or price > MAX_NEKO_AMOUNT:
        return None
    return price


def _valid_price(price: object) -> bool:
    return (
        isinstance(price, Decimal)
        and price.is_finite()
        and ZERO_NEKO < price <= MAX_NEKO_AMOUNT
        and price == price.quantize(NEKO_QUANTUM, rounding=ROUND_HALF_UP)
    )


def _decimal(value: object) -> Decimal:
    """Convert a database NUMERIC value without passing through float."""
    try:
        value_as_decimal = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as error:
        raise RuntimeError("invalid marketplace decimal value") from error
    return value_as_decimal.quantize(NEKO_QUANTUM, rounding=ROUND_HALF_UP)


def _transfer_protection_reason(value: object) -> str | None:
    """Keep stale Market rows from bypassing Eternal/OverLord sale locks."""
    if is_overlord(value):
        return "overlord_protected"
    if str(value or "").strip().casefold() == ETERNAL_RARITY.casefold():
        return "eternal_protected"
    if str(value or "").strip().casefold() == LOVE_RARITY.casefold():
        return "love_fixed_price_required"
    return None


def _character_from_row(row: Any) -> dict[str, object]:
    """Normalize shared character metadata from a market query row."""
    return {
        "character_id": int(row["character_id"]),
        "name": str(row["name"] or ""),
        "anime": str(row["anime"] or ""),
        "rarity": str(row["rarity"] or ""),
        "rarity_score": int(row["rarity_score"] or 0),
        "aliases": str(row["aliases"] or ""),
        "event": str(row["event"] or ""),
        # The legacy column holds a Telegram file ID; media_type chooses its API.
        "image_url": str(row["image_url"] or ""),
        "media_type": str(row["media_type"] or "photo"),
    }


def _listing_from_row(row: Any) -> dict[str, object]:
    """Normalize either catalog stock or an escrowed player card listing."""
    raw_seller = row["seller_user_id"]
    kind = str(row["listing_kind"] or MARKET_LISTING_CATALOG)
    if kind not in {MARKET_LISTING_CATALOG, MARKET_LISTING_PLAYER}:
        kind = MARKET_LISTING_CATALOG
    return {
        "listing_id": int(row["listing_id"]),
        "price": _decimal(row["price"]),
        "listing_kind": kind,
        "seller_user_id": int(raw_seller) if raw_seller is not None else None,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "love_code": (f"L{int(row['love_code_number'])}" if row["love_code_number"] is not None else None),
        "love_base_price": (_decimal(row["love_base_price"]) if row["love_base_price"] is not None else None),
        **_character_from_row(row),
    }


_LISTING_SELECT = """
    SELECT
        listing.id AS listing_id,
        listing.price,
        listing.listing_kind,
        listing.seller_user_id,
        listing.source_harem_id,
        listing.source_chat_id,
        listing.source_is_favorite,
        listing.source_is_ranked,
        listing.source_acquired_at,
        listing.created_at,
        listing.updated_at,
        love_card.love_code AS love_code_number,
        love_card.base_price AS love_base_price,
        character.id AS character_id,
        character.name,
        character.anime,
        character.rarity,
        character.rarity_score,
        character.aliases,
        COALESCE(character.event, '') AS event,
        character.image_url,
        COALESCE(character.media_type, 'photo') AS media_type
    FROM market_listings AS listing
    JOIN characters AS character ON character.id = listing.character_id
    LEFT JOIN love_cards AS love_card ON love_card.character_id = character.id
"""


async def list_market_listings() -> list[dict[str, object]]:
    """Return active catalog-stock and player-copy listings in one feed."""
    pool = get_pool()
    async with pool.acquire() as connection:
        rows = await connection.fetch(
            _LISTING_SELECT
            + """
            WHERE LOWER(BTRIM(COALESCE(character.rarity, ''))) NOT IN ('eternal', 'overlord')
              -- A corrupt/legacy catalog-stock Love row must not surface as a
              -- purchasable Market item; only fixed-price player escrow may.
              AND (
                  LOWER(BTRIM(COALESCE(character.rarity, ''))) <> 'love'
                  OR listing.listing_kind = 'player'
              )
            ORDER BY listing.updated_at DESC, listing.id DESC
            """
        )
    return [_listing_from_row(row) for row in rows]


async def add_market_listing(
    *,
    character_id: int,
    price: Decimal,
    listed_by: int,
) -> dict[str, object]:
    """Create/reprice the existing Full Owner catalog-stock listing.

    This preserves the established owner command surface.  It intentionally
    creates inventory from the catalog rather than consuming a player's Harem
    copy; player listings use :func:`create_player_market_listing` instead.
    """
    if (
        not isinstance(character_id, int)
        or isinstance(character_id, bool)
        or character_id <= 0
        or not isinstance(listed_by, int)
        or isinstance(listed_by, bool)
        or listed_by <= 0
        or not _valid_price(price)
    ):
        return {"success": False, "reason": "invalid_arguments"}

    pool = get_pool()
    async with pool.acquire() as connection:
        async with connection.transaction():
            character = await connection.fetchrow(
                """
                SELECT
                    id AS character_id,
                    name,
                    anime,
                    rarity,
                    rarity_score,
                    aliases,
                    COALESCE(event, '') AS event,
                    image_url,
                    COALESCE(media_type, 'photo') AS media_type
                FROM characters
                WHERE id = $1
                FOR KEY SHARE
                """,
                character_id,
            )
            if not character:
                return {"success": False, "reason": "character_not_found"}
            if str(character["rarity"] or "").strip().casefold() == LOVE_RARITY.casefold():
                return {"success": False, "reason": "love_list_only"}
            protection = _transfer_protection_reason(character["rarity"])
            if protection is not None:
                return {"success": False, "reason": protection}

            listing = await connection.fetchrow(
                """
                INSERT INTO market_listings (character_id, price, listed_by, listing_kind)
                VALUES ($1, $2, $3, 'catalog')
                ON CONFLICT (character_id) WHERE listing_kind = 'catalog' DO UPDATE SET
                    price = EXCLUDED.price,
                    listed_by = EXCLUDED.listed_by,
                    updated_at = NOW()
                RETURNING
                    id AS listing_id,
                    price,
                    listing_kind,
                    seller_user_id,
                    created_at,
                    updated_at
                """,
                character_id,
                price,
                listed_by,
            )

    if not listing:
        raise RuntimeError("market listing was not returned")
    return {
        "success": True,
        "listing": {
            "listing_id": int(listing["listing_id"]),
            "price": _decimal(listing["price"]),
            "listing_kind": MARKET_LISTING_CATALOG,
            "seller_user_id": None,
            "created_at": listing["created_at"],
            "updated_at": listing["updated_at"],
            **_character_from_row(character),
        },
    }


async def remove_market_listing(*, character_id: int) -> dict[str, object]:
    """Remove only the legacy Full Owner catalog-stock listing for one code.

    This retains the exact `/deletemarket <code>` behavior. It never deletes a
    catalog card or a Harem row. Player-copy listings are removed by exact
    listing ID through the Full Owner review/API flow so duplicates stay safe.
    """
    if (
        not isinstance(character_id, int)
        or isinstance(character_id, bool)
        or character_id <= 0
    ):
        return {"success": False, "reason": "invalid_arguments"}

    pool = get_pool()
    async with pool.acquire() as connection:
        async with connection.transaction():
            listing = await connection.fetchrow(
                _LISTING_SELECT
                + """
                WHERE listing.character_id = $1 AND listing.listing_kind = 'catalog'
                FOR UPDATE OF listing
                """,
                character_id,
            )
            if not listing:
                return {"success": False, "reason": "listing_not_found"}

            deleted_listing_id = await connection.fetchval(
                "DELETE FROM market_listings WHERE id = $1 RETURNING id",
                int(listing["listing_id"]),
            )
            if deleted_listing_id is None:
                raise RuntimeError("market listing could not be deleted")

    return {"success": True, "listing": _listing_from_row(listing)}


async def _ensure_market_user(
    connection,
    *,
    user_id: int,
    username: str | None,
    full_name: str | None,
) -> None:
    """Ensure signed Mini App users have a current durable profile row."""
    clean_username = (username or "").strip() or None
    clean_name = (full_name or "").strip() or f"User {user_id}"
    await connection.execute(
        """
        INSERT INTO users (user_id, username, full_name, last_seen_at)
        VALUES ($1, $2, $3, NOW())
        ON CONFLICT (user_id) DO UPDATE SET
            username = COALESCE(EXCLUDED.username, users.username),
            full_name = CASE WHEN BTRIM($3) <> '' THEN $3 ELSE users.full_name END,
            last_seen_at = NOW()
        """,
        user_id,
        clean_username,
        clean_name,
    )


async def create_player_market_listing(
    *,
    harem_id: int,
    seller_id: int,
    price: Decimal,
) -> dict[str, object]:
    """Escrow one transferable Harem copy for a player-to-player sale.

    The exact row ID—not a client-supplied character code—is locked and removed
    from Harem in the listing transaction. Thus a duplicate stack contributes
    exactly one listed copy and the seller cannot subsequently burn/gift/trade
    that copy before a buyer resolves the listing.
    """
    if (
        not isinstance(harem_id, int)
        or isinstance(harem_id, bool)
        or harem_id <= 0
        or not isinstance(seller_id, int)
        or isinstance(seller_id, bool)
        or seller_id <= 0
        or not _valid_price(price)
    ):
        return {"success": False, "reason": "invalid_arguments"}

    pool = get_pool()
    async with pool.acquire() as connection:
        async with connection.transaction():
            await connection.execute("SELECT pg_advisory_xact_lock($1)", seller_id)
            source = await connection.fetchrow(
                """
                SELECT
                    h.id AS source_harem_id,
                    h.user_id AS seller_user_id,
                    h.chat_id AS source_chat_id,
                    h.character_id,
                    h.is_favorite AS source_is_favorite,
                    h.is_ranked AS source_is_ranked,
                    h.acquired_at AS source_acquired_at,
                    c.name,
                    c.anime,
                    c.rarity,
                    c.rarity_score,
                    c.aliases,
                    COALESCE(c.event, '') AS event,
                    c.image_url,
                    COALESCE(c.media_type, 'photo') AS media_type
                FROM harem AS h
                JOIN characters AS c ON c.id = h.character_id
                WHERE h.id = $1 AND h.user_id = $2
                FOR UPDATE OF h
                """,
                harem_id,
                seller_id,
            )
            if not source:
                return {"success": False, "reason": "harem_copy_not_found"}
            if str(source["rarity"] or "").strip().casefold() == LOVE_RARITY.casefold():
                return {"success": False, "reason": "love_fixed_price_required"}
            protection = _transfer_protection_reason(source["rarity"])
            if protection is not None:
                return {"success": False, "reason": protection}

            # A source ID can only have an active escrow row once. It should be
            # impossible while the Harem row still exists, but checking before
            # deletion keeps a manually-corrupted database fail-safe.
            existing_escrow = await connection.fetchval(
                "SELECT 1 FROM market_listings WHERE source_harem_id = $1",
                harem_id,
            )
            if existing_escrow:
                return {"success": False, "reason": "already_listed"}

            removed = await connection.fetchval(
                "DELETE FROM harem WHERE id = $1 AND user_id = $2 RETURNING id",
                harem_id,
                seller_id,
            )
            if removed is None:
                return {"success": False, "reason": "harem_copy_not_found"}

            listing = await connection.fetchrow(
                """
                INSERT INTO market_listings (
                    character_id,
                    price,
                    listed_by,
                    listing_kind,
                    seller_user_id,
                    source_harem_id,
                    source_chat_id,
                    source_is_favorite,
                    source_is_ranked,
                    source_acquired_at
                )
                VALUES ($1, $2, $3, 'player', $3, $4, $5, $6, $7, $8)
                RETURNING id AS listing_id, price, listing_kind, seller_user_id,
                          created_at, updated_at
                """,
                int(source["character_id"]),
                price,
                seller_id,
                harem_id,
                int(source["source_chat_id"]),
                bool(source["source_is_favorite"]),
                bool(source["source_is_ranked"]),
                source["source_acquired_at"],
            )
            if not listing:
                raise RuntimeError("player market listing was not returned")

            # Market transfers have the same global-collection accounting as
            # gifts/trades: collection totals change, historical group catches do not.
            if bool(source["source_is_ranked"]):
                await _recalculate_ranked_user_stats(connection, seller_id)

    return {
        "success": True,
        "listing": {
            "listing_id": int(listing["listing_id"]),
            "price": _decimal(listing["price"]),
            "listing_kind": MARKET_LISTING_PLAYER,
            "seller_user_id": seller_id,
            "created_at": listing["created_at"],
            "updated_at": listing["updated_at"],
            **_character_from_row(source),
        },
    }


async def remove_player_market_listing(
    *,
    listing_id: int,
    actor_id: int,
    actor_is_full_owner: bool,
) -> dict[str, object]:
    """Cancel one exact player listing and return its escrowed copy to seller.

    The seller may cancel their own listing; any Full Owner may remove any
    listing for moderation. A Full Owner may also remove catalog-stock listing
    IDs through this exact-ID path. Returning a player card creates a fresh
    Harem row so stale gift/trade confirmations cannot unexpectedly consume it.
    """
    if (
        not isinstance(listing_id, int)
        or isinstance(listing_id, bool)
        or listing_id <= 0
        or not isinstance(actor_id, int)
        or isinstance(actor_id, bool)
        or actor_id <= 0
    ):
        return {"success": False, "reason": "invalid_arguments"}

    pool = get_pool()
    async with pool.acquire() as connection:
        async with connection.transaction():
            preview = await connection.fetchrow(
                "SELECT listing_kind, seller_user_id FROM market_listings WHERE id = $1",
                listing_id,
            )
            if not preview:
                return {"success": False, "reason": "listing_not_found"}
            preview_kind = str(preview["listing_kind"] or MARKET_LISTING_CATALOG)
            preview_seller = preview["seller_user_id"]
            if preview_kind == MARKET_LISTING_PLAYER and preview_seller is not None:
                seller_id = int(preview_seller)
                if actor_id != seller_id and not actor_is_full_owner:
                    return {"success": False, "reason": "not_allowed"}
                await connection.execute("SELECT pg_advisory_xact_lock($1)", seller_id)
            elif not actor_is_full_owner:
                return {"success": False, "reason": "not_allowed"}

            listing = await connection.fetchrow(
                _LISTING_SELECT + " WHERE listing.id = $1 FOR UPDATE OF listing",
                listing_id,
            )
            if not listing:
                return {"success": False, "reason": "listing_not_found"}
            kind = str(listing["listing_kind"] or MARKET_LISTING_CATALOG)

            if kind == MARKET_LISTING_CATALOG:
                if not actor_is_full_owner:
                    return {"success": False, "reason": "not_allowed"}
                removed = await connection.fetchval(
                    "DELETE FROM market_listings WHERE id = $1 RETURNING id",
                    listing_id,
                )
                if removed is None:
                    return {"success": False, "reason": "listing_not_found"}
                return {
                    "success": True,
                    "listing": _listing_from_row(listing),
                    "returned_to_seller": False,
                }

            seller_raw = listing["seller_user_id"]
            source_chat_id = listing["source_chat_id"]
            source_ranked = listing["source_is_ranked"]
            if seller_raw is None or source_chat_id is None or source_ranked is None:
                raise RuntimeError("player market listing escrow is incomplete")
            seller_id = int(seller_raw)
            if actor_id != seller_id and not actor_is_full_owner:
                return {"success": False, "reason": "not_allowed"}

            removed = await connection.fetchval(
                "DELETE FROM market_listings WHERE id = $1 RETURNING id",
                listing_id,
            )
            if removed is None:
                return {"success": False, "reason": "listing_not_found"}

            # A seller may set another Favorite while their old Favorite is
            # listed. Restore that old flag only if it remains globally unique.
            favorite_available = not bool(await connection.fetchval(
                "SELECT 1 FROM harem WHERE user_id = $1 AND is_favorite = TRUE LIMIT 1",
                seller_id,
            ))
            harem_row = await connection.fetchrow(
                """
                INSERT INTO harem (user_id, chat_id, character_id, is_favorite, is_ranked, acquired_at)
                VALUES ($1, $2, $3, $4, $5, COALESCE($6, NOW()))
                RETURNING id AS harem_id, acquired_at
                """,
                seller_id,
                int(source_chat_id),
                int(listing["character_id"]),
                bool(listing["source_is_favorite"]) and favorite_available,
                bool(source_ranked),
                listing["source_acquired_at"],
            )
            if not harem_row:
                raise RuntimeError("returned harem record was not created")
            if str(listing["rarity"] or "").strip().casefold() == LOVE_RARITY.casefold():
                restored_love = await restore_love_state_after_market_cancel(
                    connection,
                    seller_id=seller_id,
                    character_id=int(listing["character_id"]),
                )
                if not restored_love:
                    raise RuntimeError("Love listing state could not be restored")
            if bool(source_ranked):
                await _recalculate_ranked_user_stats(connection, seller_id)

    return {
        "success": True,
        "listing": _listing_from_row(listing),
        "returned_to_seller": True,
        "harem_id": int(harem_row["harem_id"]),
    }


async def purchase_market_listing(
    *,
    listing_id: int,
    buyer_id: int,
    username: str | None,
    full_name: str,
    allow_self_purchase: bool = False,
) -> dict[str, object]:
    """Atomically buy catalog stock or transfer one escrowed player copy.

    A regular seller cannot buy their own player listing. Full Owners have the
    requested exception; an owner self-purchase simply restores their escrowed
    card and leaves their wallet unchanged because sender and receiver match.
    """
    if (
        not isinstance(listing_id, int)
        or isinstance(listing_id, bool)
        or listing_id <= 0
        or not isinstance(buyer_id, int)
        or isinstance(buyer_id, bool)
        or buyer_id <= 0
    ):
        return {"success": False, "reason": "invalid_arguments"}

    pool = get_pool()
    async with pool.acquire() as connection:
        async with connection.transaction():
            # Read seller identity before taking advisory locks in numeric order.
            # This prevents a pair of users buying each other's cards from
            # deadlocking on opposite wallet rows.
            preview = await connection.fetchrow(
                "SELECT listing_kind, seller_user_id FROM market_listings WHERE id = $1",
                listing_id,
            )
            if not preview:
                return {"success": False, "reason": "listing_unavailable"}
            participant_ids = {buyer_id}
            if str(preview["listing_kind"] or MARKET_LISTING_CATALOG) == MARKET_LISTING_PLAYER:
                if preview["seller_user_id"] is None:
                    return {"success": False, "reason": "listing_corrupt"}
                participant_ids.add(int(preview["seller_user_id"]))
            for user_id in sorted(participant_ids):
                await connection.execute("SELECT pg_advisory_xact_lock($1)", user_id)

            await _ensure_market_user(
                connection,
                user_id=buyer_id,
                username=username,
                full_name=full_name,
            )
            listing = await connection.fetchrow(
                _LISTING_SELECT + " WHERE listing.id = $1 FOR UPDATE OF listing",
                listing_id,
            )
            if not listing:
                return {"success": False, "reason": "listing_unavailable"}
            kind = str(listing["listing_kind"] or MARKET_LISTING_CATALOG)
            is_love_listing = str(listing["rarity"] or "").strip().casefold() == LOVE_RARITY.casefold()
            if is_love_listing and kind != MARKET_LISTING_PLAYER:
                return {"success": False, "reason": "love_list_only"}
            protection = _transfer_protection_reason(listing["rarity"])
            if protection is not None and not is_love_listing:
                return {"success": False, "reason": protection}

            kind = str(listing["listing_kind"] or MARKET_LISTING_CATALOG)
            if kind not in {MARKET_LISTING_CATALOG, MARKET_LISTING_PLAYER}:
                return {"success": False, "reason": "listing_corrupt"}
            seller_raw = listing["seller_user_id"]
            seller_id = int(seller_raw) if seller_raw is not None else None
            if kind == MARKET_LISTING_PLAYER and seller_id is None:
                return {"success": False, "reason": "listing_corrupt"}
            if kind == MARKET_LISTING_PLAYER and seller_id == buyer_id and not allow_self_purchase:
                return {"success": False, "reason": "self_purchase_forbidden"}
            if is_love_listing and seller_id == buyer_id:
                # An escrowed Love returns through cancellation so it retains
                # its level/points; a purchase must never reset it to Level 1.
                return {"success": False, "reason": "self_purchase_forbidden"}

            locked_users: dict[int, Any] = {}
            for user_id in sorted({buyer_id, *([seller_id] if seller_id is not None else [])}):
                row = await connection.fetchrow(
                    """
                    SELECT neko_balance, total_claims, highest_rarity
                    FROM users
                    WHERE user_id = $1
                    FOR UPDATE
                    """,
                    user_id,
                )
                if not row:
                    raise RuntimeError("market participant row was not available")
                locked_users[user_id] = row

            buyer = locked_users[buyer_id]
            price = _decimal(listing["price"])
            buyer_balance_before = _decimal(buyer["neko_balance"])
            is_owner_self_purchase = kind == MARKET_LISTING_PLAYER and seller_id == buyer_id
            if not is_owner_self_purchase and buyer_balance_before < price:
                return {
                    "success": False,
                    "reason": "insufficient_neko",
                    "price": price,
                    "balance": buyer_balance_before,
                }

            seller_balance_after: Decimal | None = None
            if kind == MARKET_LISTING_PLAYER and not is_owner_self_purchase:
                assert seller_id is not None
                seller_balance_before = _decimal(locked_users[seller_id]["neko_balance"])
                if seller_balance_before > MAX_NEKO_BALANCE - price:
                    return {
                        "success": False,
                        "reason": "seller_balance_limit",
                        "price": price,
                    }
                seller_balance_after = (seller_balance_before + price).quantize(
                    NEKO_QUANTUM,
                    rounding=ROUND_HALF_UP,
                )

            # Do not consume the listing yet. A Love transfer performs
            # additional eligibility checks below; returning normally after an
            # early DELETE would commit a destructive listing removal.
            is_ranked = (
                False if is_love_listing
                else (bool(listing["source_is_ranked"]) if kind == MARKET_LISTING_PLAYER else True)
            )
            restored_self = bool(is_owner_self_purchase)
            restored_favorite = False
            target_chat_id = MARKET_CHAT_ID
            if restored_self:
                assert seller_id is not None
                source_chat_id = listing["source_chat_id"]
                if source_chat_id is None:
                    raise RuntimeError("owner self-purchase listing escrow is incomplete")
                target_chat_id = int(source_chat_id)
                if bool(listing["source_is_favorite"]):
                    restored_favorite = not bool(await connection.fetchval(
                        "SELECT 1 FROM harem WHERE user_id = $1 AND is_favorite = TRUE LIMIT 1",
                        buyer_id,
                    ))

            if is_love_listing:
                assert seller_id is not None
                transferred_love, love_reason = await transfer_love_state_after_market_purchase(
                    connection,
                    seller_id=seller_id,
                    buyer_id=buyer_id,
                    character_id=int(listing["character_id"]),
                )
                if not transferred_love:
                    # The listing is still intact at this point. No state has
                    # changed on a failed helper validation, so a normal result
                    # is safe and retryable.
                    return {"success": False, "reason": love_reason}

            # Delete only after all Love-specific eligibility/state transfer
            # work has succeeded. Any failure from this point must raise so the
            # transaction rolls back both the transfer and this listing delete.
            removed = await connection.fetchval(
                "DELETE FROM market_listings WHERE id = $1 RETURNING id",
                listing_id,
            )
            if removed is None:
                raise RuntimeError("market listing disappeared during purchase")

            harem_row = await connection.fetchrow(
                """
                INSERT INTO harem (user_id, chat_id, character_id, is_favorite, is_ranked)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id AS harem_id, acquired_at
                """,
                buyer_id,
                target_chat_id,
                int(listing["character_id"]),
                restored_favorite,
                is_ranked,
            )
            if not harem_row:
                raise RuntimeError("market harem record was not returned")

            rarity = str(listing["rarity"] or "")
            if kind == MARKET_LISTING_CATALOG:
                # Preserve established official-market behavior: catalog stock
                # grants a new ranked card and debits only the buyer.
                buyer_balance_after = (buyer_balance_before - price).quantize(
                    NEKO_QUANTUM,
                    rounding=ROUND_HALF_UP,
                )
                highest_rarity = _higher_rarity(
                    str(buyer["highest_rarity"]) if buyer["highest_rarity"] else None,
                    rarity,
                )
                total_claims = int(buyer["total_claims"] or 0) + 1
                await connection.execute(
                    """
                    UPDATE users
                    SET neko_balance = $2,
                        total_claims = $3,
                        highest_rarity = $4,
                        last_seen_at = NOW()
                    WHERE user_id = $1
                    """,
                    buyer_id,
                    buyer_balance_after,
                    total_claims,
                    highest_rarity,
                )
                await _record_wallet_ledger(
                    connection,
                    user_id=buyer_id,
                    delta=-price,
                    balance_after=buyer_balance_after,
                    reason="market_purchase",
                    reference_id=listing_id,
                )
            elif restored_self:
                # Full Owner self-purchase is an escrow return, not an economic
                # debit/credit cycle. Restore only its ranked collection totals.
                buyer_balance_after = buyer_balance_before
                if is_ranked:
                    await _recalculate_ranked_user_stats(connection, buyer_id)
                total_claims = int(await connection.fetchval(
                    "SELECT total_claims FROM users WHERE user_id = $1",
                    buyer_id,
                ) or 0)
            else:
                assert seller_id is not None and seller_balance_after is not None
                buyer_balance_after = (buyer_balance_before - price).quantize(
                    NEKO_QUANTUM,
                    rounding=ROUND_HALF_UP,
                )
                await connection.execute(
                    "UPDATE users SET neko_balance = $2, last_seen_at = NOW() WHERE user_id = $1",
                    buyer_id,
                    buyer_balance_after,
                )
                await connection.execute(
                    "UPDATE users SET neko_balance = $2, last_seen_at = NOW() WHERE user_id = $1",
                    seller_id,
                    seller_balance_after,
                )
                await _record_wallet_ledger(
                    connection,
                    user_id=buyer_id,
                    delta=-price,
                    balance_after=buyer_balance_after,
                    reason="market_purchase",
                    reference_id=listing_id,
                )
                await _record_wallet_ledger(
                    connection,
                    user_id=seller_id,
                    delta=price,
                    balance_after=seller_balance_after,
                    reason="market_sale",
                    reference_id=listing_id,
                )
                if is_ranked:
                    await _recalculate_ranked_user_stats(connection, buyer_id)
                total_claims = int(await connection.fetchval(
                    "SELECT total_claims FROM users WHERE user_id = $1",
                    buyer_id,
                ) or 0)

    return {
        "success": True,
        "listing_id": listing_id,
        "listing_kind": kind,
        "price": price,
        "balance_after": buyer_balance_after,
        "total_claims": total_claims,
        "harem_character": {
            "harem_id": int(harem_row["harem_id"]),
            "character_id": int(listing["character_id"]),
            "name": str(listing["name"] or ""),
            "anime": str(listing["anime"] or ""),
            "rarity": rarity,
            "rarity_score": int(listing["rarity_score"] or 0),
            "aliases": str(listing["aliases"] or ""),
            "event": str(listing["event"] or ""),
            "image_url": str(listing["image_url"] or ""),
            "media_type": str(listing["media_type"] or "photo"),
            "is_favorite": restored_favorite,
            "is_ranked": is_ranked,
            "acquired_at": harem_row["acquired_at"],
        },
    }
