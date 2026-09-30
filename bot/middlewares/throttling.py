"""Per-user command throttling via Redis.

Protects member slash/dot commands, including owner slash controls.
Callbacks are never throttled because their services remain transaction-safe.
"""
from __future__ import annotations

import asyncio

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, CallbackQuery

from bot.services.redis_client import get_redis
from bot.services.moderation import dot_player_command_name


class ThrottlingMiddleware(BaseMiddleware):
    """Per-user cooldown enforced via Redis INCR + EXPIRE."""

    COOLDOWN_MAP: dict[str, int] = {
        "catch":          3,
        "harem":          5,
        "daily":          3,
        "dailyneko":      3,
        "nekowallet":     3,
        "nekogive":       5,
        "nekoownercheck": 3,
        "deletewallet":   5,
        "backwallet":     5,
        "deletefromharem": 10,
        "sudoneko":       3,
        "doomneko":       3,
        "nekopass":       3,
        "addmarket":      5,
        "ownermarketcheck": 5,
        "deletemarket":   5,
        "editevent":      5,
        "eventchange":    5,
        "emptycodes":     5,
        "casino":         3,
        "check":          3,
        "lovelist":       3,
        "loveselect":     2,
        "kiss":           2,
        "hug":            2,
        "date":           2,
        "romance":        2,
        "sell":           3,
        "fav":            2,
        "gift":           5,
        "trade":          5,
        "topusers":      10,
        "ctop":          10,
        "catchtop":      10,
        "topgroups":    10,
        "getgroups":    10,
        "left":         10,
        "downowner":     5,
        "downadmin":     5,
        "mainlist":      5,
        "boom":          5,
        "boomlist":      5,
        "mute":          5,
        "changemessage":30,
        "spawn":         3,
        "broadcast":     5,
        "stopspawn":     5,
        "restorespawn":  5,
        "stoprarityspawn": 5,
        "spawnstart":    5,
        "kill":          5,
        "unkill":        5,
        "start":         1,
        "miniapp":       3,
        "help":          3,
    }

    async def __call__(self, handler, event: TelegramObject, data):
        user = data.get("event_from_user")
        if not user:
            return await handler(event, data)

        # Never throttle callbacks.
        if isinstance(event, CallbackQuery):
            return await handler(event, data)

        command = self._extract_command(event)
        if not command or command not in self.COOLDOWN_MAP:
            return await handler(event, data)

        cd = self.COOLDOWN_MAP[command]
        redis = get_redis()
        key = f"throttle:{user.id}:{command}"

        count = await redis.incr(key)
        if count == 1:
            await redis.expire(key, cd)

        if count > 1:
            # Cooldown feedback is still a command response, so it follows the
            # same visible two-second delay as accepted commands.
            await asyncio.sleep(2)
            try:
                stripped_text = (event.text or "").lstrip() if isinstance(event, Message) else ""
                command_prefix = "." if stripped_text.startswith(".") else "/"
                await event.answer(
                    f"⏳ Slow down! Try <code>{command_prefix}{command}</code> again in {cd}s.",
                    parse_mode="HTML",
                )
            except Exception:
                pass
            return  # drop the update

        return await handler(event, data)

    @classmethod
    def _extract_command(cls, event) -> str | None:
        """Return a recognized slash or requested dot-command name."""
        if not isinstance(event, Message) or not event.text:
            return None
        stripped = event.text.lstrip()
        if not stripped:
            return None
        first_token = stripped.split(maxsplit=1)[0]
        if first_token.startswith("/"):
            return first_token[1:].split("@", 1)[0].lower()
        dot_command = dot_player_command_name(event.text)
        if dot_command is not None:
            return dot_command
        return None
