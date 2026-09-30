"""Structured text-block helpers for bot messages."""
from __future__ import annotations

from typing import Any


def paragraph(*rich_text_items: dict) -> dict:
    return {"type": "paragraph", "rich_text": list(rich_text_items)}


def heading(text: str) -> dict:
    return {"type": "section_heading", "rich_text": [text_plain(text)]}


def footer(text: str) -> dict:
    return {"type": "footer", "rich_text": [text_plain(text)]}


def divider() -> dict:
    return {"type": "divider"}


def blockquote(*rich_text_items: dict) -> dict:
    return {"type": "blockquote", "rich_text": list(rich_text_items)}


def pullquote(*rich_text_items: dict) -> dict:
    return {"type": "pullquote", "rich_text": list(rich_text_items)}


def list_block(items: list[list[dict]]) -> dict:
    return {
        "type": "list",
        "items": [{"rich_text": it} for it in items],
    }


def table(headers: list[str], rows: list[list[str]]) -> dict:
    return {
        "type": "table",
        "header": [{"rich_text": [text_plain(h)]} for h in headers],
        "rows": [
            [{"rich_text": [text_plain(c)]} for c in row]
            for row in rows
        ],
    }


def math_block(expression: str) -> dict:
    return {
        "type": "mathematical_expression",
        "expression": expression,
        "format": "latex",
    }


def details(summary: list[dict], content: list[dict]) -> dict:
    return {
        "type": "details",
        "summary": {"rich_text": summary},
        "content": content,
    }


# ─── Rich text leaf builders ─────────────────────────────────────

def text_plain(text: str) -> dict:
    return {"type": "plain", "text": text}


def text_bold(text: str) -> dict:
    return {"type": "bold", "text": text}


def text_italic(text: str) -> dict:
    return {"type": "italic", "text": text}


def text_underline(text: str) -> dict:
    return {"type": "underline", "text": text}


def text_strikethrough(text: str) -> dict:
    return {"type": "strikethrough", "text": text}


def text_spoiler(text: str) -> dict:
    return {"type": "spoiler", "text": text}


def text_code(text: str) -> dict:
    return {"type": "code", "text": text}


def text_mention(user_id: int, text: str) -> dict:
    return {"type": "text_mention", "user_id": user_id, "text": text}


def text_url(text: str, url: str) -> dict:
    return {"type": "url", "text": text, "url": url}


def text_math(expression: str) -> dict:
    return {"type": "mathematical_expression", "expression": expression, "format": "latex"}


def text_custom_emoji(emoji_id: str, fallback: str) -> dict:
    return {"type": "custom_emoji", "custom_emoji_id": emoji_id, "text": fallback}


def text_marked(text: str) -> dict:
    return {"type": "marked", "text": text}


# ─── Rarity helpers ──────────────────────────────────────────────

def rarity_symbol(rarity: str) -> str:
    """Symbol used next to rarity name in all cards."""
    return {
        "Love":      "❤️",
        "OverLord":  "☬",
        "Suspicious": "ᯓ",
        "Eternal":   "𖣘",
        "OP":        "亗",
        "Sovereign": "♛",
        "Mythical":  "✵",
        "Legendary": "✪",
        "Rare":      "◈",
        "Common":    "✧",
    }.get(rarity, "❓")


def rarity_label(rarity: str) -> str:
    """Monospace-font rarity label."""
    return {
        "Love":      "𝙻𝙾𝚅𝙴",
        "OverLord":  "𝙾𝚅𝙴𝚁𝙻𝙾𝚁𝙳",
        "Suspicious": "𝚂𝚄𝚂𝙿𝙸𝙲𝙸𝙾𝚄𝚂",
        "Eternal":   "𝙴𝚃𝙴𝚁𝙽𝙰𝙻",
        "OP":        "𝙾𝙿",
        "Sovereign": "𝚂𝙾𝚅𝙴𝚁𝙴𝙸𝙶𝙽",
        "Mythical":  "𝙼𝚈𝚃𝙷𝙸𝙲𝙰𝙻",
        "Legendary": "𝙻𝙴𝙶𝙴𝙽𝙳𝙰𝚁𝚈",
        "Rare":      "𝚁𝙰𝚁𝙴",
        "Common":    "𝙲𝙾𝙼𝙼𝙾𝙽",
    }.get(rarity, rarity)


def rarity_label_bold(rarity: str) -> str:
    """Bold-font rarity label (used in catch card and inline search)."""
    return {
        "Love":      "𝗟𝗢𝗩𝗘",
        "OverLord":  "𝗢𝗩𝗘𝗥𝗟𝗢𝗥𝗗",
        "Suspicious": "𝗦𝗨𝗦𝗣𝗜𝗖𝗜𝗢𝗨𝗦",
        "Eternal":   "𝗘𝗧𝗘𝗥𝗡𝗔𝗟",
        "OP":        "𝗢𝗣",
        "Sovereign": "𝗦𝗢𝗩𝗘𝗥𝗘𝗜𝗚𝗡",
        "Mythical":  "𝗠𝗬𝗧𝗛𝗜𝗖𝗔𝗟",
        "Legendary": "𝗟𝗘𝗚𝗘𝗡𝗗𝗔𝗥𝗬",
        "Rare":      "𝗥𝗔𝗥𝗘",
        "Common":    "𝗖𝗢𝗠𝗠𝗢𝗡",
    }.get(rarity, rarity)


def event_emoji(event: str | None) -> str:
    """Extract displayable emoji/symbols from a catalog event label.

    Admins keep the complete label (for example ``bunny🐰``) in the database,
    while compact cards intentionally show only its visual marker.  ``So``
    covers ordinary emoji and symbols; variation selectors, skin tones, and
    joiners are retained when they belong to a symbol sequence.  A named event
    without an emoji receives a stable celebratory fallback instead of leaking
    its full label into compact harem/search/gift cards.
    """
    if not event or not event.strip():
        return ""

    import unicodedata

    marker: list[str] = []
    previous_was_symbol = False
    for char in event.strip():
        category = unicodedata.category(char)
        if category == "So":
            marker.append(char)
            previous_was_symbol = True
        elif previous_was_symbol and (
            char == "\u200d"
            or char in {"\ufe0e", "\ufe0f"}
            or category in {"Mn", "Me", "Sk"}
        ):
            marker.append(char)
        else:
            # Whitespace/letters delimit emoji sequences.  Keeping the last
            # visible symbol as the active base is harmless for consecutive
            # emoji while preventing arbitrary text from being added.
            previous_was_symbol = False

    return "".join(marker) or "🎊"


def event_badge(event: str | None) -> str:
    """Return the compact ``[🐰]``-style marker or an empty string."""
    emoji = event_emoji(event)
    return f"[{emoji}]" if emoji else ""


# ─── Convenience renderers ────────────────────────────────────────

def render_leaderboard_table(title: str, headers: list[str], rows: list[list[str]]) -> list[dict]:
    return [
        heading(title),
        table(headers, rows),
        footer("Updated live • HentaCatcherBot"),
    ]


def render_stats_with_math(stats: dict) -> list[dict]:
    caught = stats.get("total_caught", 0)
    unique = stats.get("unique_chars", 0)
    return [
        heading("📊 Your Collection Stats"),
        list_block([
            [text_plain("Total claims (all groups): "), text_bold(str(caught))],
            [text_plain("Unique chars in this chat: "), text_bold(str(unique))],
        ]),
    ]


_ANIME_EMOJI_POOL = ["🎬", "📺", "🎮", "🌸", "⚔️", "🔥", "💫", "🎭", "🖤", "🌙"]


def anime_emoji(anime: str) -> str:
    if not anime:
        return "📺"
    import hashlib
    digest = hashlib.md5(anime.encode("utf-8")).hexdigest()
    return _ANIME_EMOJI_POOL[int(digest, 16) % len(_ANIME_EMOJI_POOL)]


# ─── HTML fallback ────────────────────────────────────────────────

def blocks_to_html(blocks: list[dict]) -> str:
    out: list[str] = []
    for b in blocks:
        t = b.get("type")
        if t == "section_heading":
            out.append(f"<b>{_rich_to_html(b.get('rich_text', []))}</b>")
        elif t == "paragraph":
            out.append(_rich_to_html(b.get("rich_text", [])))
        elif t == "footer":
            out.append(f"<i>{_rich_to_html(b.get('rich_text', []))}</i>")
        elif t == "divider":
            out.append("— — — — —")
        elif t == "blockquote":
            out.append(f"<blockquote>{_rich_to_html(b.get('rich_text', []))}</blockquote>")
        elif t == "list":
            for it in b.get("items", []):
                out.append(f"• {_rich_to_html(it.get('rich_text', []))}")
        elif t == "table":
            headers = b.get("header", [])
            rows = b.get("rows", [])
            if headers:
                h = " | ".join(_rich_to_html(h.get("rich_text", [])) for h in headers)
                out.append(f"<b>{h}</b>")
            for r in rows:
                out.append(" | ".join(_rich_to_html(c.get("rich_text", [])) for c in r))
        elif t == "mathematical_expression":
            out.append(f"<code>{b.get('expression', '')}</code>")
    return "\n".join(out)


def _rich_to_html(items: list[dict] | str) -> str:
    if isinstance(items, str):
        return items
    parts = []
    for it in items:
        t = it.get("type", "plain")
        text = it.get("text", "")
        if t == "bold":
            parts.append(f"<b>{text}</b>")
        elif t == "italic":
            parts.append(f"<i>{text}</i>")
        elif t == "underline":
            parts.append(f"<u>{text}</u>")
        elif t == "strikethrough":
            parts.append(f"<s>{text}</s>")
        elif t == "spoiler":
            parts.append(f"<tg-spoiler>{text}</tg-spoiler>")
        elif t == "code":
            parts.append(f"<code>{text}</code>")
        elif t == "url":
            parts.append(f'<a href="{it.get("url", "")}">{text}</a>')
        elif t == "text_mention":
            parts.append(f'<a href="tg://user?id={it.get("user_id", 0)}">{text}</a>')
        elif t == "mathematical_expression":
            parts.append(f"<code>{it.get('expression', '')}</code>")
        else:
            parts.append(text)
    return "".join(parts)
