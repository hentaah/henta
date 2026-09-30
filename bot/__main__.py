"""Bot entry point. Run with: python -m bot

Sets up:
  - Postgres pool
  - Redis client
  - aiogram Dispatcher with all routers
  - Throttling + Activity middlewares
  - Cleanup scheduler (expires old spawns)
  - Durable OP-spawn scheduler (posts announced automatic OP cards)
  - Long-polling or webhook mode (per USE_WEBHOOK)
"""
from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from bot.config import settings
from bot.db.pool import init_pool, close_pool
from bot.services.redis_client import init_redis, close_redis
from bot.services.spawn_engine import load_character_cache, process_due_op_spawns
from bot.middlewares.throttling import ThrottlingMiddleware
from bot.middlewares.response_delay import CommandResponseDelayMiddleware
from bot.middlewares.activity import ActivityMiddleware
from bot.middlewares.ban import BanMiddleware
from bot.middlewares.membership import RequiredChannelMembershipMiddleware
from bot.middlewares.maintenance import MaintenanceMiddleware

# Routers
from bot.handlers import (
    char_manager, owner_tools, start, catch, harem, henmode, profile, check, fav_gift,
    trade, leaderboards, admin, search, inline_query, rarity, achievement, daily, neko, market, love,
)


logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("HentaCatcher")


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    # These must be *outer* middlewares. Inner middlewares only run after a
    # handler filter matches, which would miss ordinary group messages and
    # make a seven-message anti-spam streak unreliable.
    # Keep maintenance outermost so blocked member updates cannot create
    # activity, anti-spam, or automatic-spawn side effects.
    dp.message.outer_middleware(MaintenanceMiddleware())
    dp.callback_query.outer_middleware(MaintenanceMiddleware())
    dp.inline_query.outer_middleware(MaintenanceMiddleware())
    dp.message.outer_middleware(BanMiddleware())
    dp.callback_query.outer_middleware(BanMiddleware())
    dp.inline_query.outer_middleware(BanMiddleware())
    # Required-channel gating applies only to selected slash commands and runs
    # before activity, so a blocked command cannot advance a group spawn count.
    dp.message.outer_middleware(RequiredChannelMembershipMiddleware())
    # Activity/spam tracking must see every permitted group message before handler
    # filters or command cooldowns can discard it.
    dp.message.outer_middleware(ActivityMiddleware())
    dp.message.middleware(ThrottlingMiddleware())
    # Every normal command waits two seconds before its visible response. /catch
    # is handled specially so its atomic active-spawn claim happens immediately.
    dp.message.middleware(CommandResponseDelayMiddleware())
    # Broadcast staging-channel `/addcharacter` posts have no human sender, so
    # they deliberately skip user-scoped throttle/activity middleware. They do
    # retain the same visible command-response cadence before routing to the
    # secure private deep-link workflow.
    dp.channel_post.middleware(CommandResponseDelayMiddleware())

    # Must be first: start.router has a broad private-photo fallback,
    # while this router owns the photo steps of the character-management FSM.
    dp.include_router(char_manager.router)
    dp.include_router(start.router)
    dp.include_router(catch.router)
    dp.include_router(daily.router)                   # public rolling 24-hour reward
    dp.include_router(neko.router)                    # Neko wallet, Casino, transfers, owner wallet tools
    dp.include_router(market.router)                  # Full-owner /addmarket Mini App listings
    dp.include_router(love.router)                    # player Love cards, progression, Lovelist, fixed /sell
    dp.include_router(harem.router)
    dp.include_router(henmode.router)
    dp.include_router(profile.router)
    dp.include_router(check.router)
    dp.include_router(owner_tools.router)
    dp.include_router(fav_gift.router)
    dp.include_router(trade.router)
    dp.include_router(leaderboards.router)
    dp.include_router(admin.router)                    # includes owner-only /sudo
    dp.include_router(search.router)                   # /search, /eventsearch + rarity inline launchers
    dp.include_router(inline_query.router)             # @BotUsername ... inline mode
    dp.include_router(rarity.router)
    dp.include_router(achievement.router)
    return dp


async def scheduled_op_spawn_loop(bot: Bot):
    """Background task: emit automatic OP cards after their announced delay."""
    while True:
        try:
            emitted = await process_due_op_spawns(bot)
            if emitted:
                log.info("emitted %s scheduled OP spawn(s)", emitted)
        except Exception as exc:
            # A database/network outage must not kill the scheduler; pending
            # rows are durable and will be retried after the next short wait.
            log.warning("scheduled OP spawn loop error: %s", exc)
        await asyncio.sleep(1)


async def cleanup_loop(bot: Bot):
    """Background task: expire spawns that weren't claimed in time."""
    from bot.db.pool import get_pool
    import time
    while True:
        try:
            now = int(time.time())
            pool = get_pool()
            async with pool.acquire() as conn:
                expired = await conn.fetch(
                    "SELECT chat_id, message_id FROM active_spawns WHERE expires_at < $1",
                    now,
                )
                for row in expired:
                    edited = False
                    try:
                        await bot.edit_message_caption(
                            chat_id=row["chat_id"],
                            message_id=row["message_id"],
                            caption="💨 <i>The character escaped! Nobody claimed her in time.</i>",
                            parse_mode="HTML",
                        )
                        edited = True
                    except Exception:
                        pass
                    if not edited:
                        try:
                            await bot.edit_message_text(
                                chat_id=row["chat_id"],
                                message_id=row["message_id"],
                                text="💨 <i>The character escaped! Nobody claimed her in time.</i>",
                                parse_mode="HTML",
                            )
                        except Exception:
                            pass
                if expired:
                    await conn.execute(
                        "DELETE FROM active_spawns WHERE expires_at < $1", now,
                    )
                    log.info(f"expired {len(expired)} spawns")
        except Exception as e:
            log.warning(f"cleanup loop error: {e}")
        await asyncio.sleep(30)


async def main():
    log.info("Starting HentaCatcherBot v1.1.0")

    await init_pool()
    log.info("Postgres pool ready")
    await init_redis()
    log.info("Redis ready")
    n_chars = await load_character_cache()
    log.info(f"Loaded {n_chars} characters into memory")

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = build_dispatcher()

    await bot.delete_webhook(drop_pending_updates=True)
    from aiogram.types import BotCommand
    # Only member-facing commands are published in Telegram's / menu. Owner,
    # catalog-admin, moderation, and maintenance commands remain functional
    # but are documented inside the protected /admin panel instead.
    await bot.set_my_commands([
        BotCommand(command="start",       description="Welcome + main menu"),
        BotCommand(command="miniapp",     description="Open the HentaCatcher Mini App"),
        BotCommand(command="catch",       description="Catch a spawned character"),
        BotCommand(command="harem",       description="View your harem"),
        BotCommand(command="daily",       description="Claim a daily character reward"),
        BotCommand(command="dailyneko",   description="Claim 1–49 daily Neko"),
        BotCommand(command="nekowallet",  description="View your Neko balance"),
        BotCommand(command="casino",      description="Play Casino: Mine or Dice"),
        BotCommand(command="nekogive",    description="Give Neko (reply to a user)"),
        BotCommand(command="henmode",     description="Browse harem by anime or rarity"),
        BotCommand(command="search",      description="Open the inline character catalog"),
        BotCommand(command="eventsearch", description="Browse character cards by event"),
        BotCommand(command="csearch",     description="Browse Common character cards"),
        BotCommand(command="rsearch",     description="Browse Rare character cards"),
        BotCommand(command="lsearch",     description="Browse Legendary character cards"),
        BotCommand(command="msearch",     description="Browse Mythical character cards"),
        BotCommand(command="ssearch",     description="Browse Sovereign character cards"),
        BotCommand(command="osearch",     description="Browse OP character cards"),
        BotCommand(command="esearch",     description="Browse Eternal character cards"),
        BotCommand(command="susearch",    description="Browse Suspicious character cards"),
        BotCommand(command="ovsearch",    description="Browse OverLord character cards"),
        BotCommand(command="check",       description="Inspect a character by ID"),
        BotCommand(command="lovelist",    description="Browse and buy Love cards"),
        BotCommand(command="kiss",        description="Build Love Points"),
        BotCommand(command="hug",         description="Build Love Points"),
        BotCommand(command="date",        description="Open your Love date panel"),
        BotCommand(command="romance",     description="Level 5 Love reward"),
        BotCommand(command="loveselect",  description="Choose an active Love card"),
        BotCommand(command="sell",        description="Sell your active Love card"),
        BotCommand(command="fav",         description="Set a favorite character"),
        BotCommand(command="gift",        description="Gift a character (reply to user)"),
        BotCommand(command="trade",       description="Trade characters (reply to user)"),
        BotCommand(command="topusers",    description="Global top collectors"),
        BotCommand(command="ctop",        description="This group's top collectors"),
        BotCommand(command="topgroups",   description="Global top groups"),
        BotCommand(command="profile",     description="View your collection profile"),
        BotCommand(command="owner",       description="Show the bot owner"),
        BotCommand(command="help",        description="How to play"),
        BotCommand(command="rarity",      description="Your rarity chart"),
        BotCommand(command="achievement", description="Your Collector achievements"),
    ])

    cleanup_task = asyncio.create_task(cleanup_loop(bot), name="spawn-cleanup")
    scheduled_op_task = asyncio.create_task(
        scheduled_op_spawn_loop(bot),
        name="scheduled-op-spawns",
    )

    try:
        if settings.use_webhook:
            from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
            from aiohttp import web

            app = web.Application()
            SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=settings.webhook_path)
            setup_application(app, dp, bot=bot)
            await bot.set_webhook(
                settings.webhook_url,
                secret_token=settings.webhook_secret,
                drop_pending_updates=True,
            )
            log.info(f"Webhook set: {settings.webhook_url}")
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, host=settings.webhook_host, port=settings.webhook_port)
            await site.start()
            log.info(f"Listening on {settings.webhook_host}:{settings.webhook_port}")
            await asyncio.Event().wait()
        else:
            log.info("Starting long polling...")
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        for task in (cleanup_task, scheduled_op_task):
            task.cancel()
        for task in (cleanup_task, scheduled_op_task):
            try:
                await task
            except asyncio.CancelledError:
                pass
        await close_pool()
        await close_redis()
        log.info("Shutdown complete")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Stopped by user")
