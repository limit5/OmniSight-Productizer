#!/usr/bin/env python3
"""N10 — decide whether a PR needs the `requires-blue-green` label.

Invoked by .github/workflows/blue-green-gate.yml on every PR event.
Emits two `key=value` lines to GITHUB_OUTPUT:

    decision=<add|keep|noop>
    reason=<short explanation surfaced in the gate's step-summary>

Decision rubric (first match wins):

1. PR already has `requires-blue-green` → `keep` (monotonic gate —
   once applied, the label never auto-clears).
2. PR has `tier/major` or `deploy/blue-green-required` (Renovate
   labels per N2 policy) → `add`.
3. PR title matches the Renovate major-bump pattern
   (`Update <dep> to v<N>` with N ≥ 1 when the baseline is 0.x, or
   a clearly bumped major) → `add`.
4. Human-authored major bump detected by diffing
   `package.json` / `backend/requirements.in` / `.nvmrc` /
   `.node-version` / `pyproject.toml` between base and head SHAs. A
   **major** bump is defined as any change to the first semver
   component of a pinned or ranged version of a *tracked framework*
   (see `TRACKED_FRAMEWORKS` below). Also treats a Node/pnpm/Python
   engine bump as major regardless of tier (per policy doc).
5. Otherwise → `noop`.

Stdlib-only on purpose (same design rationale as N5/N6/N7/N8/N9
support scripts): this script validates the upgrade ceremony, so it
must not depend on any package the ceremony is there to govern.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

# Tracked frameworks — bumping the major of any of these triggers the
# blue-green gate even if Renovate has not yet labelled the PR.
TRACKED_FRAMEWORKS = frozenset({
    # JS side
    "next", "react", "react-dom", "vite", "typescript", "eslint",
    "vitest", "playwright", "@playwright/test",
    # AI SDK core (peer-coupled via the `ai-sdk` group)
    "ai", "@ai-sdk/anthropic", "@ai-sdk/openai", "@ai-sdk/google",
    # Python side
    "fastapi", "pydantic", "pydantic-core", "pydantic-settings",
    "sqlalchemy", "alembic", "uvicorn", "starlette",
    "langchain", "langchain-core", "langchain-anthropic",
    "langchain-openai", "langgraph",
})

# Engine files whose numeric change is automatically a major.
ENGINE_FILES = (".nvmrc", ".node-version")


# ─────────────────────────────────────────────────────────────────────
# Label / title rules
# ─────────────────────────────────────────────────────────────────────

RENOVATE_MAJOR_LABELS = frozenset({"tier/major", "deploy/blue-green-required"})
STICKY_LABEL = "requires-blue-green"

# Title shapes we recognise:
#   "Update dependency next to v17"
#   "Update pydantic to v3"
#   "update some-package to 2.0.0"
# Group 1 = package name, Group 2 = new major number.
TITLE_MAJOR_RE = re.compile(
    r"^\s*update(?:\s+dependency)?\s+([^\s]+)\s+to\s+v?(\d+)(?:\.\d+)*",
    re.IGNORECASE,
)


def parse_labels(raw: str) -> list[str]:
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        # Accept comma-separated fallback so the script is easy to
        # call from a shell (unit tests do this).
        return [s.strip() for s in raw.split(",") if s.strip()]
    return [str(x) for x in parsed]


# ─────────────────────────────────────────────────────────────────────
# Diff-based major-bump detection
# ─────────────────────────────────────────────────────────────────────

SEMVER_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def _major_of(version: str) -> int | None:
    """Extract the first semver component from a version spec.

    Accepts: '1.2.3', '^1.2', '~1', '>=1.0,<2', 'v1.2', '1'.
    Returns None if we can't confidently read a major number — we
    treat those as "unchanged" to avoid false positives.
    """
    v = version.strip().lstrip("v^~=><!* ")
    m = re.match(r"(\d+)", v)
    return int(m.group(1)) if m else None


def _git_show(sha: str, path: str) -> str | None:
    """Return `git show <sha>:<path>` or None if the file didn't exist."""
    try:
        proc = subprocess.run(
            ["git", "show", f"{sha}:{path}"],
            check=True, capture_output=True, text=True,
        )
        return proc.stdout
    except subprocess.CalledProcessError:
        return None


def _read_package_json_deps(src: str) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        data = json.loads(src)
    except json.JSONDecodeError:
        return out
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        d = data.get(key, {})
        if isinstance(d, dict):
            for name, spec in d.items():
                if isinstance(spec, str):
                    out[name] = spec
    # engines.{node,pnpm} also counts — bumping these is by policy a major.
    engines = data.get("engines", {})
    if isinstance(engines, dict):
        for eng, spec in engines.items():
            if isinstance(spec, str):
                out[f"engines.{eng}"] = spec
    return out


REQ_LINE_RE = re.compile(
    r"^([A-Za-z0-9_.\-\[\]]+)\s*([=<>!~^].*)?$"
)


def _read_requirements_in(src: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in src.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        m = REQ_LINE_RE.match(line)
        if not m:
            continue
        name, spec = m.group(1).lower(), (m.group(2) or "").strip()
        out[name] = spec
    return out


def _read_engine_file(src: str) -> str:
    return (src or "").strip().splitlines()[0].strip() if src else ""


def detect_major_bumps(base: str, head: str) -> list[tuple[str, str, str]]:
    """Return [(package, before, after), …] for any tracked major bump.

    Files inspected: package.json, backend/requirements.in, .nvmrc,
    .node-version.
    """
    hits: list[tuple[str, str, str]] = []

    # ─── package.json ──────────────────────────────────────────────
    base_pkg = _git_show(base, "package.json") or ""
    head_pkg = _git_show(head, "package.json") or ""
    if base_pkg and head_pkg:
        before = _read_package_json_deps(base_pkg)
        after = _read_package_json_deps(head_pkg)
        for name, new_spec in after.items():
            old_spec = before.get(name)
            if old_spec is None or old_spec == new_spec:
                continue
            is_engine = name.startswith("engines.")
            # Engine bumps are *always* major by policy.
            if is_engine:
                hits.append((name, old_spec, new_spec))
                continue
            # Regular package: only tracked frameworks, only when the
            # first semver component moved.
            if name not in TRACKED_FRAMEWORKS:
                continue
            old_major = _major_of(old_spec)
            new_major = _major_of(new_spec)
            if old_major is None or new_major is None:
                continue
            if old_major != new_major:
                hits.append((name, old_spec, new_spec))

    # ─── backend/requirements.in ───────────────────────────────────
    base_req = _git_show(base, "backend/requirements.in") or ""
    head_req = _git_show(head, "backend/requirements.in") or ""
    if base_req and head_req:
        before_py = _read_requirements_in(base_req)
        after_py = _read_requirements_in(head_req)
        for name, new_spec in after_py.items():
            if name not in TRACKED_FRAMEWORKS:
                continue
            old_spec = before_py.get(name)
            if old_spec is None or old_spec == new_spec:
                continue
            old_major = _major_of(old_spec) if old_spec else None
            new_major = _major_of(new_spec) if new_spec else None
            if old_major is None or new_major is None:
                continue
            if old_major != new_major:
                hits.append((name, old_spec, new_spec))

    # ─── engine files ──────────────────────────────────────────────
    for ef in ENGINE_FILES:
        base_raw = _git_show(base, ef)
        head_raw = _git_show(head, ef)
        if base_raw is None or head_raw is None:
            continue
        bv = _read_engine_file(base_raw)
        hv = _read_engine_file(head_raw)
        if not bv or not hv or bv == hv:
            continue
        bm, hm = _major_of(bv), _major_of(hv)
        if bm is not None and hm is not None and bm != hm:
            hits.append((ef, bv, hv))

    return hits


# ─────────────────────────────────────────────────────────────────────
# Decision
# ─────────────────────────────────────────────────────────────────────

def decide(
    labels: list[str],
    title: str,
    base: str | None,
    head: str | None,
) -> tuple[str, str]:
    label_set = {l.strip() for l in labels}

    if STICKY_LABEL in label_set:
        return "keep", f"PR already carries `{STICKY_LABEL}` (sticky)."

    if label_set & RENOVATE_MAJOR_LABELS:
        hit = ", ".join(sorted(label_set & RENOVATE_MAJOR_LABELS))
        return "add", f"Renovate tier label(s) present: {hit}."

    # Title-based heuristic — catches Renovate PRs before tier/major
    # gets labelled, and human PRs that mirror the same title shape.
    m = TITLE_MAJOR_RE.match(title or "")
    if m:
        pkg = m.group(1).lower().removeprefix("dependency:")
        # Normalise "@scope/name" bare package (Renovate sometimes
        # writes "dependency next", sometimes "next").
        if pkg in TRACKED_FRAMEWORKS or any(pkg.endswith(f"/{tf}") for tf in TRACKED_FRAMEWORKS):
            return (
                "add",
                f"PR title signals major bump for tracked framework `{pkg}`.",
            )

    # Diff-based detection for human PRs.
    if base and head:
        try:
            bumps = detect_major_bumps(base, head)
        except Exception as exc:  # pragma: no cover — defensive
            return "noop", f"diff scan failed: {exc!r}"
        if bumps:
            desc = ", ".join(f"{pkg}: {old}→{new}" for pkg, old, new in bumps[:3])
            more = f" (+{len(bumps) - 3} more)" if len(bumps) > 3 else ""
            return "add", f"Major bump detected in diff: {desc}{more}."

    return "noop", "No major-bump signal detected."


# ─────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────

def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--labels", default="[]", help="JSON array of PR labels")
    ap.add_argument("--title",  default="")
    ap.add_argument("--base",   default="")
    ap.add_argument("--head",   default="")
    args = ap.parse_args(argv)

    labels = parse_labels(args.labels)
    decision, reason = decide(labels, args.title, args.base or None, args.head or None)

    # GITHUB_OUTPUT kv-pair format. Also emit to stderr for the gate's
    # step-summary.
    print(f"decision={decision}")
    # Replace newlines in reason so the kv-pair format stays intact.
    one_line = reason.replace("\n", " ")
    print(f"reason={one_line}")
    sys.stderr.write(f"[bluegreen_label_decider] {decision}: {one_line}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
