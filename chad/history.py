"""Persistent conversation history for Chad.

Rung-1 kept `_histories` as an in-memory dict, which means a systemctl
restart wipes what Chad remembers of the current conversation. That's
about to become unacceptable — the approval system (M2) queues actions
between messages, and a pending "yes" tapped after a restart must still
find the context it was proposed against.

This module gives us:
  * One JSON file on disk, {chat_id: [messages]}.
  * Load-time tolerance: missing file → empty; corrupt file → warn,
    rename the broken file aside, start empty. The bot never crashes
    on a bad history file and the original is preserved for inspection.
  * Atomic save: write to .tmp, os.replace into position. Crash-*consistent*
    (a crash never leaves a half-written file); NOT crash-durable in the
    strict fsync sense — a power cut can lose the last write.
  * Structure-aware trim that budgets by estimated size, then repairs
    the head so the surviving history never starts with an orphaned
    tool_result block (which the Anthropic API would reject and permanently
    poison the store).

The `content` field of assistant messages arrives from the Anthropic
client as a list of pydantic block objects. We flatten those to plain
dicts before serialising so the JSON stays clean and the file remains
usable even if the client library upgrades.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("chad.history")

# Rough character budget for the entire persisted history of one chat.
# Chose chars over "turn count" because a single read_note tool_result
# can be tens of thousands of characters, so a fixed turn count has no
# meaningful upper bound in cost. ~40k chars ≈ ~10k tokens, comfortably
# small relative to the context window while keeping enough working
# memory for a real conversation.
MAX_HISTORY_CHARS = 40_000


def _to_plain(value: Any) -> Any:
    """Recursively convert pydantic blocks to dicts so json.dumps works."""
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, list):
        return [_to_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _to_plain(v) for k, v in value.items()}
    return value


def _serialised_size(entry: dict) -> int:
    """Rough char count of one history entry, used as the trim budget unit."""
    return len(json.dumps(_to_plain(entry), ensure_ascii=False))


def _is_valid_opener(entry: dict) -> bool:
    """True if `entry` can legally be the first message in a request.

    The Anthropic API rejects any message list that starts with an
    orphaned tool_result. A safe opener is a plain user text message —
    role=user AND content is a string, not a list of blocks (a list-of-
    blocks user message is a tool_result batch).
    """
    if entry.get("role") != "user":
        return False
    return isinstance(entry.get("content"), str)


def _trim(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Trim history to fit MAX_HISTORY_CHARS, then repair the head.

    Two phases:

      1. Size-based tail. Walk from the newest message backward, adding
         each entry to the surviving tail until the next one would push
         us over budget.

      2. Structural repair. Walk the surviving tail forward, dropping
         entries until the first one is a valid opener. This prevents
         the classic bug where a trim cuts between an assistant tool_use
         and the following tool_result — leaving the API to reject every
         subsequent request until someone deletes history.json by hand.
    """
    # Phase 1: size-based tail.
    tail_size = 0
    cutoff = 0
    for i in range(len(messages) - 1, -1, -1):
        entry_size = _serialised_size(messages[i])
        if tail_size + entry_size > MAX_HISTORY_CHARS:
            cutoff = i + 1
            break
        tail_size += entry_size
    tail = messages[cutoff:]

    # Phase 2: repair the head. Drop until valid opener.
    while tail and not _is_valid_opener(tail[0]):
        tail = tail[1:]

    return tail


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
            # Do not crash — a broken history file is annoying, not fatal.
            # Rename the broken file aside so the next _save() (which uses
            # os.replace) cannot destroy it. The user (or future audit
            # tooling) can inspect what went wrong.
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            preserved = self.path.with_name(f"{self.path.name}.corrupt.{ts}")
            try:
                os.rename(self.path, preserved)
                log.warning(
                    "History file at %s is unusable (%s). Preserved as %s. "
                    "Starting empty.",
                    self.path, e, preserved,
                )
            except OSError as rename_err:
                log.warning(
                    "History file at %s is unusable (%s) and could not be "
                    "preserved (%s). Starting empty; the broken file will "
                    "be overwritten on the next save.",
                    self.path, e, rename_err,
                )
            return {}

    def _save(self) -> None:
        # Atomic in the sense that no reader ever sees a half-written
        # file. NOT durable across power loss (no fsync). Fine at this
        # scale; noted for future reference.
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(_to_plain(self._data), ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)

    def get(self, chat_id: int) -> list[dict[str, Any]]:
        """Return the message list for a chat.

        Returns a SHALLOW copy of the outer list. Appending to it does
        not affect the store until set() is called (fine — brain.think
        only appends). Mutating a nested dict WOULD affect the stored
        version; callers must not do that.
        """
        return list(self._data.get(str(chat_id), []))

    def set(self, chat_id: int, messages: list[dict[str, Any]]) -> None:
        """Replace the history for a chat and persist to disk (trimmed).

        Trim happens BEFORE save so the on-disk file never carries what
        the next request wouldn't include anyway. The trim is structure-
        aware — see _trim().
        """
        trimmed = _trim(messages)
        self._data[str(chat_id)] = trimmed
        self._save()
