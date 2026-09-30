"""Persistent global maintenance (``/kill``) state."""
from __future__ import annotations

from bot.db.pool import get_pool
from bot.services.permissions import can_manage_catalog


async def is_bot_killed() -> bool:
    """Return whether member-facing updates are globally paused."""
    pool = get_pool()
    async with pool.acquire() as conn:
        value = await conn.fetchval(
            "SELECT is_killed FROM bot_runtime_settings WHERE singleton = TRUE"
        )
    return bool(value)


async def set_bot_killed(enabled: bool, changed_by: int) -> None:
    """Persist the global maintenance switch and its latest actor."""
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO bot_runtime_settings (singleton, is_killed, changed_by, changed_at)
            VALUES (TRUE, $1, $2, NOW())
            ON CONFLICT (singleton) DO UPDATE SET
              is_killed = EXCLUDED.is_killed,
              changed_by = EXCLUDED.changed_by,
              changed_at = EXCLUDED.changed_at
            """,
            bool(enabled),
            changed_by,
        )


async def can_bypass_kill_switch(user_id: int | None) -> bool:
    """Root/full owners and catalog admins continue through maintenance mode."""
    return await can_manage_catalog(user_id)
