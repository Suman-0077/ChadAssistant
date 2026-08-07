"""Reminders: a flat text file the morning cron will read in M6.

M2 builds the WRITE path only — the executor that runs when the user
taps Yes on an add_reminder proposal. M6 will add the systemd timer
that reads this file at 07:00 and surfaces items due today.

File format (deliberately dead simple so the cron is trivial):

    # Reminders

    2026-08-07 | submit COMP2000 assignment
    2026-08-10 | pay rent

One line per reminder: ISO date, space, `|`, space, text, newline.
Chad NEVER edits this file directly — it's in vault._PROTECTED_NOTES,
and the only path to write here is through the approval executor
registered below.

The validator and summariser registered alongside the executor are
what defeat two M2-review findings:
  * validator: bad args are rejected BEFORE the human sees a bogus
    preview (M1).
  * summariser: the preview shown to the user is *derived from the
    executed args*, not a free-text field the model can fake (H1). So
    the preview and the payload cannot disagree.
"""

from datetime import date as _date

from chad import proposals, vault

REMINDERS_FILENAME = "reminders.md"
_FIELD_SEP = " | "


# --- Validator + summariser + executor --------------------------------------

def _parse_iso_date(s: str) -> _date:
    """Real calendar validation, not just a shape check."""
    if not isinstance(s, str):
        raise ValueError(f"date must be a string, got {type(s).__name__}")
    try:
        return _date.fromisoformat(s)
    except ValueError:
        raise ValueError(f"date must be a real YYYY-MM-DD, got: {s!r}")


def _clean_text(text: str) -> str:
    """Normalise reminder text so it can round-trip through the file format.

    Rejects the field separator entirely — no escaping heuristics that
    the M6 parser would then have to mirror. Collapses newlines because
    reminders.md is one-line-per-reminder.
    """
    if not isinstance(text, str):
        raise ValueError(f"text must be a string, got {type(text).__name__}")
    text = text.replace("\n", " ").strip()
    if not text:
        raise ValueError("reminder text cannot be empty")
    if "|" in text:
        raise ValueError("reminder text cannot contain '|' (field separator)")
    return text


def _validate(*, date: str, text: str) -> None:
    """Validate add_reminder args. Called BEFORE the proposal is queued."""
    _parse_iso_date(date)
    _clean_text(text)


def _summarise(*, date: str, text: str) -> str:
    """Render the preview shown to the user — server-derived from the
    same args the executor will receive."""
    parsed = _parse_iso_date(date)
    weekday = parsed.strftime("%A")
    return f"Add reminder for {weekday} {date}: {_clean_text(text)}"


def add_reminder(*, date: str, text: str) -> str:
    """Append a reminder line to reminders.md.

    Called by proposals.STORE.execute() after user approval. Re-runs
    validation defensively — the args in the store could in theory be
    stale, and executor-time validation costs nothing.
    """
    _parse_iso_date(date)
    text = _clean_text(text)
    vault.append_line_raw(
        REMINDERS_FILENAME,
        f"{date}{_FIELD_SEP}{text}",
        header_if_new="# Reminders\n\n",
    )
    return f"Reminder set for {date}: {text}"


proposals.register_executor(
    kind="add_reminder",
    fn=add_reminder,
    validator=_validate,
    summarise=_summarise,
)
