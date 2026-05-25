#!/usr/bin/env python3
"""OP-1732 -- land the promotion LEDGER on develop from a stable path.

The release SOP runs ``promote_image_bundle.py`` from an EPHEMERAL
``/tmp`` develop-tip worktree, so the human-readable git ledger it writes
-- ``audit/image_promotion_audit.jsonl`` (the bundle audit row) plus the
per-image ``audit/promotion-predicates/*.json`` predicates -- is written
into ``/tmp/rel-<sha>/audit/`` and LOST on worktree cleanup, never
reaching develop. (The cryptographic record -- the cosign attestation --
is safe in the registry; this is only about the human-readable ledger.)

This helper closes that gap. Point ``promote --audit-log`` /
``--predicate-out-dir`` at a PERSISTENT directory (or preserve the
ephemeral ``audit/`` there after the promote), then run this from the
canonical repo checkout to merge that record into the committable
``audit/`` paths -- idempotently -- and stage it for a Gerrit review.

It NEVER pushes and NEVER commits without ``--commit``: the release
ledger still lands on develop through Gerrit (AI +1 / human +2). It does
not touch the registry, signing, or attestations in any way.

Typical use (the v0.6.2 first application)::

    python3 scripts/commit_promotion_ledger.py \\
        --from-dir /home/user/backups/promote-records/v0.6.2

then review the staged changes and push for review::

    git commit -m "[OP-XXXX] Land v0.6.2 promotion ledger on develop"
    git push origin HEAD:refs/for/develop
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable


REPO_ROOT = Path(__file__).resolve().parent.parent
# Canonical, committable ledger locations (mirror promote_image_bundle.py
# DEFAULT_AUDIT_LOG and sign_promotion_attestation.py DEFAULT_PREDICATE_DIR).
DEFAULT_AUDIT_LOG = REPO_ROOT / "audit" / "image_promotion_audit.jsonl"
DEFAULT_PREDICATE_DIR = REPO_ROOT / "audit" / "promotion-predicates"

# The audit-log basename promote writes; a --from-dir may hold either this
# exact name or the operator's preserved "audit-rows.jsonl" spelling.
_AUDIT_BASENAMES = ("image_promotion_audit.jsonl", "audit-rows.jsonl")


class LedgerError(RuntimeError):
    """A ledger source was malformed or could not be merged."""


def _canonical(line: str) -> str | None:
    """Normalise one JSONL row to a stable, comparable form, or None if
    the line is blank / not a JSON object."""

    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError as exc:
        raise LedgerError(f"audit row is not valid JSON: {line!r}: {exc}") from exc
    if not isinstance(obj, dict):
        raise LedgerError(f"audit row must be a JSON object, got: {line!r}")
    return json.dumps(obj, sort_keys=True)


def _read_rows(path: Path) -> list[str]:
    if not path.exists():
        return []
    out: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        canon = _canonical(line)
        if canon is not None:
            out.append(canon)
    return out


def merge_audit_rows(
    *,
    source_rows: Iterable[str],
    audit_log: Path,
    dry_run: bool = False,
) -> list[str]:
    """Append each source row to ``audit_log`` unless an identical row is
    already present. Order-preserving and idempotent; returns the rows that
    were (or would be) newly appended."""

    existing = _read_rows(audit_log)
    seen = set(existing)
    new_rows: list[str] = []
    for canon in source_rows:
        if canon in seen:
            continue
        seen.add(canon)
        new_rows.append(canon)
    if new_rows and not dry_run:
        audit_log.parent.mkdir(parents=True, exist_ok=True)
        with audit_log.open("a", encoding="utf-8") as fh:
            for canon in new_rows:
                fh.write(canon + "\n")
    return new_rows


def copy_predicates(
    *,
    source_files: Iterable[Path],
    predicate_dir: Path,
    dry_run: bool = False,
) -> list[Path]:
    """Copy each predicate JSON into ``predicate_dir``. A destination that
    already holds byte-identical content is left untouched (idempotent).
    A name collision with DIFFERENT content is a hard error -- never
    silently overwrite a recorded predicate."""

    copied: list[Path] = []
    for src in source_files:
        dest = predicate_dir / src.name
        if dest.exists():
            if dest.read_bytes() == src.read_bytes():
                continue
            raise LedgerError(
                f"refusing to overwrite {dest} with different content from {src}"
            )
        if not dry_run:
            predicate_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
        copied.append(dest)
    return copied


def _discover_audit_file(from_dir: Path) -> Path | None:
    for name in _AUDIT_BASENAMES:
        candidate = from_dir / name
        if candidate.exists():
            return candidate
    return None


def _discover_predicates(from_dir: Path) -> list[Path]:
    return sorted(from_dir.glob("*.promotion.json"))


def gather_sources(
    *,
    from_dir: Path | None,
    audit_file: Path | None,
    predicate_files: list[Path],
) -> tuple[Path | None, list[Path]]:
    """Resolve the audit-rows file and predicate files from --from-dir and/or
    the explicit --audit-file / --predicate flags."""

    resolved_audit = audit_file
    resolved_predicates = list(predicate_files)
    if from_dir is not None:
        if not from_dir.is_dir():
            raise LedgerError(f"--from-dir is not a directory: {from_dir}")
        if resolved_audit is None:
            resolved_audit = _discover_audit_file(from_dir)
        if not resolved_predicates:
            resolved_predicates = _discover_predicates(from_dir)
    if resolved_audit is None and not resolved_predicates:
        raise LedgerError(
            "no ledger found: pass --from-dir with audit/predicate files, or "
            "--audit-file / --predicate explicitly"
        )
    return resolved_audit, resolved_predicates


def stage_paths(paths: Iterable[Path], *, runner=subprocess.run) -> None:
    rel = [str(p) for p in paths]
    if not rel:
        return
    runner(["git", "add", "--", *rel], cwd=REPO_ROOT, check=True, text=True)


def commit_ledger(
    *,
    audit_log: Path,
    predicate_dir: Path,
    source_audit: Path | None,
    source_predicates: list[Path],
    dry_run: bool = False,
    stage: bool = True,
    runner=subprocess.run,
) -> dict[str, Any]:
    """Merge the ledger from the source into the canonical repo paths and
    (optionally) stage the touched files. Returns a summary dict."""

    source_rows = _read_rows(source_audit) if source_audit is not None else []
    new_rows = merge_audit_rows(source_rows=source_rows, audit_log=audit_log, dry_run=dry_run)
    copied = copy_predicates(
        source_files=source_predicates, predicate_dir=predicate_dir, dry_run=dry_run
    )

    touched: list[Path] = []
    if new_rows:
        touched.append(audit_log)
    touched.extend(copied)

    if stage and touched and not dry_run:
        stage_paths(touched, runner=runner)

    return {
        "new_rows": new_rows,
        "copied_predicates": copied,
        "touched": touched,
        "audit_log": audit_log,
        "predicate_dir": predicate_dir,
    }


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="commit_promotion_ledger",
        description=(
            "Land the promotion ledger (audit row + predicates) on develop from "
            "a stable path. Merges idempotently into the canonical audit/ paths "
            "and stages for Gerrit; never pushes."
        ),
    )
    parser.add_argument(
        "--from-dir",
        type=Path,
        default=None,
        help="Directory holding the preserved ledger (audit-rows.jsonl or "
        "image_promotion_audit.jsonl + *.promotion.json), e.g. the persistent "
        "promote --audit-log/--predicate-out-dir target.",
    )
    parser.add_argument(
        "--audit-file",
        type=Path,
        default=None,
        help="Explicit JSONL of audit rows to merge (overrides --from-dir discovery).",
    )
    parser.add_argument(
        "--predicate",
        dest="predicates",
        type=Path,
        action="append",
        default=[],
        help="Explicit predicate JSON to copy (repeatable; overrides --from-dir discovery).",
    )
    parser.add_argument("--audit-log", type=Path, default=DEFAULT_AUDIT_LOG)
    parser.add_argument("--predicate-out-dir", type=Path, default=DEFAULT_PREDICATE_DIR)
    parser.add_argument(
        "--no-stage",
        dest="stage",
        action="store_false",
        default=True,
        help="Do not 'git add' the merged files (just write them).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing or staging anything.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    try:
        source_audit, source_predicates = gather_sources(
            from_dir=args.from_dir,
            audit_file=args.audit_file,
            predicate_files=args.predicates,
        )
        summary = commit_ledger(
            audit_log=args.audit_log,
            predicate_dir=args.predicate_out_dir,
            source_audit=source_audit,
            source_predicates=source_predicates,
            dry_run=args.dry_run,
            stage=args.stage,
        )
    except LedgerError as exc:
        print(f"commit-promotion-ledger failed: {exc}", file=sys.stderr)
        return 1

    prefix = "DRY RUN: would " if args.dry_run else ""
    new_rows = summary["new_rows"]
    copied = summary["copied_predicates"]
    if not new_rows and not copied:
        print("Ledger already up to date; nothing to commit.")
        return 0
    if new_rows:
        print(f"{prefix}append {len(new_rows)} audit row(s) to {summary['audit_log']}")
    for dest in copied:
        print(f"{prefix}copy predicate -> {dest}")
    if args.stage and not args.dry_run:
        print("Staged the above for review. Next:")
        print('  git commit -m "[OP-XXXX] Land promotion ledger on develop"')
        print("  git push origin HEAD:refs/for/develop   # Gerrit review (AI +1 / human +2)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
