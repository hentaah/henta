"""Apply non-destructive database schema migrations.

Run this against an existing deployment after upgrading the bot:
    python -m bot.db.migrate

Unlike ``bot.db.init_db``, this does not import or overwrite character CSV
rows. It only executes the idempotent schema and data-normalization migrations.
"""
from __future__ import annotations

import asyncio

from bot.db.pool import close_pool, get_pool, init_pool
from bot.db.schema import SCHEMA_SQL


async def main() -> None:
    await init_pool()
    try:
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.execute(SCHEMA_SQL)
        print("✓ Database migrations applied")
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
