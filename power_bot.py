# power_bot.py
import os
import re
import sqlite3
import logging
from datetime import datetime, timedelta
from collections import defaultdict, deque

from telegram import Update, ChatPermissions
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
)

# -------------------
# Config from ENV
# -------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
ALLOWED_CHAT_IDS = [int(x) for x in os.getenv("ALLOWED_CHAT_IDS", "").split(",") if x.strip()]
DB_PATH = os.getenv("DB_PATH", "bot_data.sqlite3")
SPAM_MAX = int(os.getenv("SPAM_MAX_MSG_PER_MIN", "20"))  # messages per minute before warn

if not BOT_TOKEN or OWNER_ID == 0:
    raise RuntimeError("Please set BOT_TOKEN and OWNER_ID environment variables")

# -------------------
# Logging
# -------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# -------------------
# Database (SQLite)
# -------------------
def init_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS bans (
        user_id INTEGER PRIMARY KEY,
        reason TEXT,
        banned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS warns (
        user_id INTEGER,
        count INTEGER,
        last_warn TIMESTAMP,
        PRIMARY KEY(user_id)
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS links (
        link TEXT PRIMARY KEY,
        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.commit()
    return conn

DB = init_db()
CUR = DB.cursor()

# -------------------
# Helpers
# -------------------
def add_allowed_link(link: str):
    CUR.execute("INSERT OR IGNORE INTO links(link) VALUES(?)", (link,))
    DB.commit()

def remove_allowed_link(link: str):
    CUR.execute("DELETE FROM links WHERE link=?", (link,))
    DB.commit()

def list_allowed_links():
    CUR.execute("SELECT link FROM links")
    return [r[0] for r in CUR.fetchall()]

def ban_user_db(user_id: int, reason: str=""):
    CUR.execute("INSERT OR REPLACE INTO bans(user_id, reason) VALUES(?,?)", (user_id, reason))
    DB.commit()

def unban_user_db(user_id: int):
    CUR.execute("DELETE FROM bans WHERE user_id=?", (user_id,))
    DB.commit()

def is_banned_db(user_id: int) -> bool:
    CUR.execute("SELECT 1 FROM bans WHERE user_id=?", (user_id,))
    return CUR.fetchone() is not None

def warn_user_db(user_id: int):
    CUR.execute("SELECT count FROM warns WHERE user_id=?", (user_id,))
    row = CUR.fetchone()
    if row:
        cnt = row[0] + 1
        CUR.execute("UPDATE warns SET count=?, last_warn=CURRENT_TIMESTAMP WHERE user_id=?", (cnt, user_id))
    else:
        cnt = 1
        CUR.execute("INSERT INTO warns(user_id, count, last_warn) VALUES(?,?,CURRENT_TIMESTAMP)", (user_id, cnt))
    DB.commit()
    return cnt

def reset_warns_db(user_id: int):
    CUR.execute("DELETE FROM warns WHERE user_id=?", (user_id,))
    DB.commit()

# -------------------
# Spam tracker (in-memory sliding window per user)
# -------------------
# Will reset on restart; DB handles permanent bans/warns.
user_msg_times = defaultdict(lambda: deque())  # user_id -> deque of datetime

def add_message_time(user_id: int):
    now = datetime.utcnow()
    dq = user_msg_times[user_id]
    dq.append(now)
    # remove older than 60 seconds
    cutoff = now - timedelta(seconds=60)
    while dq and dq[0] < cutoff:
        dq.popleft()
    return len(dq)

# -------------------
# Utility: resolve username to id (tries get_chat)
# -------------------
async def resolve_username_to_id(app, username: str):
    """Try get_chat('@username') to obtain id; returns int id or None"""
    if username.startswith("@"):
        username = username[1:]
    try:
        chat = await app.bot.get_chat(f"@{username}")
        return chat.id
    except Exception as e:
        logger.debug(f"resolve fail for @{username}: {e}")
        return None

# -------------------
# Filters
# -------------------
LINK_REGEX = re.compile(r"https?://\S+")
# allow if message contains any allowed link prefix
def contains_allowed_link(text: str):
    links = list_allowed_links()
    if not links:
        return False
    txt = text.lower()
    for l in links:
        if l and l.lower() in txt:
            return True
    return False

# -------------------
# Command handlers (Owner-only)
# -------------------
async def owner_only(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ আপনি মালিক নন।")
        return False
    return True

async def cmd_addlink(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only(update, context): return
    if not context.args:
        await update.message.reply_text("ব্যবহার: /addlink https://example.com/path")
        return
    link = context.args[0].strip()
    add_allowed_link(link)
    await update.message.reply_text(f"✅ Added allowed link: {link}")

async def cmd_removelink(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only(update, context): return
    if not context.args:
        await update.message.reply_text("ব্যবহার: /removelink https://example.com/path")
        return
    link = context.args[0].strip()
    remove_allowed_link(link)
    await update.message.reply_text(f"❌ Removed allowed link: {link}")

async def cmd_listlinks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only(update, context): return
    links = list_allowed_links()
    if not links:
        await update.message.reply_text("No allowed links set.")
        return
    await update.message.reply_text("Allowed links:\n" + "\n".join(links))

async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only(update, context): return
    if not context.args:
        await update.message.reply_text("Usage: /ban <user_id or @username>")
        return
    target = context.args[0]
    target_id = None
    if target.isdigit():
        target_id = int(target)
    else:
        target_id = await resolve_username_to_id(context.application, target)
    if not target_id:
        await update.message.reply_text("User not found.")
        return
    ban_user_db(target_id, reason=f"manual by owner {update.effective_user.id}")
    try:
        await context.bot.ban_chat_member(update.effective_chat.id, target_id)
    except Exception:
        pass
    await update.message.reply_text(f"✅ Banned {target_id}")

async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only(update, context): return
    if not context.args:
        await update.message.reply_text("Usage: /unban <user_id or @username>")
        return
    target = context.args[0]
    target_id = None
    if target.isdigit():
        target_id = int(target)
    else:
        target_id = await resolve_username_to_id(context.application, target)
    if not target_id:
        await update.message.reply_text("User not found.")
        return
    unban_user_db(target_id)
    try:
        await context.bot.unban_chat_member(update.effective_chat.id, target_id)
    except Exception:
        pass
    await update.message.reply_text(f"✅ Unbanned {target_id}")

async def cmd_warn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only(update, context): return
    if not context.args:
        await update.message.reply_text("Usage: /warn <user_id or @username>")
        return
    target = context.args[0]
    target_id = None
    if target.isdigit():
        target_id = int(target)
    else:
        target_id = await resolve_username_to_id(context.application, target)
    if not target_id:
        await update.message.reply_text("User not found.")
        return
    cnt = warn_user_db(target_id)
    await update.message.reply_text(f"⚠️ Warned {target_id} — total warns: {cnt}")

async def cmd_mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only(update, context): return
    if not context.args:
        await update.message.reply_text("Usage: /mute <user_id or @username> [minutes]")
        return
    target = context.args[0]
    mins = int(context.args[1]) if len(context.args) > 1 and context.args[1].isdigit() else 10
    target_id = None
    if target.isdigit():
        target_id = int(target)
    else:
        target_id = await resolve_username_to_id(context.application, target)
    if not target_id:
        await update.message.reply_text("User not found.")
        return
    try:
        until = datetime.utcnow() + timedelta(minutes=mins)
        await context.bot.restrict_chat_member(
            update.effective_chat.id, target_id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until
        )
        await update.message.reply_text(f"🔇 Muted {target_id} for {mins} minutes")
    except Exception as e:
        await update.message.reply_text(f"Failed to mute: {e}")

async def cmd_unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only(update, context): return
    if not context.args:
        await update.message.reply_text("Usage: /unmute <user_id or @username>")
        return
    target = context.args[0]
    target_id = None
    if target.isdigit():
        target_id = int(target)
    else:
        target_id = await resolve_username_to_id(context.application, target)
    if not target_id:
        await update.message.reply_text("User not found.")
        return
    try:
        await context.bot.restrict_chat_member(
            update.effective_chat.id, target_id,
            permissions=ChatPermissions(can_send_messages=True,
                                        can_send_media_messages=True,
                                        can_send_polls=True,
                                        can_send_other_messages=True,
                                        can_add_web_page_previews=True)
        )
        await update.message.reply_text(f"🔊 Unmuted {target_id}")
    except Exception as e:
        await update.message.reply_text(f"Failed to unmute: {e}")

# -------------------
# Message Handler: anti-link, anti-spam, welcome
# -------------------
WELCOME_TEXT = "স্বাগতম {name} — গ্রুপে স্বাগতম! অনুগ্রহ করে নিয়ম মেনে চলুন।"

async def new_member_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for m in update.message.new_chat_members or []:
        if is_banned_db(m.id):
            try:
                await context.bot.ban_chat_member(update.effective_chat.id, m.id)
                await context.bot.unban_chat_member(update.effective_chat.id, m.id)
            except:
                pass
        else:
            await update.message.reply_text(WELCOME_TEXT.format(name=m.first_name or m.full_name))

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return
    chat_id = msg.chat_id
    # allowed chat filter
    if ALLOWED_CHAT_IDS and chat_id not in ALLOWED_CHAT_IDS:
        return

    user = msg.from_user
    # owner bypass
    if user.id == OWNER_ID:
        return

    # anti-spam
    count = add_message_time(user.id)
    if count > SPAM_MAX:
        # warn and possibly kick on repeated violations
        warns = warn_user_db(user.id)
        await msg.reply_text(f"⚠️ আপনি অনেক বার মেসেজ পাঠাচ্ছেন — warn #{warns}")
        if warns >= 3:
            try:
                await context.bot.ban_chat_member(chat_id, user.id)
                ban_user_db(user.id, reason="spam")
                await msg.reply_text(f"❌ {user.full_name} কে স্প্যামের কারণে ব্যান করা হয়েছে।")
            except Exception:
                await msg.reply_text("Failed to ban (missing permissions).")
        return

    text = msg.text.lower()
    # detect links
    found_links = LINK_REGEX.findall(text)
    if found_links:
        # if any allowed link substring present => ok
        if contains_allowed_link(text):
            return
        # else delete and ban/kick + PM
        try:
            await context.bot.delete_message(chat_id, msg.message_id)
        except:
            pass
        # kick and add to ban DB
        try:
            await context.bot.ban_chat_member(chat_id, user.id)
            ban_user_db(user.id, reason="posted_disallowed_link")
        except Exception:
            await msg.reply_text("I need ban rights to remove offenders.")
            return
        # PM user
        try:
            await context.bot.send_message(user.id,
                f"⚠️ আপনি '{msg.chat.title}' গ্রুপ থেকে সরানো হয়েছেন — অনুমোদিত লিংক ছাড়া লিংক শেয়ার করার জন্য।"
            )
        except:
            pass
        await msg.reply_text(f"🚫 {user.full_name} কে অনুমোদনবিহীন লিংক শেয়ারের জন্য সরানো হয়েছে.")
        return

    # other automations (you can extend)
    # e.g., auto-reply simple FAQs
    if any(w in text for w in ["বান", "remove", "ban"]):
        await msg.reply_text("আপনি যদি কোন ইউজারকে ব্যান করতে চান, Owner কে tag করে বলুন।")

# -------------------
# Startup
# -------------------
async def start_app():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # owner commands
    app.add_handler(CommandHandler("addlink", cmd_addlink))
    app.add_handler(CommandHandler("removelink", cmd_removelink))
    app.add_handler(CommandHandler("listlinks", cmd_listlinks))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("unban", cmd_unban))
    app.add_handler(CommandHandler("warn", cmd_warn))
    app.add_handler(CommandHandler("mute", cmd_mute))
    app.add_handler(CommandHandler("unmute", cmd_unmute))

    # moderation username commands (owner)
    app.add_handler(CommandHandler("kick", cmd_ban))  # reuse ban handler for simplicity

    # member events
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, new_member_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    logger.info("Starting bot...")
    await app.run_polling()

if __name__ == "__main__":
    import asyncio
    try:
        asyncio.run(start_app())
    except Exception as e:
        logger.exception("Bot crashed:", exc_info=e)
