# M6 review — audit log, morning cron, /memory command

Reviewed: `chad/audit.py` (new), `chad/cron/morning_reminders.py` (new),
`deploy/systemd/*` (new), `chad/bot.py` (`/memory`), `chad/memory.py` (audit
hooks). Findings reproduced against the real modules.

**1 critical, 4 medium, 5 low.** The critical one is a startup crash that
predates this batch — it arrived with H1 hardening and has been latent since,
because it only fires on a vault that has no `memory.md` yet.

---

## What these changes are

**`audit.py`** — an append-only JSON-lines log of every write to `memory.md`,
at `STATE_DIR/memory-audit.log`. One object per line: timestamp, source
(`bootstrap` / `inline` / later `extractor`, `consolidation`), section,
body length, full body. `memory.py` now calls it from both `ensure_exists()`
and `write_section()`. It deliberately stores no "before" state, because
`vault._backup()` already keeps pre-edit copies in `.chad-backups/` — the log
answers *what changed and why*, the backups answer *what it looked like
before*. It never raises: a broken audit log must not fail a real write.

This is the groundwork for M4's extractor. Once Chad starts deciding on its
own what to remember, "why does it think that?" needs an answer that isn't
guesswork.

**`cron/morning_reminders.py`** — the read half of the reminders feature.
M2 built the write path (propose → approve → `add_reminder` appends a line);
this is the systemd oneshot that fires at 07:00, finds lines dated today or
earlier, sends each over Telegram, and prefixes them `[done] ` so they don't
repeat. **Zero AI in this path** — it is a regex, a date comparison, and a
send. Nothing here can hallucinate, and it costs nothing per run.

**`deploy/systemd/`** — the `.service` (oneshot) and `.timer` (daily 07:00,
`Persistent=true` so a missed run fires at next boot) plus install docs.
Keeping unit files in the repo so they version with the code they run is the
right instinct.

**`/memory` command** — dumps `memory.md` straight to the chat, chunked under
Telegram's 4096-char limit. Notably it does *not* go through `brain.think()`:
asking the model to tell you what it remembers costs an API call and gives you
the model's paraphrase. Reading the file gives you the truth.

---

## CRITICAL

### C1. The bot cannot start on a fresh vault

`bot.main()` opens with `memory.ensure_exists()`. That calls
`vault.create_note("memory.md", ...)`. Since H1 added `memory.md` to
`_PROTECTED_NOTES`, `create_note` refuses it:

```
VaultError: memory.md is a protected note and cannot be modified via
create_note. Use update_memory instead.
```

The exception propagates out of `main()` and the process dies before
`run_polling()`.

Reproduced on an empty vault. **Existing deployments are unaffected** —
`ensure_exists()` returns early when the file is present, which is why this has
gone unnoticed since commit `fe4836f`. It fires only on a fresh install, a
restored-from-backup vault, or if `memory.md` is ever deleted. That is exactly
the moment you least want the bot refusing to boot, and the error message
points at a tool that isn't the problem.

**Fix:** `ensure_exists()` should use `vault.write_note_raw()` — the documented
escape hatch for guarded writers that have done their own checks — as
`memory.write_section()` already does.

**Also worth adding:** a regression test that boots against an empty vault. The
existing suite never calls `ensure_exists()`, which is why 36/36 passed with
this sitting in the startup path. A test suite that only exercises the steady
state will keep missing first-run bugs.

---

## MEDIUM

### M1. `/memory` can emit an empty chunk and 400

`_split_for_telegram` calls `rfind("\n", 0, limit)`, which returns `0` when the
text *starts* with a newline — producing an empty first chunk:

```
_split_for_telegram("\n" + "a"*5000)  -> chunk lengths [0, 4000, 1000]
                                         first chunk = ''
```

`reply_text("")` is rejected by Telegram (`message text must be non-empty`), so
`/memory` fails on any `memory.md` beginning with a blank line and longer than
4000 chars.

**Fix:** skip empty chunks before sending, or `lstrip("\n")` the input up
front. One line either way.

*(Minor, same function: the docstring says the limit leaves "headroom for
code-fence markers", but no code fences are ever added. Either wrap the output
in them or drop the sentence.)*

### M2. The cron marks reminders done all-at-once, after every send

`_mark_done` + `_save_lines_atomic` run only after the full send loop. If any
send fails partway, the exception escapes `main()` and **nothing is marked**.

Reproduced with a bot that fails on the second of three sends: reminder "one"
was delivered, the file still lists it as pending, and it re-fires on the next
run. The user gets it twice.

Duplicates are the better failure direction than silence, so this isn't
severe — but it's free to fix: mark and save after each successful send, so
delivered items are never re-sent.

### M3. The documented race silently destroys an approved reminder

The module docstring flags the read-then-rewrite race and defers it to M5's
file locking. Worth being precise about the consequence, because "a race
exists" undersells it.

Reproduced: cron calls `_load_lines()`, the bot appends an approved reminder,
cron writes back its stale snapshot. The new reminder is **gone**. Not delayed
— gone. The user tapped Yes, Chad replied "Reminder set for...", and the write
was destroyed with no error anywhere.

The window is a few seconds a day, so it will rarely fire. But the failure is
silent data loss of a *user-approved action*, in the one subsystem whose entire
premise is that approved actions actually happen.

**Cheap interim fix** (no locking needed): re-read the file immediately before
writing, and apply the `[done]` prefixes by matching line content rather than
by index. Any lines that appeared in between survive.

### M4. The cron writes `reminders.md` outside `vault.py`

```python
REMINDERS_PATH = config.VAULT_PATH / "reminders.md"
tmp.write_text(...); tmp.replace(REMINDERS_PATH)
```

No `_safe_path`, no `_assert_writable`, no `_backup`. `vault.py` still claims
to be *"the ONLY module that touches the filesystem."*

This is the exact finding M5 of the M2 review raised about `reminders.py`, which
was fixed by routing through `vault.append_line_raw()`. The next module wrote
its own file access anyway — which is what that finding predicted would happen.

Two consequences beyond tidiness:

- **No backup before a wholesale rewrite.** `edit_section` backs up before
  replacing one *section*; this replaces the entire file with none. A bug in
  `_mark_done` or `_save_lines_atomic` loses every reminder with no recovery
  copy. Confirmed: `.chad-backups/` contains nothing for `reminders.md`.
- `reminders.md` is in `_PROTECTED_NOTES`, so the guard reads as stronger than
  it is. The protection covers Chad's tools, not Chad's own codebase.

**Fix:** add `vault.replace_note_raw(note_name, content)` alongside
`write_note_raw` — backup, jail, atomic replace — and call it from the cron.

---

## LOW

### L1. `Requires=` in the timer will fire the service on enable

```ini
[Unit]
Requires=chad-morning.service
```

In a `.timer`, `Requires=` is a *start-time* dependency: starting the timer
starts the service. So `systemctl enable --now chad-morning.timer` — the exact
command in `deploy/README.md` — is likely to send every due reminder
immediately, at whatever hour you install it.

`Unit=chad-morning.service` in `[Timer]` is already the correct and sufficient
link. Drop the `Requires=`.

Same shape in the `.service`: `Wants=chad-morning.timer` under `[Unit]` means
running the service pulls in the timer. A timer-triggered oneshot shouldn't
reference its timer at all.

### L2. `User=root` with no sandboxing

The unit runs as root with none of the usual hardening. Your own plan §8 lists
least privilege as a day-one concern. A new unit is the cheapest place to start:

```ini
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/root/vault /root/chad
```

The script needs the vault, the venv, and outbound network — nothing else.

### L3. Timezone depends on the server clock, and the fallback is silent

`OnCalendar=*-*-* 07:00:00` uses the system timezone. `config.USER_TIMEZONE` is
independent. On a default-UTC VPS the timer fires at 07:00 UTC — 17:00 or 18:00
in Sydney — while the script correctly computes "today" in Sydney. Reminders
arrive, just in the evening, and nothing warns you.

The comment already names the fix. Use it:
`OnCalendar=*-*-* 07:00:00 Australia/Sydney` (systemd 247+).

### L4. Chad can create files inside `.chad-state/` and `.chad-backups/`

`_safe_path` permits any `.md` under the vault, including dot-directories:

```
create_note('.chad-state/planted.md')   -> 'Created .chad-state/planted.md.'
create_note('.chad-backups/planted.md') -> 'Created .chad-backups/planted.md.'
```

`list_notes` hides these, so Chad can write where it cannot read back. No
current impact — `history.json`, `proposals.json` and `memory-audit.log` are
all non-`.md` and therefore unreachable — but Chad's own operational state
directory shouldn't be writable by Chad at all. Refuse dot-prefixed path
segments in `_safe_path`.

### L5. The audit log is unbounded and stores full bodies

Every `update_memory` writes the complete new section body. With a 2000-token
cap on `memory.md`, a chatty extractor could add megabytes per year. The
docstring says "never rotated automatically", so this is a choice rather than
an oversight — but rotation should exist before M4's extractor starts writing
unattended.

**Verified good:** `json.dumps` escapes newlines, so a body containing a forged
JSON object cannot inject a fake log entry. Tested explicitly.

---

## What held up

- Cron core logic: due-today fires, future doesn't, overdue is labelled
  "Overdue", `[done]` lines never re-fire, second run is a no-op.
- Malformed lines (bad dates, junk text) are skipped with a warning and
  **preserved**, not silently deleted.
- Audit log records both sources correctly, is unreachable from every Chad
  tool, hidden from `list_notes`, and immune to newline-injection forgery.
- `/memory` splitting keeps every chunk under the limit and loses no content.
- Not routing `/memory` through the LLM is the right call.

---

## Order

1. **C1** — one-line fix, and add an empty-vault boot test.
2. **L1** — will misfire the moment you install the timer.
3. **M3**, **M2** — reminder loss and duplicates.
4. **M4** — while there are only two writers to fix.
5. **L3**, **L2**, **M1**, **L4**, **L5**.
