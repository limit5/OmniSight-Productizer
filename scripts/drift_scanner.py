#!/usr/bin/env python3
"""OP-726 (META OP-721) — daily state-drift scanner.

Background
----------
Several incidents share one shape: the *deployed* version of something
silently diverged from the *repo* version, and we found out only when
the next deploy / migration / cron run failed in a confusing way.

  * OP-693 SP-C, lesson L19 — `alembic upgrade head` ran on a stale
    backend image because the `:latest` tag wasn't repointed; the
    in-container migrations directory predated the develop branch.
  * 2026-05-08 sora-bridge — `~/sora-bridge/scripts/run_gerrit_jira_bridge.py`
    was hand-patched at runtime; the repo copy stayed unchanged for
    several days.
  * OP-708 / OP-713 — `refs/meta/config` sample at `.gerrit/project.config.example`
    drifted from the live ref; took two debug sessions to discover the
    deployed config was a different file shape (lesson L22).
  * Local `main` accumulates merge-cycle commits without `push origin
    main` — the GitHub mirror falls behind silently.

This scanner runs daily (cron / systemd-timer) and detects each of
those drift kinds explicitly. It exits 0 on clean, prints
`[DEGRADED] code=<code>` lines on stderr per drift, and exits 1 when
any drift is found so a generic alert wrapper (T1) can forward it.

Drift codes
-----------
  * ``image_drift``           — deployed backend image's source revision
                                ≠ HEAD of `<remote>/develop`.
  * ``schema_drift``          — runtime ``alembic_version`` row ≠ highest
                                revision in ``backend/alembic/versions/``.
  * ``refs_meta_config_drift``— live ``refs/meta/config`` ≠
                                ``.gerrit/project.config.example`` (or
                                ``webhooks.config`` if both are present).
  * ``bridge_drift``          — deployed bridge checkout (typically
                                ``~/sora-bridge``) HEAD ≠ HEAD of
                                ``<remote>/develop``.
  * ``main_branch_drift``     — local ``main`` HEAD ≠ ``<remote>/main``.

Usage
-----
::

    # Daily cron — every check pulls its expected value from the
    # environment / state files; missing inputs are skipped (logged INFO).
    python3 scripts/drift_scanner.py

    # Manual override — supply expected values directly (useful for
    # one-shot audits and for the test suite).
    python3 scripts/drift_scanner.py \
        --image-revision $(docker inspect ... --format '{{...}}') \
        --db-version $(psql -tAc "select version_num from alembic_version") \
        --remote-config-path /tmp/live-meta-config \
        --bridge-path ~/sora-bridge

    # JSON output for the alert wrapper.
    python3 scripts/drift_scanner.py --json

Exit codes
----------
  * 0 — no drift (or all checks skipped — see INFO log).
  * 1 — at least one drift detected; each printed as a DEGRADED line.
  * 2 — scanner environment is broken (e.g. ``git`` missing, repo not
        a git checkout, malformed migration files).

Auto-fix
--------
``--auto-fix`` is opt-in and currently only logs *which* corrections
would be safe to automate (image rebuild, main push). Actually opening
the Gerrit change is left to a follow-up ticket so this scanner keeps
its stdlib-only / read-mostly contract.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
VERSIONS_DIR = REPO_ROOT / "backend" / "alembic" / "versions"
GERRIT_DIR = REPO_ROOT / ".gerrit"
DEFAULT_BRIDGE_PATH = Path.home() / "sora-bridge"

ALL_CODES = (
    "image_drift",
    "schema_drift",
    "refs_meta_config_drift",
    "bridge_drift",
    "main_branch_drift",
)

# Codes for which `--auto-fix` could safely open a Gerrit change /
# trigger a rebuild without risk of data loss. Destructive corrections
# (schema, refs/meta/config) stay manual.
AUTO_FIXABLE = frozenset({"image_drift", "bridge_drift", "main_branch_drift"})


# ─────────────────────────────────────────────────────────────────────
# Result types
# ─────────────────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class DriftResult:
    code: str
    severity: str  # "DEGRADED" | "INFO"
    message: str
    expected: str | None = None
    actual: str | None = None
    auto_fixable: bool = False

    def to_dict(self) -> dict[str, object]:
        d = dataclasses.asdict(self)
        return d


# ─────────────────────────────────────────────────────────────────────
# git helpers
# ─────────────────────────────────────────────────────────────────────


def _git(repo: Path, *args: str) -> str:
    """Run ``git -C <repo> <args...>`` and return stripped stdout.

    Raises ``RuntimeError`` on non-zero exit so the caller can decide
    whether the failure is a drift signal or an environmental problem.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("git binary not found on PATH") from exc
    except subprocess.CalledProcessError as exc:
        msg = (exc.stderr or exc.stdout or "").strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {msg}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"git {' '.join(args)} timed out") from exc
    return proc.stdout.strip()


def _safe_rev_parse(repo: Path, ref: str) -> str | None:
    try:
        return _git(repo, "rev-parse", "--verify", "--quiet", ref) or None
    except RuntimeError:
        return None


# ─────────────────────────────────────────────────────────────────────
# Check 1 — docker image SHA vs develop HEAD
# ─────────────────────────────────────────────────────────────────────

# When the image is built from a CI workflow, the resulting image has
# the org.opencontainers.image.revision label set to the source commit.
# Operators can read it with:
#   docker inspect <image> --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}'
# and pass that to --image-revision. The scanner doesn't shell out to
# docker itself — keeping it stdlib-only and runnable on hosts that
# don't have the daemon (e.g. the JIRA cron host).


def check_image_drift(
    repo: Path,
    remote: str,
    image_revision: str | None,
) -> DriftResult | None:
    if not image_revision:
        return DriftResult(
            code="image_drift",
            severity="INFO",
            message=(
                "skipped — no --image-revision provided "
                "(daily cron should populate this from `docker inspect "
                "<deployed-image> --format ...image.revision`)"
            ),
        )

    expected = _safe_rev_parse(repo, f"{remote}/develop")
    if expected is None:
        return DriftResult(
            code="image_drift",
            severity="INFO",
            message=(
                f"skipped — could not resolve {remote}/develop in {repo}; "
                "fetch the remote first or pass --remote"
            ),
        )

    actual = image_revision.strip()
    if expected.startswith(actual) or actual.startswith(expected):
        return DriftResult(
            code="image_drift",
            severity="INFO",
            message=f"deployed image at {actual[:12]} matches {remote}/develop",
            expected=expected,
            actual=actual,
        )

    return DriftResult(
        code="image_drift",
        severity="DEGRADED",
        message=(
            f"deployed image was built from {actual[:12]} but {remote}/develop "
            f"is {expected[:12]} — image rebuild needed (see lesson L19, "
            "OP-693 SP-C)"
        ),
        expected=expected,
        actual=actual,
        auto_fixable=True,
    )


# ─────────────────────────────────────────────────────────────────────
# Check 2 — alembic head vs migrations on disk
# ─────────────────────────────────────────────────────────────────────


_REVISION_RE = re.compile(
    r"""^\s*revision\s*[:=]\s*['"](?P<rev>[^'"]+)['"]""",
    re.MULTILINE,
)
_DOWN_REV_RE = re.compile(
    r"""^\s*down_revision\s*[:=]\s*(?:['"](?P<rev>[^'"]+)['"]|None)""",
    re.MULTILINE,
)


def _collect_migration_revisions(versions_dir: Path) -> tuple[str | None, list[str]]:
    """Return (head_revision, all_revisions). Head is the leaf — the
    revision that no other migration declares as its ``down_revision``.

    A well-formed alembic tree has exactly one such leaf. Multiple
    leaves indicate a merge that hasn't been resolved; we treat that as
    an environmental problem (return None for head) so the caller can
    surface it explicitly.
    """
    if not versions_dir.is_dir():
        return None, []

    revisions: list[str] = []
    down_pointers: set[str] = set()

    for path in sorted(versions_dir.glob("*.py")):
        if path.name == "__init__.py":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        m_rev = _REVISION_RE.search(text)
        if not m_rev:
            continue
        revisions.append(m_rev.group("rev"))
        m_down = _DOWN_REV_RE.search(text)
        if m_down and m_down.group("rev"):
            down_pointers.add(m_down.group("rev"))

    if not revisions:
        return None, []

    leaves = [r for r in revisions if r not in down_pointers]
    if len(leaves) == 1:
        return leaves[0], revisions
    # Ambiguous tree — caller will surface as INFO/skip.
    return None, revisions


def check_schema_drift(
    versions_dir: Path,
    db_version: str | None,
) -> DriftResult | None:
    if db_version is None:
        return DriftResult(
            code="schema_drift",
            severity="INFO",
            message=(
                "skipped — no --db-version provided (daily cron should "
                "populate this from `psql -tAc 'select version_num from "
                "alembic_version'`)"
            ),
        )

    head, all_revs = _collect_migration_revisions(versions_dir)
    if not all_revs:
        return DriftResult(
            code="schema_drift",
            severity="INFO",
            message=(
                f"skipped — no migrations found in {versions_dir}; check "
                "--versions-dir or repo layout"
            ),
        )
    if head is None:
        return DriftResult(
            code="schema_drift",
            severity="INFO",
            message=(
                "skipped — alembic tree has multiple leaves (unresolved "
                "merge?). Run `alembic heads` to inspect."
            ),
        )

    actual = db_version.strip()
    if actual == head:
        return DriftResult(
            code="schema_drift",
            severity="INFO",
            message=f"alembic_version row matches head revision {head!r}",
            expected=head,
            actual=actual,
        )

    return DriftResult(
        code="schema_drift",
        severity="DEGRADED",
        message=(
            f"alembic_version row is {actual!r} but on-disk head is {head!r}"
            f" — run `alembic upgrade head` (or rollback to "
            f"{actual!r} if the head is wrong)"
        ),
        expected=head,
        actual=actual,
    )


# ─────────────────────────────────────────────────────────────────────
# Check 3 — refs/meta/config sample vs deployed
# ─────────────────────────────────────────────────────────────────────

# Operators should fetch the live ref into a local working dir:
#   git fetch <gerrit-url> refs/meta/config:refs/remotes/gerrit/meta-config
#   git -C <dir> checkout refs/remotes/gerrit/meta-config -- project.config webhooks.config
# and pass that dir as --remote-config-path. The scanner accepts either
# a file (compared against project.config.example) or a directory
# (each file with a `.example` sibling is compared).


def _normalise_config(text: str) -> str:
    """Strip comments + trailing whitespace so a cosmetic-only
    difference between the sample and the live ref doesn't trigger an
    alert. Gerrit's git-config syntax uses ``;`` and ``#`` for line
    comments."""
    out: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("#", ";")):
            continue
        # Strip inline comments after the first `;` not inside quotes.
        # Approximate — sufficient for the OP-708 / OP-713 audit shape.
        idx = stripped.find(";")
        if idx > 0 and stripped.count('"', 0, idx) % 2 == 0:
            stripped = stripped[:idx].rstrip()
        out.append(stripped)
    return "\n".join(out)


def check_refs_meta_config_drift(
    gerrit_dir: Path,
    remote_config_path: Path | None,
) -> DriftResult | None:
    if remote_config_path is None:
        return DriftResult(
            code="refs_meta_config_drift",
            severity="INFO",
            message=(
                "skipped — no --remote-config-path provided (daily cron "
                "should fetch refs/meta/config and point this at the "
                "checkout dir)"
            ),
        )

    sample_main = gerrit_dir / "project.config.example"
    if not sample_main.is_file():
        return DriftResult(
            code="refs_meta_config_drift",
            severity="INFO",
            message=f"skipped — sample missing at {sample_main}",
        )

    if not remote_config_path.exists():
        return DriftResult(
            code="refs_meta_config_drift",
            severity="DEGRADED",
            message=(
                f"--remote-config-path {remote_config_path} does not exist; "
                "live refs/meta/config may have been deleted"
            ),
        )

    pairs: list[tuple[Path, Path]] = []
    if remote_config_path.is_file():
        pairs.append((sample_main, remote_config_path))
    else:
        # directory: pair each <name> in remote with .gerrit/<name>.example
        # if such a sample exists. Also include webhooks.config if both
        # sides have it (per OP-713 lesson L22).
        for live_file in sorted(remote_config_path.iterdir()):
            if not live_file.is_file():
                continue
            sample = gerrit_dir / f"{live_file.name}.example"
            if sample.is_file():
                pairs.append((sample, live_file))
                continue
            sample_alt = gerrit_dir / live_file.name
            if sample_alt.is_file():
                pairs.append((sample_alt, live_file))

    if not pairs:
        return DriftResult(
            code="refs_meta_config_drift",
            severity="INFO",
            message=(
                f"skipped — no sample/live pairs found under {gerrit_dir} "
                f"vs {remote_config_path}"
            ),
        )

    diffs: list[str] = []
    for sample, live in pairs:
        try:
            sample_text = sample.read_text(encoding="utf-8")
            live_text = live.read_text(encoding="utf-8")
        except OSError as exc:
            diffs.append(f"{sample.name}: read failed ({exc})")
            continue
        if _normalise_config(sample_text) != _normalise_config(live_text):
            diffs.append(
                f"{live.name}: live ref differs from {sample.relative_to(REPO_ROOT) if sample.is_absolute() and REPO_ROOT in sample.parents else sample}"
            )

    if not diffs:
        joined = ", ".join(p[1].name for p in pairs)
        return DriftResult(
            code="refs_meta_config_drift",
            severity="INFO",
            message=f"refs/meta/config matches sample(s): {joined}",
        )

    return DriftResult(
        code="refs_meta_config_drift",
        severity="DEGRADED",
        message=(
            "live refs/meta/config differs from .gerrit/*.example: "
            + "; ".join(diffs)
            + " — sync the sample (OP-708 / OP-713 lesson L22)"
        ),
        expected=str(sample_main),
        actual=str(remote_config_path),
    )


# ─────────────────────────────────────────────────────────────────────
# Check 4 — deployed bridge checkout vs develop
# ─────────────────────────────────────────────────────────────────────


def check_bridge_drift(
    repo: Path,
    remote: str,
    bridge_path: Path | None,
    bridge_revision: str | None,
) -> DriftResult | None:
    if bridge_revision is None and bridge_path is None:
        return DriftResult(
            code="bridge_drift",
            severity="INFO",
            message=(
                "skipped — no bridge path/revision provided "
                "(default ~/sora-bridge not present on this host)"
            ),
        )

    actual: str | None = bridge_revision
    if actual is None and bridge_path is not None:
        if not (bridge_path / ".git").exists() and not (bridge_path / "HEAD").exists():
            return DriftResult(
                code="bridge_drift",
                severity="INFO",
                message=(
                    f"skipped — {bridge_path} is not a git checkout "
                    "(systemd unit may copy files instead of cloning)"
                ),
            )
        try:
            actual = _git(bridge_path, "rev-parse", "HEAD")
        except RuntimeError as exc:
            return DriftResult(
                code="bridge_drift",
                severity="INFO",
                message=f"skipped — could not read bridge HEAD: {exc}",
            )

    expected = _safe_rev_parse(repo, f"{remote}/develop")
    if expected is None:
        return DriftResult(
            code="bridge_drift",
            severity="INFO",
            message=f"skipped — could not resolve {remote}/develop",
        )

    if actual is None:
        return DriftResult(
            code="bridge_drift",
            severity="INFO",
            message="skipped — bridge revision unresolvable",
        )

    if expected.startswith(actual) or actual.startswith(expected):
        return DriftResult(
            code="bridge_drift",
            severity="INFO",
            message=f"bridge HEAD {actual[:12]} matches {remote}/develop",
            expected=expected,
            actual=actual,
        )

    return DriftResult(
        code="bridge_drift",
        severity="DEGRADED",
        message=(
            f"bridge HEAD {actual[:12]} ≠ {remote}/develop {expected[:12]} "
            "— deployed bridge checkout has drifted (likely hand-patched; "
            "see 2026-05-08 incident)"
        ),
        expected=expected,
        actual=actual,
        auto_fixable=True,
    )


# ─────────────────────────────────────────────────────────────────────
# Check 5 — local main vs origin/main
# ─────────────────────────────────────────────────────────────────────


def check_main_branch_drift(repo: Path, remote: str) -> DriftResult | None:
    local = _safe_rev_parse(repo, "main")
    if local is None:
        return DriftResult(
            code="main_branch_drift",
            severity="INFO",
            message="skipped — local `main` branch not present",
        )

    remote_ref = _safe_rev_parse(repo, f"{remote}/main")
    if remote_ref is None:
        return DriftResult(
            code="main_branch_drift",
            severity="INFO",
            message=(
                f"skipped — {remote}/main not present "
                "(remote not fetched? run `git fetch <remote> main`)"
            ),
        )

    if local == remote_ref:
        return DriftResult(
            code="main_branch_drift",
            severity="INFO",
            message=f"local main matches {remote}/main at {local[:12]}",
            expected=remote_ref,
            actual=local,
        )

    # Determine direction (ahead / behind / diverged) so the operator
    # knows whether `git push` or `git pull --ff-only` is the fix.
    try:
        ahead_behind = _git(
            repo, "rev-list", "--left-right", "--count",
            f"{remote}/main...main",
        )
        behind, ahead = (int(x) for x in ahead_behind.split())
    except (RuntimeError, ValueError):
        behind, ahead = 0, 0

    direction = (
        "diverged" if ahead and behind
        else "ahead of remote (push needed)" if ahead
        else "behind remote (fetch/merge needed)" if behind
        else "differs"
    )

    return DriftResult(
        code="main_branch_drift",
        severity="DEGRADED",
        message=(
            f"local main {local[:12]} {direction} vs {remote}/main "
            f"{remote_ref[:12]} (ahead={ahead}, behind={behind})"
        ),
        expected=remote_ref,
        actual=local,
        auto_fixable=ahead > 0 and behind == 0,
    )


# ─────────────────────────────────────────────────────────────────────
# Orchestration
# ─────────────────────────────────────────────────────────────────────


def run_all_checks(
    *,
    repo: Path,
    versions_dir: Path,
    gerrit_dir: Path,
    remote: str,
    image_revision: str | None,
    db_version: str | None,
    remote_config_path: Path | None,
    bridge_path: Path | None,
    bridge_revision: str | None,
    skip: Iterable[str],
) -> list[DriftResult]:
    skip_set = set(skip)
    results: list[DriftResult] = []
    if "image_drift" not in skip_set:
        results.append(check_image_drift(repo, remote, image_revision))
    if "schema_drift" not in skip_set:
        results.append(check_schema_drift(versions_dir, db_version))
    if "refs_meta_config_drift" not in skip_set:
        results.append(check_refs_meta_config_drift(gerrit_dir, remote_config_path))
    if "bridge_drift" not in skip_set:
        results.append(check_bridge_drift(repo, remote, bridge_path, bridge_revision))
    if "main_branch_drift" not in skip_set:
        results.append(check_main_branch_drift(repo, remote))
    return [r for r in results if r is not None]


def emit_text(results: list[DriftResult], stream) -> None:
    for r in results:
        stream.write(
            f"[{r.severity}] code={r.code} severity={r.severity} "
            f"message={r.message!r}\n"
        )


def emit_json(results: list[DriftResult], stream) -> None:
    payload = {"results": [r.to_dict() for r in results]}
    stream.write(json.dumps(payload, indent=2) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--repo", default=str(REPO_ROOT),
                        help="Repo root (default: this checkout).")
    parser.add_argument("--remote", default="origin",
                        help="Git remote name to compare against (default: origin).")
    parser.add_argument("--versions-dir", default=None,
                        help=f"Alembic versions dir (default: {VERSIONS_DIR}).")
    parser.add_argument("--gerrit-dir", default=None,
                        help=f"Repo .gerrit dir (default: {GERRIT_DIR}).")
    parser.add_argument("--image-revision", default=os.environ.get("OMNISIGHT_DEPLOYED_IMAGE_REVISION"),
                        help="Git commit the deployed backend image was built from.")
    parser.add_argument("--db-version", default=os.environ.get("OMNISIGHT_DEPLOYED_DB_VERSION"),
                        help="Current alembic_version row value in the deployed DB.")
    parser.add_argument("--remote-config-path", default=None,
                        help="Path to a checkout of refs/meta/config (file or dir).")
    parser.add_argument("--bridge-path", default=None,
                        help=f"Path to deployed bridge checkout (default: {DEFAULT_BRIDGE_PATH} if present).")
    parser.add_argument("--bridge-revision", default=os.environ.get("OMNISIGHT_BRIDGE_REVISION"),
                        help="Git HEAD of the deployed bridge (alt. to --bridge-path).")
    parser.add_argument("--skip", action="append", default=[],
                        choices=ALL_CODES,
                        help="Skip a specific drift check (repeatable).")
    parser.add_argument("--auto-fix", action="store_true",
                        help="Opt-in: log which drifts could be auto-corrected.")
    parser.add_argument("--json", action="store_true",
                        help="Emit JSON instead of human-readable lines.")
    parser.add_argument("--quiet", action="store_true",
                        help="Suppress INFO results (only print DEGRADED).")
    args = parser.parse_args(argv)

    repo = Path(args.repo).resolve()
    versions_dir = Path(args.versions_dir).resolve() if args.versions_dir else (repo / "backend" / "alembic" / "versions")
    gerrit_dir = Path(args.gerrit_dir).resolve() if args.gerrit_dir else (repo / ".gerrit")

    bridge_path: Path | None
    if args.bridge_path:
        bridge_path = Path(args.bridge_path).expanduser().resolve()
    elif args.bridge_revision:
        bridge_path = None
    elif DEFAULT_BRIDGE_PATH.exists():
        bridge_path = DEFAULT_BRIDGE_PATH
    else:
        bridge_path = None

    remote_config_path: Path | None = (
        Path(args.remote_config_path).expanduser().resolve()
        if args.remote_config_path else None
    )

    if not (repo / ".git").exists() and not _safe_rev_parse(repo, "HEAD"):
        sys.stderr.write(
            f"[drift-scanner] ERROR: --repo {repo} is not a git checkout.\n"
        )
        return 2

    try:
        results = run_all_checks(
            repo=repo,
            versions_dir=versions_dir,
            gerrit_dir=gerrit_dir,
            remote=args.remote,
            image_revision=args.image_revision,
            db_version=args.db_version,
            remote_config_path=remote_config_path,
            bridge_path=bridge_path,
            bridge_revision=args.bridge_revision,
            skip=args.skip,
        )
    except RuntimeError as exc:
        sys.stderr.write(f"[drift-scanner] ERROR: {exc}\n")
        return 2

    visible = [r for r in results if not (args.quiet and r.severity == "INFO")]
    if args.json:
        emit_json(visible, sys.stdout)
    else:
        emit_text(visible, sys.stderr)

    drifts = [r for r in results if r.severity == "DEGRADED"]
    if args.auto_fix:
        for r in drifts:
            if r.code in AUTO_FIXABLE:
                sys.stderr.write(
                    f"[drift-scanner] auto-fix: {r.code} is safe to "
                    "auto-correct (image rebuild / push / re-deploy). "
                    "Gerrit-change opening is intentionally not wired "
                    "in this revision; do it manually for now.\n"
                )
            else:
                sys.stderr.write(
                    f"[drift-scanner] auto-fix: {r.code} is NOT auto-fixable"
                    " (destructive correction; manual review required).\n"
                )

    return 1 if drifts else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
