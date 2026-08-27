#!/usr/bin/env python3
"""omnisight-container-reconcile — boot/periodic self-heal for the docker fleet.

Born from the 2026-08-13 incident: a WSL force-kill corrupted dockerd's libnetwork
store; at the next boot prod backend-a was started into a sandbox with ZERO network
endpoints and crash-looped for 6 hours — restart-policy restarts NEVER rebuild
endpoints, so that state is permanent until the container is RECREATED from its
compose definition. Nothing on the host detected or repaired it.

Policy (deliberately conservative — "non-destructive supervisor" doctrine):
  netless (running/restarting, 0 networks)
      -> the ONE deterministically-broken, deterministically-fixable state:
         auto `docker compose up -d --force-recreate --no-deps <svc>` IF the
         container belongs to an allowlisted compose project AND is not a
         stateful service; otherwise report-only.
  unhealthy for >10 min, or restart-looping (>=20 restarts)
      -> report-only; a human decides.

Exit 1 whenever ANYTHING was found (even if successfully remediated) so the
wrapping unit's OnFailure=omnisight-alert@%n.service files/comments the JIRA
alert (OP-2728 channel) carrying this journal as context. Exit 0 = fleet clean.

v2 (2026-08-27) — two blind spots exposed by the 3-week tunnel outage:
  exited-but-expected (allowlisted project, restart-policy always/unless-stopped,
  non-zero exit, not a compose one-off, dead >30min)   -> report-only
  SYSTEM-scope failed units (the 08-13 sweep only read --user; the failed
  system unit that owned the tunnel went unseen)        -> report-only
"""

import fcntl
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

# Auto-remediation is limited to stacks we own end-to-end. Everything else on
# this shared host (postgres-ha, ct-*, fate, tcm, ai-core, ...) is report-only:
# recreating someone else's container from here would violate the mutual
# No-Touch discipline this host runs on.
ALLOW_PROJECTS = {"omnisight-productizer", "omnisight-staging", "omnisight-dev"}
# Never auto-recreate anything that might hold state even inside our projects.
STATEFUL_RE = re.compile(r"postgres|redis|neo4j|mongo|mysql|mariadb|pg-|(^|[-_])db([-_]|$)")

UNHEALTHY_GRACE_S = 600  # healthcheck start_period + catch-up churn tolerance
RESTART_LOOP_MIN = 20
LOCK_PATH = os.path.expanduser("~/.local/state/omnisight-reconcile.lock")


def sh(argv, timeout):
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def docker_ready():
    for _ in range(3):
        if sh(["docker", "info"], 20).returncode == 0:
            return True
        time.sleep(10)
    return False


def inspect_all():
    ids = sh(["docker", "ps", "-aq"], 30).stdout.split()
    if not ids:
        return []
    out = sh(["docker", "inspect"] + ids, 60)
    return json.loads(out.stdout) if out.returncode == 0 else []


def _age_s(iso):
    try:
        t = iso.split(".")[0] + "+00:00"
        return (datetime.now(timezone.utc) - datetime.fromisoformat(t)).total_seconds()
    except Exception:
        return 0


def started_age_s(c):
    return _age_s(c["State"]["StartedAt"])


def recreate(c, name):
    lab = c["Config"]["Labels"]
    svc = lab["com.docker.compose.service"]
    argv = ["docker", "compose", "-p", lab["com.docker.compose.project"],
            "--project-directory", lab["com.docker.compose.project.working_dir"]]
    for f in lab.get("com.docker.compose.project.config_files", "").split(","):
        if f:
            argv += ["-f", f]
    envf = lab.get("com.docker.compose.project.environment_file", "")
    if envf:
        argv += ["--env-file", envf]
    argv += ["up", "-d", "--force-recreate", "--no-deps", svc]
    print(f"FIX  {name}: recreating via: {' '.join(argv)}")
    r = sh(argv, 240)
    if r.returncode != 0:
        print(f"FAIL {name}: recreate failed rc={r.returncode}: {r.stderr.strip()[-400:]}")
        return False
    deadline = time.time() + 150
    while time.time() < deadline:
        q = sh(["docker", "inspect", name, "--format",
                "{{.State.Status}}/{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}"], 20)
        st = q.stdout.strip()
        if st in ("running/healthy", "running/none"):
            print(f"FIX  {name}: recovered ({st})")
            return True
        if st.startswith(("exited", "dead")):
            break
        time.sleep(10)
    print(f"FAIL {name}: did not converge after recreate (last={st})")
    return False


def main():
    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    lock = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another reconcile run is in progress; skipping")
        return 0

    if not docker_ready():
        print("FAIL docker daemon unreachable after 3 attempts")
        return 1

    findings = 0
    for c in inspect_all():
        name = c["Name"].lstrip("/")
        status = c["State"]["Status"]
        health = (c["State"].get("Health") or {}).get("Status", "none")
        nets = len(c["NetworkSettings"]["Networks"])
        restarts = c.get("RestartCount", 0)
        lab = c["Config"]["Labels"]
        proj = lab.get("com.docker.compose.project", "")
        svc = lab.get("com.docker.compose.service", name)

        if status in ("running", "restarting") and nets == 0:
            findings += 1
            print(f"WARN {name}: NETLESS (status={status}, restarts={restarts}) — the 2026-08-13 signature")
            if proj in ALLOW_PROJECTS and not STATEFUL_RE.search(svc):
                recreate(c, name)
            else:
                print(f"WARN {name}: report-only (project={proj or 'non-compose'}, service={svc})")
        elif status == "restarting" and restarts >= RESTART_LOOP_MIN:
            findings += 1
            print(f"WARN {name}: restart-looping (restarts={restarts}) — report-only")
        elif status == "running" and health == "unhealthy" and started_age_s(c) > UNHEALTHY_GRACE_S:
            findings += 1
            print(f"WARN {name}: unhealthy for >{UNHEALTHY_GRACE_S}s — report-only")
        elif (
            status == "exited"
            and c["State"].get("ExitCode", 0) != 0
            and c["HostConfig"]["RestartPolicy"].get("Name") in ("unless-stopped", "always")
            and proj in ALLOW_PROJECTS
            and lab.get("com.docker.compose.oneoff", "False") != "True"
            and _age_s(c["State"].get("FinishedAt", "")) > 1800
        ):
            # the cloudflared signature: restart-policy says "should be up",
            # it crashed, and nothing brought it back. Human decides (an
            # operator `docker stop` also lands here — that's why report-only).
            findings += 1
            print(f"WARN {name}: exited({c['State'].get('ExitCode')}) but restart-policy="
                  f"{c['HostConfig']['RestartPolicy'].get('Name')} (project={proj}) — expected running; report-only")

    # v2: SYSTEM-scope failed units (the user-scope-only sweep missed the
    # failed system unit that owned the prod tunnel for 3 weeks).
    SYSTEM_IGNORE = {"dmesg.service"}
    for line in sh(["systemctl", "--failed", "--no-legend", "--plain"], 20).stdout.splitlines():
        parts = line.split()
        if parts and parts[0].endswith((".service", ".timer", ".socket")) and parts[0] not in SYSTEM_IGNORE:
            findings += 1
            print(f"WARN system unit FAILED: {parts[0]} — report-only")

    if findings == 0:
        print("fleet clean")
        return 0
    print(f"{findings} finding(s) — exiting 1 so OnFailure files the JIRA alert")
    return 1


if __name__ == "__main__":
    sys.exit(main())
