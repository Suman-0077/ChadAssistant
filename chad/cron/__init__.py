# chad.cron — systemd-timer entry points.
#
# Each script here is a self-contained oneshot: reads state, does work,
# exits. No polling, no persistent connections. Invoked by matching
# .timer + .service units in deploy/systemd/.
