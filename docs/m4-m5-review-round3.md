# M4 + M5 review — round 3

Re-test of the three round-2 findings, plus a pass attacking the fixes and a
regression sweep. Commit `cafdbdc`.

**All three fixed. 36/36 on the M2 suite, all modules compile, 18/18 on the
targeted re-tests.** No blocking findings. One cosmetic gap, and two flags from
my own harness that turned out not to be bugs — detailed below because the
reasoning matters more than the result.

---

## Verified fixed

| Finding | Check | Result |
|---|---|---|
| N2 | `_last_processed` deleted; `run()` takes explicit new turns | pass |
| N2 | Extraction still runs after history is trimmed | pass |
| N2 | Keeps working across repeated trims | pass |
| N1 | `1.` / `2)` / `10.` numbered-list imperatives dropped | pass |
| N1 | `-` / `*` / `>` prefixes still dropped | pass |
| N1 | Legitimate declarative facts still kept | pass |
| M1 | `_apply` refuses multi-line content on its own | pass |
| M1 | `_apply` refuses heading-shaped content | pass |
| M1 | `_apply` refuses over-length content | pass |
| M1 | Refusals are audited with `[refused: …]` | pass |
| M1 | `memory.md` still writable after all refusal attempts | pass |
| C1 | Regression: no `tool_result` or assistant text in the extractor prompt | pass |
| M2 | Regression: nested lock raises; 4 concurrent writers all complete, no interleaving | pass |

**N2's fix is the right one.** Killing the cursor entirely and having `bot.py`
pass `history[turns_before:]` removes the shared mutable state rather than
patching around it. `turns_before` is captured before `brain.think()`, and
`_history.set()` doesn't mutate the caller's list, so the slice stays correct.
I verified that specifically — it's the assumption the whole fix rests on.

`_LEADING_PUNCT = r"^[\s\W]*(?:\d+[.)]\s*)?[\s\W]*"` closes the digit gap
cleanly.

---

## Two harness flags that were NOT bugs

Recording these because a false positive that looks like a finding is worse
than no finding at all.

**"`_trim` mutates the caller's list."** It doesn't. My test appended three
consecutive `role: user` messages, which `_collapse_consecutive_same_role`
correctly merged into one — so the stored list was shorter than I'd asserted.
The collapse behaviour is right; the assertion was wrong. Confirmed separately:
`len(h)` is unchanged across `H.set(CID, h)`.

**"Approval markers become durable facts."** Not reachable. `_append_synthetic`
runs in the *callback* handler, which never invokes the extractor. The next
message computes `turns_before` after the marker is already in history, so the
slice starts past it. Verified end to end — the prompt for the turn following a
button tap contains only `[user] thanks`.

The general lesson from both: an assertion about a data structure is only as
good as the model of the flow behind it. Worth re-deriving the flow before
believing a red result.

---

## LOW — L3. `validate_edits` would accept an internal marker as a fact

```python
content = "[approved proposal ab12: Add reminder for Friday]"
validate_edits(...)  ->  kept
```

`[` is stripped by `_LEADING_PUNCT`, and `approved` isn't in the imperative verb
list, so this passes every check and would be written verbatim into `memory.md`
as a durable fact about the user.

Not reachable today (see above). It becomes reachable the moment anything
extracts from stored history rather than an explicit slice — a consolidation
job, a backfill script, a "re-extract the last week" utility. All plausible
next steps.

**Fix:** reject content starting with `[` in `_content_shape_error`. One line,
and it's the same category as the `#` check already there — refusing content
shaped like the system's own internal syntax.

---

## Still open (unchanged, all deferred by choice)

- **H1** — the imperative filter is lexical and misses declarative phrasing.
  Substantially defanged by C1 now that only user-typed text is extracted.
- **L1** — `extractor.run` blocks the event loop; roughly doubles per-message
  loop time. Matters at Rung 3 when collectors share the loop.
- **L2** — `config.py` uses `print()` rather than the logger for the
  `EXTRACTOR_MODE` warning, so a typo lands on stdout instead of the journal.

---

## Verdict

Ship it, in shadow mode. The injection boundary is structural, the locking is
sound under concurrency, and the extractor now keeps running for the life of the
conversation — which is what makes the shadow period actually mean something.

Before flipping `EXTRACTOR_MODE=live`, the evidence to collect is
`memory-audit.log`: several weeks of `[would add]` entries. The question to ask
of it isn't "did it crash" but **"would I have written these myself?"** If
there's anything in there you'd have deleted, the extractor isn't ready — and
that judgement is the only thing shadow mode can give you that testing can't.
