"""Common helpers for handlers."""
from __future__ import annotations

import re

from aiogram import F
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def claim_keyboard(character_id: int) -> InlineKeyboardMarkup:
    """Keyboard shown after a successful claim."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⭐ Favorite", callback_data=f"fav:{character_id}", style="primary"),
        InlineKeyboardButton(text="📦 Harem",    callback_data="openharem:1", style="primary"),
    ]])


def see_collection_keyboard(user_id: int, total_owned: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=f"🖼 See my collection ({total_owned})",
            switch_inline_query_current_chat=f"harem.{user_id}",
            style="primary",
        ),
    ]])


def dot_player_command_filter(command: str, *, accepts_arguments: bool = False):
    """Match one exact requested ``.command`` alias at a safe text boundary."""
    suffix = r"(?:\s+.*)?$" if accepts_arguments else r"\s*$"
    return F.text.regexp(rf"(?i)^\s*\.{re.escape(command)}{suffix}")


def non_command_reply_text_filter():
    """Allow reply-input handlers to ignore all punctuation-prefixed commands.

    A broad reply handler must not briefly match `.gift`, `.trade`, or an
    owner `/broadcast` reply before its real command handler. In aiogram an
    inner middleware can run for that first matched handler even when it later
    raises ``SkipHandler``; excluding these messages at filter time prevents a
    false second throttle count. Exclamation-prefixed text is ordinary after
    the owner controls moved back to slash commands.
    """
    return F.text.regexp(r"(?s)^(?!\s*(?:/|\.)).+$")
