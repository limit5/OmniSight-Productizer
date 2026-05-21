"""[OP-965] AUDIT-17 — continuous staging gate status emitters.

Produces the JSONL gate signals that ``scripts/release_milestone_checker.py``'s
``JsonlStatusReader`` consumes for the R3 ``ci_canary`` and ``smoke_suite``
promotion gates. Before this module existed those files had no producer in
production (OP-925 R3 cascade, 2026-05-12): the checker correctly emitted
``milestone_blocked`` forever and every release went through a manual
"poke synthetic green JSONL" dance.

Two suites, each run by its own systemd timer against the develop-tip
staging deployment (OP-878 / OP-767 blue-green staging; OP-927 D6):

  ``canary``  exercises the staging HTTP surface (liveness + readiness
              probes) and appends a ``{"suite": "canary", ...}`` line to
              ``canary-status.jsonl``.
  ``smoke``   runs ``scripts/prod_smoke_test.py`` against the staging base
              URL and appends a ``{"suite": "smoke", ...}`` line to
              ``smoke-status.jsonl``. ``release_milestone_checker`` requires
              the smoke status be < 4h old, so the timer cadence stays well
              under that (see ``deploy/systemd/staging-gate-smoke.timer``).

Record shape — must satisfy ``JsonlStatusReader.latest``
(``release_milestone_checker.py:296-334``)::

  {"suite": "canary"|"smoke", "status": "green"|"red", "branch": "develop",
   "revision": "<develop tip sha>", "run_id": "...", "timestamp": "...Z",
   "detail": "...", "bundle_id": "...",
   "backend_digest": "sha256:...", "frontend_digest": "sha256:...",
   "observed_api_version": {...}}

``status`` is ``green`` only when the suite fully passed. ``release_milestone
_checker`` treats anything outside {green, ok, pass, passed, success} as a
blocked gate, which is the conservative behaviour we want on failure.

§6 audit trail — every write also best-effort logs to the release-audit DB
via :func:`backend.audit.log` (the same generic ``audit_log`` sink that
AUDIT-13 / AUDIT-16 wire from ``backend.agents.auto_promote_main``; the
dedicated ``release_audit`` table re-target is a separate backend follow-up).
The audit write never blocks or fails the gate run.

§5 operator override — when a producer goes red the operator can still ship
via the ``release:force-promote`` mechanism (ADR-0019 / AUDIT-18, OP-966).
This module only *reports* status — it never decides promotion — so it never
interferes with the override path.

Exit codes (``main``)
---------------------
``0``  suite green — JSONL line + audit row written.
``2``  suite red — JSONL line still written (so the gate sees the red), then
       exit non-zero so the systemd ``OnFailure=`` alert hook fires.
``3``  configuration / environment error (e.g. cannot resolve develop tip) —
       nothing exercised, refuse to run, no JSONL line.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]

SUITE_CANARY = "canary"
SUITE_SMOKE = "smoke"
SUITES = (SUITE_CANARY, SUITE_SMOKE)

STATUS_GREEN = "green"
STATUS_RED = "red"
BRANCH = "develop"

# Where ``release_milestone_checker`` reads the gate signals from. Mirrors
# its ``DEFAULT_CANARY_LOG`` / ``DEFAULT_SMOKE_LOG`` and the
# ``OMNISIGHT_RELEASE_CANARY_LOG`` / ``OMNISIGHT_RELEASE_SMOKE_LOG`` env
# overrides used by ``deploy/systemd/release-milestone-checker.service`` —
# producer and consumer must agree on the path.
DEFAULT_CANARY_LOG = Path("/home/user/work/sora/logs/release-milestone/canary-status.jsonl")
DEFAULT_SMOKE_LOG = Path("/home/user/work/sora/logs/release-milestone/smoke-status.jsonl")

DEFAULT_STAGING_URL = "http://localhost:18080"
DEFAULT_GERRIT_PROJECT = "omnisight/OmniSight-Productizer"

# Liveness + readiness probes — ``/readyz`` only returns 200 when the
# process can actually serve, so a green canary means the staging deploy is
# live, not merely "the port is open". See ``backend/routers/health.py``.
DEFAULT_CANARY_PATHS = ("/healthz", "/readyz")
DEFAULT_CANARY_ATTEMPTS = 5
DEFAULT_CANARY_INTERVAL_SECONDS = 6.0
DEFAULT_HTTP_TIMEOUT_SECONDS = 15.0

# ``dag1`` = compile-flash host_native fast path (~60s). The full pair adds
# the heavy aarch64 cross-compile DAG; for an every-N-min staging gate the
# fast subset is the sane default — override with OMNISIGHT_STAGING_SMOKE_SUBSET.
DEFAULT_SMOKE_SUBSET = "dag1"
DEFAULT_SMOKE_TIMEOUT_SECONDS = 600

AUDIT_ACTION = {
    SUITE_CANARY: "release.staging_gate_canary",
    SUITE_SMOKE: "release.staging_gate_smoke",
}

# Type aliases for the injectable seams (kept testable, à la
# ``backend.agents.auto_promote_main``).
Runner = Callable[..., "subprocess.CompletedProcess[str]"]
HttpOpener = Callable[[str, float], tuple[int, str]]
Sleeper = Callable[[float], None]
Clock = Callable[[], datetime]
AuditSink = Callable[[str, dict[str, Any]], None]
SuiteProbe = Callable[[], tuple[bool, str]]
EvidenceProbe = Callable[[], dict[str, Any]]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_z(dt: datetime) -> str:
    """ISO-8601 with a literal ``Z`` — matches ``scripts/canary_pipeline.py``
    and is parsed by ``release_milestone_checker.parse_timestamp``."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_run_id(suite: str, dt: datetime) -> str:
    return f"staging-{suite}-{dt.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"


# ───────────────────────── pure record helpers ──────────────────────────


@dataclass(frozen=True)
class GateRecord:
    suite: str
    status: str
    revision: str
    run_id: str
    timestamp: str
    detail: str
    evidence: dict[str, Any] | None = None

    def to_jsonl_obj(self) -> dict[str, Any]:
        obj = {
            "suite": self.suite,
            "status": self.status,
            "branch": BRANCH,
            "revision": self.revision,
            "run_id": self.run_id,
            "timestamp": self.timestamp,
            "detail": self.detail,
        }
        if self.evidence:
            obj.update(self.evidence)
        return obj


def build_record(
    *,
    suite: str,
    ok: bool,
    detail: str,
    revision: str,
    run_id: str,
    now: datetime,
    evidence: dict[str, Any] | None = None,
) -> GateRecord:
    return GateRecord(
        suite=suite,
        status=STATUS_GREEN if ok else STATUS_RED,
        revision=revision,
        run_id=run_id,
        timestamp=iso_z(now),
        detail=detail[:2000],
        evidence=evidence,
    )


def append_jsonl(path: Path, obj: dict[str, Any]) -> None:
    """Append one JSON object as a line. Creates parent dirs. Never truncates
    — the consumer keeps the newest matching record, so history is fine."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(obj, ensure_ascii=False, sort_keys=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


# ───────────────────────── develop-tip resolution ───────────────────────


def _gerrit_query_argv(
    *, host: str, port: int, key_path: Path | None, query: str
) -> list[str]:
    argv = ["ssh"]
    if key_path is not None:
        argv += ["-i", str(key_path)]
    argv += [
        "-p", str(port),
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        host,
        "gerrit", "query",
        "--format=JSON",
        # Required on Gerrit 3.13: ``gerrit query`` no longer emits the
        # ``currentPatchSet`` block by default (see OP-959 and
        # ``release_milestone_checker.SshGerritClient._query``).
        "--current-patch-set",
        query,
    ]
    return argv


def resolve_develop_tip(
    *,
    runner: Runner,
    host: str,
    port: int,
    key_path: Path | None,
    project: str,
    timeout: int = 30,
) -> str:
    """Return the develop-tip commit SHA Gerrit currently has merged.

    Mirrors ``release_milestone_checker.SshGerritClient.develop_tip`` so the
    ``revision`` we stamp into the JSONL line matches the one the checker
    queries for that gate. Raises ``RuntimeError`` on any failure — the
    caller turns that into exit code 3 (we cannot honestly emit a gate
    status without knowing which revision it is for).
    """
    argv = _gerrit_query_argv(
        host=host, port=port, key_path=key_path,
        query=f"project:{project} branch:{BRANCH} status:merged",
    )
    proc = runner(argv, capture_output=True, text=True, timeout=timeout, check=True)
    for raw in (proc.stdout or "").splitlines():
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict) or row.get("type") == "stats":
            continue
        revision = str((row.get("currentPatchSet") or {}).get("revision") or "")
        if revision:
            return revision
    raise RuntimeError(
        "Gerrit develop query returned no merged change with "
        "currentPatchSet.revision (expected `gerrit query --current-patch-set`)"
    )


# ───────────────────────── suite probers ────────────────────────────────


def _default_http_opener(url: str, timeout: float) -> tuple[int, str]:
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "omnisight-staging-gate"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(2048).decode("utf-8", "replace")
            return int(resp.status), body
    except urllib.error.HTTPError as exc:
        return int(exc.code), ""


def http_probe(
    *,
    base_url: str,
    paths: Sequence[str],
    attempts: int,
    interval: float,
    timeout: float,
    opener: HttpOpener,
    sleeper: Sleeper,
) -> tuple[bool, str]:
    """All ``paths`` must answer 2xx within ``attempts`` tries each."""
    base = base_url.rstrip("/")
    failures: list[str] = []
    for path in paths:
        url = base + path
        last_detail = "no attempt"
        ok = False
        for attempt in range(1, max(1, attempts) + 1):
            try:
                status, _body = opener(url, timeout)
            except Exception as exc:  # noqa: BLE001 — probe must never raise
                last_detail = f"{type(exc).__name__}: {exc}"
            else:
                if 200 <= status < 300:
                    ok = True
                    break
                last_detail = f"HTTP {status}"
            if attempt < attempts:
                sleeper(interval)
        if not ok:
            failures.append(f"{path}: {last_detail}")
    if failures:
        return False, "; ".join(failures)
    return True, f"{len(paths)} probe(s) OK on {base}"


def api_version_evidence(
    *,
    base_url: str,
    timeout: float,
    opener: HttpOpener,
) -> dict[str, Any]:
    """Observe staging ``/api/version`` and extract RT-05d JSONL evidence."""
    url = base_url.rstrip("/") + "/api/version"
    status, body = opener(url, timeout)
    if not (200 <= status < 300):
        raise RuntimeError(f"/api/version: HTTP {status}")
    try:
        observed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"/api/version returned invalid JSON: {exc}") from exc
    if not isinstance(observed, dict):
        raise RuntimeError("/api/version returned non-object JSON")

    bundle_id = observed.get("bundle_id")
    backend_digest = observed.get("backend_image_digest")
    frontend_digest = observed.get("frontend_image_digest")
    missing = [
        name for name, value in (
            ("bundle_id", bundle_id),
            ("backend_image_digest", backend_digest),
            ("frontend_image_digest", frontend_digest),
        )
        if not isinstance(value, str) or not value
    ]
    if missing:
        raise RuntimeError(
            "/api/version missing staging evidence field(s): " + ",".join(missing)
        )
    return {
        "bundle_id": bundle_id,
        "backend_digest": backend_digest,
        "frontend_digest": frontend_digest,
        "observed_api_version": observed,
    }


def smoke_probe(
    *,
    base_url: str,
    subset: str,
    timeout: int,
    runner: Runner,
    script_path: Path | None = None,
    python_exe: str = sys.executable,
) -> tuple[bool, str]:
    """Run ``scripts/prod_smoke_test.py`` against the staging base URL.

    Exit 0 → green. Any non-zero exit (1 = DAG submit failed, 2 = verification
    failed) or a launch failure → red, with a trailing slice of the script's
    output as the detail so the alert is actionable.
    """
    script = script_path or (REPO_ROOT / "scripts" / "prod_smoke_test.py")
    argv = [python_exe, str(script), base_url.rstrip("/"), "--subset", subset]
    try:
        proc = runner(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return False, f"prod_smoke_test.py timed out after {timeout}s"
    except Exception as exc:  # noqa: BLE001
        return False, f"prod_smoke_test.py failed to launch: {type(exc).__name__}: {exc}"
    tail = ((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()[-8:]
    tail_text = " | ".join(tail)
    if proc.returncode == 0:
        return True, f"prod_smoke_test.py --subset {subset} passed"
    return False, f"prod_smoke_test.py exit {proc.returncode}: {tail_text}"


# ───────────────────────── audit sink (§6) ──────────────────────────────


def _write_audit(action: str, payload: dict[str, Any]) -> None:
    """Best-effort release-audit row. Mirrors
    ``backend.agents.auto_promote_main._write_audit`` — fire ``backend.audit
    .log`` on a fresh event loop, swallow everything. NEVER raises and never
    blocks the gate run; an unreachable DB just means no audit row."""

    async def _run() -> None:
        from backend import audit

        await audit.log(
            action=action,
            entity_kind="release_gate",
            entity_id=str(payload.get("suite") or "staging"),
            before=None,
            after=payload,
            actor="staging_gate",
        )

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        log.debug("staging-gate audit skipped: event loop already running")
        return
    try:
        asyncio.run(_run())
    except Exception:  # noqa: BLE001
        log.warning("staging-gate audit write failed for %s", action, exc_info=True)


# ───────────────────────── orchestration ────────────────────────────────


@dataclass(frozen=True)
class GateRunResult:
    exit_code: int
    record: GateRecord | None
    detail: str


def run_gate(
    *,
    suite: str,
    revision: str,
    probe: SuiteProbe,
    out_path: Path,
    audit_sink: AuditSink = _write_audit,
    clock: Clock = utc_now,
    run_id: str | None = None,
    evidence_probe: EvidenceProbe | None = None,
) -> GateRunResult:
    """Exercise one suite, append the JSONL gate line, write the audit row.

    Returns exit code 0 (green) or 2 (red). The JSONL line is written in
    *both* cases — a red gate signal is the whole point on failure (the
    checker then emits ``milestone_blocked`` with a non-stale revision).
    """
    if suite not in SUITES:
        raise ValueError(f"unknown suite {suite!r}")
    now = clock()
    rid = run_id or make_run_id(suite, now)
    try:
        ok, detail = probe()
    except Exception as exc:  # noqa: BLE001 — a probe crash is just "red"
        ok, detail = False, f"probe raised {type(exc).__name__}: {exc}"
    evidence: dict[str, Any] | None = None
    if evidence_probe is not None:
        try:
            evidence = evidence_probe()
        except Exception as exc:  # noqa: BLE001 — required evidence missing = red
            ok = False
            suffix = f"staging evidence failed: {type(exc).__name__}: {exc}"
            detail = f"{detail}; {suffix}" if detail else suffix
    record = build_record(
        suite=suite,
        ok=ok,
        detail=detail,
        revision=revision,
        run_id=rid,
        now=now,
        evidence=evidence,
    )
    append_jsonl(out_path, record.to_jsonl_obj())
    # §6 — best-effort, after the JSONL write so a DB hiccup never costs the gate.
    try:
        audit_sink(AUDIT_ACTION[suite], record.to_jsonl_obj())
    except Exception:  # noqa: BLE001
        log.warning("staging-gate audit_sink raised for %s", suite, exc_info=True)
    return GateRunResult(
        exit_code=0 if ok else 2,
        record=record,
        detail=detail,
    )


# ───────────────────────── CLI ──────────────────────────────────────────


def _build_canary_probe(args: argparse.Namespace) -> SuiteProbe:
    paths = tuple(
        p.strip() for p in (args.canary_paths or "").split(",") if p.strip()
    ) or DEFAULT_CANARY_PATHS
    return lambda: http_probe(
        base_url=args.base_url,
        paths=paths,
        attempts=args.canary_attempts,
        interval=args.canary_interval,
        timeout=args.http_timeout,
        opener=_default_http_opener,
        sleeper=time.sleep,
    )


def _build_smoke_probe(args: argparse.Namespace) -> SuiteProbe:
    return lambda: smoke_probe(
        base_url=args.base_url,
        subset=args.smoke_subset,
        timeout=args.smoke_timeout,
        runner=subprocess.run,
    )


def _build_evidence_probe(args: argparse.Namespace) -> EvidenceProbe:
    return lambda: api_version_evidence(
        base_url=args.base_url,
        timeout=args.http_timeout,
        opener=_default_http_opener,
    )


def _emit_status_line(record: GateRecord) -> None:
    """Print a single structured JSON line to stdout (the systemd unit appends
    it to a dedicated log; mirrors ``scripts/canary_pipeline.emit_status``)."""
    obj = {
        "timestamp": record.timestamp,
        "level": "INFO" if record.status == STATUS_GREEN else "DEGRADED",
        "event": f"staging_gate_{record.suite}_{record.status}",
        **record.to_jsonl_obj(),
    }
    print(json.dumps(obj, ensure_ascii=False, sort_keys=True), flush=True)


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--suite", choices=SUITES, required=True)
    p.add_argument(
        "--base-url",
        default=os.environ.get("OMNISIGHT_STAGING_URL", DEFAULT_STAGING_URL),
        help="staging deployment base URL (default: $OMNISIGHT_STAGING_URL or %(default)s)",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=None,
        help="JSONL file to append to (default: per-suite checker path / env)",
    )
    p.add_argument(
        "--revision",
        default=None,
        help="develop-tip SHA to stamp (default: query Gerrit). Set by CI when "
        "it already knows the tip it built.",
    )
    # Gerrit SSH (only used when --revision is not given). Same env var names
    # as release_milestone_checker / the systemd release units.
    p.add_argument("--gerrit-host", default=os.environ.get("OMNISIGHT_GERRIT_SSH_HOST", "codex-bot@sora.services"))
    p.add_argument("--gerrit-port", type=int, default=int(os.environ.get("OMNISIGHT_GERRIT_SSH_PORT", "29418")))
    p.add_argument("--gerrit-project", default=os.environ.get("OMNISIGHT_GERRIT_PROJECT", DEFAULT_GERRIT_PROJECT))
    p.add_argument(
        "--gerrit-key",
        type=Path,
        default=Path(os.environ.get("OMNISIGHT_GIT_SSH_KEY_PATH", "~/.config/omnisight/gerrit-codex-bot-ed25519")).expanduser(),
    )
    # Canary knobs.
    p.add_argument("--canary-paths", default=os.environ.get("OMNISIGHT_STAGING_CANARY_PATHS", ",".join(DEFAULT_CANARY_PATHS)))
    p.add_argument("--canary-attempts", type=int, default=int(os.environ.get("OMNISIGHT_STAGING_CANARY_ATTEMPTS", str(DEFAULT_CANARY_ATTEMPTS))))
    p.add_argument("--canary-interval", type=float, default=float(os.environ.get("OMNISIGHT_STAGING_CANARY_INTERVAL", str(DEFAULT_CANARY_INTERVAL_SECONDS))))
    p.add_argument("--http-timeout", type=float, default=float(os.environ.get("OMNISIGHT_STAGING_HTTP_TIMEOUT", str(DEFAULT_HTTP_TIMEOUT_SECONDS))))
    # Smoke knobs.
    p.add_argument("--smoke-subset", default=os.environ.get("OMNISIGHT_STAGING_SMOKE_SUBSET", DEFAULT_SMOKE_SUBSET))
    p.add_argument("--smoke-timeout", type=int, default=int(os.environ.get("OMNISIGHT_STAGING_SMOKE_TIMEOUT", str(DEFAULT_SMOKE_TIMEOUT_SECONDS))))
    p.add_argument("--no-audit", action="store_true", help="skip the §6 release-audit write (for local dry runs)")
    return p


def _default_out_path(suite: str) -> Path:
    if suite == SUITE_CANARY:
        return Path(os.environ.get("OMNISIGHT_RELEASE_CANARY_LOG", str(DEFAULT_CANARY_LOG)))
    return Path(os.environ.get("OMNISIGHT_RELEASE_SMOKE_LOG", str(DEFAULT_SMOKE_LOG)))


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = build_arg_parser().parse_args(argv)

    out_path = args.out or _default_out_path(args.suite)

    revision = args.revision
    if not revision:
        try:
            revision = resolve_develop_tip(
                runner=subprocess.run,
                host=args.gerrit_host,
                port=args.gerrit_port,
                key_path=args.gerrit_key if args.gerrit_key else None,
                project=args.gerrit_project,
            )
        except Exception as exc:  # noqa: BLE001
            log.error("cannot resolve develop tip from Gerrit: %s", exc)
            return 3

    probe = _build_canary_probe(args) if args.suite == SUITE_CANARY else _build_smoke_probe(args)
    audit_sink: AuditSink = (lambda *_a, **_k: None) if args.no_audit else _write_audit

    result = run_gate(
        suite=args.suite,
        revision=revision,
        probe=probe,
        out_path=out_path,
        audit_sink=audit_sink,
        evidence_probe=_build_evidence_probe(args),
    )
    if result.record is not None:
        _emit_status_line(result.record)
    return result.exit_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
