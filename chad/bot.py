"""Telegram front end for Chad.

Responsibilities:
  1. Accept text messages from the allowed user; drop everything else silently.
  2. Forward those messages to brain.think() and reply with the result.
  3. After each turn, surface any newly-queued proposals as Telegram
     messages with Yes / Edit / No inline keyboards.
  4. Handle button taps: execute the exact stored proposal (never
     re-invoke the LLM to interpret intent between preview and action),
     edit the button message to remove the keyboard, and append a
     synthetic history line so Chad knows the approval happened.

This file knows nothing about the vault or Claude — it just shuttles
text between Telegram, the brain, and the proposal store.
"""

import logging
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from chad import brain, config, extractor, memory, proposals, vault
from chad.history import HistoryStore

logging.basicConfig(
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("chad.bot")

# Persistent conversation history. Backed by a JSON file so a systemd
# restart doesn't wipe what Chad remembers of the current conversation
# and doesn't orphan pending approvals.
_history = HistoryStore(config.HISTORY_PATH)


# --- Approval-button rendering ---------------------------------------------

def _proposal_keyboard(pid: str) -> InlineKeyboardMarkup:
    """Three-button inline keyboard for an approval proposal.

    callback_data is capped at 64 bytes by Telegram, so we only ever put
    a short verb + pid there — the actual payload stays in the proposal
    store, which is the single source of truth for what will execute.
    """
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Yes",  callback_data=f"ok:{pid}"),
        InlineKeyboardButton("Edit", callback_data=f"ed:{pid}"),
        InlineKeyboardButton("No",   callback_data=f"no:{pid}"),
    ]])


async def _flush_proposals(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send button messages for any pending proposals that haven't been
    surfaced yet. Called after each brain turn."""
    for p in proposals.STORE.new_button_messages_needed(chat_id):
        msg = await context.bot.send_message(
            chat_id=chat_id,
            text=f"Proposal:\n{p['summary']}",
            reply_markup=_proposal_keyboard(p["pid"]),
        )
        proposals.STORE.set_message_id(p["pid"], msg.message_id)


def _append_synthetic(chat_id: int, note: str) -> None:
    """Record an approval / rejection / edit event in history.

    Without this, Chad forgets the tap happened and would re-propose
    next turn. Two implementation subtleties:

      * If the previous entry is already role=user (a text message or
        another synthetic marker), merge into that entry rather than
        adding a second consecutive user turn — the Anthropic API
        rejects role runs, and if we persisted one the whole store
        would be poisoned. _trim also collapses these defensively.
      * The note is a string so it stays a valid "opener" content
        shape (see history._is_valid_opener).
    """
    hist = _history.get(chat_id)
    if hist and hist[-1].get("role") == "user":
        prev = hist[-1].get("content")
        if isinstance(prev, str):
            hist[-1] = {"role": "user", "content": prev + "\n" + note}
        else:
            # Previous was a tool_result batch (list of blocks). Turn
            # it into a text turn — safer than trying to append to a
            # list of block objects.
            hist.append({"role": "assistant", "content": "[…]"})
            hist.append({"role": "user", "content": note})
    else:
        hist.append({"role": "user", "content": note})
    _history.set(chat_id, hist)


# --- Message handler --------------------------------------------------------

async def _handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle every text message the bot receives."""
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
    turns_before = len(history)

    # brain.think() is synchronous (network I/O to Anthropic). Blocking
    # the event loop is fine for a single-user bot — no other user is
    # waiting. Deferred to Rung 3 when collectors need concurrency.
    reply = brain.think(history, text, chat_id)

    # Persist the mutated history. Trim + atomic-write handled inside.
    _history.set(chat_id, history)

    await update.message.reply_text(reply)

    # Surface any proposals Chad queued during the turn.
    await _flush_proposals(chat_id, context)

    # M4: post-turn extractor. Runs AFTER the user's reply is already
    # out — extractor failure or slowness only delays the next message,
    # never the current one. We pass ONLY the turns that were appended
    # during this exchange (an explicit slice, not a cursor into a
    # trimmable list) so the extractor can't silently stop working
    # when history trimming shrinks the list.
    new_turns = history[turns_before:]
    try:
        extractor.run(new_turns)
    except Exception:
        log.exception("Extractor raised (best-effort; reply already sent)")


# --- Callback handler (button taps) -----------------------------------------

async def _handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Resolve a Yes / Edit / No button tap.

    This runs the executor directly against the stored proposal — the
    LLM is not consulted. That's the whole design principle: what the
    user saw in the preview IS what runs.
    """
    q = update.callback_query
    await q.answer()  # stop the Telegram spinner immediately

    # Same allow-list as text messages. Nobody else's taps are honoured.
    if q.from_user.id != config.ALLOWED_TELEGRAM_ID:
        log.warning("Blocked callback from user %s", q.from_user.id)
        return

    try:
        verb, pid = q.data.split(":", 1)
    except ValueError:
        await q.edit_message_text("Malformed callback. Ignoring.")
        return

    # DEEP-COPIED snapshot — mutating this cannot corrupt the store.
    p = proposals.STORE.get(pid)
    if p is None:
        await q.edit_message_text("That proposal is no longer available.")
        return

    if p["status"] != proposals.STATUS_PENDING:
        await q.edit_message_text(
            f"Already {p['status']}: {p['summary']}"
        )
        return

    if time.time() > p["expires"]:
        proposals.STORE.set_status(pid, proposals.STATUS_REJECTED)
        await q.edit_message_text(f"Expired — {p['summary']}")
        return

    chat_id = p["chat_id"]

    if verb == "ok":
        # execute() re-checks status/expiry/chat_id/message_id — the
        # button path also has to pass those gates. Belt-and-suspenders.
        try:
            result = proposals.STORE.execute(pid, chat_id=chat_id)
            await q.edit_message_text(
                f"Done — {p['summary']}\n\n{result}"
            )
            _append_synthetic(chat_id, f"[approved proposal {pid}: {p['summary']}]")
        except proposals.ProposalError as e:
            await q.edit_message_text(f"Cannot execute — {e}")
        except Exception as e:
            log.exception("Executor failed for proposal %s", pid)
            proposals.STORE.set_status(pid, proposals.STATUS_REJECTED)
            await q.edit_message_text(
                f"Failed — {p['summary']}\n\nError: {e}"
            )

    elif verb == "no":
        proposals.STORE.set_status(pid, proposals.STATUS_REJECTED)
        await q.edit_message_text(f"Skipped — {p['summary']}")
        _append_synthetic(chat_id, f"[rejected proposal {pid}: {p['summary']}]")

    elif verb == "ed":
        # Edit flow: mark editing, remove the keyboard, prompt for a
        # text reply. The proposal stays visible in <pending_approvals>
        # (tagged "editing") so the model knows the context on the next
        # message. Chad's job is to propose_action again with the
        # corrected args; the old proposal will simply expire in 24h.
        proposals.STORE.set_status(pid, proposals.STATUS_EDITING)
        await q.edit_message_text(
            f"{p['summary']}\n\nWhat should change? Reply with your edits."
        )
        _append_synthetic(
            chat_id,
            f"[editing proposal {pid}: {p['summary']} — next message is the edit request]",
        )

    else:
        await q.edit_message_text("Unknown action. Ignoring.")


# --- /memory command --------------------------------------------------------

_TELEGRAM_MAX = 4000  # 4096 hard cap; leave headroom for code-fence markers


def _split_for_telegram(text: str, limit: int = _TELEGRAM_MAX) -> list[str]:
    """Slice text into chunks each under Telegram's per-message limit.

    Splits on newline boundaries where possible so blocks stay readable;
    falls back to hard slicing for pathological single-line inputs.
    Empty chunks are never returned — Telegram rejects empty messages
    with a 400, which used to fail /memory on any file starting with a
    blank line and longer than the limit.
    """
    text = text.lstrip("\n")  # avoid an empty first chunk on leading newlines
    if len(text) <= limit:
        return [text] if text else []
    chunks: list[str] = []
    while text:
        if len(text) <= limit:
            if text:
                chunks.append(text)
            break
        split = text.rfind("\n", 0, limit)
        if split <= 0:
            split = limit
        chunk = text[:split]
        if chunk:
            chunks.append(chunk)
        text = text[split:].lstrip("\n")
    return chunks


async def _handle_memory_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Dump memory.md back to the chat — no LLM involved.

    The whole point is inspection: what does Chad actually think it
    knows? Answered by reading the file directly. If we routed this
    through brain.think we'd pay for an API call to have Chad tell us
    what we could read ourselves.
    """
    if update.effective_user.id != config.ALLOWED_TELEGRAM_ID:
        return
    try:
        content = vault.read_note("memory.md")
    except vault.VaultError:
        await update.message.reply_text("(memory.md does not exist yet)")
        return
    if not content.strip():
        await update.message.reply_text("(memory.md is empty)")
        return
    for chunk in _split_for_telegram(content):
        await update.message.reply_text(chunk)


# --- Wiring -----------------------------------------------------------------

def main() -> None:
    """Start polling Telegram for messages."""
    # Ensure memory.md exists with the fixed schema before Chad accepts any
    # message. A fresh vault should not need a separate bootstrap step.
    memory.ensure_exists()

    app = ApplicationBuilder().token(config.TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("memory", _handle_memory_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_message))
    app.add_handler(CallbackQueryHandler(_handle_callback))
    log.info("Chad is online. Listening for messages...")
    app.run_polling()


if __name__ == "__main__":
    main()
