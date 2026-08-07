"""Persistent conversation history for Chad.

Rung-1 kept `_histories` as an in-memory dict, which means a systemctl
restart wipes what Chad remembers of the current conversation. That
became unacceptable once approvals (M2) landed — a pending "yes" tapped
after a restart must still find the context it was proposed against.

Guarantees:
  * One JSON file on disk, `{chat_id: [messages]}`.
  * Missing file → empty; corrupt file → preserved aside, empty start.
    Delegated to chad.json_store so history and proposals cannot drift.
  * Atomic save (write .tmp, os.replace). Crash-*consistent*; NOT
    crash-durable (no fsync). Fine at this scale.
  * Structure-aware trim (see _trim). Never persists a message list
    that would be rejected by the Anthropic API for opening with an
    orphaned tool_result, or for containing two consecutive same-role
    messages.
"""

import logging
from pathlib import Path
from typing import Any

from chad import json_store

log = logging.getLogger("chad.history")

# Rough character budget for the entire persisted history of one chat.
# Chose chars over turn count because a single read_note tool_result
# can be tens of thousands of characters, so a fixed turn count has no
# meaningful upper bound in cost. ~40k chars ≈ ~10k tokens.
MAX_HISTORY_CHARS = 40_000


def _serialised_size(entry: dict) -> int:
    """Rough char count of one history entry, used as the trim-budget unit."""
    import json  # local: only used here
    return len(json.dumps(json_store.to_plain(entry), ensure_ascii=False))


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


def _collapse_consecutive_same_role(messages: list[dict]) -> list[dict]:
    """Merge consecutive messages that share a role.

    The API expects strict alternation. Two consecutive user turns
    (which _append_synthetic could produce if a tap follows a typed
    message follows another tap) will 400 the request AND — because
    HistoryStore.set persists trimmed history — brick the bot until the
    JSON file is deleted by hand. Defence-in-depth for that scenario.

    Merge rules, in order:

      * str  + str   -> joined with a newline. Keeps the common case (two
                        synthetic markers, or a marker plus a typed
                        message) as a clean text turn.
      * list + list  -> block lists concatenated.
      * mixed        -> BOTH sides normalised to block lists and
                        concatenated. Never flattened to text.

    That last rule matters more than it looks. An earlier version
    flattened mixed content with _flatten_content(), which rendered a
    tool_result block as the literal string "[tool_result]" — destroying
    it while leaving the assistant's tool_use in place. That orphaned
    tool_use is exactly the corruption phase 2 of _trim() exists to
    prevent, so the repair function had a path that caused the disease it
    treats. Normalising to blocks preserves every block instead.

    tool_result blocks are hoisted to the front of a merged list: the API
    requires them at the start of a user turn.
    """
    if not messages:
        return messages
    out: list[dict] = [dict(messages[0])]
    for entry in messages[1:]:
        prev = out[-1]
        if entry.get("role") == prev.get("role"):
            prev_content = prev.get("content")
            new_content = entry.get("content")
            if isinstance(prev_content, str) and isinstance(new_content, str):
                prev["content"] = prev_content + "\n" + new_content
            else:
                prev["content"] = _merge_blocks(prev_content, new_content)
            continue
        out.append(dict(entry))
    return out


def _as_blocks(content: Any) -> list[dict]:
    """Normalise message content to a list of API content blocks.

    A plain string becomes a single text block. A list is returned as-is.
    Anything else is stringified into a text block rather than dropped —
    losing content silently is worse than an ugly turn.
    """
    if isinstance(content, list):
        return content
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    return [{"type": "text", "text": str(content)}]


def _merge_blocks(first: Any, second: Any) -> list[dict]:
    """Concatenate two contents as block lists, tool_result blocks first.

    Preserves every block (notably tool_result, whose loss would orphan a
    tool_use and make the whole request invalid). The reordering keeps the
    result valid as a user turn, where the API expects any tool_result
    blocks at the start.
    """
    blocks = _as_blocks(first) + _as_blocks(second)
    tool_results = [b for b in blocks
                    if isinstance(b, dict) and b.get("type") == "tool_result"]
    if not tool_results:
        return blocks
    rest = [b for b in blocks
            if not (isinstance(b, dict) and b.get("type") == "tool_result")]
    return tool_results + rest
    return str(content)


def _trim(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Trim history to fit MAX_HISTORY_CHARS, repair the head, dedupe roles.

    Three phases:
      1. Size-based tail. Walk from the newest message backward until the
         next entry would exceed budget.
      2. Structural repair. Drop from the head until the first entry is a
         valid opener (user text message). Prevents the classic bug where
         a cut lands between assistant tool_use and its tool_result.
      3. Role collapse. Merge any consecutive same-role entries left by
         approvals / rejections stamping synthetic user markers next to
         real user turns.
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

    # Phase 2: repair the head.
    while tail and not _is_valid_opener(tail[0]):
        tail = tail[1:]

    # Phase 3: collapse same-role runs.
    return _collapse_consecutive_same_role(tail)


class HistoryStore:
    """Load-once, save-on-write JSON-backed history keyed by chat_id."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict[str, list[dict[str, Any]]] = json_store.load_json_dict(
            self.path, log,
        )
        log.info("Loaded history for %d chat(s) from %s",
                 len(self._data), self.path)

    def _save(self) -> None:
        json_store.save_json_dict_atomic(self.path, self._data)

    def get(self, chat_id: int) -> list[dict[str, Any]]:
        """Return the message list for a chat.

        SHALLOW copy of the outer list — appending is safe (brain.think
        only appends), but mutating a nested dict WILL affect stored
        state. Callers append; they do not mutate.
        """
        return list(self._data.get(str(chat_id), []))

    def set(self, chat_id: int, messages: list[dict[str, Any]]) -> None:
        """Replace the history for a chat and persist to disk (trimmed).

        Trim happens BEFORE save so the on-disk file never carries what
        the next request wouldn't include anyway.
        """
        self._data[str(chat_id)] = _trim(messages)
        self._save()
