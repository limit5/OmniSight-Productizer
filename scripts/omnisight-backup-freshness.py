#!/usr/bin/env python3
"""Alert when the newest COMPLETE off-site backup cohort goes stale (OP-2731 P4).

Why this exists separately from the lanes' own OnFailure=
--------------------------------------------------------
A lane that fails loudly is already covered by OnFailure=. This covers the case
that nearly went unnoticed during 2026-07-22..26: a lane that keeps *succeeding
locally* while nothing reaches S3. The alert channel is idempotent per unit and
throttled hourly by design, so "day 9 of no off-site copy" looks identical to
"day 1" — a per-run failure signal cannot express duration. This probe asserts
the property we actually care about (a recent, complete, restorable set exists
off-host) rather than the verdict of any single run.

Cohort, not object
------------------
An object being present proves nothing: a payload whose digest never uploaded is
not restorable, and a digest alone is not a backup. So freshness is evaluated
over a *cohort* — every member required by the current stage, sharing one
timestamp — and the newest COMPLETE cohort is what must be recent.

Stage is a validated enum, never free-form config
-------------------------------------------------
The first draft let configuration enumerate the required members. That is how a
gate becomes vacuous: a digest-only member set would let incomplete backups pass
as complete cohorts. Configuration may select the stage and nothing else; each
stage's minimum member set is hardcoded here and cannot be shrunk. Validation
runs on every execution, not only at deploy, so a file edited afterwards cannot
silently weaken a live check. A probe that cannot validate its own configuration
exits non-zero and alerts — it never reports "fresh".

Stages exist because the capabilities land in sequence (P4 -> F1 -> P1); each is
tightened in the same commit as the capability it depends on, so the probe never
demands an artefact that nothing yet produces.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from collections import defaultdict

# --- the enum. Configuration selects a key; it can never edit a value. --------
_PAYLOAD_S1 = re.compile(r"^(?P<ts>\d{8}T\d{6}Z)\.dump\.gz$")
_PAYLOAD_S2 = re.compile(r"^(?P<ts>\d{8}T\d{6}Z)\.dump\.gz(\.gpg)?$")

STAGES: dict[str, dict] = {
    # P4 today: lane C uploads a gzip payload plus a .sha256 sidecar.
    "s1_payload_digest": {"members": frozenset({"payload", "digest"}),
                          "payload": _PAYLOAD_S1},
    # F1: payload becomes .dump.gz.gpg. BOTH names are accepted so the probe
    # does not go red on the changeover night; it goes red if neither appears.
    "s2_encrypted": {"members": frozenset({"payload", "digest"}),
                     "payload": _PAYLOAD_S2},
    # P1: a policy attestation becomes a required member of every cohort.
    "s3_attested": {"members": frozenset({"payload", "digest", "attestation"}),
                    "payload": _PAYLOAD_S2},
}

STAGE_ENV = "OMNISIGHT_BACKUP_FRESHNESS_STAGE"
MAX_AGE_DAYS = float(os.environ.get("OMNISIGHT_BACKUP_MAX_AGE_DAYS", "2"))
PREFIX = os.environ.get("OMNISIGHT_BACKUP_FRESHNESS_PREFIX", "postgres/daily/")
AWSCLI_IMAGE = os.environ.get("OMNISIGHT_AWSCLI_IMAGE", "amazon/aws-cli")


def die(msg: str) -> None:
    """Every abnormal exit is non-zero. A probe that cannot do its job must
    fail, so OnFailure= raises it — never print a reassuring line and exit 0."""
    print(f"[FAIL] {msg}", file=sys.stderr)
    raise SystemExit(1)


def resolve_stage() -> dict:
    raw = os.environ.get(STAGE_ENV, "").strip()
    if not raw:
        die(f"{STAGE_ENV} is unset. It must name one of: {sorted(STAGES)}. "
            "Refusing to guess a stage — a guessed stage is a weakened gate.")
    if raw not in STAGES:
        die(f"{STAGE_ENV}={raw!r} is not a known stage. Valid: {sorted(STAGES)}")
    stage = STAGES[raw]
    # Belt and braces: assert the hardcoded invariant still holds, so a future
    # edit to STAGES that empties a member set fails here rather than passing.
    if not stage["members"] or not {"payload", "digest"} <= stage["members"]:
        die(f"stage {raw!r} has an invalid member set {sorted(stage['members'])}; "
            "payload+digest are the irreducible minimum")
    print(f"stage={raw} members={sorted(stage['members'])} max_age={MAX_AGE_DAYS}d")
    return stage


def s3_list(bucket: str, prefix: str) -> list[tuple[str, str]]:
    cred = os.path.expanduser("~/.config/omnisight/backup.env")
    env = {}
    try:
        for line in open(cred, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    except OSError as exc:
        die(f"cannot read {cred}: {exc}")

    cmd = ["docker", "run", "--rm",
           "-e", "AWS_ACCESS_KEY_ID", "-e", "AWS_SECRET_ACCESS_KEY",
           "-e", "AWS_DEFAULT_REGION", AWSCLI_IMAGE,
           "s3api", "list-objects-v2", "--bucket", bucket, "--prefix", prefix,
           "--query", "Contents[].[Key,LastModified]", "--output", "text"]
    runenv = dict(os.environ)
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_DEFAULT_REGION"):
        if k not in env:
            die(f"{k} missing from {cred}")
        runenv[k] = env[k]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=180,
                             env=runenv, check=False)
    except Exception as exc:  # noqa: BLE001
        die(f"listing failed to execute: {exc}")
    if out.returncode != 0:
        # A listing error is a FAILURE, never "no results, therefore fresh".
        die(f"S3 listing failed (rc={out.returncode}): {out.stderr.strip()[:300]}")
    rows = []
    for line in out.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            rows.append((parts[0], parts[1]))
    return rows


def main() -> int:
    stage = resolve_stage()
    uri = os.environ.get("OMNISIGHT_BACKUP_S3_URI", "")
    m = re.match(r"^s3://([^/]+)", uri) if uri else None
    if not m:
        # Fall back to the lane's own credential file so the probe and the lane
        # can never disagree about which bucket is being asserted.
        try:
            for line in open(os.path.expanduser("~/.config/omnisight/backup.env"),
                             encoding="utf-8"):
                if line.strip().startswith("OMNISIGHT_BACKUP_S3_URI="):
                    uri = line.split("=", 1)[1].strip()
        except OSError:
            pass
        m = re.match(r"^s3://([^/]+)", uri) if uri else None
    if not m:
        die("OMNISIGHT_BACKUP_S3_URI is not resolvable; cannot assert freshness")
    bucket = m.group(1)

    rows = s3_list(bucket, PREFIX)
    if not rows:
        die(f"no objects at all under s3://{bucket}/{PREFIX} — "
            "an empty prefix is a failure, not a fresh backup")

    # Group by timestamp into cohorts.
    cohorts: dict[str, dict[str, str]] = defaultdict(dict)
    pay_re = stage["payload"]
    for key, lastmod in rows:
        base = key.rsplit("/", 1)[-1]
        if base.endswith(".sha256"):
            stem = base[: -len(".sha256")]
            mm = pay_re.match(stem.replace("daily-", "", 1)) if stem.startswith("daily-") else None
            if mm:
                cohorts[mm.group("ts")]["digest"] = lastmod
            continue
        if base.endswith(".attestation.json"):
            stem = base[: -len(".attestation.json")]
            mm = pay_re.match(stem.replace("daily-", "", 1)) if stem.startswith("daily-") else None
            if mm:
                cohorts[mm.group("ts")]["attestation"] = lastmod
            continue
        mm = pay_re.match(base.replace("daily-", "", 1)) if base.startswith("daily-") else None
        if mm:
            cohorts[mm.group("ts")]["payload"] = lastmod

    required = stage["members"]
    complete = {ts: v for ts, v in cohorts.items() if required <= set(v)}
    incomplete = sorted(set(cohorts) - set(complete))
    if incomplete:
        print(f"note: {len(incomplete)} incomplete cohort(s), newest={incomplete[-1]} "
              f"(present: {sorted(cohorts[incomplete[-1]])}) — not counted as fresh")
    if not complete:
        die(f"{len(cohorts)} cohort(s) found but NONE complete for stage members "
            f"{sorted(required)} — an incomplete backup is not a backup")

    newest = max(complete)
    ts = time.mktime(time.strptime(newest, "%Y%m%dT%H%M%SZ")) - time.timezone
    age_days = (time.time() - ts) / 86400.0
    print(f"newest complete cohort: {newest} ({age_days:.1f}d old, "
          f"members={sorted(complete[newest])})")
    if age_days > MAX_AGE_DAYS:
        die(f"newest COMPLETE off-site cohort is {age_days:.1f}d old "
            f"(limit {MAX_AGE_DAYS}d) — the lane may be succeeding locally "
            "while nothing reaches S3")
    print("OK: off-site backup cohort is complete and fresh")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
