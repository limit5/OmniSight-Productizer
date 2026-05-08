# scripts/cron/ — one-shot scripts invoked by the systemd .timer units
# under deploy/systemd/. Each script lives in its own file and exits
# non-zero on irrecoverable error (the systemd timer's OnFailure= drop-in
# is the alerting surface).
