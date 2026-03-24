"""
Telegram 'Mention All' Bot — v2
--------------------------------
New in v2:
  • Auto-tracks members when they JOIN the group (not just when they message)
  • /add @user1 @user2 … — admins can manually add usernames
  • /remove @user1 @user2 … — admins can manually remove usernames
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


def db_upsert_by_username(chat_id: int, username: str) -> None:
    """Insert a username-only record (no user_id known). Uses negative fake ID."""
    username = username.lstrip("@").lower()
    with db_connect() as conn:
        # Check if username already exists
        existing = conn.execute(
            "SELECT user_id FROM members WHERE chat_id = ? AND LOWER(username) = ?",
            (chat_id, username),
        ).fetchone()
        if not existing:
            # Use a large negative number as a placeholder user_id
            # to avoid collisions with real Telegram IDs
            fake_id = conn.execute(
                "SELECT MIN(user_id) - 1 FROM members WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()[0] or -1
            if fake_id > 0:
                fake_id = -1
            conn.execute(
                """
                INSERT INTO members (chat_id, user_id, username)
                VALUES (?, ?, ?)
                """,
                (chat_id, fake_id, username),
            )
            conn.commit()


def db_remove_by_username(chat_id: int, username: str) -> bool:
    """Remove a member by username. Returns True if a row was deleted."""
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


async def track_join(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    ✨ NEW — Fires when a member's status changes in the chat.
    Catches new members joining so we log them immediately,
    even before they send a message.
    """
    result = update.chat_member
    if not result:
        return

    chat_id = result.chat.id
    new_status = result.new_chat_member.status
    user = result.new_chat_member.user

    # Only care about members who just joined
    if new_status in (ChatMember.MEMBER, ChatMember.ADMINISTRATOR, ChatMember.OWNER):
        old_status = result.old_chat_member.status
        # Make sure they weren't already a member (e.g. role change)
        if old_status in (ChatMember.LEFT, ChatMember.BANNED, "kicked"):
            db_upsert_member(chat_id, user.id, user.username)
            logger.info(
                "New member joined: %s (id=%s) in chat %s",
                user.username, user.id, chat_id,
            )


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
            "Members are added automatically as they send messages or join. "
            "Use /sync to nudge everyone, or /add @username to add manually."
        )
        return

    # Build mention strings
    mentions: list[str] = []
    for row in members:
        if row["username"]:
            mentions.append(f"@{row['username']}")
        else:
            # Inline mention for users without a username
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
        if i + BATCH_SIZE < total:
            await asyncio.sleep(BATCH_DELAY)

    logger.info(
        "mention_all completed for chat %s: %d members, %d batches",
        chat_id,
        total,
        -(-total // BATCH_SIZE),
    )


async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    ✨ NEW — /add @user1 @user2 …
    Admins can manually add usernames to the mention list.
    """
    if not await is_admin(update, context):
        await update.message.reply_text("⛔ Only admins can use /add.")
        return

    if not context.args:
        await update.message.reply_text(
            "ℹ️ Usage: /add @username1 @username2 …\n"
            "Example: /add @john @jane"
        )
        return

    chat_id = update.effective_chat.id
    added = []
    skipped = []

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
    """
    /remove @user1 @user2 …
    Admins can manually remove usernames from the mention list.
    """
    if not await is_admin(update, context):
        await update.message.reply_text("⛔ Only admins can use /remove.")
        return

    if not context.args:
        await update.message.reply_text(
            "ℹ️ Usage: /remove @username1 @username2 …\n"
            "Example: /remove @john @jane"
        )
        return

    chat_id = update.effective_chat.id
    removed = []
    not_found = []

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
    """/sync — Admins use this to ask all members to send a message."""
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


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/list — Show all tracked usernames in this chat."""
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
    """/start — Welcome message."""
    await update.message.reply_text(
        "👋 <b>Mention-All Bot v2 is active!</b>\n\n"
        "<b>Commands:</b>\n"
        "• Type <code>@everyone</code> — mention all tracked members (admins only)\n"
        "• /add @user1 @user2 — manually add members (admins only)\n"
        "• /remove @user1 — remove a member (admins only)\n"
        "• /list — show all tracked members (admins only)\n"
        "• /sync — ask everyone to send a message\n"
        "• /stats — show member count\n\n"
        "<i>The bot auto-tracks members when they join or send a message.</i>",
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
    app.add_handler(CommandHandler("list", list_command))
    app.add_handler(CommandHandler("add", add_command))        # ✨ NEW
    app.add_handler(CommandHandler("remove", remove_command))  # ✨ NEW

    # Track every text message in groups (also detects @everyone)
    app.add_handler(
        MessageHandler(
            filters.TEXT & filters.ChatType.GROUPS & ~filters.COMMAND,
            track_member,
        )
    )

    # ✨ NEW — Track members when they join the group
    app.add_handler(
        ChatMemberHandler(track_join, ChatMemberHandler.CHAT_MEMBER)
    )

    logger.info("Bot v2 is polling…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
