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

## Test-firing without waiting

```bash
sudo systemctl start chad-morning.service
sudo journalctl -u chad-morning.service -n 30 --no-pager
```

Runs the script once immediately. Won't schedule anything — the timer
handles that.
