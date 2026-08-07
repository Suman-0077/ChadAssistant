# Deployment: systemd units

Systemd unit files for Chad's scheduled jobs. Kept in the repo so they
version alongside the code that runs them. Install them to
`/etc/systemd/system/` on the server whenever they change.

## chad-morning — daily reminder scan

Fires `python -m chad.cron.morning_reminders` at 07:00 local time.
The script reads `reminders.md`, surfaces items due today (or overdue),
marks fired lines with `[done]`.

Install once:

```bash
# On the server, from repo root
sudo cp deploy/systemd/chad-morning.service /etc/systemd/system/
sudo cp deploy/systemd/chad-morning.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now chad-morning.timer
sudo systemctl list-timers chad-morning
```

`enable --now` both enables the timer for boot AND starts it immediately.
The `list-timers` line shows the next scheduled fire.

Timezone: the `OnCalendar=` line is 07:00 in the server's local time.
The server is UTC by default; set it to match the user's timezone once:

```bash
sudo timedatectl set-timezone Australia/Sydney
```

After changing the timezone, reload the timer:

```bash
sudo systemctl daemon-reload
sudo systemctl restart chad-morning.timer
```

## Test-firing without waiting

```bash
sudo systemctl start chad-morning.service
sudo journalctl -u chad-morning.service -n 30 --no-pager
```

Runs the script once immediately. Won't schedule anything on its own —
the timer handles that.
