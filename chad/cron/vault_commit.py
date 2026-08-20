"""Systemd-timer entry point: auto-commit the vault as an audit trail.

The project plan lists "the vault is under git; every write is a diff that
can be inspected and reverted" as a day-one security principle. This is
that. Runs on a timer, stages everything, commits if anything changed.

Deliberately dumb:
  * No AI, no network, no push. Purely local history.
  * Commits everything or nothing — no selective staging logic to get
    wrong.
  * Silent no-op when the tree is clean, so the log stays readable.

Why local-only (no remote push): the vault contains personal notes and
eventually email content. Pushing it anywhere is a decision to make
deliberately, not a side effect of wanting version history. `git log`
and `git diff` on the server give the audit trail; that's the actual
requirement.

Excluded from git via .gitignore (written by ensure_repo):
  .chad-state/    — history.json, proposals.json, locks, audit log.
                    Churns constantly; would bury real note diffs.
  .chad-backups/  — pre-edit copies. Git IS the backup now; keeping both
                    in history would double every change.
"""

import logging
import subprocess
import sys

from chad import config

logging.basicConfig(
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("chad.cron.vault_commit")

VAULT = config.VAULT_PATH

GITIGNORE = """\
# Chad's operational state — churns constantly, not vault content.
.chad-state/

# Pre-edit backups — git history supersedes these.
.chad-backups/

# Obsidian workspace files (per-device UI state, not content).
.obsidian/workspace.json
.obsidian/workspace-mobile.json

# Syncthing internals.
.stfolder/
.stversions/
"""


def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run a git command inside the vault."""
    return subprocess.run(
        ["git", "-C", str(VAULT), *args],
        capture_output=True, text=True, check=check,
    )


def ensure_repo() -> None:
    """Initialise the vault repo and .gitignore if not already set up.

    Idempotent — safe to call on every run. Sets a local identity so
    commits work without global git config on the server.
    """
    if not (VAULT / ".git").is_dir():
        _git("init", "-q")
        _git("config", "user.email", "chad@localhost")
        _git("config", "user.name", "Chad")
        # Default branch name; keeps `git log` output predictable.
        _git("branch", "-M", "main", check=False)
        log.info("Initialised git repo at %s", VAULT)

    gitignore = VAULT / ".gitignore"
    if not gitignore.exists() or gitignore.read_text(encoding="utf-8") != GITIGNORE:
        gitignore.write_text(GITIGNORE, encoding="utf-8")
        log.info("Wrote .gitignore")


def has_changes() -> bool:
    """True if the working tree differs from HEAD (or there is no HEAD yet)."""
    out = _git("status", "--porcelain").stdout.strip()
    return bool(out)


def commit() -> str | None:
    """Stage everything and commit. Returns the summary line, or None if clean."""
    _git("add", "-A")
    # Re-check AFTER staging: `git status --porcelain` reports ignored-file
    # noise in some configurations, but `diff --cached --quiet` is exact.
    staged = _git("diff", "--cached", "--quiet", check=False)
    if staged.returncode == 0:
        return None

    # Summarise what changed for the commit message — much more useful
    # than a bare timestamp when scrolling back through history.
    names = _git("diff", "--cached", "--name-only").stdout.split()
    if len(names) == 1:
        subject = f"vault: update {names[0]}"
    else:
        subject = f"vault: update {len(names)} files"

    result = _git("commit", "-q", "-m", subject, check=False)
    if result.returncode != 0:
        log.error("Commit failed: %s", result.stderr.strip())
        return None
    return subject


def main() -> None:
    if not VAULT.is_dir():
        log.error("Vault path does not exist: %s", VAULT)
        sys.exit(1)

    ensure_repo()

    if not has_changes():
        log.info("Vault clean — nothing to commit.")
        return

    subject = commit()
    if subject:
        log.info("Committed: %s", subject)
    else:
        log.info("Nothing staged after add — no commit made.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("Vault commit failed.")
        sys.exit(1)
