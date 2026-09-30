"""Pillow renderer for the reference-style visual `/ctop` leaderboard."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter, ImageFont


# The supplied reference is a wide Telegram-friendly leaderboard composition.
IMAGE_WIDTH = 1920
MIN_IMAGE_HEIGHT = 1080
HEADER_HEIGHT = 155
ROW_HEIGHT = 90
BOTTOM_PADDING = 25
BACKGROUND_PATH = Path(__file__).resolve().parents[1] / "assets" / "leaderboard_galaxy.jpg"

TITLE_COLOR = "#DEC9AA"
TEXT_COLOR = "#B5F4F5"
BADGE_COLOR = "#C9A77B"
CLAIMS_COLOR = "#BDF7F5"
RANK_COLORS = {
    1: "#C2FBFF",
    2: "#AAECF3",
    3: "#B8D8DF",
}
DEFAULT_RANK_COLOR = "#B8CDD8"

_FONT_DIR = Path("/usr/share/fonts/truetype/dejavu")
SERIF_FONT_PATH = _FONT_DIR / "DejaVuSerif.ttf"
SERIF_BOLD_FONT_PATH = _FONT_DIR / "DejaVuSerif-Bold.ttf"
SANS_FONT_PATH = _FONT_DIR / "DejaVuSans.ttf"
SANS_BOLD_FONT_PATH = _FONT_DIR / "DejaVuSans-Bold.ttf"


def _font(path: Path, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Load an installed system font, retaining a safe Pillow fallback."""
    try:
        return ImageFont.truetype(path, size=size)
    except OSError:
        return ImageFont.load_default()


def _cover_background(size: tuple[int, int]) -> Image.Image:
    """Center-crop the supplied blank galaxy art to the output canvas."""
    with Image.open(BACKGROUND_PATH) as source:
        source = source.convert("RGBA")
        scale = max(size[0] / source.width, size[1] / source.height)
        resized = source.resize(
            (round(source.width * scale), round(source.height * scale)),
            Image.Resampling.LANCZOS,
        )

    left = (resized.width - size[0]) // 2
    top = (resized.height - size[1]) // 2
    return resized.crop((left, top, left + size[0], top + size[1]))


def _value(row: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def _display_name(row: Mapping[str, Any]) -> str:
    username = str(_value(row, "username") or "").strip()
    if username:
        return username if username.startswith("@") else f"@{username}"

    full_name = " ".join(str(_value(row, "full_name") or "Player").split())
    return full_name or "Player"


def _rank_badge(row: Mapping[str, Any]) -> str:
    """Use a supplied player title/rarity, with an honest generic fallback."""
    raw = " ".join(
        str(_value(row, "rank_label", "title", "highest_rarity") or "COLLECTOR").split()
    )
    raw = raw.strip("[] ") or "COLLECTOR"
    return f"[{raw.upper()}]"


def _claim_text(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value if value not in (None, "") else 0)


def _truncate_to_width(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    max_width: int,
) -> str:
    """Keep a one-line label within its visual column."""
    if draw.textlength(text, font=font) <= max_width:
        return text

    ellipsis = "…"
    while text and draw.textlength(text + ellipsis, font=font) > max_width:
        text = text[:-1]
    return f"{text}{ellipsis}" if text else ellipsis


def _tracked_text_width(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    tracking: int,
) -> float:
    return sum(draw.textlength(char, font=font) for char in text) + tracking * max(0, len(text) - 1)


def _draw_centered_tracked_text(
    draw: ImageDraw.ImageDraw,
    *,
    center_x: int,
    center_y: int,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    fill: str | tuple[int, int, int, int],
    tracking: int,
    stroke_width: int = 0,
    stroke_fill: str | tuple[int, int, int, int] | None = None,
) -> None:
    """Draw the reference's airy, letter-spaced serif headline."""
    x = center_x - _tracked_text_width(draw, text, font, tracking) / 2
    for char in text:
        draw.text(
            (x, center_y),
            char,
            font=font,
            fill=fill,
            anchor="lm",
            stroke_width=stroke_width,
            stroke_fill=stroke_fill,
        )
        x += draw.textlength(char, font=font) + tracking


def create_leaderboard_image(rows: Iterable[Mapping[str, Any]]) -> bytes:
    """Return a landscape PNG leaderboard for every supplied row.

    The layout follows the supplied reference: a softly darkened galaxy, an
    elegant top-centred heading, glowing left-aligned player rows, and claim
    counts aligned at the far right.  The canvas grows when a caller provides more
    than the usual top ten, so no supplied row is silently omitted.
    """
    entries = list(rows)
    image_height = max(
        MIN_IMAGE_HEIGHT,
        HEADER_HEIGHT + len(entries) * ROW_HEIGHT + BOTTOM_PADDING,
    )
    canvas = _cover_background((IMAGE_WIDTH, image_height))
    canvas = Image.alpha_composite(
        canvas,
        Image.new("RGBA", canvas.size, (4, 10, 30, 112)),
    )

    title_font = _font(SERIF_FONT_PATH, 104)
    rank_font = _font(SERIF_BOLD_FONT_PATH, 82)
    badge_font = _font(SANS_FONT_PATH, 40)
    user_font = _font(SANS_FONT_PATH, 68)
    claims_font = _font(SANS_FONT_PATH, 78)

    rank_x = 190
    badge_x = 285
    minimum_name_x = 500
    claims_x = IMAGE_WIDTH - 145
    badge_max_width = 280

    # A single blurred layer creates the cyan/warm ambient glow in the
    # reference without making each row expensive to render separately.
    glow_layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow_layer)
    _draw_centered_tracked_text(
        glow_draw,
        center_x=IMAGE_WIDTH // 2,
        center_y=100,
        text="LEADERBOARD",
        font=title_font,
        fill=(229, 204, 166, 145),
        tracking=4,
    )

    rendered_rows: list[tuple[int, int, int, str, str, str]] = []
    for rank, row in enumerate(entries, start=1):
        row_center = HEADER_HEIGHT + (rank - 1) * ROW_HEIGHT + ROW_HEIGHT // 2
        rank_text = str(rank)
        badge = _truncate_to_width(
            glow_draw,
            _rank_badge(row),
            badge_font,
            badge_max_width,
        )
        name_x = max(
            minimum_name_x,
            int(badge_x + glow_draw.textlength(badge, font=badge_font) + 35),
        )
        name = _truncate_to_width(
            glow_draw,
            _display_name(row),
            user_font,
            claims_x - name_x - 270,
        )
        claims = _claim_text(_value(row, "claims", "total_claims", "rarity_score", "score"))
        rendered_rows.append((rank, row_center, name_x, badge, name, claims))

        glow_draw.text(
            (rank_x, row_center),
            rank_text,
            font=rank_font,
            fill=(110, 244, 255, 190),
            anchor="mm",
        )
        glow_draw.text(
            (name_x, row_center),
            name,
            font=user_font,
            fill=(92, 242, 249, 165),
            anchor="lm",
        )
        glow_draw.text(
            (claims_x, row_center),
            claims,
            font=claims_font,
            fill=(91, 244, 237, 175),
            anchor="rm",
        )

    canvas = Image.alpha_composite(canvas, glow_layer.filter(ImageFilter.GaussianBlur(radius=12)))
    draw = ImageDraw.Draw(canvas)

    # A subtle dark edge keeps the elegant headline readable over bright stars.
    _draw_centered_tracked_text(
        draw,
        center_x=IMAGE_WIDTH // 2,
        center_y=103,
        text="LEADERBOARD",
        font=title_font,
        fill=TITLE_COLOR,
        tracking=4,
        stroke_width=1,
        stroke_fill="#5D4734",
    )

    for rank, row_center, name_x, badge, name, claims in rendered_rows:
        draw.text(
            (rank_x, row_center),
            str(rank),
            font=rank_font,
            fill=RANK_COLORS.get(rank, DEFAULT_RANK_COLOR),
            anchor="mm",
            stroke_width=1,
            stroke_fill="#24374B",
        )
        draw.text(
            (badge_x, row_center),
            badge,
            font=badge_font,
            fill=BADGE_COLOR,
            anchor="lm",
            stroke_width=1,
            stroke_fill="#211A1A",
        )
        draw.text(
            (name_x, row_center),
            name,
            font=user_font,
            fill=TEXT_COLOR,
            anchor="lm",
            stroke_width=1,
            stroke_fill="#18394A",
        )
        draw.text(
            (claims_x, row_center),
            claims,
            font=claims_font,
            fill=CLAIMS_COLOR,
            anchor="rm",
            stroke_width=1,
            stroke_fill="#183E48",
        )

    output = BytesIO()
    canvas.convert("RGB").save(output, format="PNG", optimize=True)
    return output.getvalue()
