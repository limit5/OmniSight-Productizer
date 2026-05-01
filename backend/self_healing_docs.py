"""BP.J.1 — Self-healing Docs Watchdog.

API-change detection that reverse-updates Swagger (``openapi.json``)
and the human-readable architecture overview (``docs/architecture.md``)
whenever the FastAPI app's route surface drifts from the committed
snapshot.

Why a separate module (and not just `scripts/dump_openapi.py`?)
─────────────────────────────────────────────────────────────────
``scripts/dump_openapi.py`` is a one-shot CI gate — it diffs the
generated schema against the committed `openapi.json` and exits 1 on
drift. That works for the **machine-readable** contract.

What it does *not* do:

1. Update the human-readable doc (``docs/architecture.md``) — that
   stays stale unless someone hand-edits it.
2. Distinguish between **additive** changes (new routes, new schemas
   — safe to auto-write) and **breaking** changes (removed routes,
   changed methods on existing paths — needs operator review).
3. Plug into the post-merge hook (BP.J.2) so a developer who merges
   `main` and pulls new routes locally automatically refreshes
   their `openapi.json` working copy.

This module fills those gaps. It exposes a small, function-only API
that:

* :func:`detect_api_changes` — pure-function diff between current
  ``app.openapi()`` and the on-disk snapshot. Returns
  :class:`ApiChangeReport` (added / removed / modified path-method
  pairs + schema-name set delta).
* :func:`regenerate_openapi_snapshot` — atomic re-write of
  ``openapi.json`` (delegates to the canonicalisation logic in
  ``scripts/dump_openapi.py`` so the two paths can never disagree).
* :func:`regenerate_architecture_md` — rewrites the
  ``<!-- BEGIN AUTO-GENERATED -->`` … ``<!-- END AUTO-GENERATED -->``
  block of ``docs/architecture.md``. Manual content **outside** the
  sentinels is preserved verbatim, so authors can keep design notes
  next to the auto-generated route table.
* :func:`run` — orchestrates the three above; intended to be called
  by both the post-merge hook (BP.J.2) and a future Reporter Guild
  task (BP.B prerequisite, polish only).

Module-global state audit (SOP 2026-04-21 rule)
───────────────────────────────────────────────
The module holds **zero** mutable module-level state. All paths /
config are constants computed at import time from
:func:`pathlib.Path.resolve`. Each invocation of :func:`run` is
independent and idempotent. Multi-worker safety: writers use
``os.replace`` (atomic on POSIX) on a temp-file in the same dir as
the target, so concurrent invocations from the post-merge hook
(developer machine) and a Reporter-Guild Celery worker (server) can
never produce a half-written file. Qualified answer #1 — every
caller derives identical output from identical app state.

Read-after-write timing
───────────────────────
The only "read after write" surface is :func:`run` re-reading the
new ``openapi.json`` to compute the architecture-md table. We always
work from the in-memory schema dict, never from the on-disk file
between write & re-read, so the timing is irrelevant.

Compat-fingerprint grep
───────────────────────
Pre-commit fingerprint grep (`_conn() / await conn.commit() /
datetime('now') / VALUES (?, ?` — none of these patterns can match
this module; it touches no DB and uses no compat shim. Logged for
auditability per SOP Step 3.

CLI usage
─────────
::

    python -m backend.self_healing_docs                  # apply diff in-place
    python -m backend.self_healing_docs --check          # exit 1 on drift, no writes
    python -m backend.self_healing_docs --report-only    # print JSON report, no writes
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)


# ── Paths ──────────────────────────────────────────────────────────────
# Resolved at import time. We never mutate these; they are derived from
# this file's location so the module is robust to caller cwd (CI vs
# dev box vs pytest tmpdir).
_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
OPENAPI_SNAPSHOT: Path = _REPO_ROOT / "openapi.json"
ARCHITECTURE_MD: Path = _REPO_ROOT / "docs" / "architecture.md"

# Sentinels that delimit the auto-generated block inside
# ``architecture.md``. We use HTML comments so the markdown still
# renders cleanly on GitHub / GitLab. Any text **outside** these
# sentinels is preserved verbatim — authors can keep design notes
# adjacent to the route table.
ARCH_BEGIN: str = "<!-- BEGIN AUTO-GENERATED: backend/self_healing_docs.py -->"
ARCH_END: str = "<!-- END AUTO-GENERATED: backend/self_healing_docs.py -->"


# ── Public dataclasses ────────────────────────────────────────────────
@dataclasses.dataclass(frozen=True)
class ApiChangeReport:
    """Diff report between two OpenAPI schemas.

    All fields are sorted, frozen tuples / frozensets so two reports
    over the same input always compare equal — useful in tests.
    """

    # (path, method) tuples that exist in the new schema but not the
    # old. Method is upper-cased ("GET", "POST", …).
    added_routes: tuple[tuple[str, str], ...]

    # (path, method) tuples that existed in the old schema but vanished
    # in the new. These are **breaking** changes — flagged separately
    # so the post-merge hook can refuse to auto-write without operator
    # opt-in.
    removed_routes: tuple[tuple[str, str], ...]

    # (path, method) tuples whose JSON differs between old & new but
    # both still exist. Includes signature changes (params, request /
    # response body schemas, deprecated flag).
    modified_routes: tuple[tuple[str, str], ...]

    # Schema names that are new / dropped. Useful for surfacing a
    # bullet list in architecture.md without re-rendering the entire
    # schema table.
    added_schemas: tuple[str, ...]
    removed_schemas: tuple[str, ...]

    @property
    def is_empty(self) -> bool:
        return not (
            self.added_routes
            or self.removed_routes
            or self.modified_routes
            or self.added_schemas
            or self.removed_schemas
        )

    @property
    def has_breaking_changes(self) -> bool:
        """Removed routes / removed schemas count as breaking.

        Modifications are intentionally NOT breaking by default —
        adding an optional query param to an existing endpoint is a
        modification but additive. Operators who care can pass
        ``strict=True`` to :func:`run`.
        """
        return bool(self.removed_routes or self.removed_schemas)

    def as_dict(self) -> dict[str, Any]:
        """JSON-serialisable view (for ``--report-only`` CLI mode)."""
        return {
            "added_routes": [list(p) for p in self.added_routes],
            "removed_routes": [list(p) for p in self.removed_routes],
            "modified_routes": [list(p) for p in self.modified_routes],
            "added_schemas": list(self.added_schemas),
            "removed_schemas": list(self.removed_schemas),
            "is_empty": self.is_empty,
            "has_breaking_changes": self.has_breaking_changes,
        }


# ── Pure helpers ──────────────────────────────────────────────────────
# These do NOT touch the filesystem — caller passes dicts in, gets
# data out. Makes unit-testing trivial (BP.J.3 will use these).


_HTTP_METHODS: frozenset[str] = frozenset(
    {"get", "post", "put", "patch", "delete", "head", "options", "trace"}
)


def _route_pairs(schema: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    """Flatten OpenAPI ``paths`` into ``{(path, METHOD): operation}``.

    Filters out non-HTTP keys ("parameters", "summary", "description",
    "$ref") that OpenAPI allows at the path level. Methods are
    upper-cased for stable comparison.
    """
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for path, item in (schema.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        for method, op in item.items():
            if method.lower() not in _HTTP_METHODS:
                continue
            if not isinstance(op, dict):
                continue
            out[(path, method.upper())] = op
    return out


def _schema_names(schema: dict[str, Any]) -> set[str]:
    """Pull the set of named schemas under ``components.schemas``."""
    components = schema.get("components") or {}
    schemas = components.get("schemas") or {}
    return set(schemas.keys()) if isinstance(schemas, dict) else set()


def _canonical_op(op: dict[str, Any]) -> str:
    """Canonical JSON of an operation, for byte-equality comparison.

    We sort keys so re-ordering of fields by FastAPI / Pydantic
    minor-version bumps doesn't show up as "modified".
    """
    return json.dumps(op, sort_keys=True, ensure_ascii=False)


def detect_api_changes(
    old_schema: dict[str, Any] | None,
    new_schema: dict[str, Any],
) -> ApiChangeReport:
    """Diff two OpenAPI schema dicts.

    ``old_schema`` may be ``None`` — handled as "no prior snapshot",
    every route in ``new_schema`` is reported as added. Useful for
    bootstrapping a fresh repo (no committed ``openapi.json`` yet).
    """
    new_pairs = _route_pairs(new_schema)
    new_names = _schema_names(new_schema)

    if old_schema is None:
        return ApiChangeReport(
            added_routes=tuple(sorted(new_pairs.keys())),
            removed_routes=(),
            modified_routes=(),
            added_schemas=tuple(sorted(new_names)),
            removed_schemas=(),
        )

    old_pairs = _route_pairs(old_schema)
    old_names = _schema_names(old_schema)

    added = sorted(set(new_pairs) - set(old_pairs))
    removed = sorted(set(old_pairs) - set(new_pairs))
    modified: list[tuple[str, str]] = []
    for key in sorted(set(new_pairs) & set(old_pairs)):
        if _canonical_op(new_pairs[key]) != _canonical_op(old_pairs[key]):
            modified.append(key)

    return ApiChangeReport(
        added_routes=tuple(added),
        removed_routes=tuple(removed),
        modified_routes=tuple(modified),
        added_schemas=tuple(sorted(new_names - old_names)),
        removed_schemas=tuple(sorted(old_names - new_names)),
    )


# ── Filesystem writers (atomic) ───────────────────────────────────────
def _atomic_write(target: Path, content: str) -> None:
    """Atomic write: tempfile in same dir + ``os.replace``.

    Same-dir guarantees ``os.replace`` is a single rename syscall (no
    cross-FS copy). POSIX guarantees the rename is atomic w.r.t.
    other observers — concurrent readers either see the old file or
    the new file, never a half-written one. This matters because the
    post-merge hook (BP.J.2) and a future Reporter-Guild scheduled
    task could race on the same file.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, target)
    except BaseException:
        # Clean up the tempfile if we crashed before the replace.
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


def _canonical_openapi(schema: dict[str, Any]) -> str:
    """Canonicalise schema text identically to ``scripts/dump_openapi.py``.

    Keeping this in sync is a fingerprint risk — if the dump script
    ever changes its canonicalisation rule (e.g. switches indent),
    this module's writes would create false drift in ``--check`` mode.
    BP.J.3 includes a contract test that pins the two outputs to be
    byte-identical for the same input dict.
    """
    schema = dict(schema)  # shallow copy — don't mutate caller
    schema.pop("info", None)
    schema["info"] = {"title": "OmniSight Engine API", "version": "contract"}
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def regenerate_openapi_snapshot(
    new_schema: dict[str, Any],
    *,
    target: Path | None = None,
) -> Path:
    """Atomically rewrite the canonical ``openapi.json`` snapshot.

    Returns the resolved target path so callers can log it.
    """
    out = (target or OPENAPI_SNAPSHOT).resolve()
    _atomic_write(out, _canonical_openapi(new_schema))
    logger.info("self_healing_docs: rewrote %s", out)
    return out


# ── architecture.md generator ─────────────────────────────────────────
def _render_route_table(schema: dict[str, Any]) -> str:
    """Markdown table of every (path, method) pair, sorted.

    Only the path / method / summary triple is rendered — full
    request / response schema is the openapi.json's job. The point
    here is a *human-skim* index of the API surface that lives next
    to design notes.
    """
    pairs = _route_pairs(schema)
    if not pairs:
        return "_(no routes)_\n"

    lines = ["| Method | Path | Summary |", "|---|---|---|"]
    for (path, method) in sorted(pairs.keys()):
        op = pairs[(path, method)]
        summary = (op.get("summary") or "").strip()
        # Markdown table cells: escape pipes, collapse newlines.
        summary = summary.replace("|", "\\|").replace("\n", " ")
        lines.append(f"| `{method}` | `{path}` | {summary} |")
    return "\n".join(lines) + "\n"


def _render_auto_block(schema: dict[str, Any], report: ApiChangeReport) -> str:
    """Compose the full auto-generated block (between sentinels)."""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    pairs = _route_pairs(schema)
    schema_count = len(_schema_names(schema))

    parts: list[str] = [
        ARCH_BEGIN,
        "",
        f"_Last regenerated: {timestamp} by_ `backend/self_healing_docs.py`",
        "",
        "## API Surface (auto)",
        "",
        f"- **Total routes**: {len(pairs)}",
        f"- **Total schemas**: {schema_count}",
        "",
    ]

    if not report.is_empty:
        parts.append("### Most recent change report")
        parts.append("")
        if report.added_routes:
            parts.append(f"- Added routes: **{len(report.added_routes)}**")
        if report.removed_routes:
            parts.append(
                f"- Removed routes: **{len(report.removed_routes)}** ⚠ breaking"
            )
        if report.modified_routes:
            parts.append(f"- Modified routes: **{len(report.modified_routes)}**")
        if report.added_schemas:
            parts.append(f"- Added schemas: **{len(report.added_schemas)}**")
        if report.removed_schemas:
            parts.append(
                f"- Removed schemas: **{len(report.removed_schemas)}** ⚠ breaking"
            )
        parts.append("")

    parts.append("### Routes")
    parts.append("")
    parts.append(_render_route_table(schema))
    parts.append(ARCH_END)
    return "\n".join(parts) + "\n"


# Default scaffold used when ``docs/architecture.md`` does not yet exist.
# Authors are expected to expand the manual sections (the bits OUTSIDE
# the sentinels) over time.
_ARCH_SCAFFOLD = """\
# OmniSight Architecture

> This file mixes hand-written design notes with an auto-generated
> API surface index. The block between the
> `BEGIN AUTO-GENERATED` / `END AUTO-GENERATED` sentinels below is
> rewritten by `backend/self_healing_docs.py` whenever the FastAPI
> route surface changes. Edit anything OUTSIDE the sentinels freely;
> edits inside will be lost on next regeneration.

## Overview

_(hand-written — start here.)_

{auto}

## Notes

_(hand-written — append design context, ADRs, etc.)_
"""


def regenerate_architecture_md(
    new_schema: dict[str, Any],
    report: ApiChangeReport,
    *,
    target: Path | None = None,
) -> Path:
    """Rewrite (or seed) ``docs/architecture.md``.

    If the file does not exist, write the full scaffold. If it
    exists, replace only the content between the auto-generated
    sentinels — manual content survives.
    """
    out = (target or ARCHITECTURE_MD).resolve()
    auto_block = _render_auto_block(new_schema, report)

    if not out.exists():
        new_text = _ARCH_SCAFFOLD.format(auto=auto_block)
        _atomic_write(out, new_text)
        logger.info("self_healing_docs: seeded %s (no prior file)", out)
        return out

    existing = out.read_text(encoding="utf-8")
    if ARCH_BEGIN in existing and ARCH_END in existing:
        before, _, rest = existing.partition(ARCH_BEGIN)
        _, _, after = rest.partition(ARCH_END)
        # ``after`` may start with leftover newline — strip leading
        # whitespace so we don't accumulate blank lines on every run.
        new_text = before + auto_block + after.lstrip("\n")
    else:
        # File exists but has no sentinels — append the auto block at
        # the end rather than clobber. Operator sees both copies and
        # can decide which to keep.
        sep = "" if existing.endswith("\n") else "\n"
        new_text = existing + sep + "\n" + auto_block

    if new_text == existing:
        logger.info("self_healing_docs: %s already up to date", out)
        return out

    _atomic_write(out, new_text)
    logger.info("self_healing_docs: rewrote %s", out)
    return out


# ── App-schema loader ─────────────────────────────────────────────────
def load_current_schema() -> dict[str, Any]:
    """Introspect the FastAPI app and return its current OpenAPI dict.

    Mirrors ``scripts/dump_openapi.py::_load_schema`` — same env-var
    nudge, same ``info`` block override. We import lazily so this
    module is import-cheap (handy for the post-merge hook BP.J.2,
    which runs on every ``git merge``).
    """
    os.environ.setdefault("OMNISIGHT_DEBUG", "true")
    from backend.main import app  # local import — see docstring

    schema = app.openapi()
    schema = dict(schema)
    schema.pop("info", None)
    schema["info"] = {"title": "OmniSight Engine API", "version": "contract"}
    return schema


def load_snapshot(path: Path | None = None) -> dict[str, Any] | None:
    """Read the on-disk ``openapi.json``. Returns ``None`` if missing."""
    p = (path or OPENAPI_SNAPSHOT).resolve()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        logger.warning("self_healing_docs: %s is malformed (%s)", p, exc)
        return None


# ── Orchestrator ──────────────────────────────────────────────────────
@dataclasses.dataclass(frozen=True)
class RunResult:
    """Outcome of a :func:`run` invocation. JSON-serialisable."""

    report: ApiChangeReport
    wrote_openapi: bool
    wrote_architecture: bool
    refused_breaking: bool  # True if strict=True and breaking diff present

    def as_dict(self) -> dict[str, Any]:
        return {
            "report": self.report.as_dict(),
            "wrote_openapi": self.wrote_openapi,
            "wrote_architecture": self.wrote_architecture,
            "refused_breaking": self.refused_breaking,
        }


def run(
    *,
    check: bool = False,
    strict: bool = False,
    openapi_path: Path | None = None,
    architecture_path: Path | None = None,
) -> RunResult:
    """Detect drift and (unless ``check=True``) apply the fix.

    Args:
      check: If True, do not write — just report. Used by CI.
      strict: If True, refuse to write when ``has_breaking_changes``.
        The post-merge hook (BP.J.2) sets this so a developer who
        merges a branch that deletes routes is forced to acknowledge
        before the working-copy snapshot is overwritten.
    """
    new_schema = load_current_schema()
    old_schema = load_snapshot(openapi_path)
    report = detect_api_changes(old_schema, new_schema)

    if report.is_empty:
        logger.info("self_healing_docs: no drift")
        return RunResult(
            report=report,
            wrote_openapi=False,
            wrote_architecture=False,
            refused_breaking=False,
        )

    if check:
        logger.info(
            "self_healing_docs: drift detected — added=%d removed=%d modified=%d",
            len(report.added_routes),
            len(report.removed_routes),
            len(report.modified_routes),
        )
        return RunResult(
            report=report,
            wrote_openapi=False,
            wrote_architecture=False,
            refused_breaking=False,
        )

    if strict and report.has_breaking_changes:
        logger.warning(
            "self_healing_docs: strict mode + breaking diff "
            "(removed_routes=%d removed_schemas=%d) — refusing to write",
            len(report.removed_routes),
            len(report.removed_schemas),
        )
        return RunResult(
            report=report,
            wrote_openapi=False,
            wrote_architecture=False,
            refused_breaking=True,
        )

    regenerate_openapi_snapshot(new_schema, target=openapi_path)
    regenerate_architecture_md(new_schema, report, target=architecture_path)
    return RunResult(
        report=report,
        wrote_openapi=True,
        wrote_architecture=True,
        refused_breaking=False,
    )


# ── CLI ───────────────────────────────────────────────────────────────
def _cli(argv: Iterable[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m backend.self_healing_docs",
        description=__doc__.split("\n\n")[0] if __doc__ else None,
    )
    ap.add_argument(
        "--check",
        action="store_true",
        help="Report drift but do not write. Exit 1 if drift exists.",
    )
    ap.add_argument(
        "--strict",
        action="store_true",
        help="Refuse to overwrite if the diff includes a breaking change "
        "(removed route or removed schema). Used by the post-merge hook.",
    )
    ap.add_argument(
        "--report-only",
        action="store_true",
        help="Print a JSON report to stdout. Implies --check.",
    )
    args = ap.parse_args(list(argv) if argv is not None else None)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    check = args.check or args.report_only
    result = run(check=check, strict=args.strict)

    if args.report_only:
        json.dump(result.as_dict(), sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")

    if check:
        # CI semantics: drift → exit 1
        return 0 if result.report.is_empty else 1
    if result.refused_breaking:
        # Hook semantics: breaking diff in strict mode → exit 2
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
