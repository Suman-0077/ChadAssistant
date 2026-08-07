"""Vault access for Chad.

This is the ONLY module that touches the filesystem, and it enforces two
hard rules:

1. Path jail — every operation is confined to the vault folder. A note
   name like "../../etc/passwd" is rejected, not resolved.
2. No destructive operations — no delete, no wholesale overwrite. The
   worst a bug (or a manipulated model) can do is add text or edit a
   specific section, and every section-edit saves a timestamped backup
   first, so nothing is truly lost.

Every public function takes a note *name* relative to the vault root
(e.g. "inbox/2026-07-30.md"), never an absolute path.
"""

import fcntl
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from chad import config


# Notes that MUST go through their own guarded write path. General-purpose
# write tools (append_note, create_note, edit_section) all refuse these —
# so the schema/cap/format checks on protected files cannot be bypassed
# by picking a different tool. New guarded files MUST be added here at
# the moment their dedicated executor is created; the M2 review caught
# reminders.md missing.
_PROTECTED_NOTES = frozenset({
    "memory.md",       # guarded by memory.write_section (schema + token cap)
    "reminders.md",    # guarded by reminders.add_reminder via proposals only
})


class VaultError(Exception):
    """Raised for any invalid or unsafe vault operation.

    The message is safe to show to the model as a tool result — it
    explains what went wrong without leaking server paths.
    """


def _safe_path(note_name: str) -> Path:
    """Turn a relative note name into a vetted absolute path inside the vault.

    This is the jail. resolve() collapses any ".." segments and expands
    symlinks, and then we verify the result still lives under the vault
    root. If it doesn't, someone tried to escape.

    Also refuses any dot-prefixed path segment. Chad's operational state
    (.chad-state/, .chad-backups/, and Obsidian's .obsidian/, .git/)
    lives in dot-folders precisely because it shouldn't be user-visible
    or model-touchable. list_notes hides them; this makes read/write
    tools refuse them too.
    """
    if not note_name or note_name.startswith("/"):
        raise VaultError("Note name must be a relative path like 'notes/todo.md'.")

    candidate = (config.VAULT_PATH / note_name).resolve()

    # is_relative_to() answers: is candidate inside the vault folder?
    if not candidate.is_relative_to(config.VAULT_PATH):
        raise VaultError("Note name escapes the vault. Operation refused.")

    rel = candidate.relative_to(config.VAULT_PATH)
    if any(part.startswith(".") for part in rel.parts):
        raise VaultError(
            "Dot-prefixed folders (.chad-state, .chad-backups, .obsidian, "
            "etc.) are reserved and cannot be read or written."
        )

    if candidate.suffix != ".md":
        raise VaultError("Only .md files are allowed in the vault.")

    return candidate


# Human-readable pointer to the correct write path for each protected note.
# Used only in error messages — the actual routing is enforced by the set
# membership check above.
_GUARD_HINT = {
    "memory.md":    "update_memory",
    "reminders.md": "propose_action(kind='add_reminder', ...)",
}


def _assert_writable(note_name: str, tool_name: str) -> None:
    """Refuse writes to notes with dedicated guarded write paths.

    Every user-facing mutating tool (append_note, create_note, edit_section)
    calls this. The internal write_note_raw and append_line_raw do NOT —
    they're the escape hatches that guarded writers use to actually put
    bytes on disk after their own checks.
    """
    name = note_name.strip()
    if name in _PROTECTED_NOTES:
        hint = _GUARD_HINT.get(name, "its dedicated tool")
        raise VaultError(
            f"{name} is a protected note and cannot be modified via "
            f"{tool_name}. Use {hint} instead."
        )


def _safe_dir(folder: str) -> Path:
    """Same jail as _safe_path, but for a folder (no .md requirement).

    Also refuses dot-prefixed segments — same reasoning as _safe_path.
    """
    if folder.startswith("/"):
        raise VaultError("Folder must be a relative path like 'uni/comp2000'.")
    candidate = (config.VAULT_PATH / folder).resolve()
    if not candidate.is_relative_to(config.VAULT_PATH):
        raise VaultError("Folder escapes the vault. Operation refused.")
    rel = candidate.relative_to(config.VAULT_PATH)
    if any(part.startswith(".") for part in rel.parts):
        raise VaultError(
            "Dot-prefixed folders (.chad-state, .chad-backups, etc.) are "
            "reserved and cannot be listed or written."
        )
    if not candidate.is_dir():
        raise VaultError(f"Folder not found: {folder}")
    return candidate


def list_notes(folder: str = "") -> list[str]:
    """List markdown notes, relative to the vault root.

    With no argument, lists the whole vault. Pass a subfolder
    (e.g. "uni/comp2000") to list only that subtree — much cheaper
    in a large vault.
    """
    root = _safe_dir(folder) if folder else config.VAULT_PATH
    return sorted(
        str(p.relative_to(config.VAULT_PATH))
        for p in root.rglob("*.md")
        # Skip hidden folders like .git or .obsidian
        if not any(part.startswith(".") for part in p.relative_to(config.VAULT_PATH).parts)
    )


def read_note(note_name: str) -> str:
    """Return the full text of a note."""
    path = _safe_path(note_name)
    if not path.is_file():
        raise VaultError(f"Note not found: {note_name}")
    return path.read_text(encoding="utf-8")


def append_note(note_name: str, text: str) -> str:
    """Append text to the end of an existing note. Never overwrites."""
    _assert_writable(note_name, "append_note")
    path = _safe_path(note_name)
    if not path.is_file():
        raise VaultError(f"Note not found: {note_name}. Use create_note for new notes.")
    with path.open("a", encoding="utf-8") as f:
        # Ensure appended content starts on its own line.
        f.write("\n" + text.rstrip() + "\n")
    return f"Appended to {note_name}."


def create_note(note_name: str, text: str) -> str:
    """Create a new note. Refuses if the note already exists."""
    _assert_writable(note_name, "create_note")
    path = _safe_path(note_name)
    if path.exists():
        raise VaultError(f"Note already exists: {note_name}. Use append_note instead.")
    # Create parent folders as needed (e.g. "inbox/" for "inbox/monday.md").
    path.parent.mkdir(parents=True, exist_ok=True)
    # "x" mode = exclusive create: fails if the file appeared in the
    # meantime, so there is no window where we could clobber anything.
    with path.open("x", encoding="utf-8") as f:
        f.write(text.rstrip() + "\n")
    return f"Created {note_name}."


def append_to_inbox(text: str) -> str:
    """Append `text` to today's inbox note (inbox/YYYY-MM-DD.md).

    Computes today's date server-side in the user's timezone, so Chad
    doesn't have to know the date and can't drift onto the wrong day.
    Creates the note if it doesn't exist yet. Never touches other notes.
    """
    today = datetime.now(config.USER_TIMEZONE).date().isoformat()
    note_name = f"inbox/{today}.md"
    path = _safe_path(note_name)
    if path.is_file():
        with path.open("a", encoding="utf-8") as f:
            f.write("\n" + text.rstrip() + "\n")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as f:
            f.write(text.rstrip() + "\n")
    return f"Added to {note_name}."


# --- Section editing --------------------------------------------------------

_HEADING_RE = re.compile(r"^(#+)\s+(.+?)\s*$")


def _backup(path: Path) -> None:
    """Save a timestamped copy of a note before modifying it.

    Backups live in .chad-backups/ inside the vault. The folder starts
    with a dot so list_notes() skips it, and Obsidian typically hides
    dot-folders too. Once git auto-commit is set up this becomes a
    belt-and-suspenders redundancy, which is fine.
    """
    backup_dir = config.VAULT_PATH / ".chad-backups"
    backup_dir.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rel = path.relative_to(config.VAULT_PATH).as_posix().replace("/", "__")
    backup_path = backup_dir / f"{rel}.{ts}.bak"
    backup_path.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")


def compute_section_edit(text: str, section_heading: str, new_body: str) -> str:
    """Pure function: return what `text` would become after editing a section.

    Split out so callers (memory.write_section) can check the resulting
    size against a cap BEFORE the edit is committed to disk. Used by
    edit_section internally too.

    Raises VaultError with the same conditions as edit_section:
      - Heading missing.
      - Heading ambiguous (appears more than once at any level).
    """
    lines = text.splitlines()

    # Locate every heading in the file, with its position and level.
    headings = []
    for i, line in enumerate(lines):
        m = _HEADING_RE.match(line)
        if m:
            headings.append((i, len(m.group(1)), m.group(2).strip()))

    target = section_heading.strip()
    matches = [h for h in headings if h[2] == target]
    if not matches:
        raise VaultError(f"Section '{section_heading}' not found.")
    if len(matches) > 1:
        raise VaultError(
            f"Section '{section_heading}' appears more than once. "
            f"Rewrite the note so headings are unique."
        )

    start, level, _ = matches[0]

    # Section ends at the next heading of same-or-higher level, else EOF.
    end = len(lines)
    for i, l, _ in headings:
        if i > start and l <= level:
            end = i
            break

    before = lines[: start + 1]              # up to and including the heading
    after = lines[end:]                       # from the next section onward
    new_body_lines = new_body.rstrip().splitlines()

    # Sandwich the new body between the heading and the next section,
    # padded with one blank line on each side for readability.
    return "\n".join(before + [""] + new_body_lines + [""] + after).rstrip() + "\n"


def edit_section(note_name: str, section_heading: str, new_body: str) -> str:
    """Replace the body under a markdown heading with new_body.

    A section is delimited by its heading line (e.g. "## Preferences")
    and runs until the next heading at the same or higher level, or the
    end of the file. The heading line itself is preserved — only the
    body between it and the next section boundary is replaced.

    Fails if the heading is missing or ambiguous. Refuses protected notes
    (memory.md) via _assert_writable — those have their own guarded write
    paths that enforce schema and token caps.
    """
    _assert_writable(note_name, "edit_section")

    path = _safe_path(note_name)
    if not path.is_file():
        raise VaultError(f"Note not found: {note_name}")

    text = path.read_text(encoding="utf-8")
    new_content = compute_section_edit(text, section_heading, new_body)

    _backup(path)
    path.write_text(new_content, encoding="utf-8")
    return f"Edited section '{section_heading}' in {note_name}. Backup saved."


# --- File locking ----------------------------------------------------------

# Advisory locks live in a sibling directory to the vault-visible files,
# so they're never confused with real notes. Persistent files (never
# deleted) — only the fcntl lock state matters, not the file's content.
_LOCKS_DIR = config.STATE_DIR / "locks"


@contextmanager
def lock(note_name: str):
    """Acquire an exclusive advisory lock over a note's read-modify-write cycle.

    Any code that reads a vault file, computes a change from what it
    read, and writes the result MUST wrap that cycle in this lock.
    Without it, two writers (main loop + extractor + cron) can
    interleave — both reading the same version, both computing against
    it, both writing — and lose one of the changes.

    Blocking: acquires the lock, yields, releases on exit. In practice
    a few milliseconds because vault R-M-W is fast. If it ever blocked
    noticeably we'd have bigger problems.

    Uses fcntl.flock on a sidecar lockfile per note (path segments
    flattened to a filename). fcntl locks are process-local: safe
    across threads within one process AND across cooperating processes
    on the same machine. That's the whole reason cron + bot don't need
    a shared library — they both grab the same file.
    """
    _LOCKS_DIR.mkdir(parents=True, exist_ok=True)
    lockfile = _LOCKS_DIR / (note_name.replace("/", "__") + ".lock")
    with lockfile.open("a+") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def write_note_raw(note_name: str, new_content: str) -> None:
    """Atomically replace a note's contents, with a backup of the prior version.

    The escape hatch for guarded writers that have done their own checks:
      * memory.write_section (schema + token cap enforced)
      * memory.ensure_exists (bootstrap; file must not exist yet)
      * chad.cron.morning_reminders (rewrites reminders.md after firing)

    Atomic in the sense that no reader ever sees a half-written file
    (write to <name>.tmp, os.replace into position). NOT durable across
    power loss — no fsync. Fine at this scale.

    Skips _assert_writable on purpose (that's what "escape hatch"
    means); callers own protection responsibility. Path jail still
    applies via _safe_path.
    """
    path = _safe_path(note_name)
    if path.is_file():
        _backup(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(new_content, encoding="utf-8")
    tmp.replace(path)


def append_line_raw(note_name: str, line: str, header_if_new: str = "") -> None:
    """Append a single line to a note, bypassing _assert_writable.

    The escape hatch for guarded write paths that append rather than
    replace — currently reminders.add_reminder. Path jail still applies
    (via _safe_path); the caller is responsible for whatever format
    validation makes sense for the target file.

    If the file doesn't exist and header_if_new is provided, the header
    is written first (so e.g. reminders.md starts with "# Reminders").
    """
    if not line.endswith("\n"):
        line = line + "\n"
    path = _safe_path(note_name)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        if header_if_new:
            path.write_text(header_if_new, encoding="utf-8")
        else:
            path.write_text("", encoding="utf-8")
    with path.open("a", encoding="utf-8") as f:
        f.write(line)
