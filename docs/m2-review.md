# M2 adversarial review — approval / proposals

Reviewed: `chad/proposals.py` (new), `chad/reminders.py` (new), `chad/brain.py`,
`chad/bot.py`. All findings below were **reproduced against the real modules**,
not inferred by reading. 16 findings: 2 critical, 3 high, 6 medium, 5 low.

**Verdict: do not deploy.** Two independent paths execute side-effecting
actions with no human approval at all. M2's entire premise is currently
enforced by prompt text, not by code.

What *does* hold: the path jail (`../`, absolute paths, non-`.md` all refused),
the memory.md protection from H1 (blocked on all three write tools), and the
H1 head-repair trim (leading orphaned `tool_result` correctly stripped).

---

## CRITICAL

### C1. `approve_pending` enforces nothing — the model can approve its own proposals

`ProposalStore.execute()` checks only that the proposal and its executor
exist. It does **not** check status, expiry, or chat. `_run_tool` exposes it
directly:

```python
if name == "approve_pending":
    return proposals.STORE.execute(args["pid"])
```

And `propose_action` **returns the pid to the model** in its tool result
("Proposal cea6e093 queued"). So within a single turn the model can propose an
action, read back the pid, and approve it — before `_flush_proposals` has even
run, i.e. before the user has seen a button.

Reproduced:

```
propose_action -> 'Proposal cea6e093 queued. ... Wait for their approval.'
   message_id = None          (buttons not yet sent)
approve_pending -> 'Reminder set for 2026-12-25: SAME-TURN SELF APPROVED'
```

Every gate fails the same way:

| Attack | Blocked? |
|---|---|
| Propose + approve in the same turn, user never sees buttons | **no** |
| Approve a proposal the user explicitly tapped **No** on | **no** |
| Approve an expired (>24h) proposal | **no** |
| Approve a proposal belonging to a different chat_id | **no** |
| Execute the same pid twice (two reminder lines written) | **no** |

The `chat_id` parameter was threaded through `_run_tool` specifically for this
and is never used by `approve_pending` / `reject_pending`.

This matters far beyond a stray reminder. It is the prompt-injection
escalation path: at Rung 3, untrusted email text saying *"propose and approve
X"* reaches an agent that is technically able to comply. The only thing
stopping it is a sentence in the system prompt.

**Fix:**

1. `execute()` must assert `status == pending`, `expires > now`, and a
   caller-supplied `chat_id` match. Raise otherwise.
2. Require `message_id is not None` before a model-initiated approval. This is
   the cheap, load-bearing check: `message_id` is only set once
   `_flush_proposals` has actually delivered the buttons, so it is direct
   evidence the human saw the preview. It makes same-turn self-approval
   structurally impossible.
3. Consider dropping `approve_pending` from `TOOLS` entirely for M2. The
   button path is complete and safe; the typed-"yes" path is a convenience
   that currently costs the whole security model.

### C2. `reminders.md` is unprotected — the approval flow is optional

`add_reminder` is deliberately not exposed as a tool, and the docstring says
"the only path to write here is through the propose_action -> approval ->
executor flow."

It is not. `reminders.md` is an ordinary `.md` at the vault root and is absent
from `_PROTECTED_NOTES`:

```
append_note("reminders.md", "2099-01-01 | WROTE DIRECTLY, NO APPROVAL")
-> 'Appended to reminders.md.'
```

The H1 hardening added `_PROTECTED_NOTES` and even anticipated this in a
comment — *"(map.md at Rung 3, reminders.md if it gets structured)"* — but the
set still contains only `memory.md`.

Generalise: **any file with a dedicated guarded write path must be in
`_PROTECTED_NOTES` at the moment that path is created.** Adding an executor
without adding the protection leaves the guard as decoration.

**Fix:** `_PROTECTED_NOTES = frozenset({"memory.md", "reminders.md"})`, and
make it a checklist item for every future executor.

---

## HIGH

### H1. The preview and the payload are independent fields

`proposals.py`'s docstring states the design principle:

> Preview and execution must be the same serialized object.

They are not. `propose_action(kind, args, summary)` takes `summary` (shown to
the user) and `args` (what runs) as two unrelated model-supplied strings.
Nothing checks they agree:

```
user sees: 'Add reminder for Fri 2026-08-07: submit COMP2000 assignment'
what runs: {'date': '2030-01-01', 'text': 'TRANSFER RENT TO STRANGER'}
```

Locking execution to the stored args is worth much less when the human
approved a different sentence. Today the impact is limited (reminders only),
but the shape carries forward to calendar writes and email sends.

**Fix:** derive the summary server-side from `kind` + `args` via a per-kind
formatter. The model supplies intent; the code renders the preview. Then the
displayed string and the executed object are provably the same data.

### H2. `_append_synthetic` produces consecutive user messages

`_append_synthetic` appends `{"role": "user", ...}` after a turn that already
ended with an assistant message — correct. But two taps in a row, or a tap
followed by a typed message, yields consecutive `user` entries. Reproduced:

```
roles: ['user', 'assistant', 'user', 'user', 'user']
2 consecutive same-role pairs, persisted to disk
```

The Anthropic Messages API expects alternating roles. If it rejects this (very
likely — **verify with a 5-line script before deploying**), the failure mode is
the one H1 just fixed for `tool_result`: the broken list is saved by
`_history.set`, reloaded on every subsequent message, and rejected every time.
Restarting does not help. The bot is bricked until `history.json` is deleted
by hand.

**Fix:** in `_append_synthetic`, if the last entry is already `role: user`,
append the marker to that entry's content instead of adding a new message.
Better: extend `_trim`'s repair phase to collapse consecutive same-role
entries, so the invariant is enforced in one place rather than at every call
site.

### H3. One malformed proposal entry takes the whole bot down

`pending_for_chat` indexes directly:

```python
if p["chat_id"] == chat_id and p["status"] == ... and p["expires"] > now
```

A single entry missing a key — schema change, hand edit, an older-format file
— raises `KeyError`. That call sits inside `_build_system_prompt`, which runs
on **every** message, so the bot fails on every message until someone deletes
`proposals.json`.

Reproduced: `KeyError: 'chat_id'`.

`history.py` is defensive about exactly this. `proposals.py` is not.

**Fix:** `p.get("chat_id")`, `p.get("status")`, `p.get("expires", 0)`, and skip
entries that fail a shape check with a log line.

---

## MEDIUM

### M1. Proposal args are validated only after the user approves

`propose_action` accepts any `args` object. Reproduced: `{"totally": "wrong"}`
queues fine; the failure appears at `fn(**args)` — i.e. **after** the user has
tapped Yes:

```
TypeError: add_reminder() got an unexpected keyword argument 'totally'
```

The user gets "Failed" for something they were shown as valid.

**Fix:** validate args against a per-kind schema inside `propose_action`, so a
malformed proposal never reaches the human.

### M2. The `ed` (Edit) flow does not do what its comment claims

bot.py:173-176 says the next message lands in the normal handler where *"Chad
sees the still-visible proposal in its `<pending_approvals>` block."*

It does not. `pending_for_chat` filters `status == "pending"`, and the Edit
handler sets `status = "editing"`. Reproduced: the pid disappears from
`<pending_approvals>` the moment Edit is tapped.

So the proposal ends up in a dead state — invisible to the model for context,
keyboard stripped so no button can reach it, no transition back to `pending`
or `rejected` anywhere in the codebase — **yet still executable by pid**
through the C1 hole. It lingers as `editing` forever.

**Fix:** include `editing` in the injected block (tagged as such), and give the
state an exit — either the next turn re-proposes and rejects the old one, or a
timeout returns it to `pending`.

### M3. Corrupt `proposals.json` is destroyed, not preserved

`ProposalStore._load` is a copy of the **pre-H1** `history.py`: it logs and
returns `{}`, leaving the broken file in place for the next `_save()` to
`os.replace` over. `history.py` was fixed to rename it aside with a timestamp;
`proposals.py` did not inherit the fix.

Reproduced: no `*.corrupt.*` file created.

**Fix:** copy the current `history.py` behaviour. Better, factor the
load/atomic-save/preserve logic into one shared `JsonStore` — the two classes
are now the same object with different payloads, and they have already drifted
once.

### M4. Nothing prunes the proposal store

`done`, `rejected` and long-expired proposals stay in `_data` forever, and
`_save()` rewrites the entire file on every status change. Reproduced: 50
done+expired proposals, none removed.

**Fix:** drop terminal proposals older than ~7 days during `_load`.

### M5. `reminders.py` writes to the vault without going through `vault.py`

```python
path = config.VAULT_PATH / REMINDERS_FILENAME
path.open("a")
```

`vault.py`'s docstring claims it is *"the ONLY module that touches the
filesystem."* That is no longer true. No `_safe_path`, no backup, no protected
check. The filename is a hardcoded constant so there is no traversal risk
today, but the invariant that made the jail auditable — one module, one gate —
is broken, and the next executor will copy this pattern.

**Fix:** route through a `vault.append_line(note_name, line)` helper that
bypasses `_assert_writable` internally, exactly as `write_note_raw` does for
memory.

### M6. `ProposalStore.get()` hands out the live dict

`return self._data.get(pid)` — no copy. Reproduced: mutating the returned
dict's `args` rewrites the stored proposal in place.

bot.py holds `p` across `await` boundaries and reads `p["summary"]` after
`execute()` has mutated status. Nothing exploits it today; it is the same
class of leak already documented for `HistoryStore.get()`, and here the object
being handed out is the one that defines what executes.

---

## LOW

### L1. `add_reminder` accepts impossible dates

`_DATE_RE` checks shape, not validity. `9999-99-99` is accepted and written.
Use `datetime.date.fromisoformat()` instead.

### L2. Reminder text can inject the field separator

Newlines are collapsed but `|` is not escaped:

```
2026-05-05 | pay rent | 2020-01-01 | INJECTED SECOND FIELD
```

M6's parser will mis-split this. Fix now while the file is nearly empty — split
on the *first* `|` only, and reject or escape `|` in the text.

### L3. `brain.py` module docstring is stale

Still says *"Rung 1's tools are read-only-ish (list, read, append, create) so no
approval step yet — that arrives the moment a tool has consequences outside the
vault."* That moment is this commit.

### L4. `ProposalStore._save` does not use `_to_plain`

`args` currently arrives as a plain dict from `block.input`, so this works. If
any future kind stores a non-JSON-serialisable value, `_save()` raises **after**
`_data` was mutated, leaving memory and disk out of sync.

### L5. Executor failure is handled inconsistently across the two approval paths

The callback path catches, logs, and marks the proposal `rejected`. The
`approve_pending` path returns `"Error: ..."` to the model and leaves it
`pending` — so the buttons stay live and it can be retried. Pick one.

---

## Suggested order

1. **C1** — status/expiry/chat checks in `execute()`, plus the
   `message_id is not None` requirement. Consider removing `approve_pending`
   from `TOOLS` for now.
2. **C2** — add `reminders.md` to `_PROTECTED_NOTES`.
3. **H2** — verify the consecutive-user-message behaviour against the real API
   first; fix in `_trim` if confirmed.
4. **H3, M3** — defensive reads and corrupt-file preservation; ideally by
   extracting the shared `JsonStore`.
5. **H1** — server-derived summaries, before more executors exist.
6. The rest.

## Test harness

The reproductions live at `/tmp/ct/` (`harness.py`, `t1`–`t4.py`). They stub
the `anthropic` client so they need no API key or network. Worth moving into
`tests/` and keeping — every finding above is a regression test you do not
currently have.
