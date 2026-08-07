# M6 review — round 2

Re-test of all 10 findings from `m6-review.md`, plus a pass attacking the
fixes. Commit `e43f730`.

**All 10 fixed and verified.** M2 suite still 36/36. Two new findings, one of
which causes a reminder to notify you every single morning, forever.

---

## Verified fixed

| Finding | Check | Result |
|---|---|---|
| C1 | `ensure_exists()` works on an empty vault; schema written; bootstrap audited | pass |
| C1 | `memory.md` still refused by Chad's own tools afterwards | pass |
| M1 | Splitter emits no empty chunk — leading/multiple/only/trailing newlines, empty, at-limit, limit+1 | pass |
| M2 | Delivered reminders marked immediately; failed one stays pending; later ones still attempted | pass |
| M3 | Reminder appended mid-scan survives; scanned one still marked | pass |
| M4 | Cron writes via `vault.write_note_raw`; backup taken before rewrite | pass |
| L1 | `Requires=` gone from timer, `Wants=` gone from service | pass |
| L2 | `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=strict`, `ProtectHome`, `ReadWritePaths` | pass |
| L3 | `OnCalendar=*-*-* 07:00:00 Australia/Sydney` — no longer depends on server clock | pass |
| L4 | Dot-prefixed segments refused by `_safe_path` / `_safe_dir` (`.chad-state/`, `.chad-backups/`, `.obsidian/`, nested) | pass |

The M3 fix is the good one. Switching `_find_due` to return *content* rather
than indices, and re-reading inside `_mark_one_done` to match by line text,
removes the lost-write race without needing file locking at all. The comment
explaining why indices are unstable is worth keeping.

---

## NEW — N1. A reminder whose separator spacing differs re-fires forever

`_find_due` parses with a permissive regex:

```python
_LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\s*\|\s*(.+?)\s*$")
```

`_mark_one_done` then searches for an exact reconstruction:

```python
target = f"{date_str} | {text}"
if raw.strip() == target:
```

The reader accepts any whitespace around the `|`; the writer only recognises
exactly one space either side. Anything else parses, sends, and **never
matches**, so the line is never marked `[done]`.

Reproduced across the full matrix:

| Separator | Fires | Marked done |
|---|---|---|
| `2026-08-07 \| task` | yes | yes |
| `2026-08-07\|task` | yes | **no** |
| `2026-08-07  \|  task` | yes | **no** |
| `2026-08-07 \|task` | yes | **no** |
| `2026-08-07\| task` | yes | **no** |

The result isn't a missed reminder — it's the opposite. The reminder is
delivered, stays pending, and is delivered again the next morning, and every
morning after that. There's a `log.warning("Could not find %r ...")` in the
journal, but nothing surfaces in Telegram, so the only visible symptom is a
reminder that will not go away.

**Why this will actually happen:** `reminders.md` is a markdown file at the
vault root, and the entire premise of the vault is that you edit it in
Obsidian. Re-typing a line, or an editor normalising whitespace, is enough.
It's also the first thing to go wrong if a future writer emits a different
separator.

**Fix:** don't reconstruct the line. Have `_find_due` return the raw line (or
its index *and* the raw text) and match on that, or normalise both sides
through the same regex before comparing. One parser, one representation.

## NEW — N2. Simultaneous rewrites collapse into a single backup

`_backup()` names files with second precision:

```python
ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
```

The M2 fix made the cron rewrite the file once **per reminder**. Five
reminders firing at 07:00 complete well within the same second, so all five
backups get the same filename and overwrite each other.

Reproduced: 5 rewrites produced **1** backup file.

The reminders themselves were all delivered and marked correctly — this only
costs recovery depth. But `.chad-backups/` is the safety net for exactly the
case where a rewrite loop goes wrong, and right now a five-step loop leaves one
step of history.

**Fix:** add sub-second precision (`%H%M%S%fZ`), or a short uuid suffix, or
have `_backup` skip if an identical backup already exists rather than
overwriting.

---

## Also worth noting

**Backups are now written per reminder.** Each `_mark_one_done` is a full
read-modify-write plus a backup. Correct, and fine at ten reminders a day. If
the file ever grows to hundreds of lines it becomes O(n²) work on a cron that
has all morning to run, so not urgent — just know it's there.

**`ProtectHome=read-only` plus `ReadWritePaths=/root/vault`** is right, and
`ReadWritePaths` does override `ProtectHome` for that subtree. Two things to
confirm on the first real run:

- `STATE_DIR` defaults to `VAULT_PATH/.chad-state`, so it's covered — but if
  you ever set `STATE_DIR` elsewhere in `.env`, add it to `ReadWritePaths`.
- `/root/chad` is now read-only for this unit, so Python can't write
  `__pycache__`. Harmless (it silently skips bytecode caching), but it'll show
  up as a marginally slower start.

**`OnCalendar` with a per-line timezone needs systemd 247+.** Check with
`systemctl --version` before relying on it; on older versions the timezone
suffix is a parse error and the timer won't load at all. `systemctl
list-timers chad-morning` after install will tell you immediately.

---

## Order

1. **N1** — a reminder that never stops arriving is worse than one that never
   arrives, because there's no obvious way to make it stop.
2. **N2** — small, and it protects the thing that protects you.
