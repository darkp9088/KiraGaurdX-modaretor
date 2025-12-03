import os
import time
import json
import logging
import asyncio
from collections import defaultdict, deque
from typing import Dict, Deque, Optional, Tuple
from datetime import datetime
import re
import inspect
import re
import aiosqlite
from dotenv import load_dotenv
import aiohttp
from deep_translator import GoogleTranslator

from telegram import (
    Update,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

# --------------------- Load .env ---------------------
load_dotenv()

# --------------------- Config from env ---------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "REPLACE_ME")
DB_PATH = os.getenv("DB_PATH", "bot_data.db")
# RapidAPI credentials (loaded from .env)
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY")
RAPIDAPI_HOST = os.getenv("RAPIDAPI_HOST")
SUPPORT_GROUP = os.getenv("SUPPORT_GROUP", "https://t.me/YOUR_SOUPPORT_GROUP_NAME")
BOT_USERNAME = os.getenv("BOT_USERNAME", "")
ADMINS = set()
_adm = os.getenv("ADMINS", "")
if _adm:
    for part in _adm.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ADMINS.add(int(part))
        except ValueError:
            pass

try:
    SPAM_WINDOW_SEC = int(os.getenv("SPAM_WINDOW_SEC", "7"))
except ValueError:
    SPAM_WINDOW_SEC = 7
try:
    SPAM_MAX_MSG = int(os.getenv("SPAM_MAX_MSG", "20"))
except ValueError:
    SPAM_MAX_MSG = 20

# matches many links (broad)
LINK_PATTERN = re.compile(
    r"https?://|t\.me/|telegram\.me/|discord\.gg/|discordapp\.com/|bit\.ly/|youtu\.be/|youtube\.com/",
    re.IGNORECASE,
)

ADMIN_ONLY = os.getenv("ADMIN_ONLY", "false").lower() in ("1", "true", "yes")
WELCOME_FALLBACK = os.getenv("WELCOME_DEFAULT", "Welcome {first_name}! Read the rules and be kind.")
# --------------------- PROFANITY / BAD WORDS (GLOBAL) ---------------------
# Edit this list to add/remove words (lowercase). Avoid tiny substrings like "ass".
BAD_WORDS = [
    "fuck",
    "shit",
    "bitch",
    "asshole",
    "motherfucker",
    "dick",
    "boobs",
    "sex",
    # add more words...
]

# compiled whole-word regex (case-insensitive)
BAD_WORDS_RE = re.compile(r"\b(?:" + "|".join(re.escape(w) for w in BAD_WORDS) + r")\b", re.IGNORECASE)


# --------------------- Logging ---------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --------------------- In-memory runtime (temporary caches) ---------------------
message_history: Dict[int, Dict[int, Deque[float]]] = defaultdict(lambda: defaultdict(deque))
chat_settings_cache: Dict[int, Dict] = {}

# --------------------- Compatibility helper for ChatPermissions ---------------------
def make_perms(**kwargs):
    """
    Build a ChatPermissions object using only supported keyword args
    (inspects ChatPermissions.__init__ to avoid unexpected keyword errors).
    """
    try:
        sig = inspect.signature(ChatPermissions.__init__)
        allowed = {k: v for k, v in kwargs.items() if k in sig.parameters}
        return ChatPermissions(**allowed)
    except Exception:
        # Fallback: only the most common param
        return ChatPermissions(can_send_messages=kwargs.get("can_send_messages", True))


# --------------------- Database helpers & migration ---------------------
async def migrate_bans_table(db):
    try:
        cur = await db.execute("PRAGMA table_info(bans)")
        cols = await cur.fetchall()
        colnames = [c[1] for c in cols]
        if "mod_id" not in colnames:
            await db.execute("ALTER TABLE bans ADD COLUMN mod_id INTEGER DEFAULT 0")
            logger.info("Migrated bans table: added column 'mod_id'")
        if "reason" not in colnames:
            await db.execute("ALTER TABLE bans ADD COLUMN reason TEXT DEFAULT ''")
            logger.info("Migrated bans table: added column 'reason'")
    except Exception as e:
        logger.warning("migrate_bans_table failed: %s", e)


async def migrate_chat_settings_table(db):
    """
    Add fields 'sticker_triggers' (JSON), 'locked' (INTEGER), 'rules_text', 'bye_text', 'bye_photo' if missing,
    ensure 'anti_link' exists, and add spam-specific fields: spam_enabled, spam_limit, spam_ban_reason.
    """
    try:
        cur = await db.execute("PRAGMA table_info(chat_settings)")
        cols = await cur.fetchall()
        colnames = [c[1] for c in cols]
        if "sticker_triggers" not in colnames:
            await db.execute("ALTER TABLE chat_settings ADD COLUMN sticker_triggers TEXT DEFAULT ''")
            logger.info("Migrated chat_settings: added column 'sticker_triggers'")
        if "locked" not in colnames:
            await db.execute("ALTER TABLE chat_settings ADD COLUMN locked INTEGER DEFAULT 0")
            logger.info("Migrated chat_settings: added column 'locked'")
        if "rules_text" not in colnames:
            await db.execute("ALTER TABLE chat_settings ADD COLUMN rules_text TEXT DEFAULT ''")
            logger.info("Migrated chat_settings: added column 'rules_text'")
        if "bye_text" not in colnames:
            await db.execute("ALTER TABLE chat_settings ADD COLUMN bye_text TEXT DEFAULT ''")
            logger.info("Migrated chat_settings: added column 'bye_text'")
        if "bye_photo" not in colnames:
            await db.execute("ALTER TABLE chat_settings ADD COLUMN bye_photo TEXT DEFAULT ''")
            logger.info("Migrated chat_settings: added column 'bye_photo'")
        if "anti_link" not in colnames:
            await db.execute("ALTER TABLE chat_settings ADD COLUMN anti_link INTEGER DEFAULT 1")
            logger.info("Migrated chat_settings: added column 'anti_link'")

        # New spam-related columns
        if "spam_enabled" not in colnames:
            await db.execute("ALTER TABLE chat_settings ADD COLUMN spam_enabled INTEGER DEFAULT 1")
            logger.info("Migrated chat_settings: added column 'spam_enabled'")
        if "spam_limit" not in colnames:
            await db.execute("ALTER TABLE chat_settings ADD COLUMN spam_limit INTEGER DEFAULT 20")
            logger.info("Migrated chat_settings: added column 'spam_limit'")
        if "spam_ban_reason" not in colnames:
            await db.execute("ALTER TABLE chat_settings ADD COLUMN spam_ban_reason TEXT DEFAULT 'auto-spam-limit'")
            logger.info("Migrated chat_settings: added column 'spam_ban_reason'")

    except Exception as e:
        logger.warning("migrate_chat_settings_table failed: %s", e)


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """CREATE TABLE IF NOT EXISTS warns (
               chat_id INTEGER, user_id INTEGER, warns INTEGER,
               PRIMARY KEY (chat_id, user_id))"""
        )
        await db.execute(
            """CREATE TABLE IF NOT EXISTS mutes (
               chat_id INTEGER, user_id INTEGER, until_ts INTEGER,
               PRIMARY KEY (chat_id, user_id))"""
        )
        await db.execute(
            """CREATE TABLE IF NOT EXISTS bans (
               chat_id INTEGER,
               user_id INTEGER,
               username TEXT,
               mod_id INTEGER DEFAULT 0,
               reason TEXT DEFAULT '',
               banned_at INTEGER,
               PRIMARY KEY (chat_id, user_id)
            )"""
        )
        await db.execute(
           """CREATE TABLE IF NOT EXISTS chat_settings (
       chat_id INTEGER PRIMARY KEY,
       welcome_text TEXT,
       welcome_photo TEXT,
       welcome_video TEXT,
       anti_link INTEGER DEFAULT 1,
       sticker_triggers TEXT DEFAULT '',
       locked INTEGER DEFAULT 0,
       rules_text TEXT DEFAULT '',
       bye_text TEXT DEFAULT '',
       bye_photo TEXT DEFAULT '',
       spam_enabled INTEGER DEFAULT 1,
       spam_limit INTEGER DEFAULT 20,
       spam_ban_reason TEXT DEFAULT 'auto-spam-limit'
    )"""
        )
        await db.commit()

        await migrate_bans_table(db)
        await migrate_chat_settings_table(db)
        await db.commit()

    logger.info("DB initialized and migrated (if necessary) — %s", DB_PATH)


async def get_bans_for_chat(chat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            cur = await db.execute(
                "SELECT user_id,username,mod_id,reason,banned_at FROM bans WHERE chat_id=?", (chat_id,)
            )
            rows = await cur.fetchall()
            return rows
        except Exception as e:
            logger.warning("get_bans_for_chat primary query failed, falling back: %s", e)
            try:
                cur = await db.execute("SELECT user_id,username,banned_at FROM bans WHERE chat_id=?", (chat_id,))
                rows = await cur.fetchall()
                padded = []
                for r in rows:
                    user_id = r[0]
                    username = r[1] if len(r) > 1 else ""
                    banned_at = r[2] if len(r) > 2 else 0
                    padded.append((user_id, username, 0, "", banned_at))
                return padded
            except Exception as e2:
                logger.error("get_bans_for_chat fallback query also failed: %s", e2)
                return []


# ---------- chat settings persistence ----------
async def load_chat_settings(chat_id: int) -> Dict:
    if chat_id in chat_settings_cache:
        return chat_settings_cache[chat_id]
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT welcome_text, welcome_photo, welcome_video, anti_link, sticker_triggers, locked, rules_text, bye_text, bye_photo, spam_enabled, spam_limit, spam_ban_reason FROM chat_settings WHERE chat_id=?",
            (chat_id,),
        )
        row = await cur.fetchone()
        if row:
            (
                welcome_text,
                welcome_photo,
                welcome_video,
                anti_link,
                sticker_triggers,
                locked,
                rules_text,
                bye_text,
                bye_photo,
                spam_enabled,
                spam_limit,
                spam_ban_reason,
            ) = row
            try:
                triggers = json.loads(sticker_triggers) if sticker_triggers else {}
            except Exception:
                triggers = {}
            settings = {
                "welcome": welcome_text or WELCOME_FALLBACK,
                "welcome_photo": welcome_photo or None,
                "welcome_video": welcome_video or None,
                "anti_link": bool(anti_link),
                "sticker_triggers": triggers or {},
                "locked": bool(locked),
                "rules_text": rules_text or "",
                "bye_text": bye_text or "",
                "bye_photo": bye_photo or None,
                "spam_enabled": True if spam_enabled is None else bool(spam_enabled),
                "spam_limit": int(spam_limit) if spam_limit is not None and str(spam_limit).isdigit() else SPAM_MAX_MSG,
                "spam_ban_reason": spam_ban_reason or "auto-spam-limit",
            }
        else:
            settings = {
                "welcome": WELCOME_FALLBACK,
                "welcome_photo": None,
                "welcome_video": None,
                "anti_link": True,
                "sticker_triggers": {},
                "locked": False,
                "rules_text": "",
                "bye_text": "",
                "bye_photo": None,
                "spam_enabled": True,
                "spam_limit": SPAM_MAX_MSG,
                "spam_ban_reason": "auto-spam-limit",
            }
            await db.execute(
                "INSERT OR IGNORE INTO chat_settings (chat_id,welcome_text,anti_link,sticker_triggers,locked,rules_text,bye_text,bye_photo,spam_enabled,spam_limit,spam_ban_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (chat_id, settings["welcome"], 1, "{}", 0, "", "", "", 1, SPAM_MAX_MSG, "auto-spam-limit"),
            )
            await db.commit()
    chat_settings_cache[chat_id] = settings
    return settings


async def save_chat_settings(chat_id: int):
    s = chat_settings_cache.get(chat_id)
    if s is None:
        return
    welcome_text = s.get("welcome", WELCOME_FALLBACK)
    welcome_photo = s.get("welcome_photo") or ""
    welcome_video = s.get("welcome_video") or ""
    anti_link = 1 if s.get("anti_link", True) else 0
    triggers_json = json.dumps(s.get("sticker_triggers", {}))
    locked = 1 if s.get("locked", False) else 0
    rules_text = s.get("rules_text", "") or ""
    bye_text = s.get("bye_text", "") or ""
    bye_photo = s.get("bye_photo") or ""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "REPLACE INTO chat_settings (chat_id,welcome_text,welcome_photo,welcome_video,anti_link,sticker_triggers,locked,rules_text,bye_text,bye_photo) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (chat_id, welcome_text, welcome_photo, welcome_video, anti_link, triggers_json, locked, rules_text, bye_text, bye_photo),
        )
        await db.commit()


# ---------- warns / mutes / bans helpers ----------




async def change_warns(chat_id: int, user_id: int, delta: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT warns FROM warns WHERE chat_id=? AND user_id=?", (chat_id, user_id))
        row = await cur.fetchone()
        if row:
            new = max(0, row[0] + delta)
            await db.execute("UPDATE warns SET warns=? WHERE chat_id=? AND user_id=?", (new, chat_id, user_id))
        else:
            new = max(0, delta)
            await db.execute("INSERT INTO warns (chat_id,user_id,warns) VALUES(?,?,?)", (chat_id, user_id, new))
        await db.commit()
        return new


async def get_warns(chat_id: int, user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT warns FROM warns WHERE chat_id=? AND user_id=?", (chat_id, user_id))
        row = await cur.fetchone()
        return row[0] if row else 0


async def set_mute(chat_id: int, user_id: int, until_ts: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("REPLACE INTO mutes(chat_id,user_id,until_ts) VALUES(?,?,?)", (chat_id, user_id, until_ts))
        await db.commit()


async def get_mute(chat_id: int, user_id: int) -> Optional[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT until_ts FROM mutes WHERE chat_id=? AND user_id=?", (chat_id, user_id))
        row = await cur.fetchone()
        return row[0] if row else None


async def clear_expired_mutes(application):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT chat_id,user_id,until_ts FROM mutes")
        rows = await cur.fetchall()
        now = int(time.time())
        for chat_id, user_id, until_ts in rows:
            if until_ts and until_ts <= now:
                try:
                    await application.bot.restrict_chat_member(
                        int(chat_id),
                        int(user_id),
                        make_perms(
                            can_send_messages=True,
                            can_send_media_messages=True,
                            can_send_polls=True,
                            can_send_other_messages=True,
                            can_add_web_page_previews=True,
                        ),
                    )
                except Exception as e:
                    logger.info("Failed to unmute %s in %s: %s", user_id, chat_id, e)
                await db.execute("DELETE FROM mutes WHERE chat_id=? AND user_id=?", (chat_id, user_id))
        await db.commit()


async def record_ban(chat_id: int, user_id: int, username: Optional[str], mod_id: Optional[int], reason: Optional[str]):
    async with aiosqlite.connect(DB_PATH) as db:
        ts = int(time.time())
        await db.execute(
            "REPLACE INTO bans (chat_id,user_id,username,mod_id,reason,banned_at) VALUES(?,?,?,?,?,?)",
            (chat_id, user_id, username or "", mod_id or 0, reason or "", ts),
        )
        await db.commit()


async def remove_ban_record(chat_id: int, user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM bans WHERE chat_id=? AND user_id=?", (chat_id, user_id))
        await db.commit()


# --------------------- Utility helpers ---------------------
async def info_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /info  (admin-only, must be used as a reply)
    Admin replies to a user's message with /info to get:
      - user id (admin-only)
      - username
      - full name
      - is_bot
      - chat member status (if available)
      - warns count (from DB)
    """
    # require admin
    if not await require_admin(update, context):
        return

    msg = update.effective_message
    if not msg or not msg.reply_to_message or not msg.reply_to_message.from_user:
        await update.effective_message.reply_text("Usage: reply to a user's message with /info to view their info (admin-only).")
        return

    target = msg.reply_to_message.from_user
    chat = update.effective_chat

    # Basic fields
    uid = target.id
    username = f"@{target.username}" if target.username else "(no username)"
    full_name = " ".join(p for p in (target.first_name or "", target.last_name or "") if p).strip() or "(no name)"
    is_bot = "Yes" if target.is_bot else "No"

    # Try getting chat member info (status, maybe restricted info)
    member_status = "(unknown)"
    try:
        member = await context.bot.get_chat_member(chat.id, uid)
        member_status = getattr(member, "status", "(unknown)")
    except Exception:
        # ignore — not critical
        member_status = "(unavailable)"

    # Warns from DB
    try:
        warns = await get_warns(chat.id, uid)
    except Exception:
        warns = "(unknown)"

    # Build reply (admin-only info contains user id)
    text = (
        "<b>User info (admin-only)</b>\n\n"
        f"• <b>ID</b>: <code>{uid}</code>\n"
        f"• <b>Username</b>: {username}\n"
        f"• <b>Full name</b>: {full_name}\n"
        f"• <b>Is bot</b>: {is_bot}\n"
        f"• <b>Chat status</b>: {member_status}\n"
        f"• <b>Warns</b>: {warns}\n"
    )

    try:
        await update.effective_message.reply_html(text)
    except Exception:
        # fallback plain text
        await update.effective_message.reply_text(
            text.replace("<b>", "").replace("</b>", "").replace("<code>", "`").replace("</code>", "`")
        )


async def is_user_admin(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    try:
        if user_id in ADMINS:
            return True
        chat = update.effective_chat
        if not chat:
            return False
        member = await context.bot.get_chat_member(chat.id, user_id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False


async def require_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if not user:
        return False
    try:
        if ADMIN_ONLY:
            if user.id in ADMINS:
                return True
            await update.effective_message.reply_text("Only bot owners can use this command.")
            return False
        if await is_user_admin(update, context, user.id):
            return True
        if user.id in ADMINS:
            return True
    except Exception:
        pass
    await update.effective_message.reply_text("You need to be a chat admin to use this command.")
    return False


# --------------------- UI & welcome helpers ---------------------
def build_full_commands_menu(chat_id: Optional[int] = None, context: Optional[ContextTypes.DEFAULT_TYPE] = None):
    # default labels
    anti_label = "🔗 Links: ON"
    lock_label = "🔒 Lock chat"
    spam_label = "🟠 Spam: ON"

    try:
        # use cached settings if available, otherwise use safe defaults
        s = chat_settings_cache.get(chat_id, {}) if chat_id is not None else {}
        anti = s.get("anti_link", True)
        locked = s.get("locked", False)
        spam_on = s.get("spam_enabled", True)

        anti_label = "🔗 Links: ON" if anti else "🔗 Links: OFF"
        lock_label = "🔓 Unlock chat" if locked else "🔒 Lock chat"
        spam_label = "🟠 Spam: ON" if spam_on else "⚪ Spam: OFF"
    except Exception:
        # fall back to defaults on any error
        anti_label = anti_label
        lock_label = lock_label
        spam_label = spam_label

        
    except Exception:
        pass

    kb = [
        [InlineKeyboardButton("✨ Help", callback_data="cmd_help"), InlineKeyboardButton("📜 Ban Log", callback_data="cmd_banlog")],
        [InlineKeyboardButton("🔨 Ban (how-to)", callback_data="cmd_ban"), InlineKeyboardButton("🔓 Unban (how-to)", callback_data="cmd_unban"), InlineKeyboardButton("♻️ Unban All", callback_data="cmd_unbanall")],
        [InlineKeyboardButton("🟠 Mute (how-to)", callback_data="cmd_mute"), InlineKeyboardButton("🟢 Unmute", callback_data="cmd_unmute"), InlineKeyboardButton("🚫 Kick", callback_data="cmd_kick")],
        [InlineKeyboardButton("⚠️ Warn", callback_data="cmd_warn"), InlineKeyboardButton("📊 Warnings", callback_data="cmd_warnings"), InlineKeyboardButton("🛡️ Set Welcome", callback_data="cmd_setwelcome")],
        [InlineKeyboardButton("🖼️ Set Photo", callback_data="cmd_setphoto"), InlineKeyboardButton("🎞️ Set Video", callback_data="cmd_setvideo"), InlineKeyboardButton("🗑️ Clear Media", callback_data="cmd_clear_media")],
        [InlineKeyboardButton("🔖 Sticker Trigger (use /filter)", callback_data="cmd_filter_sticker_help"), InlineKeyboardButton("🔁 Refresh", callback_data="cmd_refresh")],
        [InlineKeyboardButton(anti_label, callback_data="cmd_toggle_link"), InlineKeyboardButton(lock_label, callback_data="cmd_toggle_lock"), InlineKeyboardButton(spam_label, callback_data="cmd_toggle_spam")],
    ]

    # Try to get a bot username safely (don't crash if not available)
    bot_username = None
    try:
        # 1) prefer explicit env var BOT_USERNAME if provided
        if BOT_USERNAME:
            bot_username = BOT_USERNAME.strip().lstrip("@")
        # 2) then try context.bot.username (if context passed)
        if not bot_username and context and getattr(context, "bot", None):
            bot_obj = getattr(context, "bot")
            # Some Bot objects expose 'username' attribute after initialize
            bot_username = getattr(bot_obj, "username", None)
            if not bot_username:
                # try bot.get_me() but that is async — we don't call it here to keep this function sync.
                # So just leave None; callers that need the add-to-group button should pass context when available.
                bot_username = None
        # 3) fallback to environment SUPPORT_GROUP if nothing else
        if bot_username:
            bot_username = bot_username.strip().lstrip("@")
    except Exception:
        bot_username = None

    # bottom row: include support group (always) and add-to-group (only if username available)
    bottom_buttons = [InlineKeyboardButton("💬 「  ꜱᴜᴘᴘᴏʀᴛ ɢʀᴏᴜᴘ 」", url=SUPPORT_GROUP)]

    if bot_username:
        add_url = f"https://t.me/{bot_username}?startgroup=true"
        bottom_buttons.append(
            InlineKeyboardButton("➕「 ᴋɪᴅɴᴀᴘ ᴍᴇ ʙᴀʙʏ 💗 ᴛᴀᴋᴇ ᴍᴇ ᴡɪᴛʜ ʏᴏᴜ 」", url=add_url)
        )

    kb.append(bottom_buttons)

    return InlineKeyboardMarkup(kb)


def pretty_welcome_text(chat_id: int, first_name: str, username: Optional[str]) -> str:
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    chat = chat_settings_cache.get(chat_id)

    # Load saved welcome text (admin custom)
    welcome_template = (chat["welcome"] if chat and chat.get("welcome") else WELCOME_FALLBACK)

    # Format the custom part
    try:
        custom_text = welcome_template.format(
            first_name=first_name,
            username=f"@{username}" if username else ""
        )
    except Exception:
        custom_text = welcome_template

    # Use stored title if available
    chat_title = chat.get("chat_title", "") if chat else ""

    # Stylish full welcome banner
    welcome_banner = (
        f"📀 <b>WELCOME TO 𝐊𝐢𝐫𝐚𝐆𝐮𝐚𝐫𝐝𝐗!</b> 🎵\n\n"
        "🚀 <b>TOP-NOTCH 24×7 UPTIME & SUPPORT</b>\n"
        "💎 <b>CRYSTAL-CLEAR MANAGEMENT</b>\n"
        "🛡 <b>ANTI-SPAM • ANTI-LINK • AUTO-MOD</b>\n\n"
        "🎵 <b>GROUP FEATURES</b>\n"
        "• Auto Welcome (Photo/Video/Text)\n"
        "• Sticker Trigger System\n"
        "• Auto Warn → Auto Ban\n"
        "• Custom Rules + Info System\n\n"
        "🛠 <b>ADMIN COMMANDS</b>\n"
        "Unmute, Kick, Ban, Unban, Warn, Filters\n\n"
        f"❤️ <b>ENJOY YOUR STAY, {first_name}!</b>\n\n"
        f"<i>Sent at {now}</i>"
    )

    # Insert custom welcome text inside the banner
    final = f"{welcome_banner}\n\n<b>Message:</b> {custom_text}"
    return final


# --------------------- Command handlers ---------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    cid = chat.id if chat else 0
    await load_chat_settings(cid)
    text = pretty_welcome_text(cid, user.first_name if user else "there", user.username if user else None)
    try:
        await update.effective_message.reply_html(text, reply_markup=build_full_commands_menu(cid))
    except Exception:
        await update.effective_message.reply_text("Welcome! Use /help to view commands.", reply_markup=build_full_commands_menu(cid))


async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_cmd(update, context)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "<b>Commands</b>\n\n"
        "/setwelcome_text <text> — set welcome text (use {first_name})\n"
        "Reply to a photo + /setwelcome_photo — sets welcome photo\n"
        "Reply to a video + /setwelcome_video — sets welcome video\n"
        "/clear_welcome_media — removes photo & video\n\n"
        "/setbye_text <text> — set goodbye text (use {first_name})\n"
        "Reply to a photo + /setbye_photo — sets goodbye photo\n"
        "/clear_bye_media — removes bye photo\n\n"
        "/filter <phrase> — (reply to a sticker + /filter <phrase>) register sticker trigger (admin only)\n"
        "/filter remove <phrase> — remove sticker trigger (admin only)\n"
        "/filter list — list sticker triggers\n\n"
        "/rules — show chat rules\n"
        "/setrules <text> — set chat rules (admin only)\n"
        "/report — reply to a message and type /report <optional reason> to report it to admins\n"
        "/ginfo — group information\n"
        "/weather <city> — get weather (OpenWeatherMap API required)\n"
        "/translate <text> <lang> — translate text to target language (e.g. en, hi, fr)\n\n"
        "Moderation: /ban /unban /unbanall /banlog /kick /mute /unmute /warn /warnings\n(Reply to a user's message to target them)\n"
    )
    await update.effective_message.reply_html(text)


# welcome text & media commands
async def setwelcome_text_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    chat_id = update.effective_chat.id
    if not context.args:
        await update.effective_message.reply_text("Usage: /setwelcome_text Your welcome text (use {first_name})")
        return
    text = " ".join(context.args)
    await load_chat_settings(chat_id)
    chat_settings_cache[chat_id]["welcome"] = text
    await save_chat_settings(chat_id)
    await update.effective_message.reply_text("Welcome text updated.")


async def setwelcome_photo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    msg = update.effective_message
    if not msg.reply_to_message or not msg.reply_to_message.photo:
        await msg.reply_text("Reply to a photo message with /setwelcome_photo to set the welcome photo.")
        return
    photo = msg.reply_to_message.photo[-1]
    file_id = photo.file_id
    chat_id = update.effective_chat.id
    await load_chat_settings(chat_id)
    chat_settings_cache[chat_id]["welcome_photo"] = file_id
    chat_settings_cache[chat_id]["welcome_video"] = None
    await save_chat_settings(chat_id)
    await msg.reply_text("Welcome photo saved. New members will receive this photo as welcome (no buttons).")


async def setwelcome_video_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    msg = update.effective_message
    if not msg.reply_to_message or not (msg.reply_to_message.video or msg.reply_to_message.document):
        await msg.reply_text("Reply to a video message with /setwelcome_video to set the welcome video.")
        return
    if msg.reply_to_message.video:
        file_id = msg.reply_to_message.video.file_id
    else:
        file_id = msg.reply_to_message.document.file_id
    chat_id = update.effective_chat.id
    await load_chat_settings(chat_id)
    chat_settings_cache[chat_id]["welcome_video"] = file_id
    chat_settings_cache[chat_id]["welcome_photo"] = None
    await save_chat_settings(chat_id)
    await msg.reply_text("Welcome video saved. New members will receive this video as welcome (no buttons).")


async def clear_welcome_media_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    chat_id = update.effective_chat.id
    await load_chat_settings(chat_id)
    chat_settings_cache[chat_id]["welcome_photo"] = None
    chat_settings_cache[chat_id]["welcome_video"] = None
    await save_chat_settings(chat_id)
    await update.effective_message.reply_text("Cleared welcome photo & video.")


# --------------------- Bye (goodbye) commands ---------------------
async def setbye_text_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    chat_id = update.effective_chat.id
    if not context.args:
        await update.effective_message.reply_text("Usage: /setbye_text Your bye text (use {first_name})")
        return
    text = " ".join(context.args)
    await load_chat_settings(chat_id)
    chat_settings_cache[chat_id]["bye_text"] = text
    await save_chat_settings(chat_id)
    await update.effective_message.reply_text("Goodbye text updated.")


async def setbye_photo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    msg = update.effective_message
    if not msg.reply_to_message or not msg.reply_to_message.photo:
        await msg.reply_text("Reply to a photo message with /setbye_photo to set the goodbye photo.")
        return
    photo = msg.reply_to_message.photo[-1]
    file_id = photo.file_id
    chat_id = update.effective_chat.id
    await load_chat_settings(chat_id)
    chat_settings_cache[chat_id]["bye_photo"] = file_id
    await save_chat_settings(chat_id)
    await msg.reply_text("Goodbye photo saved. When a member leaves, the bot will send this photo (if set).")


async def clear_bye_media_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    chat_id = update.effective_chat.id
    await load_chat_settings(chat_id)
    chat_settings_cache[chat_id]["bye_photo"] = None
    await save_chat_settings(chat_id)
    await update.effective_message.reply_text("Cleared goodbye photo.")


# ----------------- Unified /filter command (sticker triggers only) ---------------
async def filter_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Behavior:
    - Reply to a sticker + /filter <trigger phrase> -> registers sticker trigger (admin only)
    - /filter remove <trigger> -> removes sticker trigger (admin only)
    - /filter list -> lists sticker triggers
    """
    msg = update.effective_message
    chat_id = update.effective_chat.id

    # register trigger from replied sticker
    if msg.reply_to_message and msg.reply_to_message.sticker and context.args:
        if not await require_admin(update, context):
            return
        trigger = " ".join(context.args).strip().lower()
        if not trigger:
            await msg.reply_text("Trigger phrase cannot be empty.")
            return

        sticker_file_id = msg.reply_to_message.sticker.file_id

        await load_chat_settings(chat_id)
        triggers = chat_settings_cache[chat_id].get("sticker_triggers", {}) or {}

        triggers[trigger] = sticker_file_id
        chat_settings_cache[chat_id]["sticker_triggers"] = triggers

        await save_chat_settings(chat_id)

        await msg.reply_text(f"Sticker trigger added: '{trigger}'")
        return

    # remove trigger
    if context.args and context.args[0].lower() == "remove":
        if not await require_admin(update, context):
            return

        if len(context.args) < 2:
            await msg.reply_text("Usage: /filter remove <trigger phrase>")
            return

        trigger = " ".join(context.args[1:]).strip().lower()

        await load_chat_settings(chat_id)
        triggers = chat_settings_cache[chat_id].get("sticker_triggers", {}) or {}

        if trigger not in triggers:
            await msg.reply_text("No such trigger found.")
            return

        triggers.pop(trigger)
        chat_settings_cache[chat_id]["sticker_triggers"] = triggers

        await save_chat_settings(chat_id)
        await msg.reply_text(f"Removed trigger '{trigger}'.")
        return

    # list triggers
    if context.args and context.args[0].lower() == "list":
        await load_chat_settings(chat_id)
        triggers = chat_settings_cache[chat_id].get("sticker_triggers", {}) or {}

        if not triggers:
            await msg.reply_text("No triggers set.")
            return

        text = "Sticker triggers:\n" + "\n".join([f"• {t}" for t in triggers])
        await msg.reply_text(text)
        return

    # fallback help
    await msg.reply_text(
        "Usage:\n"
        "Reply to a sticker + /filter <trigger> — add sticker trigger (admin only)\n"
        "/filter remove <trigger> — remove sticker trigger\n"
        "/filter list — list all triggers\n"
    )



# moderation utilities (ban/unban/etc.)
async def resolve_target_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Tuple[Optional[int], Optional[str]]:
    msg = update.effective_message
    if msg and msg.reply_to_message:
        u = msg.reply_to_message.from_user
        return u.id, u.username or None
    if context.args:
        arg = context.args[0].strip()
        if arg.isdigit():
            return int(arg), None
        username = arg.lstrip("@")
        try:
            chat = await context.bot.get_chat(username)
            return chat.id, getattr(chat, "username", None)
        except Exception:
            try:
                member = await context.bot.get_chat_member(update.effective_chat.id, username)
                return member.user.id, member.user.username or None
            except Exception:
                return None, None
    return None, None


async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    target_id, target_username = await resolve_target_user(update, context)
    if not target_id:
        await update.effective_message.reply_text("Reply to a user or provide user id / @username to ban.")
        return
    reason = " ".join(context.args[1:]) if len(context.args) > 1 else ""
    try:
        await context.bot.ban_chat_member(update.effective_chat.id, target_id)
        await record_ban(update.effective_chat.id, target_id, target_username, update.effective_user.id, reason)
        await update.effective_message.reply_text(f"Banned {target_id}{(' (@' + target_username + ')') if target_username else ''}.")
    except Exception as e:
        await update.effective_message.reply_text("Failed to ban: " + str(e))


async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    target_id, _ = await resolve_target_user(update, context)
    if not target_id:
        await update.effective_message.reply_text("Reply to user or use: /unban <user_id or @username>")
        return
    try:
        await context.bot.unban_chat_member(update.effective_chat.id, target_id)
        await remove_ban_record(update.effective_chat.id, target_id)
        await update.effective_message.reply_text(f"User {target_id} has been unbanned.")
    except Exception as e:
        await update.effective_message.reply_text("Failed to unban: " + str(e))


async def unbanall_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    chat_id = update.effective_chat.id
    rows = await get_bans_for_chat(chat_id)
    if not rows:
        await update.effective_message.reply_text("No recorded bans for this chat.")
        return
    success = failed = 0
    for user_id, username, mod_id, reason, banned_at in rows:
        try:
            await context.bot.unban_chat_member(chat_id, user_id)
            await remove_ban_record(chat_id, user_id)
            success += 1
        except Exception as e:
            logger.info("Failed unban %s: %s", user_id, e)
            failed += 1
    await update.effective_message.reply_text(f"Unbanall complete. Success: {success}, Failed: {failed}")


async def banlog_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    chat_id = update.effective_chat.id
    rows = await get_bans_for_chat(chat_id)
    if not rows:
        await update.effective_message.reply_text("No bans recorded for this chat.")
        return
    lines = []
    for user_id, username, mod_id, reason, banned_at in rows:
        ts = datetime.fromtimestamp(banned_at).strftime("%Y-%m-%d %H:%M")
        uname = f"@{username}" if username else "(no username)"
        mod_text = f"{mod_id}" if mod_id else "(unknown)"
        reason_text = f" — {reason}" if reason else ""
        lines.append(f"• <code>{user_id}</code> — {uname}\n   banned at: {ts} by {mod_text}{reason_text}")
    text = "<b>Ban Log:</b>\n\n" + "\n\n".join(lines)
    await update.effective_message.reply_html(text)


async def kick_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    msg = update.effective_message
    if not msg.reply_to_message:
        await msg.reply_text("Reply to a user's message to kick them.")
        return
    target = msg.reply_to_message.from_user
    try:
        await context.bot.ban_chat_member(update.effective_chat.id, target.id)
        await context.bot.unban_chat_member(update.effective_chat.id, target.id)
        await msg.reply_text(f"Kicked {target.full_name}")
    except Exception as e:
        await msg.reply_text("Failed to kick: " + str(e))


async def mute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    msg = update.effective_message
    if not msg.reply_to_message:
        await msg.reply_text("Reply to user to mute.")
        return
    target = msg.reply_to_message.from_user
    seconds = 60
    if context.args:
        try:
            seconds = int(context.args[0])
        except Exception:
            seconds = 60
    until_ts = int(time.time()) + seconds
    try:
        await context.bot.restrict_chat_member(
            update.effective_chat.id,
            target.id,
            make_perms(can_send_messages=False),
            until_date=until_ts,
        )
        await set_mute(update.effective_chat.id, target.id, until_ts)
        await msg.reply_text(f"Muted {target.full_name} for {seconds} seconds")
    except Exception as e:
        await msg.reply_text("Failed to mute: " + str(e))


async def unmute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    msg = update.effective_message
    if not msg.reply_to_message:
        await msg.reply_text("Reply to user to unmute.")
        return
    target = msg.reply_to_message.from_user
    try:
        await context.bot.restrict_chat_member(
            update.effective_chat.id,
            target.id,
            make_perms(
                can_send_messages=True,
                can_send_media_messages=True,
                can_send_polls=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
            ),
        )
        await set_mute(update.effective_chat.id, target.id, 0)
        await msg.reply_text(f"Unmuted {target.full_name}")
    except Exception as e:
        await msg.reply_text("Failed to unmute: " + str(e))


async def warn_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    msg = update.effective_message
    if not msg.reply_to_message:
        await msg.reply_text("Reply to user to warn.")
        return
    target = msg.reply_to_message.from_user
    new = await change_warns(update.effective_chat.id, target.id, 1)
    await msg.reply_text(f"Warned {target.full_name}. Total warns: {new}")
    if new >= 3:
        try:
            await context.bot.ban_chat_member(update.effective_chat.id, target.id)
            await record_ban(update.effective_chat.id, target.id, target.username or None, update.effective_user.id, "auto-warn-3")
            await msg.reply_text(f"{target.full_name} was banned after reaching {new} warns.")
        except Exception as e:
            await msg.reply_text("Failed to auto-ban: " + str(e))


async def warnings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg.reply_to_message and not context.args:
        await msg.reply_text("Reply to user or provide id to check warns.")
        return
    if msg.reply_to_message:
        target = msg.reply_to_message.from_user
    else:
        try:
            uid = int(context.args[0])
            target = type("T", (), {"id": uid, "full_name": str(uid)})()
        except Exception:
            await msg.reply_text("Could not find user.")
            return
    n = await get_warns(update.effective_chat.id, target.id)
    await msg.reply_text(f"{target.full_name} has {n} warns.")


# --------------------- Lock/Unlock helpers ---------------------
async def lock_chat_permissions(bot, chat_id: int):
    """
    Set chat permissions so regular users cannot send messages.
    Uses set_chat_permissions which sets default permissions for the chat.
    """
    try:
        perms = make_perms(can_send_messages=False)
        await bot.set_chat_permissions(chat_id, perms)
        return True
    except Exception as e:
        logger.exception("Failed to lock chat %s: %s", chat_id, e)
        return False


async def unlock_chat_permissions(bot, chat_id: int):
    """
    Restore basic permissions (allow sending messages and web previews).
    Adjust as needed for your group's defaults.
    """
    try:
        perms = make_perms(
            can_send_messages=True,
            can_send_media_messages=True,
            can_send_polls=True,
            can_send_other_messages=True,
            can_add_web_page_previews=True,
        )
        await bot.set_chat_permissions(chat_id, perms)
        return True
    except Exception as e:
        logger.exception("Failed to unlock chat %s: %s", chat_id, e)
        return False


# --------------------- Member join & leave & message moderation ---------------------
# on_member_join updated earlier; keep as-is (sends welcome)
async def on_member_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Sends configured welcome for each new member.
    + Shows who added the user OR if they joined via link.
    """
    msg = update.effective_message
    if not msg or not msg.new_chat_members:
        return

    chat = update.effective_chat
    chat_id = chat.id if chat else None
    if not chat_id:
        return

    # Reload latest settings
    try:
        await load_chat_settings(chat_id)
    except Exception:
        logger.exception("Failed to load chat settings for welcome.")

    s = chat_settings_cache.get(chat_id, {})
    welcome_template = s.get("welcome", WELCOME_FALLBACK)

    inviter = msg.from_user  # person who added them (or self-joined)

    for member in msg.new_chat_members:

        # ---------------------------
        # Detect inviter
        # ---------------------------
        if inviter and inviter.id != member.id:
            added_line = f"🫂 <b>Added by:</b> {inviter.first_name}"
        else:
            added_line = "🟢 <b>Joined via link / self-joined</b>"

        # Build caption
        try:
            first = member.first_name or ""
            uname = f"@{member.username}" if member.username else ""
            try:
                formatted = welcome_template.format(first_name=first, username=uname)
            except Exception:
                formatted = welcome_template
        except Exception:
            formatted = WELCOME_FALLBACK

        final_caption = (
            f"🎉 <b>Welcome {member.first_name}!</b>\n"
            f"{added_line}\n\n"
            f"{formatted}"
        )

        # Send photo / video / text
        try:
            if s.get("welcome_photo"):
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=s["welcome_photo"],
                    caption=final_caption,
                    parse_mode="HTML"
                )
            elif s.get("welcome_video"):
                await context.bot.send_video(
                    chat_id=chat_id,
                    video=s["welcome_video"],
                    caption=final_caption,
                    parse_mode="HTML"
                )
            else:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=final_caption,
                    parse_mode="HTML"
                )
        except Exception:
            # Fallback minimal welcome
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"Welcome, {member.first_name}!"
                )
            except Exception:
                logger.exception(
                    "Failed to send welcome message for member %s in chat %s",
                    getattr(member, "id", None), chat_id
                )


# NEW: on_member_left handler (goodbye)
async def on_member_left(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Fires when a user leaves the group. Sends configured bye text or photo (if set).
    """
    if not update.effective_message:
        return
    left = update.effective_message.left_chat_member
    if not left:
        return
    chat = update.effective_chat
    chat_id = chat.id if chat else None
    if not chat_id:
        return

    # ensure latest settings
    try:
        await load_chat_settings(chat_id)
    except Exception:
        logger.exception("Failed to load chat settings for bye.")

    s = chat_settings_cache.get(chat_id, {})
    bye_template = s.get("bye_text", "") or ""
    bye_photo = s.get("bye_photo", None)

    # format with first_name
    name = left.first_name or left.full_name or ""
    username = ('@' + left.username) if left.username else ""
    try:
        if bye_template:
            try:
                text = bye_template.format(first_name=name, username=username)
            except Exception:
                text = bye_template
        else:
            text = f"Goodbye, {name}!"
    except Exception:
        text = f"Goodbye, {name}!"

    # Send photo if configured; else send text
    try:
        if bye_photo:
            try:
                await context.bot.send_photo(chat_id=chat_id, photo=bye_photo, caption=text, parse_mode="HTML")
            except Exception:
                # fallback plain text
                await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
        else:
            await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
    except Exception:
        logger.exception("Failed to send bye message for member %s in chat %s", getattr(left, "id", None), chat_id)


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # route "start"
    if update.effective_message and update.effective_message.text:
        txt = update.effective_message.text.strip().lower()
        if txt == "start":
            await start_cmd(update, context)
            return

    message = update.effective_message
    if not message or not message.from_user:
        return
    user = message.from_user
    chat_id = update.effective_chat.id

    await load_chat_settings(chat_id)
    s = chat_settings_cache[chat_id]




    # If chat is locked, optionally delete messages from non-admins (set_chat_permissions already prevents sending, but some clients still allow via bots/api)
    if s.get("locked", False) and not await is_user_admin(update, context, user.id):
        try:
            await message.delete()
        except Exception:
            pass
        return

     # Sticker trigger handling: match trigger as substring (works when message contains @username + phrase)
    if message.text:
        raw_text = message.text
        txt = raw_text.lower()
        triggers = s.get("sticker_triggers", {}) or {}

        matched_trigger = None
        matched_file_id = None
        for trigger_phrase, file_id in triggers.items():
            if not trigger_phrase:
                continue
            if trigger_phrase.lower() in txt:
                matched_trigger = trigger_phrase
                matched_file_id = file_id
                break

        if matched_trigger and matched_file_id:
            # send sticker (best effort)
            try:
                await context.bot.send_sticker(chat_id, matched_file_id)
            except Exception:
                try:
                    await context.bot.send_message(chat_id, f"(sticker unavailable for trigger '{matched_trigger}')")
                except Exception:
                    pass

            # Determine mention_target_html (clickable) if possible
            mention_html = None

            # 1) prefer text_mention entity (has User object)
            try:
                if message.entities:
                    for ent in message.entities:
                        if ent.type == "text_mention" and getattr(ent, "user", None):
                            mention_html = ent.user.mention_html()
                            break
            except Exception:
                mention_html = None

            # 2) otherwise if there's a plain @username mention, try to resolve to a User id and build tg://user link
            if mention_html is None:
                try:
                    if message.entities:
                        for ent in message.entities:
                            if ent.type == "mention":
                                start = ent.offset
                                length = ent.length
                                username_text = raw_text[start : start + length]  # like "@rohit_2007_18_11"
                                username = username_text.lstrip("@")
                                # try resolving username to a chat (this returns a Chat/User-like object)
                                try:
                                    chat_obj = await context.bot.get_chat(username)
                                    # chat_obj.id is the user's id when username refers to a user
                                    user_id = getattr(chat_obj, "id", None)
                                    display = username_text  # keep @username as visible text
                                    if user_id:
                                        mention_html = f'<a href="tg://user?id={user_id}">{display}</a>'
                                        break
                                except Exception:
                                    # resolution failed (user may not exist or privacy); continue to next entity
                                    mention_html = None
                                    continue
                except Exception:
                    mention_html = None

            # 3) fallback: mention the message sender (clickable via mention_html if we have user object)
            if mention_html is None:
                try:
                    mention_html = message.from_user.mention_html()
                except Exception:
                    mention_html = getattr(message.from_user, "first_name", "Someone")

            # Send message replying to original with HTML parse_mode so mention works
            try:
                await context.bot.send_message(
                    chat_id,
                    f"{mention_html} triggered: '{matched_trigger}'",
                    parse_mode="HTML",
                    reply_to_message_id=message.message_id,
                )
            except Exception:
                # final fallback: plain text without HTML mention
                try:
                    await context.bot.send_message(
                        chat_id,
                        f"{getattr(message.from_user, 'first_name', 'Someone')} triggered: '{matched_trigger}'",
                        reply_to_message_id=message.message_id,
                    )
                except Exception:
                    pass

            return

    # Anti-link: now enforced via toggle + auto-ban on 3 warns
    if s.get("anti_link", True):
        text = message.text or message.caption or ""
        if text and LINK_PATTERN.search(text):
            if not await is_user_admin(update, context, user.id):
                # delete message if possible
                try:
                    await message.delete()
                except Exception:
                    pass
                # increment warn
                new = await change_warns(chat_id, user.id, 1)
                try:
                    await context.bot.send_message(chat_id, f"Links are not allowed — {user.first_name} was warned. Warns: {new}")
                except Exception:
                    pass
                # auto-ban on 3 warns
                if new >= 3:
                    try:
                        await context.bot.ban_chat_member(chat_id, user.id)
                        await record_ban(chat_id, user.id, user.username or None, None, "auto-link-3")
                        try:
                            await context.bot.send_message(chat_id, f"{user.full_name} was banned after reaching {new} warns (links).")
                        except Exception:
                            pass
                    except Exception as e:
                        logger.exception("Failed to auto-ban user for link warns: %s", e)
                return
    
    # --------------------- PROFANITY CHECK ---------------------
    try:
        text_for_check = (message.text or message.caption or "") or ""
        if text_for_check and BAD_WORDS_RE.search(text_for_check):
            # skip bots
            if message.from_user and message.from_user.is_bot:
                pass
            else:
                uid = message.from_user.id if message.from_user else None
                # skip chat admins / global ADMINS
                if uid and await is_user_admin(update, context, uid):
                    # admins are not moderated
                    pass
                else:
                    # Try to delete the offending message (best effort)
                    try:
                        await message.delete()
                    except Exception:
                        logger.debug("Could not delete profane message (may lack permission).")

                    # issue a warn using existing helper
                    new_warns = await change_warns(chat_id, uid, 1)

                    # notify (best-effort)
                    try:
                        await context.bot.send_message(chat_id, f"{message.from_user.first_name}, inappropriate language is not allowed. Warns: {new_warns}")
                    except Exception:
                        logger.debug("Could not send profanity warn message.")

                    # auto-ban on 3 warns (same behaviour as existing flows)
                    if new_warns >= 3:
                        try:
                            await context.bot.ban_chat_member(chat_id, uid)
                            await record_ban(chat_id, uid, message.from_user.username or None, None, "auto-profanity-3")
                            try:
                                await context.bot.send_message(chat_id, f"{message.from_user.full_name or message.from_user.first_name} was banned after reaching {new_warns} warns (bad language).")
                            except Exception:
                                pass
                        except Exception:
                            logger.exception("Failed auto-ban for profanity on user %s in chat %s", uid, chat_id)

                    # stop further processing of this message
                    return
    except Exception as e:
        logger.exception("Profanity filter failed: %s", e)


    # Anti-spam (per-chat configurable)
    now = time.time()
    dq = message_history[user.id][chat_id]
    dq.append(now)
    while dq and now - dq[0] > SPAM_WINDOW_SEC:
        dq.popleft()

    # respect per-chat spam toggle/limit
    spam_enabled = s.get("spam_enabled", True)
    spam_limit = int(s.get("spam_limit", SPAM_MAX_MSG))

    if spam_enabled and len(dq) > spam_limit and not await is_user_admin(update, context, user.id):
        # delete offending message (best effort)
        try:
            await message.delete()
        except Exception:
            pass

        new = await change_warns(chat_id, user.id, 1)
        try:
            await context.bot.send_message(chat_id, f"{user.first_name}, please stop spamming. You were warned. Warns: {new}")
        except Exception:
            pass

        # auto-ban after 3 warns (same behaviour as existing flows)
        if new >= 3:
            try:
                reason = s.get("spam_ban_reason", "auto-spam-limit")
                await context.bot.ban_chat_member(chat_id, user.id)
                await record_ban(chat_id, user.id, user.username or None, None, reason)
                try:
                    await context.bot.send_message(chat_id, f"{user.full_name} was banned after reaching {new} warns (spam).")
                except Exception:
                    pass
            except Exception:
                logger.exception("Failed auto-ban for spam on user %s in chat %s", user.id, chat_id)
        return


# --------------------- CallbackQuery handler ---------------------
async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.data:
        return
    data = q.data

    try:
        await q.answer()
    except Exception:
        pass

    def _chat_id():
        try:
            if q.message and q.message.chat and getattr(q.message.chat, "id", None):
                return q.message.chat.id
        except Exception:
            pass
        try:
            if update.effective_chat and getattr(update.effective_chat, "id", None):
                return update.effective_chat.id
        except Exception:
            pass
        return None

    chat_id = _chat_id()

    async def send(chat_id_inner, text, parse_mode="HTML", reply_markup=None):
        if not chat_id_inner:
            try:
                await q.message.reply_text(text)
            except Exception:
                logger.warning("Cannot send message: no chat id and reply failed.")
            return
        try:
            await context.bot.send_message(chat_id=chat_id_inner, text=text, parse_mode=parse_mode, reply_markup=reply_markup)
        except Exception:
            try:
                await q.message.reply_text(text)
            except Exception:
                logger.exception("Failed to send callback response for %s", data)

    async def clicker_is_admin():
        try:
            if not q.from_user:
                return False
            return await is_user_admin(update, context, q.from_user.id)
        except Exception:
            return False

    # HELP
    if data == "cmd_help":
        help_text = (
         "<b>Commands</b>\n\n"
"/setwelcome_text <text> — set welcome text (use {first_name})\n"
"Reply to a photo + /setwelcome_photo — sets welcome photo\n"
"Reply to a video + /setwelcome_video — sets welcome video\n"
"/clear_welcome_media — removes photo & video\n\n"
"/setbye_text <text> — set goodbye text (use {first_name})\n"
"Reply to a photo + /setbye_photo — sets goodbye photo\n"
"/clear_bye_media — clears goodbye photo\n\n"
"Sticker triggers: reply to a sticker + /filter <phrase> to register a trigger (admin-only).\n"
"/filter remove <phrase> — remove trigger\n"
"/filter list — list triggers (only phrases)\n\n"
"Group lock: /lock (admin) — lock chat; /unlock (admin) — unlock chat; /lock_status — show state\n\n"
"Rules: /setrules <text> (admin) — set rules; /rules — show rules; /delrules (admin) — delete rules\n\n"
"Moderation & utility:\n"
"/ban /unban /unbanall /banlog /kick /mute /unmute /warn /warnings\n"
"/info — reply to a user (admin-only) to view user id and details\n"
"/report — reply to a message and send /report <reason> to notify admins\n"
"/ginfo — show group info\n"
"/weather <city> — get weather\n"
"/translate <text> <lang> — translate text to language code\n\n"
"You can also type /menu to open the full interactive menu."
        )
        await send(chat_id, help_text)
        return

    # BAN LOG
    if data == "cmd_banlog":
        if not await clicker_is_admin():
            try:
                await q.answer(text="Ban log is admin-only.", show_alert=False)
            except Exception:
                await send(chat_id, "Ban log is admin-only. Use /help for available commands.")
            return
        try:
            rows = await get_bans_for_chat(chat_id)
            if not rows:
                await send(chat_id, "No bans recorded for this chat.")
                return
            lines = []
            for user_id, username, mod_id, reason, banned_at in rows:
                ts = datetime.fromtimestamp(banned_at).strftime("%Y-%m-%d %H:%M") if banned_at else "(unknown)"
                uname = f"@{username}" if username else "(no username)"
                mod_text = f"{mod_id}" if mod_id else "(unknown)"
                reason_text = f" — {reason}" if reason else ""
                lines.append(f"• <code>{user_id}</code> — {uname}\n   banned at: {ts} by {mod_text}{reason_text}")
            text = "<b>Ban Log:</b>\n\n" + "\n\n".join(lines)
            await send(chat_id, text)
        except Exception:
            logger.exception("Failed to handle cmd_banlog")
            await send(chat_id, "Failed to fetch ban log. Try /banlog.")
        return

    # Toggle anti-link button (admin-only)
    if data == "cmd_toggle_link":
        if not await clicker_is_admin():
            try:
                await q.answer(text="This action is for chat admins only.", show_alert=True)
            except Exception:
                await send(chat_id, "This action is for chat admins only.")
            return
        try:
            await load_chat_settings(chat_id)
            s = chat_settings_cache.get(chat_id, {})
            current = bool(s.get("anti_link", True))
            s["anti_link"] = not current
            chat_settings_cache[chat_id] = s
            await save_chat_settings(chat_id)
            try:
                await q.message.edit_reply_markup(reply_markup=build_full_commands_menu(chat_id))
            except Exception:
                pass
            state_txt = "ON" if s["anti_link"] else "OFF"
            await send(chat_id, f"Anti-link is now: {state_txt}")
        except Exception as e:
            logger.exception("Failed toggling anti-link: %s", e)
            await send(chat_id, "Failed to toggle anti-link.")
        return

    # Toggle lock/unlock button (admin-only)
    if data == "cmd_toggle_lock":
        if not await clicker_is_admin():
            try:
                await q.answer(text="This action is for chat admins only.", show_alert=True)
            except Exception:
                await send(chat_id, "This action is for chat admins only.")
            return
        try:
            await load_chat_settings(chat_id)
            s = chat_settings_cache.get(chat_id, {})
            locked = bool(s.get("locked", False))
            # flip
            new_locked = not locked
            if new_locked:
                ok = await lock_chat_permissions(context.bot, chat_id)
            else:
                ok = await unlock_chat_permissions(context.bot, chat_id)
            if ok:
                s["locked"] = new_locked
                chat_settings_cache[chat_id] = s
                await save_chat_settings(chat_id)
                try:
                    await q.message.edit_reply_markup(reply_markup=build_full_commands_menu(chat_id))
                except Exception:
                    pass
                await send(chat_id, "Chat locked." if new_locked else "Chat unlocked.")
            else:
                await send(chat_id, "Failed to change chat lock state.")
        except Exception as e:
            logger.exception("Failed toggling lock: %s", e)
            await send(chat_id, "Failed to toggle lock.")
        return
    
       # Toggle lock/unlock button (admin-only)
    if data == "cmd_toggle_lock":
        ...  # existing lock handling
        return
    
        # Toggle spam (admin-only)
    if data == "cmd_toggle_spam":
        if not await clicker_is_admin():
            try:
                await q.answer(text="This action is for chat admins only.", show_alert=True)
            except Exception:
                await send(chat_id, "This action is for chat admins only.")
            return
        try:
            await load_chat_settings(chat_id)
            s = chat_settings_cache.get(chat_id, {})
            current = bool(s.get("spam_enabled", True))
            s["spam_enabled"] = not current
            chat_settings_cache[chat_id] = s
            await save_chat_settings(chat_id)
            try:
                await q.message.edit_reply_markup(reply_markup=build_full_commands_menu(chat_id))
            except Exception:
                pass
            state_txt = "ON" if s["spam_enabled"] else "OFF"
            await send(chat_id, f"Spam protection is now: {state_txt}")
        except Exception as e:
            logger.exception("Failed toggling spam: %s", e)
            await send(chat_id, "Failed to toggle spam protection.")
        return


       # Toggle lock/unlock button (admin-only)
    if data == "cmd_toggle_lock":
        ...  # existing lock handling
        return

    # Toggle spam (admin-only)
    if data == "cmd_toggle_spam":
        if not await clicker_is_admin():
            try:
                await q.answer(text="This action is for chat admins only.", show_alert=True)
            except Exception:
                await send(chat_id, "This action is for chat admins only.")
            return
        try:
            await load_chat_settings(chat_id)
            s = chat_settings_cache.get(chat_id, {})
            current = bool(s.get("spam_enabled", True))
            s["spam_enabled"] = not current
            chat_settings_cache[chat_id] = s
            await save_chat_settings(chat_id)
            try:
                await q.message.edit_reply_markup(reply_markup=build_full_commands_menu(chat_id))
            except Exception:
                pass
            state_txt = "ON" if s["spam_enabled"] else "OFF"
            await send(chat_id, f"Spam protection is now: {state_txt}")
        except Exception as e:
            logger.exception("Failed toggling spam: %s", e)
            await send(chat_id, "Failed to toggle spam protection.")
        return

    # Admin-only actions set
   

 

    # Admin-only actions set
    admin_actions = {
        "cmd_ban", "cmd_unban", "cmd_unbanall", "confirm_unbanall",
        "cmd_mute", "cmd_unmute", "cmd_kick", "cmd_warn", "cmd_warnings",
        "cmd_setwelcome", "cmd_setphoto", "cmd_setvideo", "cmd_clear_media",
        "cmd_filter_sticker_help", "cmd_refresh",
    }
    if data in admin_actions:
        if not await clicker_is_admin():
            try:
                await q.answer(text="This action is for chat admins only.", show_alert=False)
            except Exception:
                await send(chat_id, "⚠️ This action is for chat admins only.")
            return

    try:
        if data == "cmd_ban":
            await send(chat_id, "To ban: reply to the user's message and type /ban, or use /ban <id> or /ban @username")
        elif data == "cmd_unban":
            await send(chat_id, "To unban: reply to the user's message and type /unban, or /unban <id> or /unban @username")
        elif data == "cmd_unbanall":
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Confirm Unban All", callback_data="confirm_unbanall")],
                [InlineKeyboardButton("❌ Cancel", callback_data="cmd_help")],
            ])
            try:
                await q.message.edit_reply_markup(reply_markup=kb)
                await q.answer(text="Confirm Unban All (button updated).", show_alert=False)
            except Exception:
                await send(chat_id, "Confirm unban-all by pressing the Confirm button below:", reply_markup=kb)
        elif data == "confirm_unbanall":
            await unbanall_cmd(update, context)
        elif data == "cmd_mute":
            await send(chat_id, "Reply to a user's message and type /mute <seconds> (e.g. /mute 300).")
        elif data == "cmd_unmute":
            await send(chat_id, "Reply to a user's message and type /unmute.")
        elif data == "cmd_kick":
            await send(chat_id, "Reply to a user's message and type /kick to remove them once.")
        elif data == "cmd_warn":
            await send(chat_id, "Reply to a user's message and type /warn to add a warn.")
        elif data == "cmd_warnings":
            await send(chat_id, "Reply to a user and type /warnings to see their warns.")
        elif data == "cmd_setwelcome":
            await send(chat_id, "Set welcome text with:\n\n/setwelcome_text Welcome {first_name} 👋")
        elif data == "cmd_setphoto":
            await send(chat_id, "Reply to a photo and send /setwelcome_photo to save it as the welcome photo.")
        elif data == "cmd_setvideo":
            await send(chat_id, "Reply to a video and send /setwelcome_video to save it as the welcome video.")
        elif data == "cmd_clear_media":
            await send(chat_id, "Use /clear_welcome_media to remove saved photo/video.")
        elif data == "cmd_filter_sticker_help":
            await send(chat_id, "Sticker triggers: reply to a sticker + /filter <phrase> to register a trigger. When anyone types that phrase the bot will send that sticker once. Use '/filter remove <phrase>' to remove, '/filter list' to list (only phrases).")
        elif data == "cmd_refresh":
            try:
                text = pretty_welcome_text(chat_id, q.from_user.first_name if q.from_user else "there", q.from_user.username if q.from_user else None)
                try:
                    await q.message.edit_text(text, parse_mode="HTML", reply_markup=build_full_commands_menu(chat_id))
                except Exception:
                    await send(chat_id, text, reply_markup=build_full_commands_menu(chat_id))
            except Exception:
                logger.exception("Failed to refresh menu via callback")
                await send(chat_id, "Unable to refresh this message.")
        else:
            try:
                await q.answer(text="Unknown button action.", show_alert=False)
            except Exception:
                await send(chat_id, "Unknown button action.")
    except Exception as e:
        logger.exception("Error handling callback %s: %s", data, e)
        try:
            await q.answer(text="An error occurred while handling that button.", show_alert=False)
        except Exception:
            await send(chat_id, "An error occurred while handling that button.")


# --------------------- Periodic job wrapper ---------------------
async def periodic_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        await clear_expired_mutes(context.application)
    except Exception as e:
        logger.exception("Periodic job failed: %s", e)


# --------------------- Admin commands to lock/unlock (text commands) ---------------------
async def lock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    chat_id = update.effective_chat.id
    ok = await lock_chat_permissions(context.bot, chat_id)
    if ok:
        await load_chat_settings(chat_id)
        chat_settings_cache[chat_id]["locked"] = True
        await save_chat_settings(chat_id)
        await update.effective_message.reply_text("Chat has been locked (non-admins cannot send messages).")
    else:
        await update.effective_message.reply_text("Failed to lock chat — ensure I have permission to change chat settings.")


async def unlock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    chat_id = update.effective_chat.id
    ok = await unlock_chat_permissions(context.bot, chat_id)
    if ok:
        await load_chat_settings(chat_id)
        chat_settings_cache[chat_id]["locked"] = False
        await save_chat_settings(chat_id)
        await update.effective_message.reply_text("Chat has been unlocked.")
    else:
        await update.effective_message.reply_text("Failed to unlock chat — ensure I have permission to change chat settings.")

async def setspam_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not context.args or context.args[0].lower() not in ("on", "off"):
        await update.effective_message.reply_text("Usage: /setspam on|off")
        return
    chat_id = update.effective_chat.id
    await load_chat_settings(chat_id)
    chat_settings_cache[chat_id]["spam_enabled"] = (context.args[0].lower() == "on")
    await save_chat_settings(chat_id)
    await update.effective_message.reply_text(f"Spam protection set to: {'ON' if chat_settings_cache[chat_id]['spam_enabled'] else 'OFF'}")

async def setspam_limit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /setspam_limit <number>  — number of messages in spam window that triggers warn/ban.")
        return
    try:
        n = int(context.args[0])
        if n < 1:
            raise ValueError()
    except Exception:
        await update.effective_message.reply_text("Please provide a positive integer for the limit.")
        return
    chat_id = update.effective_chat.id
    await load_chat_settings(chat_id)
    chat_settings_cache[chat_id]["spam_limit"] = n
    await save_chat_settings(chat_id)
    await update.effective_message.reply_text(f"Spam limit updated to {n} messages (per {SPAM_WINDOW_SEC}s window).")

async def setspam_reason_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /setspam_reason <text>  — reason to record when auto-banning for spam.")
        return
    reason = " ".join(context.args).strip()
    chat_id = update.effective_chat.id
    await load_chat_settings(chat_id)
    chat_settings_cache[chat_id]["spam_ban_reason"] = reason
    await save_chat_settings(chat_id)
    await update.effective_message.reply_text(f"Spam auto-ban reason set to: {reason}")



# --------------------- /lock_status command ---------------------
async def lock_status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Show whether the chat is currently locked.
    Anyone can call this.
    """
    chat_id = update.effective_chat.id
    await load_chat_settings(chat_id)
    locked = chat_settings_cache.get(chat_id, {}).get("locked", False)
    if locked:
        await update.effective_message.reply_text("🔒 Chat is currently locked. Only admins can send messages.")
    else:
        await update.effective_message.reply_text("🔓 Chat is currently unlocked. Everyone may send messages.")


# --------------------- Rules commands ---------------------
async def rules_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await load_chat_settings(chat_id)
    rules = chat_settings_cache.get(chat_id, {}).get("rules_text", "") or ""
    if not rules:
        await update.effective_message.reply_text("No rules set for this chat. Admins can set rules with /setrules <text>.")
    else:
        await update.effective_message.reply_text(f"📜 Group rules:\n\n{rules}")


async def setrules_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    chat_id = update.effective_chat.id
    if not context.args:
        await update.effective_message.reply_text("Usage: /setrules <text>\nExample: /setrules Be respectful. No spam.")
        return
    rules_text = " ".join(context.args).strip()
    await load_chat_settings(chat_id)
    chat_settings_cache[chat_id]["rules_text"] = rules_text
    await save_chat_settings(chat_id)
    await update.effective_message.reply_text("Rules updated.")


# --------------------- Report command ---------------------
async def report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Usage:
    - Reply to a message and type: /report <optional reason>
    This will forward the reported message and metadata to chat admins and to ADMINS list.
    """
    msg = update.effective_message
    if not msg.reply_to_message:
        await msg.reply_text("Reply to the message you want to report, then use /report <optional reason>.")
        return
    reported = msg.reply_to_message
    reporter = msg.from_user
    reason = " ".join(context.args) if context.args else "(no reason provided)"

    # Build a report text
    text_lines = [
        "<b>Report</b>",
        f"Reporter: {reporter.mention_html()} (id: {reporter.id})",
        f"Chat: {update.effective_chat.title or update.effective_chat.id} (id: {update.effective_chat.id})",
        f"Reason: {reason}",
        f"Link to message (if available):",
    ]
    report_text = "\n".join(text_lines)

    # Forward the actual reported message to admins where possible, with context
    try:
        admins = await context.bot.get_chat_administrators(update.effective_chat.id)
    except Exception:
        admins = []

    admin_ids = [a.user.id for a in admins if a and a.user and not a.user.is_bot]
    # also include global ADMINS from env
    for adm in ADMINS:
        if adm not in admin_ids:
            admin_ids.append(adm)

    forwarded_count = 0
    for admin_id in admin_ids:
        try:
            await context.bot.send_message(admin_id, report_text, parse_mode="HTML")
            try:
                await context.bot.forward_message(admin_id, update.effective_chat.id, reported.message_id)
            except Exception:
                await context.bot.send_message(admin_id, f"(Could not forward full message) from {update.effective_chat.title or update.effective_chat.id}")
            forwarded_count += 1
        except Exception:
            continue

    await msg.reply_text(f"Report submitted. Admins notified (attempted {forwarded_count} admins). Thank you.")


# --------------------- ginfo command ---------------------
async def ginfo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    try:
        member_count = None
        try:
            member_count = await context.bot.get_chat_member_count(chat.id)
        except Exception:
            try:
                member_count = await context.bot.get_chat_members_count(chat.id)
            except Exception:
                member_count = None
        text = f"<b>Group Info</b>\n\nTitle: {chat.title or chat.id}\nID: {chat.id}\nType: {chat.type}\n"
        if member_count is not None:
            text += f"Members: {member_count}\n"
        if chat.description:
            text += f"\nDescription:\n{chat.description}\n"
        await update.effective_message.reply_html(text)
    except Exception as e:
        await update.effective_message.reply_text("Could not fetch group info: " + str(e))









# --------------------- weather command ---------------------
async def get_weather_from_rapidapi(lat, lon):
    url = f"https://{RAPIDAPI_HOST}/v1/weather"
    headers = {
        "X-RapidAPI-Key": RAPIDAPI_KEY,
        "X-RapidAPI-Host": RAPIDAPI_HOST
    }
    params = {"lat": lat, "lon": lon}

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers, params=params) as res:
            if res.status != 200:
                text = await res.text()
                raise RuntimeError(f"Weather API returned {res.status}: {text}")
            return await res.json()


async def geocode_city(city: str):
    """
    Free geocoding using Open-Meteo (no API key required)
    """
    url = "https://geocoding-api.open-meteo.com/v1/search"
    params = {"name": city, "count": 1}

    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params) as res:
            data = await res.json()
            if "results" in data and data["results"]:
                r = data["results"][0]
                return r["latitude"], r["longitude"], r["name"], r.get("country", "")
            else:
                return None


async def weather_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message

    if not context.args:
        await msg.reply_text("Usage: /weather <city>\nExample: /weather Mumbai")
        return

    city = " ".join(context.args)

    geodata = await geocode_city(city)
    if not geodata:
        await msg.reply_text("City not found.")
        return

    lat, lon, name, country = geodata

    await msg.reply_text(f"Fetching weather for {name}, {country}...\nCoordinates: {lat}, {lon}")

    try:
        weather = await get_weather_from_rapidapi(lat, lon)
    except Exception as e:
        await msg.reply_text(f"Failed to get weather: {e}")
        return

    text = (
        f"🌤 Weather for *{name}, {country}*\n"
        f"🌡 Temperature: {weather.get('temp')}°C\n"
        f"🤗 Feels like: {weather.get('feels_like')}°C\n"
        f"💧 Humidity: {weather.get('humidity')}%\n"
        f"💨 Wind: {weather.get('wind_speed')} m/s\n"
        f"☁️ Clouds: {weather.get('cloud_pct')}%\n"
        f"🌅 Sunrise: {weather.get('sunrise')}\n"
        f"🌇 Sunset: {weather.get('sunset')}"
    )

    await msg.reply_markdown(text)


# --------------------- translate command ---------------------
async def translate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Usage: /translate <text> <lang>
    Example: /translate Hello world hi
    - Translates text to language code specified. Uses deep-translator (GoogleTranslator).
    """
    if not context.args or len(context.args) < 2:
        await update.effective_message.reply_text("Usage: /translate <text> <lang>\nExample: /translate Hello world hi")
        return
    # last arg is target language
    target = context.args[-1]
    text = " ".join(context.args[:-1])
    # run translation in thread (blocking library)
    try:
        translated = await asyncio.to_thread(GoogleTranslator(source='auto', target=target).translate, text)
        await update.effective_message.reply_text(f"Translated ({target}):\n{translated}")
    except Exception as e:
        await update.effective_message.reply_text("Translation failed: " + str(e))


# --------------------- Main / startup ---------------------
def main():
    if BOT_TOKEN == "REPLACE_ME" or not BOT_TOKEN:
        print("Please set BOT_TOKEN environment variable in .env file.")
        return

    asyncio.run(init_db())

    # Ensure event loop exists
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("menu", menu_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    # welcome text & media
    app.add_handler(CommandHandler("setwelcome_text", setwelcome_text_cmd))
    app.add_handler(CommandHandler("setwelcome_photo", setwelcome_photo_cmd))
    app.add_handler(CommandHandler("setwelcome_video", setwelcome_video_cmd))
    app.add_handler(CommandHandler("clear_welcome_media", clear_welcome_media_cmd))

    # goodbye text & media
    app.add_handler(CommandHandler("setbye_text", setbye_text_cmd))
    app.add_handler(CommandHandler("setbye_photo", setbye_photo_cmd))
    app.add_handler(CommandHandler("clear_bye_media", clear_bye_media_cmd))

    # unified /filter command (sticker triggers)
    app.add_handler(CommandHandler("filter", filter_cmd))

    # lock/unlock commands
    app.add_handler(CommandHandler("lock", lock_cmd))
    app.add_handler(CommandHandler("unlock", unlock_cmd))
    app.add_handler(CommandHandler("lock_status", lock_status_cmd))

    # rules & utility
    app.add_handler(CommandHandler("rules", rules_cmd))
    app.add_handler(CommandHandler("setrules", setrules_cmd))
    app.add_handler(CommandHandler("report", report_cmd))
    app.add_handler(CommandHandler("ginfo", ginfo_cmd))
    app.add_handler(CommandHandler("weather", weather_cmd))
    app.add_handler(CommandHandler("translate", translate_cmd))
    app.add_handler(CommandHandler("info", info_cmd))

    # moderation
    app.add_handler(CommandHandler("ban", ban_cmd))
    app.add_handler(CommandHandler("unban", unban_cmd))
    app.add_handler(CommandHandler("unbanall", unbanall_cmd))
    app.add_handler(CommandHandler("banlog", banlog_cmd))
    app.add_handler(CommandHandler("kick", kick_cmd))
    app.add_handler(CommandHandler("mute", mute_cmd))
    app.add_handler(CommandHandler("unmute", unmute_cmd))
    app.add_handler(CommandHandler("warn", warn_cmd))
    app.add_handler(CommandHandler("warnings", warnings_cmd))
    app.add_handler(CommandHandler("setspam", setspam_cmd))
    app.add_handler(CommandHandler("setspam_limit", setspam_limit_cmd))
    app.add_handler(CommandHandler("setspam_reason", setspam_reason_cmd))


    # Member joins (welcome)
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_member_join))
    # Member left (goodbye)
    app.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, on_member_left))

    # SINGLE message handler to avoid double-calls:
    combined_filter = filters.ALL & ~filters.COMMAND & ~filters.StatusUpdate.NEW_CHAT_MEMBERS & ~filters.StatusUpdate.LEFT_CHAT_MEMBER
    app.add_handler(MessageHandler(combined_filter, on_message))

    # Callback queries
    app.add_handler(CallbackQueryHandler(callback_router))

    # Job queue
    app.job_queue.run_repeating(periodic_job, interval=30, first=10)

    logger.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
