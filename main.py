import os
import re
import sqlite3
import logging
from datetime import datetime, timedelta, timezone

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("madara_giveaway_bot")

IST = timezone(timedelta(hours=5, minutes=30))

# ---------- ENV ----------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN env var missing")

# REQUIRED_CHANNELS: comma-separated: @channel1,@channel2,-1001234567890
REQUIRED_CHANNELS = [c.strip() for c in os.getenv("REQUIRED_CHANNELS", "").split(",") if c.strip()]
# REQUIRED_CHANNEL_LINKS: comma-separated join links in same order (optional)
REQUIRED_CHANNEL_LINKS = [c.strip() for c in os.getenv("REQUIRED_CHANNEL_LINKS", "").split(",") if c.strip()]

ADMINS = {int(x.strip()) for x in os.getenv("ADMINS", "").split(",") if x.strip().isdigit()}
OWNER_HANDLE = os.getenv("OWNER_HANDLE", "@Obito_uchiha77").strip()

DB_PATH = os.getenv("DB_PATH", "bot.db")

# ---------- DB ----------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn

def init_db():
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            first_name TEXT,
            last_name TEXT,
            username TEXT,
            joined_ok INTEGER DEFAULT 0,
            total_participate INTEGER DEFAULT 0,
            win_record INTEGER DEFAULT 0,
            created_at TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS redeem_codes (
            code TEXT PRIMARY KEY,
            expires_at TEXT,           -- ISO
            max_uses INTEGER,
            uses INTEGER DEFAULT 0,
            created_by INTEGER,
            created_at TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS redemptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            code TEXT,
            redeemed_at TEXT,
            UNIQUE(user_id, code)
        )
    """)
    conn.commit()
    conn.close()

def upsert_user(u):
    conn = db()
    conn.execute("""
        INSERT INTO users(user_id, first_name, last_name, username, created_at)
        VALUES(?,?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET
            first_name=excluded.first_name,
            last_name=excluded.last_name,
            username=excluded.username
    """, (
        u.id,
        u.first_name or "",
        u.last_name or "",
        (u.username or ""),
        datetime.now(IST).isoformat()
    ))
    conn.commit()
    conn.close()

def set_joined_ok(user_id: int, ok: bool):
    conn = db()
    conn.execute("UPDATE users SET joined_ok=? WHERE user_id=?", (1 if ok else 0, user_id))
    conn.commit()
    conn.close()

def get_profile(user_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT first_name,last_name,username,total_participate,win_record FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return ("", "", "", 0, 0)
    return row

def all_user_ids():
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users")
    rows = cur.fetchall()
    conn.close()
    return [r[0] for r in rows]

# ---------- JOIN CHECK ----------
async def is_joined_all_channels(app: Application, user_id: int) -> bool:
    """
    Checks membership for all REQUIRED_CHANNELS via getChatMember.
    For channels, bot often must be admin in that channel.
    """
    if not REQUIRED_CHANNELS:
        return True

    for chat in REQUIRED_CHANNELS:
        try:
            member = await app.bot.get_chat_member(chat_id=chat, user_id=user_id)
            status = getattr(member, "status", None)
            # accepted statuses: member/admin/owner
            if status in ("left", "kicked", "banned") or status is None:
                return False
        except TelegramError as e:
            # If bot has no access (not admin / private channel), treat as NOT joined.
            log.warning("Join check failed for %s: %s", chat, e)
            return False
    return True

def join_keyboard():
    buttons = []
    # If links provided, use them; else use channel @username (works if public)
    for i, ch in enumerate(REQUIRED_CHANNELS):
        link = None
        if i < len(REQUIRED_CHANNEL_LINKS):
            link = REQUIRED_CHANNEL_LINKS[i]
        if not link:
            if isinstance(ch, str) and ch.startswith("@"):
                link = f"https://t.me/{ch[1:]}"
        if link:
            buttons.append([InlineKeyboardButton("➕ Join Channel", url=link)])
        else:
            buttons.append([InlineKeyboardButton(f"➕ Join: {ch}", callback_data="noop")])

    buttons.append([InlineKeyboardButton("✅ JOINED", callback_data="joined_check")])
    return InlineKeyboardMarkup(buttons)

def main_menu_kb(is_admin: bool):
    rows = [
        ["👤 MY PROFILE", "💳 Redeem Code"],
    ]
    if is_admin:
        rows.append(["🛠 ADMIN PANEL"])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

# ---------- MESSAGES ----------
def welcome_text(user):
    uname = f"@{user.username}" if user.username else user.first_name
    return (
        f"👋 <b>Welcome!</b> {uname}\n\n"
        f"Join all channels and click <b>JOINED</b>"
    )

def profile_text(user_id: int, user_obj):
    first, last, username, total_p, win_r = get_profile(user_id)
    u = user_obj
    return (
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "👤 <b>YOUR TELEGRAM PROFILE</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"▸ <b>First Name</b>  : {u.first_name or ''}\n"
        f"▸ <b>Last Name</b>   : {u.last_name or ''}\n"
        f"▸ <b>Telegram ID</b> : <code>{u.id}</code>\n"
        f"▸ <b>Username</b>    : @{u.username}\n" if u.username else
        (
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "👤 <b>YOUR TELEGRAM PROFILE</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"▸ <b>First Name</b>  : {u.first_name or ''}\n"
        f"▸ <b>Last Name</b>   : {u.last_name or ''}\n"
        f"▸ <b>Telegram ID</b> : <code>{u.id}</code>\n"
        f"▸ <b>Username</b>    : (none)\n"
        )
    ) + (
        "\n━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📊 <b>GIVEAWAY INFORMATION</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"▪ <b>Total Participate</b> : {int(total_p)}\n"
        f"▪ <b>Win Record</b>        : {int(win_r)}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "⚡ <b>MADARA GIVEAWAY BOT</b>\n"
    )

# ---------- CONVERSATIONS ----------
REDEEM_WAIT_CODE = 10
ADMIN_BROADCAST_WAIT = 20
ADMIN_CREATE_CODE_WAIT = 30

# ---------- HANDLERS ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    upsert_user(user)

    joined = await is_joined_all_channels(context.application, user.id)
    if not joined:
        await update.message.reply_text(
            welcome_text(user),
            parse_mode=ParseMode.HTML,
            reply_markup=join_keyboard()
        )
        return

    set_joined_ok(user.id, True)
    await update.message.reply_text(
        "✅ <b>Access Granted!</b>\nSelect an option:",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_kb(user.id in ADMINS)
    )

async def joined_check_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user = query.from_user
    upsert_user(user)

    joined = await is_joined_all_channels(context.application, user.id)
    if not joined:
        await query.message.reply_text("❌ Join all channels first!")
        return

    set_joined_ok(user.id, True)
    await query.message.reply_text(
        "✅ <b>Access Granted!</b>\nSelect an option:",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_kb(user.id in ADMINS)
    )

async def ensure_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Gate all features behind join-check."""
    user = update.effective_user
    upsert_user(user)
    joined = await is_joined_all_channels(context.application, user.id)
    if not joined:
        # Works for both normal messages and callbacks
        if update.message:
            await update.message.reply_text(
                welcome_text(user),
                parse_mode=ParseMode.HTML,
                reply_markup=join_keyboard()
            )
        elif update.callback_query:
            await update.callback_query.message.reply_text(
                welcome_text(user),
                parse_mode=ParseMode.HTML,
                reply_markup=join_keyboard()
            )
        return False
    set_joined_ok(user.id, True)
    return True

async def my_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_access(update, context):
        return
    user = update.effective_user
    await update.message.reply_text(
        profile_text(user.id, user),
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_kb(user.id in ADMINS)
    )

# ---------- REDEEM ----------
def normalize_code(s: str) -> str:
    s = s.strip()
    s = re.sub(r"\s+", "", s)
    return s.upper()

def redeem_lookup(code: str):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT code, expires_at, max_uses, uses FROM redeem_codes WHERE code=?", (code,))
    row = cur.fetchone()
    conn.close()
    return row

def redeem_increment_use(user_id: int, code: str) -> str:
    """
    Returns: "OK", "ALREADY", "LIMIT", "EXPIRED", "MISSING"
    """
    code = normalize_code(code)
    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT code, expires_at, max_uses, uses FROM redeem_codes WHERE code=?", (code,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return "MISSING"

    _, expires_at, max_uses, uses = row

    # expiry
    if expires_at:
        exp = datetime.fromisoformat(expires_at)
        if datetime.now(IST) > exp:
            conn.close()
            return "EXPIRED"

    # already redeemed by this user?
    cur.execute("SELECT 1 FROM redemptions WHERE user_id=? AND code=?", (user_id, code))
    if cur.fetchone():
        conn.close()
        return "ALREADY"

    # limit
    if max_uses is not None and int(uses) >= int(max_uses):
        conn.close()
        return "LIMIT"

    # write redemption + increment uses
    try:
        cur.execute(
            "INSERT INTO redemptions(user_id, code, redeemed_at) VALUES(?,?,?)",
            (user_id, code, datetime.now(IST).isoformat())
        )
        cur.execute("UPDATE redeem_codes SET uses = uses + 1 WHERE code=?", (code,))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return "ALREADY"

    conn.close()
    return "OK"

async def redeem_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_access(update, context):
        return ConversationHandler.END

    await update.message.reply_text("🎁 Please send your redeem code:")
    return REDEEM_WAIT_CODE

async def redeem_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_access(update, context):
        return ConversationHandler.END

    code = normalize_code(update.message.text or "")
    result = redeem_increment_use(update.effective_user.id, code)

    if result == "OK":
        await update.message.reply_text(
            f"CONGRATULATIONS 🎉 REDEEM SUCCESSFULLY ✅ CONTACT OUR OWNER FOR PRIZE ~ {OWNER_HANDLE}"
        )
    elif result == "LIMIT":
        await update.message.reply_text("❌ Redeem limit has been reached.")
    elif result == "EXPIRED":
        await update.message.reply_text("❌ Redeem code expired.")
    else:
        await update.message.reply_text("❌ Invalid redeem code.\nPlease try again.")

    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.", reply_markup=main_menu_kb(update.effective_user.id in ADMINS))
    return ConversationHandler.END

# ---------- ADMIN PANEL ----------
def admin_panel_text():
    return (
        "🛠 <b>ADMIN PANEL</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Choose an option:\n"
        "📣 BROADCAST\n"
        "🎫 CREATE REDEEM CODE\n"
    )

def admin_kb():
    return ReplyKeyboardMarkup(
        [["📣 BROADCAST", "🎫 CREATE REDEEM CODE"], ["⬅️ BACK"]],
        resize_keyboard=True
    )

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMINS:
        return
    if not await ensure_access(update, context):
        return
    await update.message.reply_text(admin_panel_text(), parse_mode=ParseMode.HTML, reply_markup=admin_kb())

async def admin_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Back to menu.", reply_markup=main_menu_kb(update.effective_user.id in ADMINS))

# Broadcast flow
async def admin_broadcast_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMINS:
        return ConversationHandler.END
    await update.message.reply_text("📣 Send the message you want to broadcast to all users:\n\n(Or /cancel)")
    return ADMIN_BROADCAST_WAIT

async def admin_broadcast_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMINS:
        return ConversationHandler.END

    msg = update.message
    user_ids = all_user_ids()
    sent = 0
    failed = 0

    for uid in user_ids:
        try:
            # forward style: copy
            await msg.copy(chat_id=uid)
            sent += 1
        except TelegramError:
            failed += 1

    await update.message.reply_text(f"✅ Broadcast done.\nSent: {sent}\nFailed: {failed}", reply_markup=admin_kb())
    return ConversationHandler.END

# Create redeem flow
async def admin_create_code_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMINS:
        return ConversationHandler.END

    await update.message.reply_text(
        "🎫 Send redeem code details like this:\n\n"
        "<code>CODE MAX_USERS VALID_MINUTES</code>\n\n"
        "Example:\n<code>MADARA50 50 1440</code>\n\n"
        "(VALID_MINUTES = 0 means no expiry)\n\nOr /cancel",
        parse_mode=ParseMode.HTML
    )
    return ADMIN_CREATE_CODE_WAIT

async def admin_create_code_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMINS:
        return ConversationHandler.END

    text = (update.message.text or "").strip()
    parts = text.split()
    if len(parts) != 3:
        await update.message.reply_text("❌ Format wrong. Use: CODE MAX_USERS VALID_MINUTES")
        return ADMIN_CREATE_CODE_WAIT

    code = normalize_code(parts[0])
    try:
        max_users = int(parts[1])
        valid_minutes = int(parts[2])
        if max_users <= 0:
            raise ValueError
        if valid_minutes < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ MAX_USERS must be > 0 and VALID_MINUTES must be >= 0.")
        return ADMIN_CREATE_CODE_WAIT

    expires_at = None
    if valid_minutes > 0:
        expires_at = (datetime.now(IST) + timedelta(minutes=valid_minutes)).isoformat()

    conn = db()
    try:
        conn.execute("""
            INSERT INTO redeem_codes(code, expires_at, max_uses, uses, created_by, created_at)
            VALUES(?,?,?,?,?,?)
        """, (
            code,
            expires_at,
            max_users,
            0,
            update.effective_user.id,
            datetime.now(IST).isoformat()
        ))
        conn.commit()
    except sqlite3.IntegrityError:
        await update.message.reply_text("❌ Code already exists. Try a new code name.")
        conn.close()
        return ADMIN_CREATE_CODE_WAIT

    conn.close()

    exp_txt = expires_at if expires_at else "No expiry"
    await update.message.reply_text(
        f"✅ Redeem code created!\n\n"
        f"Code: <code>{code}</code>\n"
        f"Max users: {max_users}\n"
        f"Expiry: {exp_txt}",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_kb()
    )
    return ConversationHandler.END

# ---------- ROUTER ----------
async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ✅ SAFETY CHECK (MOST IMPORTANT)
    if update.message is None or update.message.text is None:
        return

    t = update.message.text.strip()

    if t == "👤 MY PROFILE":
        return await my_profile(update, context)

    if t == "💳 Redeem Code":
        return await redeem_entry(update, context)

    if t == "🛠 ADMIN PANEL":
        return await admin_panel(update, context)

    if t == "📣 BROADCAST":
        return await admin_broadcast_entry(update, context)

    if t == "🎫 CREATE REDEEM CODE":
        return await admin_create_code_entry(update, context)

    if t == "⬅️ BACK":
        return await admin_back(update, context)

    # 🔁 Default fallback
    if not await ensure_access(update, context):
        return

    await update.message.reply_text(
        "Choose an option from menu ✅",
        reply_markup=main_menu_kb(update.effective_user.id in ADMINS)
            )

def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(joined_check_cb, pattern="^joined_check$"))

    redeem_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"^💳 Redeem Code$"), redeem_entry)],
        states={REDEEM_WAIT_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, redeem_receive)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(redeem_conv)

    admin_broadcast_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"^📣 BROADCAST$"), admin_broadcast_entry)],
        states={ADMIN_BROADCAST_WAIT: [MessageHandler(filters.ALL & ~filters.COMMAND, admin_broadcast_send)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(admin_broadcast_conv)

    admin_create_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"^🎫 CREATE REDEEM CODE$"), admin_create_code_entry)],
        states={ADMIN_CREATE_CODE_WAIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_create_code_save)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(admin_create_conv)

    app.add_handler(MessageHandler(filters.Regex(r"^👤 MY PROFILE$"), my_profile))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    return app

def main():
    init_db()
    app = build_app()
    # Long polling (good for Render background service/web service)
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
