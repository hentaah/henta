"""Global member-access gate used by the owner ``/kill`` switch."""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, InlineQuery, Message, TelegramObject

from bot.services.maintenance import can_bypass_kill_switch, is_bot_killed


class MaintenanceMiddleware(BaseMiddleware):
    """Silently block member messages, callbacks, and inline queries on kill.

    The switch intentionally applies before activity/spawn handling, so paused
    member chatter does not advance group counters. Root/full owners and
    catalog admins are the explicit bypass roles requested for maintenance.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, (Message, CallbackQuery, InlineQuery)):
            return await handler(event, data)

        user = event.from_user
        if not user or not await is_bot_killed():
            return await handler(event, data)
        if await can_bypass_kill_switch(user.id):
            return await handler(event, data)

        # No acknowledgement is sent: for ordinary members the bot is meant
        # to behave as fully off in every update surface.
        return None
