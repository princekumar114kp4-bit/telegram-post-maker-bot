#!/usr/bin/env python3
"""
Telegram Channel & Group Post Maker Bot
Supports Channels, Supergroups & Groups.
Features: reply-keyboard navigation that changes on every step, coloured
keyboard buttons, optional premium emoji icons on buttons, button colour
styling for the post's own inline buttons, clean cancel, target removal.
Credit: ALEX-LX

Setup:
    BOT_TOKEN           (required) bot token from BotFather, set as env variable
    DB_FILE             (optional) sqlite file, default bot_data.db
    PREMIUM_EMOJI_IDS   (optional) JSON {"button_key": "custom_emoji_id", ...}
                        or put the same JSON in premium_emoji.json next to this file.
    Needs python-telegram-bot >= 22.7 for native keyboard button colours/icons
    (older versions fall back to raw API fields, newer Telegram clients show them).
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import aiosqlite
from telegram import (
    ChatMemberAdministrator,
    ChatMemberOwner,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    MessageEntity,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# >>> hyv-injected:web (added by the implementer bot: starts one background web server)
try:
    import threading as _hyv_threading
    import os as _hyv_os
    from http.server import BaseHTTPRequestHandler as _hyv_Base, ThreadingHTTPServer as _hyv_Server

    class _hyv_WebHandler(_hyv_Base):
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/health"):
                code, body = 200, b"ok"
            else:
                code, body = 404, b"not found"
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    def _hyv_serve_web():
        try:
            _hyv_port = int(_hyv_os.environ.get("PORT", "10000"))
            _hyv_Server(("0.0.0.0", _hyv_port), _hyv_WebHandler).serve_forever()
        except Exception:
            print("web server could not start", flush=True)

    _hyv_threading.Thread(target=_hyv_serve_web, daemon=True).start()
except Exception:
    print("web server could not start", flush=True)
# <<< hyv-injected:web
# >>> hyv-injected:pinger (added by the implementer bot: pings RENDER_EXTERNAL_URL in a background thread)
try:
    import threading as _hyv_threading2
    import os as _hyv_os2
    import time as _hyv_time
    import urllib.request as _hyv_urlreq

    def _hyv_ping_minutes():
        try:
            _hyv_m = int(_hyv_os2.environ.get("PING_MINUTES", "5").strip())
        except ValueError:
            return 5
        return _hyv_m if 1 <= _hyv_m <= 60 else 5

    def _hyv_ping_loop():
        _hyv_time.sleep(30)
        while True:
            try:
                _hyv_url = _hyv_os2.environ.get("RENDER_EXTERNAL_URL", "").strip()
                if _hyv_url.lower().startswith(("http://", "https://")):
                    try:
                        with _hyv_urlreq.urlopen(_hyv_url, timeout=30) as _hyv_resp:
                            _hyv_code = str(_hyv_resp.status)
                    except _hyv_urlreq.HTTPError as _hyv_err:
                        _hyv_code = str(_hyv_err.code)
                    except Exception:
                        _hyv_code = "down"
                    print("keep-alive ping:", _hyv_code, flush=True)
            except Exception:
                pass
            _hyv_time.sleep(_hyv_ping_minutes() * 60)

    _hyv_threading2.Thread(target=_hyv_ping_loop, daemon=True).start()
except Exception:
    print("pinger could not start", flush=True)
# <<< hyv-injected:pinger


# Token comes ONLY from the environment (never hardcode it in this file).
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

DB_FILE = os.getenv("DB_FILE", "bot_data.db")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("PostMakerBot")


def btn(
    text: str,
    callback_data: Optional[str] = None,
    url: Optional[str] = None,
    style: Optional[str] = None,
) -> InlineKeyboardButton:
    """
    Build InlineKeyboardButton with optional color style.
    Used ONLY for the buttons attached to the user's published post.
    style can be 'primary' | 'success' | 'danger'.
    Silently falls back if PTB version doesn't support it.
    """
    kwargs: Dict[str, Any] = {"text": text}
    if callback_data is not None:
        kwargs["callback_data"] = callback_data
    if url is not None:
        kwargs["url"] = url

    if style:
        try:
            return InlineKeyboardButton(**kwargs, style=style)
        except TypeError:
            pass
    return InlineKeyboardButton(**kwargs)


COLOR_EMOJI = {"primary": "🔵", "success": "🟢", "danger": "🔴"}


def track_msg(context: ContextTypes.DEFAULT_TYPE, message_id: Optional[int]) -> None:
    """Track a message ID so it can be deleted on cancel/publish."""
    if not message_id:
        return
    lst = context.user_data.setdefault("_trash", [])
    if message_id not in lst:
        lst.append(message_id)


async def send_tracked(target, context: ContextTypes.DEFAULT_TYPE, text: str, **kwargs):
    """reply_text + auto-track for cleanup."""
    sent = await target.reply_text(text, **kwargs)
    track_msg(context, sent.message_id)
    return sent


async def purge_trash(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    """Delete every tracked message and clear the list."""
    ids = context.user_data.get("_trash", [])
    for mid in ids:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=mid)
        except TelegramError:
            pass
    context.user_data.pop("_trash", None)


async def init_db() -> None:
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                channel_title TEXT NOT NULL,
                chat_type TEXT NOT NULL DEFAULT 'channel',
                created_at TEXT NOT NULL,
                UNIQUE(user_id, channel_id)
            );
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                channel_title TEXT NOT NULL,
                post_type TEXT NOT NULL,
                content_preview TEXT NOT NULL,
                published_at TEXT NOT NULL,
                telegram_message_id INTEGER NOT NULL
            );
            """
        )
        async with db.execute("PRAGMA table_info(channels);") as cursor:
            columns = [row[1] for row in await cursor.fetchall()]
            if "chat_type" not in columns:
                await db.execute(
                    "ALTER TABLE channels ADD COLUMN chat_type TEXT NOT NULL DEFAULT 'channel';"
                )
        await db.commit()


async def save_channel(user_id: int, channel_id: int, title: str, chat_type: str) -> None:
    async with aiosqlite.connect(DB_FILE) as db:
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            """
            INSERT INTO channels (user_id, channel_id, channel_title, chat_type, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, channel_id) DO UPDATE SET
                channel_title = excluded.channel_title,
                chat_type = excluded.chat_type;
            """,
            (user_id, channel_id, title, chat_type, now),
        )
        await db.commit()


async def delete_channel(user_id: int, channel_id: int) -> None:
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "DELETE FROM channels WHERE user_id = ? AND channel_id = ?",
            (user_id, channel_id),
        )
        await db.commit()


async def get_user_channels(user_id: int) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT channel_id, channel_title, chat_type FROM channels WHERE user_id = ? ORDER BY id DESC",
            (user_id,),
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]


async def get_user_channel(user_id: int, channel_id: int) -> Optional[Dict[str, Any]]:
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT channel_id, channel_title, chat_type FROM channels WHERE user_id = ? AND channel_id = ?",
            (user_id, channel_id),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def save_published_post(
    user_id: int,
    channel_id: int,
    channel_title: str,
    post_type: str,
    content_preview: str,
    telegram_message_id: int,
) -> None:
    async with aiosqlite.connect(DB_FILE) as db:
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            """
            INSERT INTO posts (user_id, channel_id, channel_title, post_type, content_preview, published_at, telegram_message_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, channel_id, channel_title, post_type, content_preview[:120], now, telegram_message_id),
        )
        await db.commit()


async def get_user_posts(user_id: int, limit: int = 10) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT channel_title, post_type, content_preview, published_at, telegram_message_id
            FROM posts WHERE user_id = ? ORDER BY id DESC LIMIT ?
            """,
            (user_id, limit),
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]


def is_valid_url(url: str) -> bool:
    parsed = urlparse(url.strip())
    if parsed.scheme.lower() not in ("http", "https", "tg"):
        return False
    if parsed.scheme.lower() in ("http", "https") and not parsed.netloc:
        return False
    return True


def build_post_markup(buttons: List[List[Dict[str, str]]]) -> Optional[InlineKeyboardMarkup]:
    if not buttons:
        return None
    keyboard = []
    for row in buttons:
        btn_row = [btn(text=b["text"], url=b["url"], style=b.get("style")) for b in row]
        if btn_row:
            keyboard.append(btn_row)
    return InlineKeyboardMarkup(keyboard) if keyboard else None


async def check_bot_posting_permission(bot, target_id: int) -> tuple[bool, str]:
    try:
        member = await bot.get_chat_member(chat_id=target_id, user_id=bot.id)
        if isinstance(member, ChatMemberOwner):
            return True, "Owner"
        if isinstance(member, ChatMemberAdministrator):
            can_post = getattr(member, "can_post_messages", None)
            if can_post is False:
                return False, "Bot is Admin but lacks 'Post Messages' permission."
            return True, "Administrator"
        return False, "Bot is not an Administrator in this Channel/Group."
    except BadRequest as e:
        if "chat not found" in e.message.lower():
            return False, "Bot is not added to this chat. Pehle bot ko Admin banayein!"
        return False, f"Telegram error: {e.message}"
    except TelegramError as e:
        return False, f"Telegram API error: {e.message}"
    except Exception as e:
        logger.error("Error checking bot permission: %s", e)
        return False, "Verification failed. Check the ID."


async def verify_user_channel_ownership(bot, target_id: int, user_id: int) -> tuple[bool, str]:
    try:
        user_member = await bot.get_chat_member(chat_id=target_id, user_id=user_id)
        if isinstance(user_member, ChatMemberOwner):
            return True, "Owner"
        if isinstance(user_member, ChatMemberAdministrator):
            return True, "Administrator"
        return False, "Aap is Channel/Group ke Owner ya Admin nahi hain."
    except BadRequest as e:
        if "user not found" in e.message.lower() or "participant" in e.message.lower():
            return False, "Aap is Channel/Group ke member/admin nahi hain."
        return False, f"Verification failed: {e.message}"
    except Exception as e:
        logger.error("Error checking user ownership: %s", e)
        return False, "User verification error."



# ---------------------------------------------------------------------------
# Reply-keyboard navigation layer
# ---------------------------------------------------------------------------
# Every screen has its own reply keyboard. Pressing a reply-keyboard button
# sends its label as a normal text message, so we remember (per user) which
# screen is open and which label maps to which action, and route the text.

@dataclass(frozen=True)
class RB:
    """One reply-keyboard button."""
    action: str                  # what happens when pressed
    text: str                    # label text (without emoji)
    emoji: str = ""              # regular emoji shown before the text
    style: str = "primary"       # primary=blue, success=green, danger=red
    key: Optional[str] = None    # key for an optional premium custom emoji icon


PREMIUM_EMOJI_KEYS = (
    "set_target", "create_post", "my_targets", "history", "help", "back",
    "cancel", "add_new", "remove", "yes_remove", "text", "photo", "video",
    "skip", "c_primary", "c_success", "c_danger", "publish",
)


def _load_premium_ids() -> Dict[str, str]:
    """Optional premium emoji icons: {"button_key": "custom_emoji_id"}."""
    raw = os.getenv("PREMIUM_EMOJI_IDS", "").strip()
    if not raw:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "premium_emoji.json")
        try:
            if os.path.isfile(path):
                with open(path, "r", encoding="utf-8") as fh:
                    raw = fh.read()
        except OSError:
            raw = ""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning("PREMIUM_EMOJI_IDS is not valid JSON - ignoring.")
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(k): str(v).strip()
        for k, v in data.items()
        if str(k) in PREMIUM_EMOJI_KEYS and str(v).strip().isdigit()
    }


PREMIUM_EMOJI_IDS: Dict[str, str] = _load_premium_ids()
ICONS_ENABLED = bool(PREMIUM_EMOJI_IDS)

VALID_STYLES = ("primary", "success", "danger")

# Screen names
SC_MAIN = "main"
SC_HELP = "help"
SC_HISTORY = "history"
SC_TARGETS = "targets"
SC_REMOVE = "remove"
SC_CONFIRM_DEL = "confirm_delete"
SC_NO_TARGET = "no_target"
SC_SET_CHANNEL = "set_channel"
SC_SELECT_CHANNEL = "select_channel"
SC_SELECT_TYPE = "select_type"
SC_CONTENT = "enter_content"
SC_CAPTION = "enter_caption"
SC_BUTTONS = "add_buttons"
SC_COLOR = "select_button_color"
SC_CONFIRM_PUBLISH = "confirm_publish"

# Screens where plain text/media typed by the user is real input (an ID,
# the post text, a caption, button lines) and not a button press.
INPUT_SCREENS = {SC_SET_CHANNEL, SC_CONTENT, SC_CAPTION, SC_BUTTONS}
# Screens that belong to a multi-step flow (messages get cleaned on finish/cancel).
FLOW_SCREENS = INPUT_SCREENS | {SC_SELECT_CHANNEL, SC_SELECT_TYPE, SC_COLOR, SC_CONFIRM_PUBLISH}

BACK_BTN = RB("menu:main", "Back to Menu", "🔙", "primary", "back")
CANCEL_BTN = RB("cancel", "Cancel", "❌", "danger", "cancel")
SET_TARGET_BTN = RB("menu:set_target", "Set Target", "⚙️", "primary", "set_target")


def rows_main() -> List[List[RB]]:
    return [
        [SET_TARGET_BTN,
         RB("menu:create", "Create Post", "📝", "success", "create_post")],
        [RB("menu:targets", "My Targets", "📋", "primary", "my_targets"),
         RB("menu:history", "History", "📚", "primary", "history")],
        [RB("menu:help", "Help & Guide", "❓", "primary", "help")],
    ]


def rows_back() -> List[List[RB]]:
    return [[BACK_BTN]]


def rows_cancel() -> List[List[RB]]:
    return [[CANCEL_BTN]]


def rows_targets(has_channels: bool) -> List[List[RB]]:
    if not has_channels:
        return [[SET_TARGET_BTN], [BACK_BTN]]
    return [
        [RB("menu:set_target", "Add New", "➕", "primary", "add_new"),
         RB("targets:remove", "Remove", "🗑", "danger", "remove")],
        [BACK_BTN],
    ]


def _chan_icon(ch: Dict[str, Any]) -> str:
    return "📢" if str(ch.get("chat_type") or "Channel").lower() == "channel" else "👥"


def _short(title: Optional[str], n: int = 40) -> str:
    title = (title or "Unnamed").strip() or "Unnamed"
    return title if len(title) <= n else title[: n - 1] + "…"


def rows_remove(channels: List[Dict[str, Any]]) -> List[List[RB]]:
    rows = [
        [RB(f"del:{ch['channel_id']}", _short(ch.get("channel_title")),
            f"🗑 {_chan_icon(ch)}", "danger")]
        for ch in channels
    ]
    rows.append([RB("menu:targets", "Back", "🔙", "primary", "back")])
    return rows


def rows_confirm_delete() -> List[List[RB]]:
    return [
        [RB("delconfirm", "Yes, Remove", "✅", "danger", "yes_remove")],
        [RB("menu:targets", "Cancel", "🔙", "primary", "back")],
    ]


def rows_no_target() -> List[List[RB]]:
    return [[SET_TARGET_BTN], [RB("menu:main", "Main Menu", "🔙", "primary", "back")]]


def rows_select_channel(channels: List[Dict[str, Any]]) -> List[List[RB]]:
    rows = [
        [RB(f"chan:{ch['channel_id']}", _short(ch.get("channel_title")), _chan_icon(ch), "primary")]
        for ch in channels
    ]
    rows.append([CANCEL_BTN])
    return rows


def rows_select_type() -> List[List[RB]]:
    return [
        [RB("type:text", "Text", "📄", "primary", "text"),
         RB("type:photo", "Photo", "🖼", "primary", "photo"),
         RB("type:video", "Video", "🎥", "primary", "video")],
        [CANCEL_BTN],
    ]


def rows_skip_caption() -> List[List[RB]]:
    return [[RB("skip_caption", "Skip Caption", "⏭", "primary", "skip")], [CANCEL_BTN]]


def rows_skip_buttons() -> List[List[RB]]:
    return [[RB("skip_buttons", "Skip Buttons", "⏭", "primary", "skip")], [CANCEL_BTN]]


def rows_color() -> List[List[RB]]:
    return [
        [RB("color:primary", "Primary", "🔵", "primary", "c_primary"),
         RB("color:success", "Success", "🟢", "success", "c_success"),
         RB("color:danger", "Danger", "🔴", "danger", "c_danger")],
        [CANCEL_BTN],
    ]


def rows_confirm_publish() -> List[List[RB]]:
    return [[RB("publish", "Publish Now", "🚀", "success", "publish")], [CANCEL_BTN]]


def make_kb_button(label: str, style: Optional[str], icon_id: Optional[str]) -> KeyboardButton:
    """KeyboardButton with colour (+ optional premium icon). Falls back to raw API fields."""
    if style not in VALID_STYLES:
        style = "primary"
    extra: Dict[str, Any] = {"style": style}
    if icon_id:
        extra["icon_custom_emoji_id"] = icon_id
    try:
        return KeyboardButton(label, **extra)
    except TypeError:
        # Older PTB without these fields: send them straight in the API payload.
        return KeyboardButton(label, api_kwargs=extra)


def build_reply_markup(
    rows: List[List[RB]], placeholder: Optional[str] = None
) -> Tuple[ReplyKeyboardMarkup, Dict[str, str], bool]:
    """Returns (markup, {label: action}, used_premium_icons)."""
    kb_map: Dict[str, str] = {}
    keyboard: List[List[KeyboardButton]] = []
    used_icons = False
    for row in rows:
        out: List[KeyboardButton] = []
        for rb in row:
            icon = PREMIUM_EMOJI_IDS.get(rb.key) if (ICONS_ENABLED and rb.key) else None
            # With a premium icon the icon replaces the regular emoji in the label.
            label = rb.text if (icon or not rb.emoji) else f"{rb.emoji} {rb.text}"
            base, n = label, 2
            while label in kb_map:
                label = f"{base} ({n})"
                n += 1
            kb_map[label] = rb.action
            used_icons = used_icons or bool(icon)
            out.append(make_kb_button(label, rb.style, icon))
        keyboard.append(out)
    markup = ReplyKeyboardMarkup(
        keyboard,
        resize_keyboard=True,
        input_field_placeholder=(placeholder[:64] if placeholder else None),
    )
    return markup, kb_map, used_icons


async def send_screen(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    text: str,
    screen: str,
    rows: List[List[RB]],
    *,
    placeholder: Optional[str] = None,
    track: bool = False,
):
    """Send a message with this screen's reply keyboard and remember the screen."""
    global ICONS_ENABLED
    sent = None
    for attempt in range(2):
        markup, kb_map, used_icons = build_reply_markup(rows, placeholder)
        try:
            sent = await context.bot.send_message(
                chat_id=chat_id, text=text, reply_markup=markup, parse_mode=ParseMode.HTML
            )
        except BadRequest as e:
            if attempt == 0 and used_icons:
                logger.warning("Premium emoji icons rejected (%s) - using normal emoji.", e.message)
                ICONS_ENABLED = False
                continue
            raise
        break
    context.user_data["screen"] = screen
    context.user_data["kb"] = kb_map
    context.user_data["kb_markup"] = markup
    if track and sent is not None:
        track_msg(context, sent.message_id)
    return sent


def _norm(label: str) -> str:
    """Label without leading emoji/symbols, lowercase (for tolerant matching)."""
    return re.sub(r"^[\W_]+", "", label or "").casefold().strip()


def resolve_action(context: ContextTypes.DEFAULT_TYPE, text: str) -> Optional[str]:
    """Map pressed-button text to an action for the CURRENT screen."""
    ud = context.user_data
    screen = ud.get("screen")
    kb: Dict[str, str] = ud.get("kb") or {}
    if text in kb:
        return kb[text]
    if screen in INPUT_SCREENS:
        return None  # strict on input screens: free text is input, not a button
    n = _norm(text)
    if not n:
        return None
    if kb:
        for label, action in kb.items():
            if _norm(label) == n:
                return action
        return None
    # No keyboard known (e.g. bot restarted): accept main-menu labels / back / cancel.
    for row in rows_main():
        for rb in row:
            if _norm(rb.text) == n:
                return rb.action
    if n in ("cancel", "back to menu", "main menu", "back"):
        return "menu:main"
    return None




# ---------------------------------------------------------------------------
# Screens / handlers
# ---------------------------------------------------------------------------

WELCOME_TEXT = (
    "👋 <b>Welcome to Post Maker Bot</b>\n"
    "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    "Apne <b>Channels</b> aur <b>Groups</b> me professional posts\n"
    "publish karein — text, photo, video aur colored URL buttons.\n\n"
    "🔐 <b>Security:</b> Sirf wahi targets add karein jinke aap\n"
    "<b>Owner / Admin</b> ho.\n"
    "🎨 <b>Colors:</b> Primary · Success · Danger\n"
    "😀 <b>Emoji:</b> Button labels me koi bhi emoji use karein\n\n"
    "👇 Neeche ke keyboard buttons se menu use karein."
)

HELP_TEXT = (
    "📖 <b>Help & Guide</b>\n"
    "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    "<b>🔹 Channel / Group ID kaise nikalein?</b>\n"
    "1. Bot ko target me <b>Admin</b> banayein.\n"
    "2. Koi post <b>@username_to_id_bot</b> pe forward karein.\n"
    "3. Numeric ID milegi — e.g. <code>-1001234567890</code>.\n\n"
    "<b>🔹 Post kaise banayein?</b>\n"
    "1. <b>Create Post</b> → target select karein.\n"
    "2. Format chunein (Text / Photo / Video).\n"
    "3. Content + caption add karein.\n"
    "4. URL buttons daalein aur unka <b>color</b> chunein.\n"
    "5. <b>🚀 Publish Now</b> dabayein.\n\n"
    "<b>🔹 Button format:</b>\n"
    "<code>Label - https://link.com</code>\n"
    "Ek row me 2 buttons: <code>A - url | B - url</code>\n"
    "Emoji allowed: <code>🔥 Join - https://t.me/x</code>"
)


async def show_main_menu(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str = "🏠 <b>Main Menu</b>"
) -> None:
    chat_id = update.effective_chat.id
    await purge_trash(context, chat_id)
    context.user_data.pop("draft", None)
    context.user_data.pop("pending_delete", None)
    await send_screen(context, chat_id, text, SC_MAIN, rows_main(),
                      placeholder="Menu se option chunein")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await purge_trash(context, update.effective_chat.id)
    context.user_data.clear()
    await send_screen(context, update.effective_chat.id, WELCOME_TEXT, SC_MAIN, rows_main(),
                      placeholder="Menu se option chunein")


async def legacy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Old inline-button messages (from before the keyboard menu) land here."""
    query = update.callback_query
    try:
        await query.answer("Menu update ho gaya hai - naya keyboard menu use karein.", show_alert=False)
    except TelegramError:
        pass
    await send_screen(context, update.effective_chat.id,
                      "🔄 <b>Menu update ho gaya!</b>\n\nAb sab kuch neeche ke keyboard buttons se chalta hai.",
                      SC_MAIN, rows_main(), placeholder="Menu se option chunein")


async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE, arg: str = "") -> None:
    await send_screen(context, update.effective_chat.id, HELP_TEXT, SC_HELP, rows_back())


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await purge_trash(context, update.effective_chat.id)
    context.user_data.pop("draft", None)
    await show_help(update, context)


async def show_my_targets(update: Update, context: ContextTypes.DEFAULT_TYPE, arg: str = "") -> None:
    channels = await get_user_channels(update.effective_user.id)

    if not channels:
        msg = (
            "📋 <b>My Targets</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Abhi tak koi target add nahi kiya.\n\n"
            "👉 <b>Set Target</b> se shuru karein."
        )
    else:
        msg = "📋 <b>My Verified Targets</b>\n━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        for idx, ch in enumerate(channels, 1):
            title = html.escape(ch.get("channel_title") or "Unnamed")
            icon = _chan_icon(ch)
            msg += f"{icon} <b>{idx}. {title}</b>\n<code>ID: {ch['channel_id']}</code>\n\n"

    await send_screen(context, update.effective_chat.id, msg, SC_TARGETS,
                      rows_targets(bool(channels)))


async def show_remove_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, arg: str = "") -> None:
    channels = await get_user_channels(update.effective_user.id)
    if not channels:
        await show_my_targets(update, context)
        return
    await send_screen(
        context, update.effective_chat.id,
        "🗑 <b>Remove Target</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Jis target ko remove karna hai, uska button dabayein:",
        SC_REMOVE, rows_remove(channels),
    )


async def delete_channel_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, arg: str = "") -> None:
    try:
        channel_id = int(arg)
    except ValueError:
        await show_main_menu(update, context, "❌ Invalid selection.")
        return

    channel = await get_user_channel(update.effective_user.id, channel_id)
    if not channel:
        await show_my_targets(update, context)
        return

    context.user_data["pending_delete"] = channel_id
    icon = _chan_icon(channel)
    await send_screen(
        context, update.effective_chat.id,
        "⚠️ <b>Confirm Removal</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{icon} <b>{html.escape(channel['channel_title'])}</b>\n"
        f"🆔 <code>{channel['channel_id']}</code>\n\n"
        "Pakka ye target remove karna hai?",
        SC_CONFIRM_DEL, rows_confirm_delete(),
    )


async def confirm_delete_channel(update: Update, context: ContextTypes.DEFAULT_TYPE, arg: str = "") -> None:
    channel_id = context.user_data.pop("pending_delete", None)
    if channel_id is None:
        await show_my_targets(update, context)
        return
    await delete_channel(update.effective_user.id, channel_id)
    await context.bot.send_message(chat_id=update.effective_chat.id, text="✅ Target removed.")
    await show_my_targets(update, context)


async def show_history(update: Update, context: ContextTypes.DEFAULT_TYPE, arg: str = "") -> None:
    posts = await get_user_posts(update.effective_user.id, limit=10)

    if not posts:
        msg = (
            "📚 <b>Post History</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Abhi tak koi post publish nahi kiya."
        )
    else:
        msg = "📚 <b>Last 10 Published Posts</b>\n━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        for idx, post in enumerate(posts, 1):
            ch_title = html.escape(post.get("channel_title") or "Target")
            p_type = post.get("post_type", "").capitalize()
            preview = html.escape(post.get("content_preview") or "")
            pub_date = post.get("published_at", "")[:16].replace("T", " ")
            msg += (
                f"<b>{idx}. [{p_type}]</b> → {ch_title}\n"
                f"🗓 <code>{pub_date} UTC</code>\n"
                f"💬 <i>{preview}</i>\n\n"
            )

    await send_screen(context, update.effective_chat.id, msg, SC_HISTORY, rows_back())


async def goto_main(update: Update, context: ContextTypes.DEFAULT_TYPE, arg: str = "") -> None:
    await show_main_menu(update, context)


async def set_channel_start(update: Update, context: ContextTypes.DEFAULT_TYPE, arg: str = "") -> None:
    track_msg(context, update.message.message_id)
    msg = (
        "⚙️ <b>Set Target — Channel / Group</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "<b>Steps:</b>\n"
        "1️⃣ Bot ko apne target me <b>Administrator</b> banayein.\n"
        "2️⃣ Us channel/group ki numeric ID copy karein.\n"
        "3️⃣ Neeche paste karke bhej dein.\n\n"
        "📌 <b>Format:</b> <code>-1001234567890</code>"
    )
    await send_screen(context, update.effective_chat.id, msg, SC_SET_CHANNEL, rows_cancel(),
                      placeholder="-1001234567890", track=True)


async def set_channel_process(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    text_val = (update.message.text or "").strip()

    if text_val.startswith("@") or not (text_val.startswith("-") or text_val.isdigit()):
        await send_tracked(
            update.message, context,
            "❌ <b>Invalid input</b>\n\n"
            "Sirf numeric Chat ID bhejein — jaise <code>-1001234567890</code>.",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        target_id = int(text_val)
    except ValueError:
        await send_tracked(update.message, context, "❌ Valid numeric ID bhejein.")
        return

    try:
        chat = await context.bot.get_chat(target_id)
    except TelegramError as e:
        await send_tracked(
            update.message, context,
            f"❌ <b>Chat not found</b>\n\n{html.escape(e.message)}\n\n"
            "Confirm karein ki bot us channel/group me <b>Admin</b> hai.",
            parse_mode=ParseMode.HTML,
        )
        return

    if chat.type not in ("channel", "supergroup", "group"):
        await send_tracked(
            update.message, context,
            f"❌ Yeh ek <b>{chat.type}</b> hai. Sirf Channels aur Groups support hain.",
            parse_mode=ParseMode.HTML,
        )
        return

    bot_ok, bot_reason = await check_bot_posting_permission(context.bot, target_id)
    if not bot_ok:
        await send_tracked(
            update.message, context,
            f"❌ <b>Bot permission missing</b>\n\n{html.escape(bot_reason)}\n\n"
            "Bot ko Admin banakar <b>Post Messages</b> permission enable karein.",
            parse_mode=ParseMode.HTML,
        )
        return

    user_ok, user_reason = await verify_user_channel_ownership(context.bot, target_id, user_id)
    if not user_ok:
        await send_tracked(
            update.message, context,
            f"⛔ <b>Access Denied</b>\n\n{html.escape(user_reason)}\n\n"
            "Aap sirf apna hi channel/group add kar sakte hain.",
            parse_mode=ParseMode.HTML,
        )
        return

    chat_type_str = "Group" if chat.type in ("group", "supergroup") else "Channel"
    await save_channel(user_id, target_id, chat.title or chat_type_str, chat_type_str)

    await purge_trash(context, update.effective_chat.id)

    icon = "📢" if chat_type_str == "Channel" else "👥"
    await send_screen(
        context, update.effective_chat.id,
        f"✅ <b>Target Linked</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{icon} <b>{html.escape(chat.title or 'Unnamed')}</b>\n"
        f"🆔 <code>{target_id}</code>\n"
        f"👤 <b>Your role:</b> {user_reason}\n\n"
        "Ab aap is target par post kar sakte hain!",
        SC_MAIN, rows_main(), placeholder="Menu se option chunein",
    )


async def create_post_start(update: Update, context: ContextTypes.DEFAULT_TYPE, arg: str = "") -> None:
    channels = await get_user_channels(update.effective_user.id)

    if not channels:
        await send_screen(
            context, update.effective_chat.id,
            "⚠️ <b>No target added</b>\n\n"
            "Post banane se pehle ek Channel ya Group add karein.",
            SC_NO_TARGET, rows_no_target(),
        )
        return

    track_msg(context, update.message.message_id)
    await send_screen(
        context, update.effective_chat.id,
        "📝 <b>Create Post — 1/6</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Kis target par post karni hai?",
        SC_SELECT_CHANNEL, rows_select_channel(channels),
        placeholder="Target chunein", track=True,
    )


async def select_channel(update: Update, context: ContextTypes.DEFAULT_TYPE, arg: str = "") -> None:
    try:
        channel_id = int(arg)
    except ValueError:
        await show_main_menu(update, context, "❌ Invalid selection.")
        return

    channel = await get_user_channel(update.effective_user.id, channel_id)
    if not channel:
        await show_main_menu(update, context, "⛔ Yeh target aapke account se linked nahi hai.")
        return

    context.user_data["draft"] = {
        "channel_id": channel["channel_id"],
        "channel_title": channel["channel_title"],
        "chat_type": channel.get("chat_type", "Channel"),
        "post_type": None,
        "text": None,
        "media_file_id": None,
        "caption": None,
        "buttons": [],
        "button_style": None,
    }

    await send_screen(
        context, update.effective_chat.id,
        "📝 <b>Create Post — 2/6</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🎯 <b>Target:</b> {html.escape(channel['channel_title'])}\n\n"
        "Post ka format chunein:",
        SC_SELECT_TYPE, rows_select_type(), placeholder="Text / Photo / Video", track=True,
    )


async def select_post_type(update: Update, context: ContextTypes.DEFAULT_TYPE, arg: str = "") -> None:
    post_type = arg
    draft = context.user_data.get("draft")
    if not draft or post_type not in ("text", "photo", "video"):
        await show_main_menu(update, context, "❌ Session expired.")
        return

    draft["post_type"] = post_type

    prompts = {
        "text": ("📝 <b>Create Post — 3/6</b>\n"
                 "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                 "Apna message text bhejein.\n"
                 "<i>HTML supported — &lt;b&gt;, &lt;i&gt;, &lt;a href&gt;</i>"),
        "photo": ("🖼 <b>Create Post — 3/6</b>\n"
                  "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                  "Photo upload karein.\n"
                  "<i>Caption saath me bhej sakte hain.</i>"),
        "video": ("🎥 <b>Create Post — 3/6</b>\n"
                  "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                  "Video upload karein.\n"
                  "<i>Caption saath me bhej sakte hain.</i>"),
    }
    placeholders = {"text": "Apna message likhein...", "photo": "Photo bhejein", "video": "Video bhejein"}

    await send_screen(
        context, update.effective_chat.id, prompts[post_type],
        SC_CONTENT, rows_cancel(), placeholder=placeholders[post_type], track=True,
    )


async def enter_content_process(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    draft = context.user_data.get("draft")
    if not draft:
        await show_main_menu(update, context, "❌ Session expired.")
        return

    post_type = draft["post_type"]

    if post_type == "text":
        if not update.message.text:
            await send_tracked(update.message, context, "❌ Text message bhejein.")
            return
        draft["text"] = update.message.text_html
        await ask_button_step(update, context)
        return

    if post_type == "photo":
        if not update.message.photo:
            await send_tracked(update.message, context, "❌ Photo bhejein.")
            return
        draft["media_file_id"] = update.message.photo[-1].file_id
        if update.message.caption:
            draft["caption"] = update.message.caption_html
            await ask_button_step(update, context)
            return
        await ask_caption_step(update, context)
        return

    if post_type == "video":
        if not update.message.video:
            await send_tracked(update.message, context, "❌ Video bhejein.")
            return
        draft["media_file_id"] = update.message.video.file_id
        if update.message.caption:
            draft["caption"] = update.message.caption_html
            await ask_button_step(update, context)
            return
        await ask_caption_step(update, context)
        return


async def ask_caption_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_screen(
        context, update.effective_chat.id,
        "📝 <b>Create Post — 4/6</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Caption add karna chahenge?\n"
        "Text bhejein ya <b>Skip Caption</b> dabayein.",
        SC_CAPTION, rows_skip_caption(), placeholder="Caption likhein...", track=True,
    )


async def enter_caption_process(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    draft = context.user_data.get("draft")
    if not draft:
        await show_main_menu(update, context, "❌ Session expired.")
        return

    if update.message.text:
        draft["caption"] = update.message.text_html

    await ask_button_step(update, context)


async def skip_caption(update: Update, context: ContextTypes.DEFAULT_TYPE, arg: str = "") -> None:
    draft = context.user_data.get("draft")
    if not draft:
        await show_main_menu(update, context, "❌ Session expired.")
        return
    draft["caption"] = None
    await ask_button_step(update, context)


async def ask_button_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = (
        "🔗 <b>Create Post — 5/6</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "<b>URL Buttons (optional)</b>\n\n"
        "Format: <code>Label - https://link.com</code>\n\n"
        "<b>Examples:</b>\n"
        "• 1 button: <code>Website - https://example.com</code>\n"
        "• Emoji bhi allowed: <code>🔥 Join - https://t.me/x</code>\n"
        "• 1 row me 2: <code>Site - https://a.com | Join - https://t.me/x</code>\n\n"
        "Button nahi chahiye to <b>Skip Buttons</b> dabayein."
    )
    await send_screen(
        context, update.effective_chat.id, msg,
        SC_BUTTONS, rows_skip_buttons(), placeholder="Label - https://link.com", track=True,
    )


async def add_buttons_process(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # The user's own post buttons: these stay real inline buttons on the published post.
    draft = context.user_data.get("draft")
    if not draft:
        await show_main_menu(update, context, "❌ Session expired.")
        return

    text = update.message.text or ""
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    rows: List[List[Dict[str, str]]] = []

    for line in lines:
        row_items = []
        for raw_btn in line.split("|"):
            parts = re.split(r"\s+-\s+", raw_btn.strip(), maxsplit=1)
            if len(parts) != 2:
                await send_tracked(
                    update.message, context,
                    f"❌ <b>Format galat</b>\n\n"
                    f"Sahi: <code>Label - https://link.com</code>\n"
                    f"Aapne: <code>{html.escape(raw_btn.strip())}</code>",
                    parse_mode=ParseMode.HTML,
                )
                return

            btn_label = parts[0].strip()
            btn_url = parts[1].strip()

            if not is_valid_url(btn_url):
                await send_tracked(
                    update.message, context,
                    f"❌ <b>Invalid URL</b>: <code>{html.escape(btn_url)}</code>\n\n"
                    "Must start with <code>http://</code>, <code>https://</code> ya <code>tg://</code>",
                    parse_mode=ParseMode.HTML,
                )
                return

            row_items.append({"text": btn_label, "url": btn_url})
        if row_items:
            rows.append(row_items)

    draft["buttons"] = rows

    await send_screen(
        context, update.effective_chat.id,
        "🎨 <b>Create Post — 6/6</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Buttons ban gaye! Ab inka <b>color</b> chunein:\n\n"
        "🔵 <b>Primary</b> — blue tone\n"
        "🟢 <b>Success</b> — green tone\n"
        "🔴 <b>Danger</b> — red tone",
        SC_COLOR, rows_color(), placeholder="Button color chunein", track=True,
    )


async def skip_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE, arg: str = "") -> None:
    draft = context.user_data.get("draft")
    if not draft:
        await show_main_menu(update, context, "❌ Session expired.")
        return
    draft["buttons"] = []
    draft["button_style"] = None
    await show_preview(update, context)


async def select_button_color(update: Update, context: ContextTypes.DEFAULT_TYPE, arg: str = "") -> None:
    style = arg
    draft = context.user_data.get("draft")
    if not draft or style not in VALID_STYLES:
        await show_main_menu(update, context, "❌ Session expired.")
        return

    draft["button_style"] = style
    for row in draft.get("buttons", []):
        for b in row:
            b["style"] = style

    await show_preview(update, context)


async def show_preview(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    draft = context.user_data.get("draft")
    if not draft:
        return

    post_type = draft["post_type"]
    post_markup = build_post_markup(draft.get("buttons", []))

    target_msg = update.effective_message

    header = (
        "👁️ <b>Post Preview</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 <b>Target:</b> {html.escape(draft['channel_title'])} "
        f"(<code>{draft['channel_id']}</code>)\n"
    )
    if draft.get("button_style"):
        header += f"🎨 <b>Button color:</b> {COLOR_EMOJI.get(draft['button_style'], '')} {draft['button_style'].capitalize()}\n"
    header += "━━━━━━━━━━━━━━━━━━━━━━━\n\nNeeche preview 👇"

    sent_header = await target_msg.reply_text(header, parse_mode=ParseMode.HTML)
    track_msg(context, sent_header.message_id)

    try:
        if post_type == "text":
            sent = await target_msg.reply_text(
                draft["text"],
                parse_mode=ParseMode.HTML,
                reply_markup=post_markup,
                disable_web_page_preview=False,
            )
        elif post_type == "photo":
            sent = await target_msg.reply_photo(
                photo=draft["media_file_id"],
                caption=draft.get("caption"),
                parse_mode=ParseMode.HTML,
                reply_markup=post_markup,
            )
        elif post_type == "video":
            sent = await target_msg.reply_video(
                video=draft["media_file_id"],
                caption=draft.get("caption"),
                parse_mode=ParseMode.HTML,
                reply_markup=post_markup,
            )
        else:
            sent = None
        if sent:
            track_msg(context, sent.message_id)
    except TelegramError as e:
        sent = await target_msg.reply_text(f"⚠️ Preview error: {html.escape(e.message)}")
        track_msg(context, sent.message_id)

    await send_screen(
        context, update.effective_chat.id,
        "Sab theek lag raha hai?\nPublish karne ke liye <b>🚀 Publish Now</b> dabayein.",
        SC_CONFIRM_PUBLISH, rows_confirm_publish(), placeholder="Publish ya Cancel", track=True,
    )


async def publish_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, arg: str = "") -> None:
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    draft = context.user_data.get("draft")

    if not draft:
        await show_main_menu(update, context, "❌ Draft expired.")
        return

    channel_id = draft["channel_id"]
    user_ok, _ = await verify_user_channel_ownership(context.bot, channel_id, user_id)
    if not user_ok:
        await show_main_menu(
            update, context,
            "⛔ <b>Security Block</b>\n\nAap ab is target ke Admin nahi hain.",
        )
        return

    post_type = draft["post_type"]
    post_markup = build_post_markup(draft.get("buttons", []))
    published_msg = None
    content_preview = ""

    try:
        if post_type == "text":
            published_msg = await context.bot.send_message(
                chat_id=channel_id, text=draft["text"],
                parse_mode=ParseMode.HTML, reply_markup=post_markup,
            )
            content_preview = re.sub(r"<[^>]*>", "", draft["text"])
        elif post_type == "photo":
            published_msg = await context.bot.send_photo(
                chat_id=channel_id, photo=draft["media_file_id"],
                caption=draft.get("caption"),
                parse_mode=ParseMode.HTML, reply_markup=post_markup,
            )
            content_preview = re.sub(r"<[^>]*>", "", draft.get("caption") or "[Photo]")
        elif post_type == "video":
            published_msg = await context.bot.send_video(
                chat_id=channel_id, video=draft["media_file_id"],
                caption=draft.get("caption"),
                parse_mode=ParseMode.HTML, reply_markup=post_markup,
            )
            content_preview = re.sub(r"<[^>]*>", "", draft.get("caption") or "[Video]")
    except TelegramError as e:
        logger.error("Failed to post: %s", e.message)
        await show_main_menu(
            update, context,
            f"❌ <b>Publishing Failed</b>\n\n{html.escape(e.message)}",
        )
        return

    if published_msg:
        await save_published_post(
            user_id=user_id, channel_id=channel_id,
            channel_title=draft["channel_title"],
            post_type=post_type, content_preview=content_preview,
            telegram_message_id=published_msg.message_id,
        )

    draft_title = draft["channel_title"]
    context.user_data.pop("draft", None)

    await purge_trash(context, chat_id)

    try:
        await send_screen(
            context, chat_id,
            "🎉 <b>Post Published Successfully!</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🎯 <b>Target:</b> {html.escape(draft_title)}\n"
            f"🆔 <b>Message ID:</b> <code>{published_msg.message_id if published_msg else 'N/A'}</code>\n\n"
            "Main menu:",
            SC_MAIN, rows_main(), placeholder="Menu se option chunein",
        )
    except TelegramError as e:
        logger.error("Failed to send success message: %s", e)


async def cancel_action(update: Update, context: ContextTypes.DEFAULT_TYPE, arg: str = "") -> None:
    chat_id = update.effective_chat.id

    if update.message:
        track_msg(context, update.message.message_id)  # the Cancel press / /cancel itself

    await purge_trash(context, chat_id)
    context.user_data.pop("draft", None)
    context.user_data.pop("pending_delete", None)

    try:
        await send_screen(
            context, chat_id,
            "✅ <b>Action cancelled.</b>\n\nMain menu:",
            SC_MAIN, rows_main(), placeholder="Menu se option chunein",
        )
    except TelegramError:
        pass


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cancel_action(update, context)


async def emojiid_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Helper: send /emojiid with premium emoji(s) to get their IDs for button icons."""
    msg = update.effective_message
    found: List[Tuple[str, str]] = []
    sources = [msg, getattr(msg, "reply_to_message", None)]
    for src in sources:
        if not src:
            continue
        try:
            ents = src.parse_entities([MessageEntity.CUSTOM_EMOJI]) or {}
        except Exception:
            ents = {}
        for ent, char in ents.items():
            if ent.custom_emoji_id:
                found.append((char, ent.custom_emoji_id))
    if not found:
        await msg.reply_text(
            "Premium emoji ka ID chahiye? <code>/emojiid</code> ke saath premium emoji bhejein "
            "(ya kisi premium emoji wale message ko reply karke /emojiid likhein).\n\n"
            "Phir <code>PREMIUM_EMOJI_IDS</code> me button key ke saath ID daalein. Keys:\n"
            f"<code>{', '.join(PREMIUM_EMOJI_KEYS)}</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    lines = "\n".join(f"{html.escape(c)} → <code>{i}</code>" for c, i in found)
    await msg.reply_text(f"🆔 <b>Custom emoji IDs</b>\n\n{lines}", parse_mode=ParseMode.HTML)


# ---------------------------------------------------------------------------
# Router: reply-keyboard presses arrive as plain text messages
# ---------------------------------------------------------------------------

ACTION_HANDLERS = {
    "menu:set_target": set_channel_start,
    "menu:create": create_post_start,
    "menu:targets": show_my_targets,
    "menu:history": show_history,
    "menu:help": show_help,
    "menu:main": goto_main,
    "targets:remove": show_remove_menu,
    "delconfirm": confirm_delete_channel,
    "cancel": cancel_action,
    "skip_caption": skip_caption,
    "skip_buttons": skip_buttons,
    "publish": publish_confirm,
}

PREFIX_HANDLERS = {
    "del:": delete_channel_prompt,
    "chan:": select_channel,
    "type:": select_post_type,
    "color:": select_button_color,
}

INPUT_HANDLERS = {
    SC_SET_CHANNEL: set_channel_process,
    SC_CONTENT: enter_content_process,
    SC_CAPTION: enter_caption_process,
    SC_BUTTONS: add_buttons_process,
}


def find_handler(action: str):
    """Returns (handler, arg) for an action string, or (None, "")."""
    if action in ACTION_HANDLERS:
        return ACTION_HANDLERS[action], ""
    for prefix, handler in PREFIX_HANDLERS.items():
        if action.startswith(prefix):
            return handler, action[len(prefix):]
    return None, ""


async def route_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None:
        return
    ud = context.user_data
    screen = ud.get("screen")
    text = (msg.text or "").strip()

    if screen in FLOW_SCREENS:
        track_msg(context, msg.message_id)

    action = resolve_action(context, text) if text else None
    if action:
        handler, arg = find_handler(action)
        if handler is not None:
            await handler(update, context, arg)
            return

    # Not a button: real input on input screens.
    input_handler = INPUT_HANDLERS.get(screen)
    if input_handler is not None:
        if screen == SC_CONTENT or msg.text:
            await input_handler(update, context)
        return

    if not ud.get("kb"):
        # Bot was restarted / session lost: refresh with the main menu.
        await show_main_menu(update, context, "🔄 Menu refresh kar diya.\n\n🏠 <b>Main Menu</b>")
        return

    if msg.text:
        await msg.reply_text(
            "👇 Neeche ke keyboard buttons se option chunein.",
            reply_markup=ud.get("kb_markup"),
        )


def main() -> None:
    token = BOT_TOKEN.strip()
    if not token:
        sys.stderr.write(
            "CRITICAL ERROR: BOT_TOKEN environment variable is empty. "
            "Set it first, e.g.  export BOT_TOKEN='123456:ABC...'\n"
        )
        sys.exit(1)

    async def _post_init(app: Application) -> None:
        await init_db()

    app: Application = ApplicationBuilder().token(token).post_init(_post_init).build()

    private = filters.ChatType.PRIVATE

    app.add_handler(CommandHandler("start", start_command, filters=private))
    app.add_handler(CommandHandler("help", help_command, filters=private))
    app.add_handler(CommandHandler("cancel", cancel_command, filters=private))
    app.add_handler(CommandHandler("emojiid", emojiid_command, filters=private))
    app.add_handler(
        MessageHandler(
            private & ~filters.COMMAND & (filters.TEXT | filters.PHOTO | filters.VIDEO),
            route_message,
        )
    )
    # Old inline-button messages from before the keyboard menu.
    app.add_handler(CallbackQueryHandler(legacy_callback))

    logger.info("Bot starting — reply-keyboard navigation + coloured buttons...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
