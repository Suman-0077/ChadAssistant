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

from chad import config, brain

logging.basicConfig(
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("chad.bot")

# One conversation history per chat, keyed by Telegram chat ID.
# For now there's only one allowed user, but the structure is ready
# for the day (if ever) that changes.
_histories: dict[int, list[dict]] = {}


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
    history = _histories.setdefault(chat_id, [])

    # brain.think() is synchronous (network I/O to Anthropic). Running
    # it inside the async handler like this blocks the event loop, which
    # is fine for a single-user bot — there's nobody else waiting. If we
    # ever needed concurrency we'd push this into a thread or use the
    # async Anthropic client.
    reply = brain.think(history, text)

    await update.message.reply_text(reply)


def main() -> None:
    """Start polling Telegram for messages."""
    app = ApplicationBuilder().token(config.TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_message))
    log.info("Chad is online. Listening for messages...")
    app.run_polling()


if __name__ == "__main__":
    main()
