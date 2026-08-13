#!/usr/bin/env python3
"""omnisight-replica-up-check — alert when a prod replica is down or missing.

Born from the 2026-08-13 incident: Prometheus recorded up{backend-a:8000}==0
continuously from 2026-08-06 12:32 — SEVEN DAYS of single-replica prod — and no
alert existed anywhere on the path (no Alertmanager; a rule would fire into the
void). This closes that gap with the host's one proven channel: exit 1 ->
OnFailure=omnisight-alert@%n.service -> JIRA.

A MISSING series is treated as worse than up==0: it means the scrape target
itself disappeared (container gone / prometheus config changed).

Env overrides:
  OMNISIGHT_PROM_URL     default http://127.0.0.1:9090
  OMNISIGHT_EXPECTED_UP  default 'omnisight-backend=backend-a:8000,backend-b:8001'
                         format: job=instance,instance[;job2=...]
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request

PROM = os.environ.get("OMNISIGHT_PROM_URL", "http://127.0.0.1:9090")
EXPECTED = os.environ.get("OMNISIGHT_EXPECTED_UP",
                          "omnisight-backend=backend-a:8000,backend-b:8001")


def query_up(job):
    q = urllib.parse.urlencode({"query": f'up{{job="{job}"}}'})
    url = f"{PROM}/api/v1/query?{q}"
    last = None
    # tolerate a prometheus restart window: 3 attempts over ~30s
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                data = json.load(r)
            return {m["metric"].get("instance", "?"): m["value"][1]
                    for m in data["data"]["result"]}
        except Exception as e:  # noqa: BLE001 — any failure = retry then alert
            last = e
            if attempt < 2:
                time.sleep(15)
    print(f"FAIL prometheus unreachable at {PROM}: {last}")
    return None


def main():
    bad = 0
    for spec in EXPECTED.split(";"):
        job, _, instances = spec.partition("=")
        job = job.strip()
        got = query_up(job)
        if got is None:
            return 1
        for inst in [i.strip() for i in instances.split(",") if i.strip()]:
            v = got.get(inst)
            if v is None:
                bad += 1
                print(f"FAIL {job}/{inst}: series MISSING from Prometheus (target gone?)")
            elif v != "1":
                bad += 1
                print(f"FAIL {job}/{inst}: up={v} — replica DOWN")
            else:
                print(f"ok   {job}/{inst}: up=1")
    if bad:
        print(f"{bad} replica problem(s) — exiting 1 so OnFailure files the JIRA alert")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
