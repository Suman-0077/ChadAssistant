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

Two hardening patterns applied per the M6 review:

  * Mark-after-each-send. If a send fails partway through the batch,
    everything already delivered is already recorded done — we don't
    re-fire it on the next run.

  * Re-read-before-write. Between the initial load and the write-back
    the bot may have appended a newly-approved reminder. Instead of
    clobbering with a stale snapshot, we re-read fresh from disk each
    time and locate our line by *content*, not by index. Any lines
    that appeared in the interim survive. Closes the silent-data-loss
    race that M5's proper locking will make redundant.

  * All writes go through vault.write_note_raw (M4 fix) — atomic,
    backed up, path-jailed. vault.py stays the only module that
    touches the filesystem.
"""

import asyncio
import logging
import re
import sys
from datetime import date as _date, datetime

from telegram import Bot

from chad import config, vault

logging.basicConfig(
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("chad.cron.morning_reminders")

REMINDERS_FILENAME = "reminders.md"
REMINDERS_PATH = config.VAULT_PATH / REMINDERS_FILENAME
DONE_PREFIX = "[done] "

# Pending line: bare ISO date, space-ish, pipe, text.
_LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\s*\|\s*(.+?)\s*$")


def _load_lines() -> list[str]:
    if not REMINDERS_PATH.exists():
        return []
    return REMINDERS_PATH.read_text(encoding="utf-8").splitlines(keepends=True)


def _find_due(lines: list[str], today: _date) -> list[tuple[str, str]]:
    """Return (date_str, text) for each pending line due today or earlier.

    Returns content, not indices — indices are unstable across the read /
    send / re-read cycle we use to survive concurrent bot appends.
    """
    due: list[tuple[str, str]] = []
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
            due.append((date_str, text))
    return due


def _mark_one_done(date_str: str, text: str) -> None:
    """Prefix the matching pending line with DONE_PREFIX. Re-reads the file
    right before writing so any lines appended by the bot since we started
    survive. Only marks the FIRST match — same-content duplicates are rare
    and marking one at a time is safe (the other will fire next run).

    Locked (M5) against concurrent bot appends. Combined with the re-read
    inside the lock, the race that used to silently drop approved
    reminders is closed properly, not just narrowed.
    """
    with vault.lock(REMINDERS_FILENAME):
        lines = _load_lines()
        target = f"{date_str} | {text}"
        for j, raw in enumerate(lines):
            if raw.strip() == target:
                lines[j] = DONE_PREFIX + raw.lstrip()
                vault.write_note_raw(REMINDERS_FILENAME, "".join(lines))
                return
        log.warning(
            "Could not find %r in reminders.md to mark done (was it edited?).",
            target,
        )


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
    for date_str, text in due:
        prefix = "Reminder" if date_str == today.isoformat() else "Overdue"
        try:
            await bot.send_message(
                chat_id=config.ALLOWED_TELEGRAM_ID,
                text=f"{prefix} [{date_str}]: {text}",
            )
        except Exception:
            # Don't mark done if we failed to deliver — user should get
            # notified next run. Better a late reminder than a lost one.
            log.exception("Failed to send reminder for %s: %s", date_str, text)
            continue

        # Mark done immediately after successful delivery (M2 fix).
        # Re-read-and-match inside _mark_one_done (M3 fix) so a bot
        # append between our load and this write isn't clobbered.
        try:
            _mark_one_done(date_str, text)
        except Exception:
            log.exception(
                "Sent reminder but failed to mark done — will re-fire next run: "
                "%s | %s", date_str, text,
            )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        log.exception("Morning reminders script failed.")
        sys.exit(1)
