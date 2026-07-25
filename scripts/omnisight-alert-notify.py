#!/usr/bin/env python3
"""OP-2728 — systemd OnFailure -> JIRA alert channel.

WHY THIS EXISTS
---------------
Audited 2026-07-25: this host had NO working alert delivery path. Both
`deployment-audit-alert.service` and `staging-gate-alert.service` only `printf`
a JSON line to the journal; there is no Alertmanager; the Discord client env is
unset. Consequences found in one sweep: `pipeline-coordinator` failed for 5 days,
`omnisight-prod-backup` for 3 nights (it had no `OnFailure=` at all), and two
staging timers had failed 10,093 / 75 times without ever succeeding. JIRA is the
only push channel empirically proven to work on this host.

DESIGN
------
* **Idempotent by construction**: at most ONE open JIRA issue per unit, keyed on
  the label ``alert:unit=<slug>``. A repeat failure comments on that issue (or is
  throttled), never files a second one. This is load-bearing: the DLP gate this
  pairs with (OP-2729) blocks *by design* on every unreviewed content change, so a
  chatty alert would be muted within a month and recreate the silence it prevents.
* **Throttled**: at most one comment per unit per ALERT_THROTTLE_SECONDS (default
  1h), so a 10-minute-cadence unit cannot add 144 comments/day.
* **Never fails**: always exits 0. An OnFailure handler that itself fails is how
  you get restart loops and lost signal. Unreachable JIRA spools locally and is
  flushed on the next invocation.
* **No repo dependency**: deliberately standalone under ~/.local/bin. The three
  repo checkouts on this host are respectively release-pinned, 202 commits behind,
  and 735 files dirty. An alerting path must not inherit that.

USAGE
-----
    omnisight-alert-notify.py <failed-unit-name>          # from OnFailure=
    omnisight-alert-notify.py --self-test                 # channel delivery proof

Credentials: OMNISIGHT_ALERT_JIRA_ENV (default ~/.config/omnisight/jira-claude.env)
plus the matching token file (…-token).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from base64 import b64encode
from datetime import datetime, timezone
from pathlib import Path

USER_AGENT = "omnisight-alert-notify/1.0 (OP-2728)"
STATE_DIR = Path(os.environ.get("OMNISIGHT_ALERT_STATE_DIR", str(Path.home() / ".local/state/omnisight-alerts")))
SPOOL = STATE_DIR / "spool.jsonl"
RUNLOG = STATE_DIR / "run.log"
THROTTLE_S = int(os.environ.get("ALERT_THROTTLE_SECONDS", "3600"))
SPOOL_MAX_REPLAY = int(os.environ.get("ALERT_SPOOL_MAX_REPLAY", "50"))
LOG_TAIL_LINES = 40
HTTP_TIMEOUT = 30


# --------------------------------------------------------------------------- io


def _log(msg: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    line = f"{datetime.now(timezone.utc).isoformat()} {msg}\n"
    with RUNLOG.open("a", encoding="utf-8") as fh:
        fh.write(line)
    print(msg, file=sys.stderr)


def _run(cmd: list[str], timeout: int = 20) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (out.stdout or "") + (out.stderr or "")
    except Exception as exc:  # noqa: BLE001 - never let diagnostics kill the alert
        return f"<{cmd[0]} failed: {exc}>"


def _slug(unit: str) -> str:
    """JIRA-label-safe key for a unit name (labels must not contain spaces)."""
    return re.sub(r"[^A-Za-z0-9_.@-]", "-", unit.removesuffix(".service"))


# ------------------------------------------------------------------- jira glue


def _jira_config() -> tuple[str, str, str]:
    env_path = Path(os.environ.get("OMNISIGHT_ALERT_JIRA_ENV", str(Path.home() / ".config/omnisight/jira-claude.env")))
    env: dict[str, str] = {}
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        k, v = raw.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    site = env["OMNISIGHT_JIRA_SITE_URL"].rstrip("/")
    project = env.get("OMNISIGHT_JIRA_PROJECT_KEY", "OP")
    email = env["OMNISIGHT_JIRA_CLAUDE_EMAIL"]
    token_path = env_path.with_name(env_path.stem + "-token")
    token = token_path.read_text(encoding="utf-8").strip()
    auth = "Basic " + b64encode(f"{email}:{token}".encode()).decode()
    return site, project, auth


def _request(method: str, url: str, auth: str, body: dict | None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": auth,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        payload = resp.read().decode()
        return json.loads(payload) if payload else {}


def _adf(paragraphs: list[str], code_block: str | None = None) -> dict:
    content: list[dict] = [
        {"type": "paragraph", "content": [{"type": "text", "text": p}]} for p in paragraphs if p
    ]
    if code_block:
        content.append({"type": "codeBlock", "content": [{"type": "text", "text": code_block[:30000]}]})
    return {"type": "doc", "version": 1, "content": content}


# ---------------------------------------------------------------- diagnostics


def collect_context(unit: str) -> tuple[dict[str, str], str]:
    props = {}
    raw = _run(
        [
            "systemctl", "--user", "show", unit,
            "-p", "ActiveState", "-p", "SubState", "-p", "Result",
            "-p", "ExecMainStatus", "-p", "ExecMainExitTimestamp",
            "-p", "NRestarts", "-p", "StandardOutput", "-p", "Description",
        ]
    )
    for line in raw.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            props[k] = v

    std = props.get("StandardOutput", "")
    if std.startswith("append:") or std.startswith("file:"):
        path = Path(std.split(":", 1)[1])
        try:
            with path.open("rb") as fh:
                fh.seek(0, os.SEEK_END)
                fh.seek(max(0, fh.tell() - 16384))
                tail = fh.read().decode("utf-8", "replace")
            log = "\n".join(tail.splitlines()[-LOG_TAIL_LINES:])
            log = f"[tail of {path}]\n{log}"
        except Exception as exc:  # noqa: BLE001
            log = f"<could not read {path}: {exc}>"
    else:
        log = _run(["journalctl", "--user", "-u", unit, "-n", str(LOG_TAIL_LINES), "--no-pager"])
    return props, log


# --------------------------------------------------------------------- alerting


def _throttled(slug: str) -> bool:
    stamp = STATE_DIR / f"{slug}.stamp"
    if not stamp.exists():
        return False
    return (time.time() - stamp.stat().st_mtime) < THROTTLE_S


def _touch_stamp(slug: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / f"{slug}.stamp").write_text(str(int(time.time())), encoding="utf-8")


def _find_open_issue(site: str, project: str, auth: str, slug: str) -> str | None:
    jql = f'project = "{project}" AND labels = "alert:unit={slug}" AND statusCategory != Done ORDER BY created DESC'
    url = f"{site}/rest/api/3/search/jql?jql={urllib.parse.quote(jql)}&maxResults=1&fields=key"
    resp = _request("GET", url, auth, None)
    issues = resp.get("issues") or []
    return issues[0]["key"] if issues else None


def _create_issue(
    site: str, project: str, auth: str, unit: str, slug: str, props: dict, log: str,
    priority: str = "High", summary: str | None = None,
) -> str:
    summary = summary or f"[ALERT] {unit} failed on {os.uname().nodename}"
    body = {
        "fields": {
            "project": {"key": project},
            "summary": summary[:250],
            "description": _adf(
                [
                    f"Automated alert from omnisight-alert-notify (OP-2728). Unit: {unit}",
                    f"Description: {props.get('Description', '?')}",
                    f"Result={props.get('Result', '?')}  ExecMainStatus={props.get('ExecMainStatus', '?')}  "
                    f"NRestarts={props.get('NRestarts', '?')}  exited={props.get('ExecMainExitTimestamp', '?')}",
                    "This issue is the single open alert for this unit. Repeat failures are added as "
                    "comments (throttled). Close it when the unit is healthy; the next failure opens a new one.",
                ],
                code_block=log,
            ),
            "issuetype": {"name": "Story"},
            "priority": {"name": priority},
            "labels": [
                "tier:X",
                "type:bug",
                "priority:meta",
                "class:subscription-claude",
                "area:devops",
                "scope:ops-alert",
                f"alert:unit={slug}",
            ],
        }
    }
    return _request("POST", f"{site}/rest/api/3/issue", auth, body)["key"]


def _comment(site: str, auth: str, key: str, unit: str, props: dict, log: str) -> None:
    body = {
        "body": _adf(
            [
                f"Repeat failure of {unit} at {datetime.now(timezone.utc).isoformat()} "
                f"(Result={props.get('Result', '?')}, ExecMainStatus={props.get('ExecMainStatus', '?')}).",
            ],
            code_block=log,
        )
    }
    _request("POST", f"{site}/rest/api/3/issue/{key}/comment", auth, body)


def _spool(unit: str, reason: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with SPOOL.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "unit": unit, "reason": reason}) + "\n")


def flush_spool(site: str, project: str, auth: str) -> int:
    """Drain events that could not reach JIRA earlier (OP-2733: proven necessary by a
    real Atlassian HTTP 500 during the DR-drill alert test).

    Without this the spool is a write-only file and a transient outage of the
    destination silently loses the alert — the exact failure this channel exists to
    remove. Entries are thinner than a live alert (they carry only unit/ts/reason,
    not the log tail) which is stated in the issue text; a thin alert beats none.
    Anything still failing is kept for the next attempt. Never raises.
    """
    if not SPOOL.exists():
        return 0
    try:
        entries = [json.loads(line) for line in SPOOL.read_text(encoding="utf-8").splitlines() if line.strip()]
    except Exception:  # noqa: BLE001 - a corrupt spool must not break live alerting
        return 0
    if not entries:
        return 0
    delivered, remaining = 0, []
    for entry in entries[-SPOOL_MAX_REPLAY:]:
        unit = entry.get("unit", "unknown")
        slug = _slug(unit)
        try:
            key = _find_open_issue(site, project, auth, slug)
            detail = (f"Delayed delivery. The alert for {unit} at {entry.get('ts')} could not reach JIRA "
                      f"at the time ({entry.get('reason', '?')}) and was spooled locally. "
                      "Context is limited to what the spool retained.")
            props = {"Description": f"delayed alert for {unit}", "Result": "spooled",
                     "ExecMainStatus": "-", "NRestarts": "-"}
            if key is None:
                _create_issue(site, project, auth, unit, slug, props, detail,
                              summary=f"[ALERT] {unit} failed on {os.uname().nodename} (delayed)")
            else:
                _comment(site, auth, key, unit, props, detail)
            delivered += 1
        except Exception:  # noqa: BLE001 - still unreachable; keep it for next time
            remaining.append(entry)
    if delivered:
        SPOOL.write_text("".join(json.dumps(e) + "\n" for e in remaining), encoding="utf-8")
        _log(f"SPOOL: delivered {delivered} deferred alert(s); {len(remaining)} still pending")
    return delivered


def notify_source(source: str, title: str, detail: str, priority: str = "High") -> int:
    """Generic (non-systemd) alert entry point — same idempotent one-issue-per-source
    semantics as unit failures. Used by the host-disk floor check (OP-2728 AC4), which
    cannot be a Prometheus rule: `rule_files` in the prod prometheus.yml is an explicit
    single-entry list living inside the RELEASE-PINNED checkout, so an operator-owned
    rule could only be added by masking the release's own config. Keeping the threshold
    here keeps it decoupled from the release artifact."""
    slug = _slug(source)
    props = {"Description": title, "Result": "threshold", "ExecMainStatus": "-", "NRestarts": "-"}
    try:
        site, project, auth = _jira_config()
    except Exception as exc:  # noqa: BLE001
        _log(f"ALERT {source}: no JIRA config ({exc}); spooled")
        _spool(source, f"config: {exc}")
        return 0
    flush_spool(site, project, auth)
    try:
        key = _find_open_issue(site, project, auth, slug)
        if key is None:
            key = _create_issue(site, project, auth, title, slug, props, detail,
                                priority=priority, summary=f"[ALERT] {title}")
            _touch_stamp(slug)
            _log(f"ALERT {source}: created {key}")
        elif _throttled(slug):
            _log(f"ALERT {source}: {key} already open, throttled - no comment")
        else:
            _comment(site, auth, key, source, props, detail)
            _touch_stamp(slug)
            _log(f"ALERT {source}: commented on {key}")
    except Exception as exc:  # noqa: BLE001
        _log(f"ALERT {source}: {type(exc).__name__}: {exc}; spooled")
        _spool(source, f"{type(exc).__name__}: {exc}")
    return 0


def clear_source(source: str) -> None:
    """Drop the throttle stamp so a future breach alerts immediately."""
    stamp = STATE_DIR / f"{_slug(source)}.stamp"
    if stamp.exists():
        stamp.unlink()


def notify(unit: str) -> int:
    slug = _slug(unit)
    props, log = collect_context(unit)
    try:
        site, project, auth = _jira_config()
    except Exception as exc:  # noqa: BLE001
        _log(f"ALERT {unit}: no JIRA config ({exc}); spooled")
        _spool(unit, f"config: {exc}")
        return 0

    flush_spool(site, project, auth)
    try:
        key = _find_open_issue(site, project, auth, slug)
        if key is None:
            key = _create_issue(site, project, auth, unit, slug, props, log)
            _touch_stamp(slug)
            _log(f"ALERT {unit}: created {key}")
        elif _throttled(slug):
            _log(f"ALERT {unit}: {key} already open, throttled (<{THROTTLE_S}s) - no comment")
        else:
            _comment(site, auth, key, unit, props, log)
            _touch_stamp(slug)
            _log(f"ALERT {unit}: commented on {key}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:300] if exc.fp else ""
        _log(f"ALERT {unit}: JIRA HTTP {exc.code} {detail}; spooled")
        _spool(unit, f"http {exc.code}: {detail}")
    except Exception as exc:  # noqa: BLE001
        _log(f"ALERT {unit}: {type(exc).__name__}: {exc}; spooled")
        _spool(unit, f"{type(exc).__name__}: {exc}")
    return 0  # NEVER non-zero: an OnFailure handler must not itself fail


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv[0] == "--flush-spool":
        site, project, auth = _jira_config()
        print(f"delivered {flush_spool(site, project, auth)} deferred alert(s)")
        return 0
    if argv[0] == "--self-test":
        site, project, auth = _jira_config()
        who = _request("GET", f"{site}/rest/api/3/myself", auth, None)
        print(f"JIRA reachable as {who.get('emailAddress') or who.get('displayName')}; project={project}")
        return 0
    return notify(argv[0])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
