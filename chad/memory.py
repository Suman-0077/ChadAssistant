"""Chad's persistent long-term memory: memory.md.

memory.md is the file that gets auto-injected into every system prompt.
It's how Chad remembers preferences and facts across sessions. Two
invariants matter, and this module enforces both:

  1. FIXED SECTION SCHEMA. The extractor (M4) will target these headings
     by name; free-form structure means it has to guess and it will drift.
     Writes to any other section name are refused.

  2. HARD TOKEN CAP. Without a cap, memory grows without bound, prompt
     caching (M8) becomes worthless, and per-request cost grows linearly
     with age. The cap must be a code check, not a note in the system
     prompt — the model can't be trusted to enforce its own budget.

Everything else in the memory system (extractor, consolidation, audit
log) will build on this module.
"""

import logging

from chad import audit, config, vault

log = logging.getLogger("chad.memory")

MEMORY_FILENAME = "memory.md"

# The fixed section schema. Order matters — it's what the bootstrap
# template uses, and it's the order sections appear in the injected
# system prompt.
SECTIONS: tuple[str, ...] = (
    "Identity",
    "Preferences",
    "Ongoing",
    "Decisions",
    "Archive",
)

# Token cap. We use a rough char-count estimate (~4 chars per token) rather
# than a real tokenizer. Reasons:
#   - No extra dependency to install, no network call per write.
#   - Consistent even across model changes.
#   - Slightly conservative (English text averages 3.5-4 chars/token, so
#     len(text)//4 tends to under-count). Fine for a cap check.
# The cap is generous — 2000 tokens is far more than a curated memory
# file will ever legitimately need.
TOKEN_CAP = 2000
CHARS_PER_TOKEN = 4
CHAR_CAP = TOKEN_CAP * CHARS_PER_TOKEN

INITIAL_CONTENT = """# Chad's memory

This is Chad's persistent long-term memory. It is auto-injected into the
system prompt on every request. Keep it short — distilled facts and
preferences, not conversation transcripts. Chad updates it via the
update_memory tool.

## Identity

Stable facts about the user: name, program, year, employer, location, timezone.

## Preferences

How Chad should behave: tone, notification timing, formatting choices,
things to never do.

## Ongoing

Live commitments with an end in sight: current courses, active projects,
recurring shifts. Items here are pruned by consolidation once they end.

## Decisions

Conclusions reached that should not be relitigated, with dates.

## Archive

Pointers to archive/YYYY-MM.md files, one line each, so Chad knows what
older context exists and can fetch it via read_note when needed.
"""


class CapExceeded(Exception):
    """Raised when a memory write would push memory.md over the token cap.

    Callers should surface this to the user and trigger consolidation
    (M9). Distinct from VaultError so callers can react specifically.
    """


def _estimated_tokens(text: str) -> int:
    return len(text) // CHARS_PER_TOKEN


def ensure_exists() -> None:
    """Create memory.md with the fixed schema if it doesn't already exist.

    Called from bot startup so a fresh vault comes online with a valid
    memory file — no separate bootstrap step for the user to remember.
    Locked so a concurrent startup + extractor can't both bootstrap.
    """
    path = config.VAULT_PATH / MEMORY_FILENAME
    if path.exists():
        return
    with vault.lock(MEMORY_FILENAME):
        # Re-check inside the lock — another writer may have created it
        # while we waited.
        if path.exists():
            return
        vault.write_note_raw(MEMORY_FILENAME, INITIAL_CONTENT)
        audit.log_memory_write("bootstrap", "*", INITIAL_CONTENT)
        log.info("Bootstrapped memory.md with fixed section schema at %s", path)


def read_section_body(section: str) -> str:
    """Return just the body of a memory.md section (no heading, no framing).

    Used by the M4 extractor for its dedupe check. Empty string if the
    file doesn't exist or the section is empty. Raises VaultError if
    section isn't in the fixed schema.
    """
    if section not in SECTIONS:
        raise vault.VaultError(
            f"'{section}' is not a valid memory section. "
            f"Use one of: {', '.join(SECTIONS)}."
        )
    path = config.VAULT_PATH / MEMORY_FILENAME
    if not path.is_file():
        return ""

    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    heading_re = vault._HEADING_RE
    # Find matching heading, extract body between it and the next
    # heading of same/higher level.
    headings = []
    for i, line in enumerate(lines):
        m = heading_re.match(line)
        if m:
            headings.append((i, len(m.group(1)), m.group(2).strip()))

    for idx, (i, level, name) in enumerate(headings):
        if name != section:
            continue
        end = len(lines)
        for j, l, _ in headings[idx + 1:]:
            if l <= level:
                end = j
                break
        return "\n".join(lines[i + 1:end]).strip()

    return ""


def _write_section_locked(section: str, new_body: str, source: str) -> str:
    """The write half of write_section, assuming the caller holds vault.lock.

    Used by write_section directly and by the M4 extractor apply loop,
    which needs to hold the lock across its own read + dedupe + write.
    """
    if section not in SECTIONS:
        raise vault.VaultError(
            f"'{section}' is not a valid memory section. "
            f"Use one of: {', '.join(SECTIONS)}."
        )

    path = config.VAULT_PATH / MEMORY_FILENAME
    if not path.is_file():
        # Cannot recursively call ensure_exists (nested lock). Since
        # we're inside the lock, bootstrap here directly.
        vault.write_note_raw(MEMORY_FILENAME, INITIAL_CONTENT)
        audit.log_memory_write("bootstrap", "*", INITIAL_CONTENT)

    current = path.read_text(encoding="utf-8")
    proposed = vault.compute_section_edit(current, section, new_body)

    if len(proposed) > CHAR_CAP:
        raise CapExceeded(
            f"Refused write to memory.md: file would grow to "
            f"~{_estimated_tokens(proposed)} tokens (cap: {TOKEN_CAP}). "
            f"Consolidation needed before further additions."
        )

    vault.write_note_raw(MEMORY_FILENAME, proposed)
    audit.log_memory_write(source, section, new_body)
    return (
        f"Updated memory.md section '{section}' "
        f"(~{_estimated_tokens(proposed)}/{TOKEN_CAP} tokens used)."
    )


def write_section(section: str, new_body: str, *, source: str = "inline") -> str:
    """Replace the body of a memory.md section, enforcing schema + cap.

    Atomic — holds vault.lock(MEMORY_FILENAME) across the read-compute-write
    cycle so concurrent writers can't lose each other's changes.

    Raises:
      vault.VaultError — section name isn't in the fixed schema, or the
        underlying vault operation failed.
      CapExceeded — the resulting file would exceed the token cap. The
        write is NOT applied; consolidation must run first.
    """
    with vault.lock(MEMORY_FILENAME):
        return _write_section_locked(section, new_body, source)
