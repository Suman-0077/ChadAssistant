"""Append-only audit log for memory.md writes.

Every change to memory.md — from any source — writes a JSON-lines entry
here. Grep by section, source, date; never rotated automatically. The
log is the ground truth for "did Chad remember something I didn't tell
it to remember" questions, which becomes urgent once M4's extractor
lands.

Location: STATE_DIR/memory-audit.log
Format: one JSON object per line. Keys:
  time         — ISO UTC timestamp, seconds precision
  source       — 'inline' (Chad's update_memory tool),
                 'bootstrap' (initial creation),
                 'extractor' (M4, later),
                 'consolidation' (M9, later)
  section      — one of the fixed section names, or "*" for whole-file
                 replacements (bootstrap, consolidation)
  body_chars   — length of the new body
  body         — the full new body content

No before-body is stored: vault.py already backs up every pre-edit
version into .chad-backups/, so historical content is recoverable
without duplicating it here. The audit log answers "what changed
when and why", the backups answer "what did it look like before".
"""

import json
import logging
from datetime import datetime, timezone

from chad import config

log = logging.getLogger("chad.audit")

AUDIT_LOG_PATH = config.STATE_DIR / "memory-audit.log"


def log_memory_write(source: str, section: str, new_body: str) -> None:
    """Append one entry describing a memory.md write.

    Never raises to the caller — a broken audit log is not a reason to
    fail a real memory write. If the log can't be written we still
    complete the underlying operation.
    """
    entry = {
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": source,
        "section": section,
        "body_chars": len(new_body or ""),
        "body": new_body or "",
    }
    try:
        with AUDIT_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        log.warning("Failed to write memory audit entry: %s", e)
