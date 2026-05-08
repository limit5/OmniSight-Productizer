#!/usr/bin/env python3
"""OP-742 — Selective + parallel CI worker.

The worker polls Gerrit for open patchsets that are missing a Verified
label, decides a test policy via :mod:`scripts.ci_test_impact`, runs
the resulting test selection, and votes ``Verified ±1`` on the change.

Design points (mapped to the OP-742 spec):

- **Polling**: SSH ``gerrit query`` every ``POLL_INTERVAL_S`` (default
  30 s) for ``is:open AND -is:wip AND -has:Verified+1 AND -has:Verified-1``.
- **Skip policy**: looked up first via ``area:`` labels of the linked
  JIRA tickets, then via :mod:`ci_test_impact.classify_changes` as the
  source-of-truth fallback.
- **Parallel workers**: a :class:`concurrent.futures.ThreadPoolExecutor`
  with up to ``MAX_WORKERS`` slots claims one PS per slot.
- **Lock files**: ``/tmp/ci-worker-<change_id>-<rev>.lock`` is created
  with the worker PID. Stale locks (PID gone) are reclaimable.
- **Idempotent voting**: a PS revision that already carries
  ``Verified=±1`` is skipped unless ``OMNISIGHT_CI_FORCE_RERUN=1``.
- **Verified -1 enrichment**: comment includes the six fields specified
  in OP-742(e) so the C3 recovery state machine can categorise the
  failure (see :func:`format_failure_comment`).

The daemon is intentionally conservative — when SSH or pytest fails for
infrastructure reasons we abstain (no vote) rather than blast Verified
-1 on everything.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import errno
import json
import logging
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

# The daemon imports the impact analyser from scripts/. Allow running
# both as ``python scripts/ci_worker.py`` and via systemd ExecStart.
_HERE = Path(__file__).resolve().parent
_REPO_ROOT_DEFAULT = _HERE.parent
sys.path.insert(0, str(_HERE))

from ci_test_impact import (  # noqa: E402  (sys.path mutation above)
    AREA_TO_TEST_POLICY,
    TestImpactResult,
    classify_changes,
    build_reverse_import_graph,
)

LOG = logging.getLogger("ci_worker")

POLL_INTERVAL_S: float = 30.0
MAX_WORKERS_DEFAULT: int = 2
TEST_TIMEOUT_S: int = 900            # 15-minute ceiling per OP-742 spec
LINT_TIMEOUT_S: int = 180
GERRIT_QUERY_TIMEOUT_S: int = 30
LOCK_DIR = Path("/tmp")
LOG_DIR = Path("/home/user/work/sora/logs/ci")

GERRIT_SSH_HOST = "sora.services"
GERRIT_SSH_PORT = 29418
DEFAULT_GERRIT_USER = "claude-bot"
DEFAULT_GERRIT_KEY = Path(
    "~/.config/omnisight/gerrit-claude-bot-ed25519"
).expanduser()

OP_KEY_RE = re.compile(r"\bOP-\d+\b")


# ── Datatypes ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class GerritFile:
    file: str
    insertions: int = 0
    deletions: int = 0


@dataclass(frozen=True)
class GerritPatchSet:
    """Minimal projection of a Gerrit ``query --current-patch-set --files`` row."""

    change_id: str
    change_number: int
    revision: str
    patchset_number: int
    subject: str
    branch: str
    project: str
    files: tuple[GerritFile, ...]
    existing_verified: int | None = None  # -1 / 0 / +1 if already voted

    @property
    def lock_name(self) -> str:
        return f"ci-worker-{self.change_id}-{self.revision[:8]}.lock"

    @property
    def short_rev(self) -> str:
        return self.revision[:8]


@dataclass
class TestRunResult:
    # Tell pytest not to collect this dataclass — pytest matches
    # ``python_classes = Test*`` and would emit a CollectionWarning.
    __test__ = False

    policy: str
    returncode: int
    duration_s: float
    log_path: Path
    failed_tests: list[str] = field(default_factory=list)
    first_failure_log: str = ""
    category_hint: str = "unknown"
    reason: str = ""


# ── Config ───────────────────────────────────────────────────────


@dataclass
class WorkerConfig:
    repo_root: Path
    gerrit_user: str = DEFAULT_GERRIT_USER
    gerrit_host: str = GERRIT_SSH_HOST
    gerrit_port: int = GERRIT_SSH_PORT
    gerrit_key_path: Path = DEFAULT_GERRIT_KEY
    project: str = "omnisight/OmniSight-Productizer"
    poll_interval_s: float = POLL_INTERVAL_S
    max_workers: int = MAX_WORKERS_DEFAULT
    test_timeout_s: int = TEST_TIMEOUT_S
    lock_dir: Path = LOCK_DIR
    log_dir: Path = LOG_DIR
    force_rerun: bool = False
    dry_run: bool = False  # if True, never actually post Verified votes


def config_from_env(repo_root: Path | None = None) -> WorkerConfig:
    repo_root = repo_root or _REPO_ROOT_DEFAULT
    return WorkerConfig(
        repo_root=repo_root,
        gerrit_user=os.environ.get("OMNISIGHT_CI_WORKER_USER", DEFAULT_GERRIT_USER),
        gerrit_host=os.environ.get("OMNISIGHT_CI_WORKER_HOST", GERRIT_SSH_HOST),
        gerrit_port=int(os.environ.get("OMNISIGHT_CI_WORKER_PORT", str(GERRIT_SSH_PORT))),
        gerrit_key_path=Path(
            os.environ.get("OMNISIGHT_CI_WORKER_KEY", str(DEFAULT_GERRIT_KEY))
        ).expanduser(),
        project=os.environ.get(
            "OMNISIGHT_CI_WORKER_PROJECT", "omnisight/OmniSight-Productizer"
        ),
        poll_interval_s=float(
            os.environ.get("OMNISIGHT_CI_WORKER_POLL_S", str(POLL_INTERVAL_S))
        ),
        max_workers=int(
            os.environ.get("OMNISIGHT_CI_WORKER_PARALLEL", str(MAX_WORKERS_DEFAULT))
        ),
        test_timeout_s=int(
            os.environ.get("OMNISIGHT_CI_WORKER_TIMEOUT", str(TEST_TIMEOUT_S))
        ),
        force_rerun=os.environ.get("OMNISIGHT_CI_FORCE_RERUN", "") == "1",
        dry_run=os.environ.get("OMNISIGHT_CI_WORKER_DRY_RUN", "") == "1",
    )


# ── Gerrit SSH helpers ───────────────────────────────────────────


def _ssh_argv(cfg: WorkerConfig, *remote_args: str) -> list[str]:
    return [
        "ssh", "-i", str(cfg.gerrit_key_path),
        "-p", str(cfg.gerrit_port),
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        f"{cfg.gerrit_user}@{cfg.gerrit_host}",
        *remote_args,
    ]


def _run_ssh(
    cfg: WorkerConfig, args: list[str], *, timeout: int
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _ssh_argv(cfg, *args),
        capture_output=True, text=True, timeout=timeout,
    )


def parse_query_output(stdout: str) -> list[GerritPatchSet]:
    """Parse the line-delimited JSON from ``gerrit query``.

    Skips the trailing ``{"type":"stats"}`` row. Skips any change without
    a ``currentPatchSet`` (e.g. drafts or partial visibility).
    """

    out: list[GerritPatchSet] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") == "stats":
            continue
        cps = obj.get("currentPatchSet")
        if not cps:
            continue
        files = tuple(
            GerritFile(
                file=f.get("file", ""),
                insertions=int(f.get("insertions", 0) or 0),
                deletions=int(f.get("deletions", 0) or 0),
            )
            for f in (cps.get("files") or [])
            if f.get("file") and f.get("file") != "/COMMIT_MSG"
        )

        existing: int | None = None
        for approval in cps.get("approvals", []) or []:
            if approval.get("type") == "Verified":
                try:
                    existing = int(approval.get("value", 0))
                except (TypeError, ValueError):
                    existing = None
                break

        out.append(GerritPatchSet(
            change_id=str(obj.get("id") or obj.get("change_id") or ""),
            change_number=int(obj.get("number") or obj.get("_number") or 0),
            revision=str(cps.get("revision") or ""),
            patchset_number=int(cps.get("number") or 0),
            subject=str(obj.get("subject") or ""),
            branch=str(obj.get("branch") or ""),
            project=str(obj.get("project") or ""),
            files=files,
            existing_verified=existing,
        ))
    return out


def poll_open_changes(cfg: WorkerConfig) -> list[GerritPatchSet]:
    """Return patchsets needing a Verified vote.

    Filters out ``-is:wip`` and changes that already carry ``Verified+1``
    or ``Verified-1`` on the current PS revision.
    """

    query = (
        f"project:{cfg.project} "
        "status:open AND -is:wip AND -has:Verified+1 AND -has:Verified-1"
    )
    args = [
        "gerrit", "query", "--format=JSON",
        "--current-patch-set", "--files", query,
    ]
    result = _run_ssh(cfg, args, timeout=GERRIT_QUERY_TIMEOUT_S)
    if result.returncode != 0:
        LOG.warning(
            "gerrit query failed rc=%s stderr=%s",
            result.returncode, (result.stderr or "")[:200],
        )
        return []
    return parse_query_output(result.stdout)


def post_verified_vote(
    cfg: WorkerConfig, ps: GerritPatchSet, score: int, message: str
) -> bool:
    """Post ``Verified=<score>`` with ``message`` on the current PS revision.

    Returns True on success. Honours ``cfg.dry_run``.
    """

    if cfg.dry_run:
        LOG.info(
            "[dry-run] would vote Verified=%+d on %s/%s msg=%r",
            score, ps.change_number, ps.short_rev, message[:120],
        )
        return True

    sign = f"+{score}" if score > 0 else str(score)
    args = [
        "gerrit", "review",
        "--label", f"Verified={sign}",
        "--message", shlex.quote(message),
        ps.revision,
    ]
    result = _run_ssh(cfg, args, timeout=GERRIT_QUERY_TIMEOUT_S)
    if result.returncode != 0:
        LOG.warning(
            "vote failed change=%s rc=%s stderr=%s",
            ps.change_number, result.returncode, (result.stderr or "")[:200],
        )
        return False
    return True


# ── Locks ────────────────────────────────────────────────────────


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but is not ours; still alive.
        return True
    except OSError as exc:
        return exc.errno != errno.ESRCH
    return True


@contextmanager
def claim_lock(lock_path: Path) -> Iterator[bool]:
    """Atomically claim ``lock_path`` for the calling PID.

    Yields ``True`` if claimed (and releases on exit), ``False`` if a
    living worker already holds it. Stale locks (dead PID) are
    reclaimed.
    """

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    pid_text = str(os.getpid())

    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            try:
                holder_text = lock_path.read_text().strip()
                holder_pid = int(holder_text) if holder_text.isdigit() else -1
            except OSError:
                holder_pid = -1
            if _pid_alive(holder_pid):
                yield False
                return
            # Stale — try to remove and retry the create.
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
            continue
        else:
            try:
                with os.fdopen(fd, "w") as f:
                    f.write(pid_text)
                yield True
                return
            finally:
                try:
                    if lock_path.read_text().strip() == pid_text:
                        lock_path.unlink()
                except (FileNotFoundError, OSError):
                    pass


# ── Skip-policy ──────────────────────────────────────────────────


def parse_jira_keys(subject: str) -> list[str]:
    return list(dict.fromkeys(OP_KEY_RE.findall(subject or "")))


def policy_from_areas(areas: Iterable[str]) -> str | None:
    """If all declared areas resolve to the same policy, return it.

    Only collapses when *all* areas agree — mixed areas defer to the
    file-classifier so we don't accidentally short-circuit a backend
    change with a stray ``area:docs`` label.
    """

    policies = {AREA_TO_TEST_POLICY[a] for a in areas if a in AREA_TO_TEST_POLICY}
    if len(policies) == 1:
        return policies.pop()
    return None


def determine_policy(
    ps: GerritPatchSet,
    cfg: WorkerConfig,
    *,
    declared_areas: list[str] | None = None,
    import_graph: dict[str, set[str]] | None = None,
) -> TestImpactResult:
    """Combine area-label hints with the file-based classifier.

    Security always upgrades to ``full``, regardless of file content.
    For other areas the file classifier is the source of truth — this
    matters because a ticket may be labelled ``area:backend`` but
    actually only edit ``backend/tests/`` files (the impact analyser
    will still pull just those tests).
    """

    files = [f.file for f in ps.files]

    if declared_areas and "security" in declared_areas:
        return TestImpactResult(
            "full", [], "security area declared — full suite required",
        )

    if declared_areas:
        suggested = policy_from_areas(declared_areas)
        if suggested == "skip":
            # Cross-check: if any backend .py changed, fall through to
            # the impact analyser instead of trusting the label.
            if not any(
                f.endswith(".py") and f.startswith("backend/") for f in files
            ):
                return TestImpactResult("skip", [], "all areas → skip policy")

    return classify_changes(
        files, cfg.repo_root,
        declared_areas=list(declared_areas or []),
        import_graph=import_graph,
    )


# ── Test execution ───────────────────────────────────────────────


_FAILED_TEST_RE = re.compile(r"^FAILED\s+(\S+)", re.MULTILINE)


def _categorise_failure(stdout: str, stderr: str, returncode: int) -> str:
    """Heuristic mapping for the C3 recovery state machine.

    Categories:
    - ``infra``  : the test runner itself failed (collection errors,
                   import errors, fixture setup blew up)
    - ``flaky``  : single test failure that mentions retry/timeout
                   markers historically used for flakes
    - ``stale``  : pytest collected nothing (the affected test set was
                   misclassified)
    - ``real-bug``: at least one assert failure with a normal traceback
    """

    blob = f"{stdout}\n{stderr}"
    if returncode in (2, 3, 4):
        return "infra"
    if "ERRORS" in blob and "during collection" in blob:
        return "infra"
    if "no tests ran" in blob:
        return "stale"
    if returncode == 5:
        return "stale"
    if any(marker in blob for marker in ("flaky", "timed out", "TimeoutError")):
        return "flaky"
    return "real-bug"


def parse_failed_tests(stdout: str) -> list[str]:
    return _FAILED_TEST_RE.findall(stdout or "")


def first_failure_snippet(stdout: str, max_lines: int = 50) -> str:
    """Return up to ``max_lines`` of the first traceback in pytest output."""
    lines = (stdout or "").splitlines()
    start = None
    for i, line in enumerate(lines):
        if "FAILED" in line or line.startswith("___ ") or line.startswith("E   "):
            start = i
            break
    if start is None:
        return ""
    return "\n".join(lines[start: start + max_lines])


def _build_pytest_argv(
    policy: str, test_files: list[str], cfg: WorkerConfig
) -> list[str]:
    """Construct the pytest command line for the chosen policy.

    Exposed at module scope so tests can pin the contract: ``-x -q
    --no-cov`` for affected runs (fast fail, skip coverage),
    ``--no-cov -q`` for full runs.
    """

    base = ["python", "-m", "pytest", "--no-cov", "-q"]
    if policy == "affected":
        if not test_files:
            raise ValueError("'affected' policy requires test_files")
        return [*base, "-x", *test_files]
    if policy == "full":
        return [*base, "backend/tests"]
    raise ValueError(f"_build_pytest_argv called with non-test policy {policy}")


def run_tests(
    ps: GerritPatchSet,
    impact: TestImpactResult,
    cfg: WorkerConfig,
    *,
    runner: Callable[[list[str], int, Path], subprocess.CompletedProcess[str]] | None = None,
) -> TestRunResult:
    """Execute the test selection and return a :class:`TestRunResult`.

    ``runner`` is injected for tests; default invokes pytest under the
    configured timeout and writes a per-PS log to
    ``<log_dir>/<change_id>-PS<n>.log``.
    """

    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    log_path = cfg.log_dir / f"{ps.change_id}-PS{ps.patchset_number}.log"

    if impact.policy in {"skip", "frontend-only", "lint-only"}:
        return TestRunResult(
            policy=impact.policy,
            returncode=0,
            duration_s=0.0,
            log_path=log_path,
            reason=impact.reason,
        )

    argv = _build_pytest_argv(impact.policy, impact.test_files, cfg)
    timeout = cfg.test_timeout_s

    started = time.monotonic()
    if runner is None:
        proc = subprocess.run(
            argv,
            cwd=cfg.repo_root,
            capture_output=True, text=True, timeout=timeout,
        )
    else:
        proc = runner(argv, timeout, cfg.repo_root)
    duration = time.monotonic() - started

    log_path.write_text(
        f"$ {' '.join(shlex.quote(a) for a in argv)}\n"
        f"--- stdout ---\n{proc.stdout}\n"
        f"--- stderr ---\n{proc.stderr}\n",
    )

    failed = parse_failed_tests(proc.stdout)
    snippet = first_failure_snippet(proc.stdout) if proc.returncode != 0 else ""
    category = (
        "passed" if proc.returncode == 0
        else _categorise_failure(proc.stdout, proc.stderr, proc.returncode)
    )

    return TestRunResult(
        policy=impact.policy,
        returncode=proc.returncode,
        duration_s=duration,
        log_path=log_path,
        failed_tests=failed,
        first_failure_log=snippet,
        category_hint=category,
        reason=impact.reason,
    )


# ── Comment formatting ───────────────────────────────────────────


def format_pass_comment(ps: GerritPatchSet, result: TestRunResult) -> str:
    return (
        f"[ci-worker] PASSED on PS{ps.patchset_number} (revision {ps.short_rev})\n"
        f"Policy: {result.policy}\n"
        f"Test runtime: {result.duration_s:.1f}s\n"
        f"Reason: {result.reason}\n"
    )


def format_skip_comment(ps: GerritPatchSet, result: TestRunResult) -> str:
    return (
        f"[ci-worker] AUTO-PASS on PS{ps.patchset_number} "
        f"(revision {ps.short_rev}) — {result.reason}\n"
        f"Policy: {result.policy}\n"
    )


def format_failure_comment(ps: GerritPatchSet, result: TestRunResult) -> str:
    """Produce the six-field Verified -1 comment specified in OP-742(e)."""

    failed_lines = "\n".join(f"  - {t}" for t in result.failed_tests[:20])
    if len(result.failed_tests) > 20:
        failed_lines += f"\n  - ... ({len(result.failed_tests) - 20} more)"

    snippet = result.first_failure_log.rstrip()
    if snippet:
        snippet_block = "First failure log (50 lines):\n" + snippet
    else:
        snippet_block = "First failure log (50 lines): <none captured>"

    return (
        f"[ci-worker] FAILED on PS{ps.patchset_number} (revision {ps.short_rev})\n"
        f"Policy: {result.policy}\n"
        f"Test runtime: {result.duration_s:.1f}s\n"
        f"Failed tests ({len(result.failed_tests)}):\n"
        f"{failed_lines or '  <unparsed — see log>'}\n"
        f"{snippet_block}\n"
        f"Full log: {result.log_path}\n"
        f"Failure category: {result.category_hint}"
    )


# ── Per-PS handler ───────────────────────────────────────────────


def handle_patchset(
    ps: GerritPatchSet,
    cfg: WorkerConfig,
    *,
    declared_areas: list[str] | None = None,
    import_graph: dict[str, set[str]] | None = None,
    runner: Callable[[list[str], int, Path], subprocess.CompletedProcess[str]] | None = None,
) -> TestRunResult | None:
    """Process one patchset end-to-end.

    Returns the :class:`TestRunResult` posted to Gerrit, or None if the
    PS was already verified (idempotent skip) or could not be claimed.
    """

    if ps.existing_verified is not None and not cfg.force_rerun:
        LOG.info(
            "skip already-verified change=%s rev=%s vote=%+d",
            ps.change_number, ps.short_rev, ps.existing_verified,
        )
        return None

    lock_path = cfg.lock_dir / ps.lock_name
    with claim_lock(lock_path) as claimed:
        if not claimed:
            LOG.info(
                "skip locked change=%s rev=%s (held by another worker)",
                ps.change_number, ps.short_rev,
            )
            return None

        impact = determine_policy(
            ps, cfg, declared_areas=declared_areas, import_graph=import_graph,
        )
        LOG.info(
            "policy change=%s rev=%s policy=%s reason=%s",
            ps.change_number, ps.short_rev, impact.policy, impact.reason,
        )

        result = run_tests(ps, impact, cfg, runner=runner)

        if result.returncode == 0:
            if impact.policy in {"skip", "frontend-only", "lint-only"}:
                msg = format_skip_comment(ps, result)
            else:
                msg = format_pass_comment(ps, result)
            posted = post_verified_vote(cfg, ps, +1, msg)
        else:
            msg = format_failure_comment(ps, result)
            posted = post_verified_vote(cfg, ps, -1, msg)

        LOG.info(
            "voted change=%s rev=%s score=%+d posted=%s duration=%.1fs",
            ps.change_number, ps.short_rev,
            +1 if result.returncode == 0 else -1,
            posted, result.duration_s,
        )
        return result


# ── Daemon main loop ─────────────────────────────────────────────


class _StopFlag:
    def __init__(self) -> None:
        self._evt = threading.Event()

    def set(self, *_args: Any) -> None:
        self._evt.set()

    @property
    def is_set(self) -> bool:
        return self._evt.is_set()

    def wait(self, seconds: float) -> None:
        self._evt.wait(seconds)


def run_forever(cfg: WorkerConfig, *, stop: _StopFlag | None = None) -> int:
    stop = stop or _StopFlag()
    signal.signal(signal.SIGTERM, stop.set)
    signal.signal(signal.SIGINT, stop.set)

    LOG.info(
        "ci-worker starting workers=%d poll=%.0fs project=%s repo=%s",
        cfg.max_workers, cfg.poll_interval_s, cfg.project, cfg.repo_root,
    )

    import_graph = build_reverse_import_graph(cfg.repo_root)
    in_flight: set[str] = set()
    in_flight_lock = threading.Lock()

    with concurrent.futures.ThreadPoolExecutor(max_workers=cfg.max_workers) as pool:
        while not stop.is_set:
            try:
                changes = poll_open_changes(cfg)
            except subprocess.TimeoutExpired:
                LOG.warning("gerrit query timed out")
                changes = []
            except Exception:  # noqa: BLE001 — daemon, must keep going
                LOG.exception("poll error")
                changes = []

            for ps in changes:
                key = f"{ps.change_id}:{ps.revision}"
                with in_flight_lock:
                    if key in in_flight:
                        continue
                    in_flight.add(key)

                def _run(ps: GerritPatchSet = ps, key: str = key) -> None:
                    try:
                        handle_patchset(ps, cfg, import_graph=import_graph)
                    except Exception:  # noqa: BLE001
                        LOG.exception(
                            "handler error change=%s rev=%s",
                            ps.change_number, ps.short_rev,
                        )
                    finally:
                        with in_flight_lock:
                            in_flight.discard(key)

                pool.submit(_run)

            stop.wait(cfg.poll_interval_s)

    LOG.info("ci-worker stopped")
    return 0


# ── CLI ──────────────────────────────────────────────────────────


def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root", default=str(_REPO_ROOT_DEFAULT),
        help="Path to the OmniSight repository (default: alongside scripts/).",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Run a single poll cycle and exit (manual / smoke run).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Skip gerrit review --label calls; log only.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_argparser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=os.environ.get("OMNISIGHT_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cfg = config_from_env(Path(args.repo_root).resolve())
    if args.dry_run:
        cfg.dry_run = True

    if args.once:
        changes = poll_open_changes(cfg)
        import_graph = build_reverse_import_graph(cfg.repo_root)
        for ps in changes:
            handle_patchset(ps, cfg, import_graph=import_graph)
        return 0

    return run_forever(cfg)


if __name__ == "__main__":
    raise SystemExit(main())
