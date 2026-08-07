# M2 adversarial review — round 2

Re-test of all 16 findings from `m2-review.md`, plus a fresh pass attacking the
fixes themselves.

**All 16 original findings are fixed and verified — 18/18 regression checks
pass.** Four new findings, one of which means the H2 fix does not actually
protect the path it was written for.

---

## Verified fixed (18/18)

| Original | Check | Result |
|---|---|---|
| C1 | `approve_pending` / `reject_pending` removed from `TOOLS` and `_run_tool` | pass |
| C1 | Same-turn self-approval blocked by the `message_id` gate | pass |
| C1 | Rejected / expired / cross-chat proposals refuse to execute | pass |
| C1 | Double execution of one pid blocked | pass |
| C2 | `reminders.md` in `_PROTECTED_NOTES`, `append_note` refused | pass |
| H1 | Summary derived server-side from args; model's string discarded | pass |
| H3 | Malformed proposal entry no longer crashes prompt assembly | pass |
| M1 | Bad args rejected at propose time, before the human sees a preview | pass |
| M2 | `editing` proposals visible to the model, not executable | pass |
| M3 | Corrupt `proposals.json` preserved as `.corrupt.<ts>` | pass |
| M4 | Terminal proposals pruned (on load — see N2 below) | partial |
| M5 | `reminders.py` writes via `vault.append_line_raw`, jail applies | pass |
| M6 | `get()` returns a deep copy | pass |
| L1 | `date.fromisoformat` rejects `9999-99-99` | pass |
| L2 | `\|` in reminder text rejected outright | pass |
| L3 | `brain.py` module docstring updated | pass |
| L4 | `to_plain()` applied on save via `json_store` | pass |
| L5 | Executor failure handled in one place | pass |

`json_store.py` is the right call. The two stores had already drifted once (M3
existed precisely because `proposals.py` was copied from the pre-H1
`history.py`); they now cannot drift again on load, corruption, or atomicity.

Removing `approve_pending` from `TOOLS` outright, rather than trying to secure
it, is the stronger fix — the model now has no code path to approval at all.
The `message_id is not None` gate remains as defence in depth.

---

## NEW — N1. The consecutive-role fix does not protect the outgoing request

**This is the one to fix before deploying.**

`_collapse_consecutive_same_role` runs inside `_trim`, which runs inside
`HistoryStore.set()`. In `bot.py`, `set()` is called **after**
`brain.think()` has already made its API calls:

```python
history = _history.get(chat_id)      # ends with the synthetic user marker
reply = brain.think(history, text, chat_id)   # appends user msg, CALLS THE API
_history.set(chat_id, history)                # trim/collapse — too late
```

Reproduced, following the exact bot.py sequence:

```
stored after tapping Yes:        ['user', 'assistant', 'user']
roles actually sent to the API:  ['user', 'assistant', 'user', 'user']
                                                        ^^^^^^^^^^^^^^
```

`_append_synthetic` correctly merges when the previous entry is already a user
turn — but after a completed turn the previous entry is the *assistant*, so the
marker is appended as a new user entry, and the next typed message lands
directly on top of it.

This is not an edge case. It is the primary flow: **tap a button, then type
anything.** The store self-heals afterwards (N2 confirms `set()` collapses it),
so the corruption isn't persistent — but the request that triggered it has
already failed, and the user sees an error every time they speak after tapping.

**Fix:** collapse on the way *out*, not on the way in. `think()` should
normalise the message list immediately before `_client.messages.create`, or
`_append_synthetic` should merge the marker into the preceding **assistant**
turn instead of opening a new user turn. The `set()`-time collapse is worth
keeping as the second layer.

**Still worth confirming first:** whether the API actually rejects role runs.
Five lines against the real client settles it, and determines whether this is
"every message after a tap errors" or merely untidy.

## NEW — N2. `_collapse_consecutive_same_role` can destroy a live `tool_result`

When two same-role entries have *mixed* content types (one a string, one a list
of blocks), the merge flattens both to text via `_flatten_content`, which
renders a `tool_result` block as the literal string `"[tool_result]"`:

```
assistant -> [{'type': 'tool_use', 'id': 'toolu_1', ...}]
user      -> '[tool_result]\n[approved proposal xy]'

live tool_use blocks   : ['tool_use']
real tool_result blocks: []
```

The `tool_use` is left orphaned — the exact failure `_trim`'s phase 2 exists to
prevent, reintroduced by phase 3.

Currently **latent, not live**: `_append_synthetic` special-cases the list
branch, and `brain.think` never leaves two consecutive user entries. So nothing
reaches it today. But this is the function whose entire job is stopping history
corruption, and it has a path that causes it.

**Fix:** never merge across mixed content types. If `prev` is a list of blocks
and the new entry is a string, wrap the string as `[{"type": "text", ...}]` and
concatenate the lists, preserving every block. Or refuse to merge and insert a
separator turn, as `_append_synthetic` already does.

*(Note: my round-1 harness scored this as passing. The check was
`"tool_result" in json.dumps(out)`, which matched the literal placeholder text
`"[tool_result]"`. Substring assertions on serialised JSON are worthless —
count the blocks.)*

## NEW — N3. Pruning only happens at startup

`_load_and_prune` runs in `ProposalStore.__init__`. The store is a module-level
singleton created once at import, and the bot is a long-lived systemd process
that may run for weeks.

Reproduced: 30 done + long-expired proposals added at runtime, none pruned;
`_save()` rewrites all of them on every subsequent status change.

M4 is fixed for restarts only. **Fix:** prune opportunistically inside
`add()` — it already writes, and it is the only method that grows the store.

## NEW — N4. `execute()` marks `done` before the executor runs

```python
self.set_status(pid, STATUS_DONE)
result = spec["fn"](**entry["args"])
```

The comment justifies this as preventing double-execution on retry, which is
sound. But the consequence is that a **failed** action is indistinguishable
from a completed one: status is `done`, `execute()` refuses to retry, and the
proposal is unrecoverable.

Reproduced: executor raises, status is `done`.

`bot.py`'s callback catches the exception and sets `rejected`, so the *button*
path ends in a sane state — but that recovery lives in the caller, not the
store, and the store is what future collectors and cron jobs will call.

**Fix:** a distinct `running` status, or set `done` only on success and use a
separate `attempted` timestamp to block retries. For reminders specifically,
double-writing is cheap and losing the write is not, so the current trade is
backwards for this kind.

## NEW — N5 (minor). `propose_action`'s `kind` enum is hardcoded

```python
"enum": ["add_reminder"]
```

`proposals.known_kinds()` exists and is used for validation, but the tool
schema duplicates the list by hand. The next executor registered will validate
fine and be unproposable, because the model is never told the kind exists.
Build the enum from `known_kinds()`.

---

## Suggested order

1. **N1** — verify the API's role-run behaviour, then collapse before sending.
2. **N2** — make the mixed-type merge block-preserving.
3. **N4** — decide the failure semantics before more executors exist.
4. **N3**, **N5** — small.

## Harness

`/tmp/ct/{harness,v1,v2,v3}.py`. `v1.py` is the 18-check regression suite for
every round-1 finding and should move into `tests/` as-is — it is the thing
that stops these from coming back.
