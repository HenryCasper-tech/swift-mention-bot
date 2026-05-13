"""
Telegram 'Mention All' Bot — v3
--------------------------------
New in v3:
  • Auto-removes members when they LEAVE the group
  • /everyone command as alternative trigger to @everyone
  • 5-minute cooldown on @everyone / /everyone to prevent spam
"""

import os
import logging
import sqlite3
import asyncio
import time
from telegram import Update, ChatMember
from telegram.ext import (
    Application,
    MessageHandler,
    CommandHandler,
    ChatMemberHandler,
    filters,
    ContextTypes,
)
from telegram.constants import ParseMode

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
DB_PATH: str = "members.db"
BATCH_SIZE: int = 8
BATCH_DELAY: float = 1.5
COOLDOWN_SECONDS: int = 300  # 5 minutes

if not BOT_TOKEN:
    raise EnvironmentError(
        "TELEGRAM_BOT_TOKEN environment variable is not set.\n"
        "Run:  export TELEGRAM_BOT_TOKEN='your_token_here'"
    )

# ── Cooldown tracker (in-memory) ───────────────────────────────────────────────
cooldown_tracker: dict[int, float] = {}


# ── Database helpers ────────────────────────────────────────────────────────────

def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def db_init() -> None:
    with db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS members (
                chat_id   INTEGER NOT NULL,
                user_id   INTEGER NOT NULL,
                username  TEXT,
                PRIMARY KEY (chat_id, user_id)
            )
            """
        )
        conn.commit()
    logger.info("Database initialised at %s", DB_PATH)


def db_upsert_member(chat_id: int, user_id: int, username: str | None) -> None:
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO members (chat_id, user_id, username)
            VALUES (?, ?, ?)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET username = excluded.username
            """,
            (chat_id, user_id, username),
        )
        conn.commit()


def db_upsert_by_username(chat_id: int, username: str) -> None:
    username = username.lstrip("@").lower()
    with db_connect() as conn:
        existing = conn.execute(
            "SELECT user_id FROM members WHERE chat_id = ? AND LOWER(username) = ?",
            (chat_id, username),
        ).fetchone()
        if not existing:
            fake_id = conn.execute(
                "SELECT MIN(user_id) - 1 FROM members WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()[0] or -1
            if fake_id > 0:
                fake_id = -1
            conn.execute(
                "INSERT INTO members (chat_id, user_id, username) VALUES (?, ?, ?)",
                (chat_id, fake_id, username),
            )
            conn.commit()


def db_remove_member(chat_id: int, user_id: int) -> None:
    with db_connect() as conn:
        conn.execute(
            "DELETE FROM members WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        )
        conn.commit()


def db_remove_by_username(chat_id: int, username: str) -> bool:
    username = username.lstrip("@").lower()
    with db_connect() as conn:
        cursor = conn.execute(
            "DELETE FROM members WHERE chat_id = ? AND LOWER(username) = ?",
            (chat_id, username),
        )
        conn.commit()
        return cursor.rowcount > 0


def db_get_members(chat_id: int) -> list[sqlite3.Row]:
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT user_id, username FROM members WHERE chat_id = ?",
            (chat_id,),
        ).fetchall()
    return rows


# ── Cooldown helper ─────────────────────────────────────────────────────────────

def check_cooldown(chat_id: int) -> int:
    last = cooldown_tracker.get(chat_id, 0)
    elapsed = time.time() - last
    remaining = COOLDOWN_SECONDS - elapsed
    return max(0, int(remaining))


def set_cooldown(chat_id: int) -> None:
    cooldown_tracker[chat_id] = time.time()


# ── Permission helper ───────────────────────────────────────────────────────────

async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    try:
        member: ChatMember = await context.bot.get_chat_member(chat_id, user_id)
        return member.status in (ChatMember.ADMINISTRATOR, ChatMember.OWNER)
    except Exception as exc:
        logger.warning("Could not check admin status: %s", exc)
        return False


# ── Core mention logic ──────────────────────────────────────────────────────────

async def mention_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Core mention-all logic shared by @everyone text and /everyone command."""
    if not await is_admin(update, context):
        await update.message.reply_text(
            "⛔ Only group administrators can use @everyone."
        )
        return

    chat_id = update.effective_chat.id

    # Cooldown check
    remaining = check_cooldown(chat_id)
    if remaining > 0:
        minutes = remaining // 60
        seconds = remaining % 60
        await update.message.reply_text(
            f"⏳ Please wait <b>{minutes}m {seconds}s</b> before using @everyone again.",
            parse_mode=ParseMode.HTML,
        )
        return

    members = db_get_members(chat_id)

    if not members:
        await update.message.reply_text(
            "📭 No members in the database yet.\n"
            "Members are added automatically as they send messages or join. "
            "Use /sync to nudge everyone, or /add @username to add manually."
        )
        return

    set_cooldown(chat_id)

    mentions: list[str] = []
    for row in members:
        if row["username"]:
            mentions.append(f"@{row['username']}")
        else:
            mentions.append(f"[​\u200b](tg://user?id={row['user_id']})")

    total = len(mentions)
    await update.message.reply_text(
        f"📣 Mentioning {total} member(s) in batches of {BATCH_SIZE}…"
    )

    for i in range(0, total, BATCH_SIZE):
        batch = mentions[i : i + BATCH_SIZE]
        text = " ".join(batch)
        await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
        )
        if i + BATCH_SIZE < total:
            await asyncio.sleep(BATCH_DELAY)

    logger.info(
        "mention_all completed for chat %s: %d members, %d batches",
        chat_id, total, -(-total // BATCH_SIZE),
    )


# ── Handlers ────────────────────────────────────────────────────────────────────

async def track_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return

    user = update.effective_user
    chat = update.effective_chat

    if chat.type not in ("group", "supergroup"):
        return

    db_upsert_member(chat.id, user.id, user.username)
    logger.debug("Tracked %s (id=%s) in chat %s", user.username, user.id, chat.id)

    text = update.message.text or ""
    if "@everyone" in text.lower():
        await mention_all(update, context)


async def track_join_leave(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Tracks members joining AND leaving the group."""
    result = update.chat_member
    if not result:
        return

    chat_id = result.chat.id
    user = result.new_chat_member.user
    new_status = result.new_chat_member.status
    old_status = result.old_chat_member.status

    # Member joined
    if new_status in (ChatMember.MEMBER, ChatMember.ADMINISTRATOR, ChatMember.OWNER):
        if old_status in (ChatMember.LEFT, ChatMember.BANNED, "kicked"):
            db_upsert_member(chat_id, user.id, user.username)
            logger.info(
                "New member joined: %s (id=%s) in chat %s",
                user.username, user.id, chat_id,
            )

    # ✨ NEW — Member left or was kicked/banned → auto-remove
    elif new_status in (ChatMember.LEFT, ChatMember.BANNED, "kicked"):
        if old_status in (ChatMember.MEMBER, ChatMember.ADMINISTRATOR, ChatMember.OWNER):
            db_remove_member(chat_id, user.id)
            logger.info(
                "Member removed (left/kicked): %s (id=%s) in chat %s",
                user.username, user.id, chat_id,
            )


async def everyone_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """✨ NEW — /everyone as alternative to typing @everyone."""
    await mention_all(update, context)


async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_admin(update, context):
        await update.message.reply_text("⛔ Only admins can use /add.")
        return

    if not context.args:
        await update.message.reply_text(
            "ℹ️ Usage: /add @username1 @username2\nExample: /add @john @jane"
        )
        return

    chat_id = update.effective_chat.id
    added, skipped = [], []

    for arg in context.args:
        if arg.startswith("@"):
            db_upsert_by_username(chat_id, arg)
            added.append(arg)
        else:
            skipped.append(arg)

    response = ""
    if added:
        response += f"✅ Added: {', '.join(added)}\n"
    if skipped:
        response += f"⚠️ Skipped (no @ prefix): {', '.join(skipped)}"
    await update.message.reply_text(response.strip())


async def remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_admin(update, context):
        await update.message.reply_text("⛔ Only admins can use /remove.")
        return

    if not context.args:
        await update.message.reply_text(
            "ℹ️ Usage: /remove @username1 @username2\nExample: /remove @john"
        )
        return

    chat_id = update.effective_chat.id
    removed, not_found = [], []

    for arg in context.args:
        username = arg.lstrip("@")
        if db_remove_by_username(chat_id, username):
            removed.append(f"@{username}")
        else:
            not_found.append(f"@{username}")

    response = ""
    if removed:
        response += f"✅ Removed: {', '.join(removed)}\n"
    if not_found:
        response += f"⚠️ Not found: {', '.join(not_found)}"
    await update.message.reply_text(response.strip())


async def sync_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_admin(update, context):
        await update.message.reply_text("⛔ Only admins can use /sync.")
        return

    await update.message.reply_text(
        "🔄 <b>Sync requested!</b>\n\n"
        "Hey everyone — please send <i>any</i> message so the bot can see you "
        "and add you to the mention list. 👋",
        parse_mode=ParseMode.HTML,
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    members = db_get_members(chat_id)
    await update.message.reply_text(
        f"📊 <b>Members tracked in this group:</b> {len(members)}",
        parse_mode=ParseMode.HTML,
    )


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_admin(update, context):
        await update.message.reply_text("⛔ Only admins can use /list.")
        return

    chat_id = update.effective_chat.id
    members = db_get_members(chat_id)

    if not members:
        await update.message.reply_text("📭 No members tracked yet.")
        return

    lines = []
    for row in members:
        if row["username"]:
            lines.append(f"• @{row['username']}")
        else:
            lines.append(f"• [no username] (id: {row['user_id']})")

    text = f"📋 <b>Tracked members ({len(members)}):</b>\n" + "\n".join(lines)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 <b>Mention-All Bot v3 is active!</b>\n\n"
        "<b>Trigger mention:</b>\n"
        "• Type <code>@everyone</code> in chat\n"
        "• Or use /everyone command\n"
        "• ⏳ 5 minute cooldown between mentions\n\n"
        "<b>Admin commands:</b>\n"
        "• /everyone — mention all members\n"
        "• /add @user1 @user2 — manually add members\n"
        "• /remove @user1 — remove a member\n"
        "• /list — show all tracked members\n"
        "• /sync — ask everyone to send a message\n"
        "• /stats — show member count\n\n"
        "<i>Auto-tracks members who join or message. "
        "Auto-removes members who leave.</i>",
        parse_mode=ParseMode.HTML,
    )


# ── Main ────────────────────────────────────────────────────────────────────────

def main() -> None:
    db_init()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("sync", sync_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("list", list_command))
    app.add_handler(CommandHandler("add", add_command))
    app.add_handler(CommandHandler("remove", remove_command))
    app.add_handler(CommandHandler("everyone", everyone_command))  # ✨ NEW

    app.add_handler(
        MessageHandler(
            filters.TEXT & filters.ChatType.GROUPS & ~filters.COMMAND,
            track_member,
        )
    )

    app.add_handler(
        ChatMemberHandler(track_join_leave, ChatMemberHandler.CHAT_MEMBER)
    )

    logger.info("Bot v3 is polling…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
