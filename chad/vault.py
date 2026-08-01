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

import re
from datetime import datetime, timezone
from pathlib import Path

from chad import config


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
    """
    if not note_name or note_name.startswith("/"):
        raise VaultError("Note name must be a relative path like 'notes/todo.md'.")

    candidate = (config.VAULT_PATH / note_name).resolve()

    # is_relative_to() answers: is candidate inside the vault folder?
    if not candidate.is_relative_to(config.VAULT_PATH):
        raise VaultError("Note name escapes the vault. Operation refused.")

    if candidate.suffix != ".md":
        raise VaultError("Only .md files are allowed in the vault.")

    return candidate


def _safe_dir(folder: str) -> Path:
    """Same jail as _safe_path, but for a folder (no .md requirement)."""
    if folder.startswith("/"):
        raise VaultError("Folder must be a relative path like 'uni/comp2000'.")
    candidate = (config.VAULT_PATH / folder).resolve()
    if not candidate.is_relative_to(config.VAULT_PATH):
        raise VaultError("Folder escapes the vault. Operation refused.")
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
    path = _safe_path(note_name)
    if not path.is_file():
        raise VaultError(f"Note not found: {note_name}. Use create_note for new notes.")
    with path.open("a", encoding="utf-8") as f:
        # Ensure appended content starts on its own line.
        f.write("\n" + text.rstrip() + "\n")
    return f"Appended to {note_name}."


def create_note(note_name: str, text: str) -> str:
    """Create a new note. Refuses if the note already exists."""
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


def edit_section(note_name: str, section_heading: str, new_body: str) -> str:
    """Replace the body under a markdown heading with new_body.

    A section is delimited by its heading line (e.g. "## Preferences")
    and runs until the next heading at the same or higher level, or the
    end of the file. The heading line itself is preserved — only the
    body between it and the next section boundary is replaced.

    Fails if the heading is missing, or if it appears more than once
    (ambiguous). In the ambiguous case the caller should rewrite the
    note so headings are unique, or use a different tool.
    """
    path = _safe_path(note_name)
    if not path.is_file():
        raise VaultError(f"Note not found: {note_name}")

    lines = path.read_text(encoding="utf-8").splitlines()

    # Locate every heading in the file, with its position and level.
    headings = []
    for i, line in enumerate(lines):
        m = _HEADING_RE.match(line)
        if m:
            headings.append((i, len(m.group(1)), m.group(2).strip()))

    target = section_heading.strip()
    matches = [h for h in headings if h[2] == target]
    if not matches:
        raise VaultError(f"Section '{section_heading}' not found in {note_name}.")
    if len(matches) > 1:
        raise VaultError(
            f"Section '{section_heading}' appears more than once in "
            f"{note_name}. Rewrite the note so headings are unique."
        )

    start, level, _ = matches[0]

    # Section ends at the next heading of same-or-higher level, else EOF.
    end = len(lines)
    for i, l, _ in headings:
        if i > start and l <= level:
            end = i
            break

    _backup(path)

    before = lines[: start + 1]              # up to and including the heading
    after = lines[end:]                       # from the next section onward
    new_body_lines = new_body.rstrip().splitlines()

    # Sandwich the new body between the heading and the next section,
    # padded with one blank line on each side for readability.
    rebuilt = "\n".join(before + [""] + new_body_lines + [""] + after).rstrip() + "\n"
    path.write_text(rebuilt, encoding="utf-8")

    return f"Edited section '{section_heading}' in {note_name}. Backup saved."
