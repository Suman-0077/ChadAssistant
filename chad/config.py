"""Configuration for Chad.

Loads settings from environment variables (via a .env file) and validates
them once at import time. If anything required is missing, we crash
immediately with a clear message — a config error should never be
discovered halfway through a conversation.
"""

import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

# Read the .env file sitting in the project root (the directory you run
# `python -m chad.bot` from). Existing real environment variables win over
# .env values, which is standard behaviour.
load_dotenv()


def _require(name: str) -> str:
    """Fetch a required environment variable or exit with a clear error."""
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"Config error: required environment variable {name} is not set. "
                 f"Copy .env.example to .env and fill it in.")
    return value


# --- Secrets and identifiers -------------------------------------------------

ANTHROPIC_API_KEY = _require("ANTHROPIC_API_KEY")
TELEGRAM_BOT_TOKEN = _require("TELEGRAM_BOT_TOKEN")

# The ONLY Telegram user Chad will talk to. Everything else is ignored.
try:
    ALLOWED_TELEGRAM_ID = int(_require("ALLOWED_TELEGRAM_ID"))
except ValueError:
    sys.exit("Config error: ALLOWED_TELEGRAM_ID must be a number "
             "(your numeric Telegram user ID, not your @username).")

# --- Vault -------------------------------------------------------------------

# resolve() turns the path into an absolute, symlink-free form. vault.py
# depends on this for its path-jail check, so we do it here, once.
VAULT_PATH = Path(_require("VAULT_PATH")).resolve()

if not VAULT_PATH.is_dir():
    sys.exit(f"Config error: VAULT_PATH does not exist or is not a directory: "
             f"{VAULT_PATH}")

# --- State (history, pending approvals, audit log) --------------------------

# Persistent state that isn't part of the human-visible vault lives in a
# hidden sibling folder. Kept inside the vault so backups/git cover it, but
# dot-prefixed so Obsidian and list_notes ignore it.
STATE_DIR = Path(
    os.environ.get("STATE_DIR", str(VAULT_PATH / ".chad-state"))
).resolve()
STATE_DIR.mkdir(parents=True, exist_ok=True)

HISTORY_PATH = STATE_DIR / "history.json"

# --- Time --------------------------------------------------------------------

# The server runs in UTC but the user lives in a real timezone. Anything Chad
# reasons about — "today", "tomorrow", "yesterday's roster" — has to be in the
# user's local time. Defaults to UTC so bad config is obvious rather than
# subtly wrong.
_tz_name = os.environ.get("USER_TIMEZONE", "UTC").strip()
try:
    USER_TIMEZONE = ZoneInfo(_tz_name)
except ZoneInfoNotFoundError:
    sys.exit(f"Config error: USER_TIMEZONE '{_tz_name}' is not a valid IANA "
             f"timezone (try 'Australia/Sydney', 'Europe/London', etc.).")

# --- Model -------------------------------------------------------------------

# Optional, with a sensible default. Override in .env to try other models.
MODEL = os.environ.get("MODEL", "claude-sonnet-4-5").strip()

# The extractor uses a cheaper model (Haiku by default) since it does one
# bounded classification pass, not full reasoning.
EXTRACTOR_MODEL = os.environ.get("EXTRACTOR_MODEL", "claude-haiku-4-5").strip()

# --- Extractor (M4) ---------------------------------------------------------

# 'shadow' = extractor runs, its proposed edits get logged to memory-audit.log
#   with source='extractor-shadow' but NEVER touch memory.md. This is the
#   default until the extractor has been observed working correctly.
# 'live'   = extractor's edits also apply to memory.md (source='extractor').
# Anything else is treated as 'shadow' with a warning.
EXTRACTOR_MODE = os.environ.get("EXTRACTOR_MODE", "shadow").strip().lower()
if EXTRACTOR_MODE not in ("shadow", "live"):
    print(f"Config warning: EXTRACTOR_MODE={EXTRACTOR_MODE!r} unrecognised; "
          f"defaulting to 'shadow'.")
    EXTRACTOR_MODE = "shadow"
