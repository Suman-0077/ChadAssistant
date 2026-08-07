"""Systemd-timer entry point: surface reminders due today via Telegram.

Runs at 07:00 in the user's timezone (see deploy/systemd/chad-morning.timer).
ZERO AI in this path — reads reminders.md, filters for lines whose date
is today (or overdue) and haven't been marked done, sends each via
Telegram, marks them done in the file.

File format:

    # Reminders

    2026-08-10 | info1112 quiz                # pending
    [done] 2026-08-07 | submit assignment 1   # already fired

The write path (chad/reminders.py) always emits the pending form; this
script prefixes lines with `[done] ` after firing so we don't re-notify.

Concurrency note: reminders.md is currently appended to by the bot and
rewritten by this script — a race exists (bot append lands between our
read and rewrite → the append gets lost). Window is small (seconds per
day) and M5's file locking will close it properly. Documented, not
fixed here.
"""

import asyncio
import logging
import re
import sys
from datetime import date as _date, datetime

from telegram import Bot

from chad import config

logging.basicConfig(
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("chad.cron.morning_reminders")

REMINDERS_PATH = config.VAULT_PATH / "reminders.md"
DONE_PREFIX = "[done] "

# Pending line: bare ISO date, space-ish, pipe, text.
_LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\s*\|\s*(.+?)\s*$")


def _load_lines() -> list[str]:
    if not REMINDERS_PATH.exists():
        return []
    return REMINDERS_PATH.read_text(encoding="utf-8").splitlines(keepends=True)


def _save_lines_atomic(lines: list[str]) -> None:
    """Write lines back atomically — write to .tmp, os.replace."""
    tmp = REMINDERS_PATH.with_suffix(REMINDERS_PATH.suffix + ".tmp")
    tmp.write_text("".join(lines), encoding="utf-8")
    tmp.replace(REMINDERS_PATH)


def _find_due(lines: list[str], today: _date) -> list[tuple[int, str, str]]:
    """Return (index, date_str, text) for each pending line due today or earlier."""
    due: list[tuple[int, str, str]] = []
    for i, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped or stripped.startswith(DONE_PREFIX) or stripped.startswith("#"):
            continue
        m = _LINE_RE.match(stripped)
        if not m:
            log.warning("Skipping malformed line %d: %r", i, stripped)
            continue
        date_str, text = m.group(1), m.group(2)
        try:
            reminder_date = _date.fromisoformat(date_str)
        except ValueError:
            log.warning("Skipping bad date on line %d: %r", i, date_str)
            continue
        if reminder_date <= today:
            due.append((i, date_str, text))
    return due


def _mark_done(lines: list[str], indices: list[int]) -> None:
    """Prefix the given lines with DONE_PREFIX. Mutates lines in place."""
    for i in indices:
        lines[i] = DONE_PREFIX + lines[i].lstrip()


async def main() -> None:
    today = datetime.now(config.USER_TIMEZONE).date()
    log.info("Morning reminders scan for %s", today.isoformat())

    lines = _load_lines()
    if not lines:
        log.info("No reminders.md — nothing to do.")
        return

    due = _find_due(lines, today)
    if not due:
        log.info("Nothing due today.")
        return

    log.info("Firing %d reminder(s)", len(due))

    bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
    for _, date_str, text in due:
        prefix = "Reminder" if date_str == today.isoformat() else "Overdue"
        await bot.send_message(
            chat_id=config.ALLOWED_TELEGRAM_ID,
            text=f"{prefix} [{date_str}]: {text}",
        )

    _mark_done(lines, [i for i, _, _ in due])
    _save_lines_atomic(lines)
    log.info("Marked %d reminder(s) as done.", len(due))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        log.exception("Morning reminders script failed.")
        sys.exit(1)
