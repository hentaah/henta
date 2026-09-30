"""Uniform two-second visible-response delay for bot text commands.

The middleware intentionally delays handler execution only for ordinary commands.
`/catch` is exempt so its state mutation can acquire the active-spawn lock without
waiting; its handler delays every visible reply after the claim decision instead.
"""
from __future__ import annotations

import asyncio

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject

from bot.services.moderation import dot_player_command_name


class CommandResponseDelayMiddleware(BaseMiddleware):
    """Wait two seconds before normal slash and requested dot-command execution."""

    DELAY_SECONDS = 2.0
    IMMEDIATE_STATE_COMMANDS = frozenset({"catch"})

    @classmethod
    def _command_name(cls, event: TelegramObject) -> str | None:
        if not isinstance(event, Message) or not event.text:
            return None
        token = event.text.lstrip().split(maxsplit=1)[0] if event.text.lstrip() else ""
        if token.startswith("/"):
            return token[1:].split("@", 1)[0].casefold() or None
        dot_command = dot_player_command_name(event.text)
        if dot_command is not None:
            return dot_command
        return None

    async def __call__(self, handler, event: TelegramObject, data):
        command = self._command_name(event)
        if command and command not in self.IMMEDIATE_STATE_COMMANDS:
            await asyncio.sleep(self.DELAY_SECONDS)
        return await handler(event, data)
