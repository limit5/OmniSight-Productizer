#!/usr/bin/env python3
"""classify_tier_via_backend.py — return Tier (s|m|l|x) for a path set.

This wrapper is invoked by the server-side Gerrit ``patchset-created``
hook (``scripts/gerrit-hooks/ref-update-tier-classify.sh``) once per
patchset upload. It implements OP-805 / G3 of the META OP-802 epic and
realises ADR-0005 §4 layer 1 — path-based force-upgrade. The contributor
cannot downgrade the tier; the hook overwrites whatever they set.

Strategies, tried in order:

1. **In-process** — import ``backend.governance.tier_classifier`` (G2).
   Fastest path; preferred when the backend repo is cloned alongside
   the Gerrit hooks tree on the Gerrit host (PYTHONPATH wired in the
   shell wrapper).
2. **HTTPS** — ``POST <backend>/api/internal/classify-tier`` with the
   bot's API key. Used when in-process import is unavailable, e.g. the
   hooks host runs the Gerrit JVM but cannot host Python deps.
3. **Default to ``m``** — deny-by-default per ADR-0005 risk note: "if
   classifier crashes, default to Tier M (deny-by-default) and log
   loudly so operator notices". Tier M still requires AI+1 + Human+1,
   so a misclassification cannot bypass human review.

Exit code is always 0 — a classifier failure must never reject the
upload (the patchset is already committed by the time the hook runs;
rejecting here just leaves the label unset and confuses operators).
The shell wrapper inspects stdout for the tier value.

Usage::

    classify_tier_via_backend.py --paths a.py b.py
    classify_tier_via_backend.py --paths-stdin <changed-files.txt
    classify_tier_via_backend.py --paths-stdin    # reads stdin

Audit lines are written to ``--audit-log`` (default
``/var/log/gerrit-tier-hook.log``) at INFO level. The shell wrapper
adds the ``change_id``/``existing_label``/``final_label`` fields after
calling SSH ``gerrit query`` / ``gerrit review`` — this script only
emits the ``computed_tier`` field, since it does not talk to Gerrit.
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

LOG = logging.getLogger("gerrit-tier-hook.classify")

VALID_TIERS = ("s", "m", "l", "x")
DEFAULT_FALLBACK_TIER = "m"
DEFAULT_AUDIT_LOG = "/var/log/gerrit-tier-hook.log"
DEFAULT_HTTP_TIMEOUT_SECONDS = 1.5  # ADR-0005 risk: hook latency SLA <2s.


def _configure_logging(audit_log_path: str) -> None:
    """Wire the root logger to write to the audit log + stderr.

    Weekly rotation (7 days × keep 8 weeks) is configured here so the
    operator runbook only has to point Gerrit at the hook script — no
    separate logrotate config required for the rotation itself. (The
    runbook still documents an OS-level logrotate fallback for
    operators who prefer it.)
    """
    LOG.setLevel(logging.INFO)
    LOG.propagate = False
    if LOG.handlers:
        return
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    file_ok = False
    try:
        Path(audit_log_path).parent.mkdir(parents=True, exist_ok=True)
        file_h = logging.handlers.TimedRotatingFileHandler(
            audit_log_path, when="W0", backupCount=8, utc=True,
        )
        file_h.setFormatter(fmt)
        LOG.addHandler(file_h)
        file_ok = True
    except OSError as exc:
        sys.stderr.write(
            f"gerrit-tier-hook: cannot open {audit_log_path}: {exc}; "
            "falling back to stderr-only logging\n"
        )
    if not file_ok:
        # Only attach a stderr handler when the file handler isn't
        # active. The shell wrapper writes its own audit lines and may
        # also redirect our stderr to the same file — adding a second
        # handler here would double every line in the log.
        stderr_h = logging.StreamHandler(sys.stderr)
        stderr_h.setFormatter(fmt)
        LOG.addHandler(stderr_h)


def _try_in_process(paths: list[str]) -> str | None:
    """Strategy 1: import G2's ``backend.governance.tier_classifier``.

    Returns the tier on success, ``None`` if the module is not yet
    deployed (G2 lands separately) or raises an unexpected exception.
    """
    try:
        from backend.governance import tier_classifier  # type: ignore[import-not-found]
    except ImportError:
        LOG.info("strategy=in-process status=unavailable reason=import-error")
        return None
    try:
        tier = tier_classifier.classify_paths(paths)
    except Exception as exc:  # noqa: BLE001 — classifier crash must not nuke the hook
        LOG.error("strategy=in-process status=crash error=%r", exc)
        return None
    tier = (tier or "").lower().strip()
    if tier not in VALID_TIERS:
        LOG.error("strategy=in-process status=invalid-tier returned=%r", tier)
        return None
    LOG.info("strategy=in-process status=ok computed_tier=%s", tier)
    return tier


def _try_https(paths: list[str], backend_url: str, api_key: str) -> str | None:
    """Strategy 2: ``POST <backend>/api/internal/classify-tier``."""
    endpoint = backend_url.rstrip("/") + "/api/internal/classify-tier"
    body = json.dumps({"paths": paths}).encode("utf-8")
    req = urllib.request.Request(
        endpoint, data=body, method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "gerrit-tier-hook/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_HTTP_TIMEOUT_SECONDS) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        LOG.error("strategy=https status=fail endpoint=%s error=%r", endpoint, exc)
        return None
    tier = str(data.get("tier", "")).lower().strip()
    if tier not in VALID_TIERS:
        LOG.error("strategy=https status=invalid-tier returned=%r", tier)
        return None
    LOG.info("strategy=https status=ok endpoint=%s computed_tier=%s", endpoint, tier)
    return tier


def classify(paths: list[str]) -> str:
    """Run the strategy chain, return tier letter (always one of VALID_TIERS).

    Falls back to ``m`` per the ADR-0005 deny-by-default rule. The
    fallback emits a loud ``ERROR`` line so the operator notices.
    """
    if not paths:
        # Empty path lists shouldn't reach the classifier — the shell
        # wrapper skips merge-empty changes — but guard anyway.
        LOG.warning("computed_tier=%s reason=empty-paths", DEFAULT_FALLBACK_TIER)
        return DEFAULT_FALLBACK_TIER

    tier = _try_in_process(paths)
    if tier is not None:
        return tier

    backend_url = os.environ.get("GERRIT_HOOK_BACKEND_URL", "").strip()
    api_key = os.environ.get("GERRIT_HOOK_BACKEND_API_KEY", "").strip()
    if backend_url and api_key:
        tier = _try_https(paths, backend_url, api_key)
        if tier is not None:
            return tier
    else:
        LOG.info("strategy=https status=skipped reason=no-credentials-configured")

    LOG.error(
        "computed_tier=%s reason=all-strategies-failed paths_count=%d",
        DEFAULT_FALLBACK_TIER, len(paths),
    )
    return DEFAULT_FALLBACK_TIER


def _read_paths(args: argparse.Namespace) -> list[str]:
    if args.paths_stdin:
        return [line.strip() for line in sys.stdin if line.strip()]
    return [p for p in (args.paths or []) if p.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--paths", nargs="*", default=None,
        help="Changed paths (space-separated).",
    )
    parser.add_argument(
        "--paths-stdin", action="store_true",
        help="Read newline-separated paths from stdin instead of --paths.",
    )
    parser.add_argument(
        "--audit-log", default=os.environ.get("GERRIT_HOOK_AUDIT_LOG", DEFAULT_AUDIT_LOG),
        help=f"Audit log path (default: {DEFAULT_AUDIT_LOG}).",
    )
    args = parser.parse_args(argv)
    _configure_logging(args.audit_log)
    paths = _read_paths(args)
    tier = classify(paths)
    sys.stdout.write(tier + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
