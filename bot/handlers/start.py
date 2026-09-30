"""/start — welcome screen with a minimal menu (Help, Owner, Add to group)."""
from __future__ import annotations

from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from bot.config import settings
from bot.services.permissions import is_bot_owner

router = Router()

# Static Telegram-hosted artwork.  These file IDs avoid a fresh upload on every
# command while keeping the welcome and wallet text in the same media caption.
START_IMAGE_FILE_ID = "AgACAgQAAyEFAAT_2IEuAAICG2qlp3rsp6C8zUhU9TLLjI8PMjBOAAJnDmsb59wwUVPusMnYmSiFAQADAgADeQADPQQ"
MINIAPP_IMAGE_FILE_ID = "AgACAgQAAyEFAAT_2IEuAAICHWqmOyeE79oXsiRTAAJxnGZgfdK8AAK1Dmsb59wwUbYKjXbzvft0AQADAgADeQADPQQ"
MINIAPP_URL = "https://t.me/HentaCatcherBot/HentaCatcher"
MINIAPP_CAPTION = "To open the Mini App, tap the button below."


def main_menu_keyboard() -> InlineKeyboardMarkup:
    add_to_group_url = f"https://t.me/{settings.bot_username}?startgroup=true"
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📖 Help",  callback_data="menu:help", style="primary"),
            InlineKeyboardButton(text="👑 Owner", callback_data="menu:owner", style="primary"),
        ],
        [InlineKeyboardButton(text="➕ Add to your group", url=add_to_group_url, style="success")],
    ])


def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⬅️ Back", callback_data="menu:back", style="primary"),
    ]])


def miniapp_keyboard() -> InlineKeyboardMarkup:
    """One primary-styled URL button that opens the configured Telegram Mini App."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="🚀 Open Mini App",
            url=MINIAPP_URL,
            style="primary",
        ),
    ]])


WELCOME_HTML = (
    "<b>🙋‍♀️✨ Welcome to HentaCatcherBot!</b>\n"
    "━━━━━━━━━━━━━━━━━━━━━━\n\n"
    "✨ After enough group messages, a new character appears.\n"
    "✨ Every <b>catch</b> expands your harem.\n"
    "✨ Every <b>rarity</b> brings a new challenge.\n\n"
    "<i>🥳 Add me to your group and start your journey today!</i>\n\n"
    "<b>👇 Tap a button below to begin.</b>"
)

HELP_HTML = (
    "<b>📖 Help — HentaCatcherBot</b>\n"
    "━━━━━━━━━━━━━━━━━━━━━━\n\n"
    "<b>How to play:</b>\n"
    "1️⃣  Add the bot to a group as admin.\n"
    "2️⃣  After enough messages, a character spawns.\n"
    "3️⃣  Wait 4 seconds after it appears, then use <code>/catch &lt;name&gt;</code> to claim her.\n"
    "4️⃣  Rarer characters score higher. You can catch the same "
    "character more than once.\n"
    "5️⃣  Each player can catch up to <b>25</b> characters per shared "
    "9-hour window.\n"
    "🔒 <code>/catch</code>, <code>/harem</code>, <code>/gift</code>, "
    "<code>/check</code>, and <code>/casino</code> require membership in both official channels.\n\n"
    "<b>Useful commands:</b>\n"
    "<code>/catch &lt;name&gt;</code> — claim the current spawn\n"
    "<code>/harem</code> — view your collection\n"
    "<code>/daily</code> — claim one personal 24-hour character reward\n"
    "<code>/dailyneko</code> — claim an independent personal 24-hour reward of 1–49 Neko\n"
    "<code>/nekowallet</code> — view your Neko balance, rank, and Pass state\n"
    "<code>/miniapp</code> — open the HentaCatcher Mini App\n"
    "<code>/casino</code> — choose Mine or Dice, then reply with a 50–2,000 Neko stake (Mine: 3×3/one bomb; Dice: Odd/Even pays x1.5, exact face pays x2.5; a Dice loss burns only its stake; 70-second personal start cooldown)\n"
    "<i>Dot shortcuts:</i> <code>.casino</code>, <code>.harem</code>, <code>.trade</code>, <code>.gift</code>, and <code>.henmode</code> work like their slash commands.\n"
    "<code>/nekogive &lt;amount&gt;</code> — reply to a user, then confirm a Neko transfer (up to 2 decimals)\n"
    "<code>/search</code> — open the full inline catalog (scroll for more cards)\n"
    "<i>Inline name search:</i> type <code>@BotUsername Alya</code> in any chat\n"
    "<code>/eventsearch</code> — choose an event, then browse only its character cards\n"
    "<code>/csearch</code> / <code>/rsearch</code> / <code>/lsearch</code> — browse a rarity tier\n"
    "<code>/msearch</code> / <code>/ssearch</code> / <code>/osearch</code> / <code>/esearch</code> — browse a rarity tier\n"
    "<code>/susearch</code> / <code>/ovsearch</code> — browse Suspicious / OverLord cards\n"
    "<code>/check &lt;character_id&gt; [up to 3 IDs]</code> — inspect any character\n"
    "<code>/check L1 1</code> — preview a public Love art level (1–5)\n"
    "<code>/lovelist</code> — browse and buy one Love card with Neko\n"
    "<code>/kiss</code> / <code>/hug</code> — build points with your active Love card\n"
    "<code>/date</code> — open the private Restaurant/Cinema Love panel\n"
    "<code>/romance</code> — Level 5 safe Love reward (24-hour cooldown)\n"
    "<code>/loveselect L1</code> — choose an active owner-granted Love card\n"
    "<code>/sell</code> — list your active Love card at its fixed original price\n"
    "<code>/profile</code> — your personal stats\n"
    "<code>/rarity</code> — your rarity collection chart\n"
    "<code>/achievement</code> — Collector achievements and reward progress\n"
    "<code>/topusers</code> / <code>/ctop</code> — leaderboards"
)

OWNER_HTML = (
    "<b>👑 Owner</b>\n"
    "━━━━━━━━━━━━━━━━━━━━━━\n\n"
    "<b>Bot Owner ID:</b> <code>{owner_id}</code>"
)


async def _send_welcome_photo(message: Message) -> None:
    """Send the fixed welcome artwork with the welcome copy as one caption."""
    await message.answer_photo(
        photo=START_IMAGE_FILE_ID,
        caption=WELCOME_HTML,
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(),
    )


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    """Open ordinary welcome content or a protected staged-catalog deep link."""
    parts = (message.text or "").strip().split(maxsplit=1)
    payload = parts[1].strip() if len(parts) == 2 else ""
    if payload:
        # Import locally to keep the basic welcome router independent from the
        # catalog FSM at module-import time. The handler itself is registered
        # after char_manager in the dispatcher and resumes only recognized
        # `addmedia_` payloads.
        from bot.handlers.char_manager import resume_staged_catalog_add_from_start
        if await resume_staged_catalog_add_from_start(message, state, payload):
            return
    await _send_welcome_photo(message)


@router.message(Command("miniapp"))
async def cmd_miniapp(message: Message) -> None:
    """Send the fixed Mini App artwork and its primary URL launcher button."""
    await message.answer_photo(
        photo=MINIAPP_IMAGE_FILE_ID,
        caption=MINIAPP_CAPTION,
        reply_markup=miniapp_keyboard(),
    )


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        HELP_HTML,
        parse_mode="HTML",
        reply_markup=back_keyboard(),
        disable_web_page_preview=True,
    )


@router.message(Command("owner"))
async def cmd_owner(message: Message):
    await message.answer(
        OWNER_HTML.format(owner_id=settings.owner_id),
        parse_mode="HTML",
        reply_markup=back_keyboard(),
        disable_web_page_preview=True,
    )


async def _replace_menu_with_text(
    callback: CallbackQuery,
    text: str,
) -> None:
    """Replace the photo menu with one normal Help/Owner text card.

    Telegram cannot turn a photo message into a text message with edit_text,
    so removing the old photo first prevents the old menu from remaining above
    a separate response. The return path creates the welcome photo again.
    """
    if not callback.message:
        return
    try:
        await callback.message.delete()
    except Exception:
        # A deleted/expired menu can still be followed by a fresh text card.
        pass
    await callback.message.answer(
        text,
        parse_mode="HTML",
        reply_markup=back_keyboard(),
        disable_web_page_preview=True,
    )


@router.callback_query(F.data == "menu:back")
async def cb_back(callback: CallbackQuery):
    if callback.message:
        try:
            await callback.message.delete()
        except Exception:
            pass
        try:
            await _send_welcome_photo(callback.message)
        except Exception:
            # Preserve navigation if Telegram ever invalidates the configured
            # media ID; this fallback intentionally remains exceptional.
            await callback.message.answer(
                WELCOME_HTML,
                parse_mode="HTML",
                reply_markup=main_menu_keyboard(),
                disable_web_page_preview=True,
            )
    await callback.answer("Back to menu")


@router.callback_query(F.data == "menu:help")
async def cb_help(callback: CallbackQuery):
    await _replace_menu_with_text(callback, HELP_HTML)
    await callback.answer("Help")


@router.callback_query(F.data == "menu:owner")
async def cb_owner(callback: CallbackQuery):
    await _replace_menu_with_text(callback, OWNER_HTML.format(owner_id=settings.owner_id))
    await callback.answer("Owner info")

@router.message(F.photo, F.chat.type == "private")
async def handle_owner_photo(message: Message):
    if not message.from_user or not await is_bot_owner(message.from_user.id):
        return
    file_id = message.photo[-1].file_id
    await message.reply(f"<code>{file_id}</code>", parse_mode="HTML")