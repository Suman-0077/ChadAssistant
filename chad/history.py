"""Persistent conversation history for Chad.

Rung-1 kept `_histories` as an in-memory dict, which means a systemctl
restart wipes what Chad remembers of the current conversation. That's
about to become unacceptable — the approval system (M2) queues actions
between messages, and a pending "yes" tapped after a restart must still
find the context it was proposed against.

This module gives us:
  * One JSON file on disk, {chat_id: [messages]}.
  * Load-time tolerance: missing file → empty; corrupt file → warn,
    empty, do not crash. A malformed history should never break the bot.
  * Atomic save: write to .tmp, os.replace into position. Foreshadows
    the M5 file-locking work; already gives us crash-safety.
  * Trim to the last MAX_TURNS entries per chat before saving. Unbounded
    growth kills prompt caching (M8) and inflates every request.

The `content` field of assistant messages arrives from the Anthropic
client as a list of pydantic block objects. We flatten those to plain
dicts before serialising so the JSON stays clean and the file remains
usable even if the client library upgrades.
"""

import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger("chad.history")

# Trim policy: keep the tail of the message list. A "turn" here is one
# message entry (user, assistant, or tool-result batch), not a
# conversational round-trip, so this covers roughly 10-20 exchanges
# depending on how many tool calls each involves.
MAX_TURNS = 40


def _to_plain(value: Any) -> Any:
    """Recursively convert pydantic blocks to dicts so json.dumps works."""
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, list):
        return [_to_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _to_plain(v) for k, v in value.items()}
    return value


class HistoryStore:
    """Load-once, save-on-write JSON-backed history keyed by chat_id."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict[str, list[dict[str, Any]]] = self._load()

    def _load(self) -> dict[str, list[dict[str, Any]]]:
        if not self.path.exists():
            log.info("No history file at %s — starting empty.", self.path)
            return {}
        try:
            raw = self.path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("history file is not a JSON object")
            log.info("Loaded history for %d chat(s) from %s",
                     len(data), self.path)
            return data
        except (json.JSONDecodeError, ValueError, OSError) as e:
            # Do not crash — a broken history file is annoying but not
            # a reason to take the bot offline. Log loudly and reset.
            log.warning(
                "History file at %s is unusable (%s). Starting empty; "
                "the old file is left in place for inspection.",
                self.path, e,
            )
            return {}

    def _save(self) -> None:
        # Atomic write: content lands in one syscall, so a crash can
        # never leave a half-written history file.
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(_to_plain(self._data), ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)

    def get(self, chat_id: int) -> list[dict[str, Any]]:
        """Return a fresh copy of the history list for a chat."""
        # JSON keys are always strings; normalise on the way out. Copy
        # so the caller mutating the list doesn't touch our state until
        # they call set().
        return list(self._data.get(str(chat_id), []))

    def set(self, chat_id: int, messages: list[dict[str, Any]]) -> None:
        """Replace the history for a chat and persist to disk (trimmed)."""
        # Trim BEFORE saving so the on-disk file never carries what the
        # next request wouldn't include anyway.
        trimmed = messages[-MAX_TURNS:] if len(messages) > MAX_TURNS else messages
        self._data[str(chat_id)] = trimmed
        self._save()
