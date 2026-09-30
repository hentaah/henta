"""Persistent privilege checks for the root owner, full owners, and catalog admins."""
from __future__ import annotations

from bot.config import settings
from bot.db.pool import get_pool


def is_root_owner(user_id: int | None) -> bool:
    """Only the OWNER_ID configured in .env is the non-delegable root owner."""
    return bool(user_id is not None and user_id == settings.owner_id)


async def is_bot_owner(user_id: int | None) -> bool:
    """Return True for the root owner or a user promoted by /upowner."""
    if is_root_owner(user_id):
        return True
    if user_id is None:
        return False

    pool = get_pool()
    async with pool.acquire() as conn:
        return bool(
            await conn.fetchval(
                "SELECT 1 FROM bot_owners WHERE user_id = $1",
                user_id,
            )
        )


async def can_manage_catalog(user_id: int | None) -> bool:
    """Full owners plus /upadmin users may manage catalog cards and events."""
    if await is_bot_owner(user_id):
        return True
    if user_id is None:
        return False

    pool = get_pool()
    async with pool.acquire() as conn:
        return bool(
            await conn.fetchval(
                "SELECT 1 FROM bot_admins WHERE user_id = $1",
                user_id,
            )
        )


async def grant_catalog_admin(target_user_id: int, granted_by: int) -> None:
    """Grant only catalog add/update permissions to a Telegram user."""
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO bot_admins (user_id, granted_by, granted_at)
            VALUES ($1, $2, NOW())
            ON CONFLICT (user_id) DO UPDATE SET
              granted_by = EXCLUDED.granted_by,
              granted_at = NOW()
            """,
            target_user_id,
            granted_by,
        )


async def grant_bot_owner(target_user_id: int, granted_by: int) -> None:
    """Grant the full-owner role and remove any narrower catalog-only role."""
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO bot_owners (user_id, granted_by, granted_at)
                VALUES ($1, $2, NOW())
                ON CONFLICT (user_id) DO UPDATE SET
                  granted_by = EXCLUDED.granted_by,
                  granted_at = NOW()
                """,
                target_user_id,
                granted_by,
            )
            await conn.execute(
                "DELETE FROM bot_admins WHERE user_id = $1",
                target_user_id,
            )


async def revoke_bot_owner(target_user_id: int) -> bool:
    """Remove only a delegated full owner; the configured root is immutable."""
    if not isinstance(target_user_id, int) or target_user_id <= 0 or is_root_owner(target_user_id):
        return False

    pool = get_pool()
    async with pool.acquire() as conn:
        removed = await conn.fetchval(
            "DELETE FROM bot_owners WHERE user_id = $1 RETURNING user_id",
            target_user_id,
        )
    return removed is not None


async def revoke_catalog_admin(target_user_id: int) -> bool:
    """Remove a standalone catalog admin without changing any full owner."""
    if not isinstance(target_user_id, int) or target_user_id <= 0 or is_root_owner(target_user_id):
        return False

    pool = get_pool()
    async with pool.acquire() as conn:
        # A correct grant flow removes this duplicate automatically. The guard
        # still protects a manually altered legacy database from using /downadmin to
        # mutate a full owner's role record.
        removed = await conn.fetchval(
            """
            DELETE FROM bot_admins AS admin
            WHERE admin.user_id = $1
              AND NOT EXISTS (
                  SELECT 1 FROM bot_owners AS owner
                  WHERE owner.user_id = admin.user_id
              )
            RETURNING admin.user_id
            """,
            target_user_id,
        )
    return removed is not None


async def list_role_members() -> dict[str, list[int]]:
    """Return stable numeric full-owner and standalone-admin rosters.

    The configured root owner is included even though it deliberately has no
    removable row in ``bot_owners``. Admin rows that overlap an owner row are
    excluded so /mainlist describes each account's effective highest role once.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        owner_rows = await conn.fetch(
            "SELECT user_id FROM bot_owners WHERE user_id <> $1 ORDER BY user_id ASC",
            settings.owner_id,
        )
        admin_rows = await conn.fetch(
            """
            SELECT admin.user_id
            FROM bot_admins AS admin
            LEFT JOIN bot_owners AS owner ON owner.user_id = admin.user_id
            WHERE admin.user_id <> $1 AND owner.user_id IS NULL
            ORDER BY admin.user_id ASC
            """,
            settings.owner_id,
        )

    owner_ids = sorted({int(settings.owner_id), *(int(row["user_id"]) for row in owner_rows)})
    admin_ids = sorted({int(row["user_id"]) for row in admin_rows} - set(owner_ids))
    return {"owner_ids": owner_ids, "admin_ids": admin_ids}
