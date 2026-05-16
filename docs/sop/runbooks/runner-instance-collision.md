# Runner instance collision runbook (OP-1068)

The C10 check reports duplicate effective `OMNISIGHT_RUNNER_INSTANCE_ID`
values across systemd runner services. It scans `systemctl list-units
--type=service`, keeps `omnisight-runner-{claude,codex}-{instance}.service`,
then reads `systemctl show <unit> -p Environment -p MainPID`. Missing or
empty IDs mean `default`. Duplicates emit `Severity.CRITICAL` code
`runner_instance_collision` with `instance_id`, `units`, and `pids`.

```sh
sudo cp deploy/systemd/runner-instance-uniqueness-check.service /etc/systemd/system/
sudo cp deploy/systemd/runner-instance-uniqueness-check.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now runner-instance-uniqueness-check.timer
```

Operator response: inspect `units` and `pids`, stop the duplicate runner
or correct its ID, reload systemd if unit files changed, restart only the
corrected service, then verify:

```sh
sudo systemctl start runner-instance-uniqueness-check.service
sudo systemctl status runner-instance-uniqueness-check.service
```

Non-goals: no auto-recovery and no tmux enumeration.
