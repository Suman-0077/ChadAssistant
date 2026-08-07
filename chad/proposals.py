"""Deferred-execution approval store for side-effecting actions.

Every action with a real-world side effect (add a reminder, send a
draft, create a calendar event, ...) is *proposed* here first. It
surfaces to the user as a Telegram message with Yes / Edit / No
buttons and only executes on explicit approval — and *only* after the
user has actually seen the buttons (message_id must be set).

Design principle from the memory-system doc §7, tightened after the
M2 adversarial review:

    The approval press must not go back through the LLM. If pressing Yes
    re-invokes the model, the model may execute something slightly
    different from what was previewed. Preview and execution must be
    the same serialized object — and the human must have seen the
    preview before execution can happen.

What that means in code (see `execute()`):

  * status must be pending
  * expires must not have passed
  * chat_id must match the caller's chat
  * message_id must be set (i.e. the buttons have actually reached the
    user; the human had a chance to see the preview)

Any one of those failing raises `ProposalError` and no side effect
occurs. This kills the same-turn self-approval attack the adversarial
review demonstrated — the model can call propose_action and can
technically call approve_pending, but the message hasn't been flushed
yet so message_id is None, so execute() refuses.

Executors are registered with a validator (checked BEFORE the proposal
is queued, so bad args never reach the human) and a summariser (which
derives the preview string server-side from the args, so the preview
and the payload cannot disagree). The model supplies intent; the code
renders the preview.

State layout (`proposals.json`):

    {
      "<pid>": {
        "pid": "a3f9c201",
        "kind": "add_reminder",
        "args": {"date": "2026-08-07", "text": "submit assignment"},
        "summary": "Add reminder for Fri 2026-08-07: submit assignment",
        "chat_id": 123456789,
        "status": "pending" | "done" | "rejected" | "editing",
        "created": <unix>,
        "expires": <unix>,
        "message_id": null | <telegram message id>
      }
    }
"""

import copy
import logging
import time
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from chad import config, json_store

log = logging.getLogger("chad.proposals")

EXPIRY_SECONDS = 24 * 60 * 60          # 24h before pending proposals expire
TERMINAL_RETENTION_SECONDS = 7 * 24 * 60 * 60   # keep done/rejected 7 days

STATUS_PENDING = "pending"
STATUS_DONE = "done"
STATUS_REJECTED = "rejected"
STATUS_EDITING = "editing"
_TERMINAL_STATUSES = frozenset({STATUS_DONE, STATUS_REJECTED})


class ProposalError(Exception):
    """Raised when a proposal cannot be executed for any reason.

    Distinct from vault / memory errors so the callback handler can
    surface it clearly to the user without confusing it with executor
    failures.
    """


# --- Executor registry ------------------------------------------------------

# Each `kind` maps to a tuple of (executor, validator, summariser):
#   executor(**args)   -> str result
#   validator(**args)  -> None or raises ValueError
#   summariser(**args) -> str preview (shown to the user; must render the
#                                      exact same args that will execute)
_EXECUTORS: dict[str, dict[str, Callable]] = {}


def register_executor(
    kind: str,
    fn: Callable[..., str],
    validator: Callable[..., None],
    summarise: Callable[..., str],
) -> None:
    """Register everything needed to safely propose and execute a kind.

    All three callables are required. The validator runs BEFORE the
    proposal is queued (so bad args never reach the human); the
    summariser is what the human sees (so preview and payload cannot
    diverge); the executor runs only after approval.
    """
    if kind in _EXECUTORS:
        log.warning("Re-registering executor for kind '%s'", kind)
    _EXECUTORS[kind] = {"fn": fn, "validator": validator, "summarise": summarise}


def known_kinds() -> list[str]:
    """Names of every kind currently registered."""
    return sorted(_EXECUTORS.keys())


# --- Store ------------------------------------------------------------------

_REQUIRED_KEYS = ("pid", "kind", "args", "summary", "chat_id",
                  "status", "created", "expires", "message_id")


def _is_well_formed(entry: Any) -> bool:
    """Cheap shape check to survive schema drift and hand edits."""
    if not isinstance(entry, dict):
        return False
    return all(k in entry for k in _REQUIRED_KEYS)


class ProposalStore:
    """JSON-backed persistent store for pending proposals."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict[str, dict[str, Any]] = self._load_and_prune()

    def _load_and_prune(self) -> dict[str, dict[str, Any]]:
        raw = json_store.load_json_dict(self.path, log)
        # Drop malformed entries and long-terminal ones. Never let a
        # single bad entry take the bot down (H3 in the review) and
        # keep the file from growing without bound (M4).
        now = time.time()
        pruned: dict[str, dict[str, Any]] = {}
        dropped_malformed = 0
        dropped_terminal = 0
        for pid, entry in raw.items():
            if not _is_well_formed(entry):
                dropped_malformed += 1
                continue
            status = entry.get("status")
            if status in _TERMINAL_STATUSES:
                age = now - entry.get("created", now)
                if age > TERMINAL_RETENTION_SECONDS:
                    dropped_terminal += 1
                    continue
            pruned[pid] = entry
        if dropped_malformed or dropped_terminal:
            log.info(
                "Pruned proposals: %d malformed, %d old terminal.",
                dropped_malformed, dropped_terminal,
            )
        return pruned

    def _save(self) -> None:
        json_store.save_json_dict_atomic(self.path, self._data)

    # --- writes -------------------------------------------------------------

    def add(self, kind: str, args: dict, chat_id: int) -> str:
        """Queue a new proposal. Returns its short pid.

        Validates args and derives the preview summary server-side.
        Raises ProposalError if the kind is unknown, or ValueError if
        the args fail validation — either way, nothing is queued.
        """
        if kind not in _EXECUTORS:
            raise ProposalError(
                f"Unknown proposal kind '{kind}'. Valid kinds: "
                f"{', '.join(known_kinds())}"
            )
        spec = _EXECUTORS[kind]
        # Validate BEFORE queuing so the human never sees a bad preview.
        spec["validator"](**args)
        # Derive the summary from args, ignoring whatever the model would
        # have written. Preview and payload now cannot disagree.
        summary = spec["summarise"](**args)

        pid = uuid4().hex[:8]
        now = time.time()
        self._data[pid] = {
            "pid": pid,
            "kind": kind,
            "args": args,
            "summary": summary,
            "chat_id": chat_id,
            "status": STATUS_PENDING,
            "created": now,
            "expires": now + EXPIRY_SECONDS,
            "message_id": None,
        }
        self._save()
        log.info("Queued proposal %s (kind=%s) for chat %s",
                 pid, kind, chat_id)
        return pid

    def set_message_id(self, pid: str, message_id: int) -> None:
        """Record which Telegram message carries the buttons for this proposal.

        Also the load-bearing check that gates execute(): message_id is
        only set once _flush_proposals has actually delivered the
        buttons, so its presence is direct evidence the human saw the
        preview.
        """
        entry = self._data.get(pid)
        if entry is not None:
            entry["message_id"] = message_id
            self._save()

    def set_status(self, pid: str, status: str) -> None:
        entry = self._data.get(pid)
        if entry is not None:
            entry["status"] = status
            self._save()

    # --- reads --------------------------------------------------------------

    def get(self, pid: str) -> dict | None:
        """Return a DEEP COPY of a proposal, or None.

        Callers cannot mutate stored state by accident (M6). To change
        a proposal, call set_status / set_message_id explicitly.
        """
        entry = self._data.get(pid)
        return copy.deepcopy(entry) if entry is not None else None

    def visible_for_chat(self, chat_id: int) -> list[dict]:
        """Pending + editing proposals for a chat, oldest first.

        Both statuses are shown to the model so 'yes' still resolves
        against an editing proposal that hasn't been rejected yet (M2
        in the review).
        """
        now = time.time()
        visible = []
        for p in self._data.values():
            try:
                if p.get("chat_id") != chat_id:
                    continue
                if p.get("status") not in (STATUS_PENDING, STATUS_EDITING):
                    continue
                if p.get("expires", 0) <= now:
                    continue
            except (TypeError, AttributeError):
                # Never let a weird entry crash system prompt assembly.
                continue
            visible.append(copy.deepcopy(p))
        visible.sort(key=lambda p: p.get("created", 0))
        return visible

    def pending_for_chat(self, chat_id: int) -> list[dict]:
        """Only the pending subset — used by the flusher."""
        return [p for p in self.visible_for_chat(chat_id)
                if p.get("status") == STATUS_PENDING]

    def new_button_messages_needed(self, chat_id: int) -> list[dict]:
        """Pending proposals for a chat that haven't been surfaced yet."""
        return [p for p in self.pending_for_chat(chat_id)
                if p.get("message_id") is None]

    # --- execution ----------------------------------------------------------

    def execute(self, pid: str, chat_id: int) -> str:
        """Run the approved action after ALL gates pass.

        Gates (any failure raises ProposalError, no side effect occurs):
          * proposal exists
          * status is pending (not done, rejected, or editing)
          * has not expired
          * chat_id matches (prevents cross-chat approval)
          * message_id is set (the human has actually seen the preview)

        The last one is what defeats same-turn self-approval by the
        model: message_id is set only after _flush_proposals sends the
        button message, which happens AFTER the turn ends.
        """
        entry = self._data.get(pid)
        if entry is None:
            raise ProposalError(f"No such proposal: {pid}")
        if entry.get("status") != STATUS_PENDING:
            raise ProposalError(
                f"Proposal {pid} is {entry.get('status')}, not pending."
            )
        if entry.get("expires", 0) <= time.time():
            raise ProposalError(f"Proposal {pid} has expired.")
        if entry.get("chat_id") != chat_id:
            raise ProposalError(
                f"Proposal {pid} does not belong to this chat."
            )
        if entry.get("message_id") is None:
            raise ProposalError(
                f"Proposal {pid} has not been shown to the user yet."
            )

        spec = _EXECUTORS.get(entry["kind"])
        if spec is None:
            raise ProposalError(
                f"No executor registered for kind '{entry['kind']}'"
            )

        # Mark done BEFORE running the executor so a crash mid-executor
        # cannot double-execute on retry. If the executor fails, the
        # status is still done — the caller catches and reports.
        self.set_status(pid, STATUS_DONE)
        result = spec["fn"](**entry["args"])
        return result


# Module-level singleton — brain, bot, future cron scripts all share it.
STORE = ProposalStore(config.STATE_DIR / "proposals.json")
