"""Telegram front end for Chad.

Responsibilities:
  1. Accept messages from Telegram.
  2. Reject any sender who isn't the allowed user (silently — don't
     even acknowledge the message exists).
  3. Forward allowed messages to brain.think().
  4. Send the reply back to Telegram.

This file knows nothing about the vault or Claude — it just shuttles
text between Telegram and the brain.
"""

import logging

from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

from chad import config, brain, memory
from chad.history import HistoryStore

logging.basicConfig(
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("chad.bot")

# Persistent conversation history. Backed by a JSON file so a systemd
# restart doesn't wipe what Chad remembers of the current conversation
# (and, once M2 lands, doesn't orphan pending approvals).
_history = HistoryStore(config.HISTORY_PATH)


async def _handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle every text message the bot receives."""
    # --- Allow-list gate ---
    user_id = update.effective_user.id
    if user_id != config.ALLOWED_TELEGRAM_ID:
        log.warning("Blocked message from user %s", user_id)
        return  # Silent rejection: no reply, no error, no clue.

    text = update.message.text
    if not text:
        return

    log.info("Message from allowed user: %s", text[:80])

    chat_id = update.effective_chat.id
    history = _history.get(chat_id)

    # brain.think() is synchronous (network I/O to Anthropic). Running
    # it inside the async handler like this blocks the event loop, which
    # is fine for a single-user bot — there's nobody else waiting. If we
    # ever needed concurrency we'd push this into a thread or use the
    # async Anthropic client.
    reply = brain.think(history, text)

    # Persist the mutated history (brain.think appends in place). Trim
    # + atomic-write is HistoryStore's job.
    _history.set(chat_id, history)

   await update.message.reply_text(reply) 


def main() -> None:
    """Start polling Telegram for messages."""
    # Ensure memory.md exists with the fixed schema before Chad accepts any
    # message. A fresh vault should not need a separate bootstrap step.
    memory.ensure_exists()

    app = ApplicationBuilder().token(config.TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_message))
    log.info("Chad is online. Listening for messages...")
    app.run_polling()


if __name__ == "__main__":
    main()
