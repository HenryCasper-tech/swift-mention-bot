"""
Telegram 'Mention All' Bot
--------------------------
Triggers: Any message (to log users) + @everyone command (admins only)
Database: SQLite (auto-created on first run)
"""

import os
import logging
import sqlite3
import asyncio
from telegram import Update, ChatMember
from telegram.ext import (
    Application,
    MessageHandler,
    CommandHandler,
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
BATCH_SIZE: int = 8          # users per mention message
BATCH_DELAY: float = 1.5     # seconds between batches (anti-flood)

if not BOT_TOKEN:
    raise EnvironmentError(
        "TELEGRAM_BOT_TOKEN environment variable is not set.\n"
        "Run:  export TELEGRAM_BOT_TOKEN='your_token_here'"
    )


# ── Database helpers ────────────────────────────────────────────────────────────

def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def db_init() -> None:
    """Create the members table if it does not exist."""
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


def db_get_members(chat_id: int) -> list[sqlite3.Row]:
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT user_id, username FROM members WHERE chat_id = ?",
            (chat_id,),
        ).fetchall()
    return rows


def db_remove_member(chat_id: int, user_id: int) -> None:
    with db_connect() as conn:
        conn.execute(
            "DELETE FROM members WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        )
        conn.commit()


# ── Permission helper ───────────────────────────────────────────────────────────

async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Return True if the message sender is a group admin or creator."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    try:
        member: ChatMember = await context.bot.get_chat_member(chat_id, user_id)
        return member.status in (ChatMember.ADMINISTRATOR, ChatMember.OWNER)
    except Exception as exc:
        logger.warning("Could not check admin status: %s", exc)
        return False


# ── Handlers ────────────────────────────────────────────────────────────────────

async def track_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Fires on every group message.
    Saves the sender's user_id + username to the DB.
    Also checks whether the message contains '@everyone' and, if so,
    delegates to mention_all (admin check included).
    """
    if not update.message or not update.effective_user:
        return

    user = update.effective_user
    chat = update.effective_chat

    # Only track in group / supergroup chats
    if chat.type not in ("group", "supergroup"):
        return

    # Persist member
    db_upsert_member(chat.id, user.id, user.username)
    logger.debug("Tracked %s (id=%s) in chat %s", user.username, user.id, chat.id)

    # Trigger mention-all if the message contains @everyone
    text = update.message.text or ""
    if "@everyone" in text.lower():
        await mention_all(update, context)


async def mention_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Mention every stored member of the group.
    Admin-only. Sends in batches to stay within Telegram rate limits.
    """
    if not await is_admin(update, context):
        await update.message.reply_text(
            "⛔ Only group administrators can use @everyone."
        )
        return

    chat_id = update.effective_chat.id
    members = db_get_members(chat_id)

    if not members:
        await update.message.reply_text(
            "📭 No members in the database yet.\n"
            "Members are added automatically as they send messages. "
            "Use /sync to nudge everyone."
        )
        return

    # Build mention strings
    # Prefer @username; fall back to inline mention via user_id
    mentions: list[str] = []
    for row in members:
        if row["username"]:
            mentions.append(f"@{row['username']}")
        else:
            # Inline mention works even without a username
            mentions.append(
                f"[​\u200b](tg://user?id={row['user_id']})"
            )

    total = len(mentions)
    await update.message.reply_text(
        f"📣 Mentioning {total} member(s) in batches of {BATCH_SIZE}…"
    )

    # Send in batches
    for i in range(0, total, BATCH_SIZE):
        batch = mentions[i : i + BATCH_SIZE]
        text = " ".join(batch)
        await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
        )
        if i + BATCH_SIZE < total:          # don't sleep after the last batch
            await asyncio.sleep(BATCH_DELAY)

    logger.info(
        "mention_all completed for chat %s: %d members, %d batches",
        chat_id,
        total,
        -(-total // BATCH_SIZE),            # ceiling division
    )


async def sync_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /sync — Admins use this to ask all members to send a message
    so the bot can record them.
    """
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
    """/stats — Show how many members are tracked in this chat."""
    chat_id = update.effective_chat.id
    members = db_get_members(chat_id)
    await update.message.reply_text(
        f"📊 <b>Members tracked in this group:</b> {len(members)}",
        parse_mode=ParseMode.HTML,
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start — Welcome message (works in private chat too)."""
    await update.message.reply_text(
        "👋 <b>Mention-All Bot is active!</b>\n\n"
        "<b>Commands:</b>\n"
        "• Type <code>@everyone</code> in a group to mention all tracked members "
        "(admins only).\n"
        "• /sync — Ask members to send a message so the bot can track them.\n"
        "• /stats — Show the number of tracked members.\n\n"
        "<i>The bot silently tracks every member who sends a message.</i>",
        parse_mode=ParseMode.HTML,
    )


# ── Main ────────────────────────────────────────────────────────────────────────

def main() -> None:
    db_init()

    app = Application.builder().token(BOT_TOKEN).build()

    # Command handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("sync", sync_command))
    app.add_handler(CommandHandler("stats", stats_command))

    # Track every text message in groups (also detects @everyone)
    app.add_handler(
        MessageHandler(
            filters.TEXT & filters.ChatType.GROUPS & ~filters.COMMAND,
            track_member,
        )
    )

    logger.info("Bot is polling…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()