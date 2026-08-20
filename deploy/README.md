# Deployment: systemd units

Systemd unit files for Chad's scheduled jobs. Kept in the repo so they
version alongside the code that runs them. Install to
`/etc/systemd/system/` on the server whenever they change.

## chad-morning — daily reminder scan

Fires `python -m chad.cron.morning_reminders` at 07:00 Sydney time
(per-line timezone in the timer, so the server's own clock doesn't matter).
The script reads `reminders.md`, surfaces items due today (or overdue),
marks fired lines with `[done]`.

Sandboxing: runs as root but with `ProtectSystem=strict`,
`ProtectHome=read-only`, `PrivateTmp=true`, and only `/root/vault`
mounted read-write. The script can't touch anything else.

Install once:

```bash
# On the server, from repo root
sudo cp deploy/systemd/chad-morning.service /etc/systemd/system/
sudo cp deploy/systemd/chad-morning.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now chad-morning.timer
systemctl list-timers chad-morning
```

`enable --now` enables the timer for boot AND starts the timer itself
(NOT the service — the timer's own dependencies don't cascade the
oneshot on install). `list-timers` shows the next scheduled fire.

## chad-vault-commit — vault git audit trail

Commits the vault to a local git repo every 15 minutes. Purely local:
no remote, no push. `git log` and `git diff` inside `/root/vault` give
the audit trail the project plan calls for.

`.chad-state/` and `.chad-backups/` are gitignored — the first churns
constantly, the second is superseded by git history itself.

Install once:

```bash
sudo cp deploy/systemd/chad-vault-commit.service /etc/systemd/system/
sudo cp deploy/systemd/chad-vault-commit.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now chad-vault-commit.timer
systemctl list-timers chad-vault-commit
```

Inspect the history:

```bash
git -C /root/vault log --oneline
git -C /root/vault diff HEAD~1        # what changed in the last commit
git -C /root/vault log -p memory.md   # full history of one note
```

Revert a bad change:

```bash
git -C /root/vault checkout HEAD~1 -- memory.md
```

## Test-firing without waiting

```bash
sudo systemctl start chad-morning.service
sudo journalctl -u chad-morning.service -n 30 --no-pager

sudo systemctl start chad-vault-commit.service
sudo journalctl -u chad-vault-commit.service -n 20 --no-pager
```

Runs the script once immediately. Won't schedule anything — the timer
handles that.
