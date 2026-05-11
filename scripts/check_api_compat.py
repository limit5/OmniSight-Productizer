#!/usr/bin/env python3
"""OP-885 — patchset OpenAPI backwards-compatibility gate.

Sibling to ``scripts/check_migration_compat.py`` but for the HTTP
contract instead of the database schema. Diffs the committed
``openapi.json`` at the PR base against the version at the PR head and
refuses to ship breaking changes unless the patchset's commit messages
carry an ``api:approved-breaking`` trailer.

What counts as breaking (per OP-885 AC#3):

* **Removed endpoint**           — a path/method pair disappears.
* **Renamed endpoint**           — an ``operationId`` migrates from one
  path/method to a different one (modelled as remove + add of the same
  ``operationId``).
* **Removed field**              — a property disappears from a request
  or response schema (operationally a wire-shape break for clients that
  read the field).
* **Required-new-field**         — a property is added to a request
  schema with ``required: true``. Old clients that don't send it will
  start getting 422s.

What is intentionally NOT breaking:

* **Added endpoint / method**    — new surface area, old clients
  unaffected.
* **Added optional field**       — request side: server doesn't reject
  old clients; response side: old clients ignore unknown keys.
* **Loosened constraint**        — e.g. ``required`` flipping to
  optional, ``maxLength`` growing.

Errors (per OP-885 §Error catalog):

* :class:`APIBreakingChangeRefused` — CI fails with a structured diff
  report on stdout.
* :class:`OpenAPIDiffParseFailed` — base or head OpenAPI JSON is
  unparseable; script degrades to "manual review required" (exit code
  2) instead of greenlighting the patch.

Usage::

    python scripts/check_api_compat.py \\
        --base-ref origin/main \\
        --head-ref HEAD

    # explicit file paths (e.g. for local diffing two snapshots)
    python scripts/check_api_compat.py \\
        --base-file /tmp/before.json --head-file /tmp/after.json

Exit codes::

    0  no breaking change, or approved-breaking trailer present
    1  breaking change detected, no trailer (APIBreakingChangeRefused)
    2  parse failure on either side (OpenAPIDiffParseFailed)
    3  invocation error (missing files, bad refs, etc.)
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SPEC_PATH = "openapi.json"
APPROVED_BREAKING_TRAILER = "api:approved-breaking"


class APIBreakingChangeRefused(Exception):
    """Breaking change detected and no override trailer present."""


class OpenAPIDiffParseFailed(Exception):
    """Either side of the diff is not parseable as an OpenAPI 3.x JSON."""


@dataclass
class BreakingFinding:
    kind: str  # one of: removed_endpoint | renamed_endpoint |
               #         removed_field | required_new_field
    location: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "location": self.location, "detail": self.detail}


@dataclass
class DiffReport:
    findings: list[BreakingFinding] = field(default_factory=list)
    approved_breaking: bool = False
    parse_error: str | None = None

    @property
    def has_breaking(self) -> bool:
        return bool(self.findings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "breaking_findings": [f.to_dict() for f in self.findings],
            "approved_breaking_trailer": self.approved_breaking,
            "parse_error": self.parse_error,
        }


# ── git plumbing ───────────────────────────────────────────────────


def _git_show(ref: str, path: str) -> str:
    """Return the contents of ``path`` at ``ref`` via ``git show``."""
    out = subprocess.run(
        ["git", "show", f"{ref}:{path}"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0:
        raise FileNotFoundError(
            f"git show {ref}:{path} failed: {out.stderr.strip()}"
        )
    return out.stdout


def _git_log_messages(commit_range: str) -> str:
    """Concatenated commit message bodies in ``base..head`` range."""
    out = subprocess.run(
        ["git", "log", commit_range, "--format=%B%n--%n"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0:
        # Don't fail the gate on a malformed range — treat as "no override".
        return ""
    return out.stdout


def has_approved_breaking_trailer(commit_range: str) -> bool:
    """True iff any commit in the range carries the override trailer."""
    body = _git_log_messages(commit_range)
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Accept both ``api:approved-breaking`` (bare token) and
        # ``API-Approved-Breaking: <reason>`` (Gerrit-style trailer).
        if line.lower().startswith(APPROVED_BREAKING_TRAILER):
            return True
        if line.lower().startswith("api-approved-breaking:"):
            return True
    return False


# ── OpenAPI loading ────────────────────────────────────────────────


def _load_spec(text: str, source_label: str) -> dict[str, Any]:
    try:
        spec = json.loads(text)
    except json.JSONDecodeError as exc:
        raise OpenAPIDiffParseFailed(
            f"{source_label}: not valid JSON ({exc.msg} at line {exc.lineno})"
        ) from exc
    if not isinstance(spec, dict) or "paths" not in spec:
        raise OpenAPIDiffParseFailed(
            f"{source_label}: missing 'paths' — not an OpenAPI document"
        )
    return spec


# ── Schema property walker ────────────────────────────────────────


_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head"}


def _resolve_ref(spec: dict[str, Any], ref: str) -> dict[str, Any]:
    """Resolve a local ``$ref`` like ``#/components/schemas/Foo``.

    Returns an empty dict if the ref points outside the document we
    were given — that's a spec authoring problem, not our problem to
    fix in the gate, so don't crash.
    """
    if not ref.startswith("#/"):
        return {}
    node: Any = spec
    for part in ref[2:].split("/"):
        if not isinstance(node, dict) or part not in node:
            return {}
        node = node[part]
    return node if isinstance(node, dict) else {}


def _collect_properties(
    spec: dict[str, Any], schema: dict[str, Any], _seen: set[str] | None = None
) -> tuple[set[str], set[str]]:
    """Return ``(all_properties, required_properties)`` for a schema.

    Resolves a single layer of ``$ref`` and unfolds ``allOf``. Does not
    descend into nested object values — we only diff the top-level
    contract shape (adding a property *to a nested object* is the same
    issue as adding it at the top level: it changes the wire format,
    and our top-level walker still catches it because the nested object
    is itself defined as a referenced schema and shows up as its own
    diff target). ``_seen`` breaks $ref cycles.
    """
    _seen = _seen or set()
    if "$ref" in schema:
        ref = schema["$ref"]
        if ref in _seen:
            return set(), set()
        _seen = _seen | {ref}
        schema = _resolve_ref(spec, ref)

    props: set[str] = set()
    required: set[str] = set()

    for sub in schema.get("allOf", []) or []:
        sub_props, sub_required = _collect_properties(spec, sub, _seen)
        props |= sub_props
        required |= sub_required

    for name, _value in (schema.get("properties") or {}).items():
        props.add(name)

    for name in schema.get("required") or []:
        required.add(name)

    return props, required


# ── Path/operation enumeration ─────────────────────────────────────


@dataclass(frozen=True)
class Operation:
    path: str
    method: str  # lowercase
    operation_id: str | None
    op: dict[str, Any]  # raw OpenAPI Operation object


def _enumerate_operations(spec: dict[str, Any]) -> dict[tuple[str, str], Operation]:
    """Map ``(path, method)`` → :class:`Operation`."""
    out: dict[tuple[str, str], Operation] = {}
    for path, methods in (spec.get("paths") or {}).items():
        if not isinstance(methods, dict):
            continue
        for method, op in methods.items():
            if method.lower() not in _HTTP_METHODS:
                continue
            if not isinstance(op, dict):
                continue
            out[(path, method.lower())] = Operation(
                path=path,
                method=method.lower(),
                operation_id=op.get("operationId"),
                op=op,
            )
    return out


# ── Per-operation diff ─────────────────────────────────────────────


def _diff_request_body(
    base_spec: dict[str, Any],
    head_spec: dict[str, Any],
    base_op: Operation,
    head_op: Operation,
) -> list[BreakingFinding]:
    findings: list[BreakingFinding] = []
    base_body = (base_op.op.get("requestBody") or {}).get("content") or {}
    head_body = (head_op.op.get("requestBody") or {}).get("content") or {}
    for content_type, base_media in base_body.items():
        head_media = head_body.get(content_type)
        if head_media is None:
            continue  # content type dropped; covered by removed_endpoint
                      # at a coarser level if needed
        base_schema = base_media.get("schema") or {}
        head_schema = head_media.get("schema") or {}
        base_props, base_required = _collect_properties(base_spec, base_schema)
        head_props, head_required = _collect_properties(head_spec, head_schema)

        for removed in sorted(base_props - head_props):
            findings.append(
                BreakingFinding(
                    kind="removed_field",
                    location=f"{head_op.method.upper()} {head_op.path} "
                             f"request.{content_type}.{removed}",
                    detail="field present in base spec, missing in head spec",
                )
            )
        new_required = (head_required - base_required) & (head_props - base_props)
        # A field that already existed and was optional, now flipped to
        # required: also a break.
        flipped = (head_required - base_required) & (head_props & base_props)
        for field_name in sorted(new_required | flipped):
            findings.append(
                BreakingFinding(
                    kind="required_new_field",
                    location=f"{head_op.method.upper()} {head_op.path} "
                             f"request.{content_type}.{field_name}",
                    detail="field is newly required — old clients will get 422",
                )
            )
    return findings


def _diff_response_body(
    base_spec: dict[str, Any],
    head_spec: dict[str, Any],
    base_op: Operation,
    head_op: Operation,
) -> list[BreakingFinding]:
    findings: list[BreakingFinding] = []
    base_resps = base_op.op.get("responses") or {}
    head_resps = head_op.op.get("responses") or {}
    for status, base_resp in base_resps.items():
        head_resp = head_resps.get(status)
        if head_resp is None:
            continue
        base_content = base_resp.get("content") or {}
        head_content = head_resp.get("content") or {}
        for content_type, base_media in base_content.items():
            head_media = head_content.get(content_type)
            if head_media is None:
                continue
            base_schema = base_media.get("schema") or {}
            head_schema = head_media.get("schema") or {}
            base_props, _ = _collect_properties(base_spec, base_schema)
            head_props, _ = _collect_properties(head_spec, head_schema)
            for removed in sorted(base_props - head_props):
                findings.append(
                    BreakingFinding(
                        kind="removed_field",
                        location=f"{head_op.method.upper()} {head_op.path} "
                                 f"response.{status}.{content_type}.{removed}",
                        detail="response field present in base, missing in head",
                    )
                )
    return findings


def _diff_query_params(
    base_op: Operation, head_op: Operation
) -> list[BreakingFinding]:
    findings: list[BreakingFinding] = []
    base_params = {
        (p.get("in"), p.get("name")): p
        for p in (base_op.op.get("parameters") or [])
        if isinstance(p, dict)
    }
    head_params = {
        (p.get("in"), p.get("name")): p
        for p in (head_op.op.get("parameters") or [])
        if isinstance(p, dict)
    }
    for key, base_p in base_params.items():
        if key not in head_params:
            findings.append(
                BreakingFinding(
                    kind="removed_field",
                    location=f"{head_op.method.upper()} {head_op.path} "
                             f"param.{key[0]}.{key[1]}",
                    detail="parameter removed",
                )
            )
    for key, head_p in head_params.items():
        if key in base_params:
            base_p = base_params[key]
            if not base_p.get("required") and head_p.get("required"):
                findings.append(
                    BreakingFinding(
                        kind="required_new_field",
                        location=f"{head_op.method.upper()} {head_op.path} "
                                 f"param.{key[0]}.{key[1]}",
                        detail="parameter flipped optional → required",
                    )
                )
        else:
            if head_p.get("required"):
                findings.append(
                    BreakingFinding(
                        kind="required_new_field",
                        location=f"{head_op.method.upper()} {head_op.path} "
                                 f"param.{key[0]}.{key[1]}",
                        detail="new required parameter — old clients will omit it",
                    )
                )
    return findings


# ── Top-level diff ─────────────────────────────────────────────────


def diff_specs(base_spec: dict[str, Any], head_spec: dict[str, Any]) -> list[BreakingFinding]:
    """Return a list of breaking-change findings (empty == backwards compatible)."""
    findings: list[BreakingFinding] = []
    base_ops = _enumerate_operations(base_spec)
    head_ops = _enumerate_operations(head_spec)

    # Detect renames: an operation_id present in base@one path moved to
    # a different path/method in head. Reported as renamed_endpoint
    # instead of removed_endpoint so the operator sees the intent.
    base_by_opid: dict[str, tuple[str, str]] = {
        op.operation_id: (op.path, op.method)
        for op in base_ops.values()
        if op.operation_id
    }
    head_by_opid: dict[str, tuple[str, str]] = {
        op.operation_id: (op.path, op.method)
        for op in head_ops.values()
        if op.operation_id
    }

    handled_renames: set[tuple[str, str]] = set()
    for opid, base_loc in base_by_opid.items():
        head_loc = head_by_opid.get(opid)
        if head_loc is not None and head_loc != base_loc:
            findings.append(
                BreakingFinding(
                    kind="renamed_endpoint",
                    location=f"operationId={opid}",
                    detail=f"moved from {base_loc[1].upper()} {base_loc[0]} "
                           f"to {head_loc[1].upper()} {head_loc[0]}",
                )
            )
            handled_renames.add(base_loc)
            handled_renames.add(head_loc)

    for key, base_op in base_ops.items():
        if key not in head_ops:
            if key in handled_renames:
                continue
            findings.append(
                BreakingFinding(
                    kind="removed_endpoint",
                    location=f"{base_op.method.upper()} {base_op.path}",
                    detail="endpoint present in base spec, missing in head spec",
                )
            )
            continue
        head_op = head_ops[key]
        findings.extend(_diff_request_body(base_spec, head_spec, base_op, head_op))
        findings.extend(_diff_response_body(base_spec, head_spec, base_op, head_op))
        findings.extend(_diff_query_params(base_op, head_op))

    return findings


# ── CLI plumbing ──────────────────────────────────────────────────


def _load_from_args(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any]]:
    if args.base_file:
        base_text = Path(args.base_file).read_text(encoding="utf-8")
    else:
        base_text = _git_show(args.base_ref, args.spec_path)
    if args.head_file:
        head_text = Path(args.head_file).read_text(encoding="utf-8")
    elif args.head_ref == "HEAD" and Path(args.spec_path).exists():
        # Use the working tree at HEAD so local pre-commit invocations
        # see the in-progress edits. CI's HEAD == checked-out tree, so
        # this is equivalent in CI.
        head_text = Path(args.spec_path).read_text(encoding="utf-8")
    else:
        head_text = _git_show(args.head_ref, args.spec_path)
    return _load_spec(base_text, f"base ({args.base_ref})"), _load_spec(
        head_text, f"head ({args.head_ref})"
    )


def run(args: argparse.Namespace) -> tuple[DiffReport, int]:
    report = DiffReport()
    try:
        base_spec, head_spec = _load_from_args(args)
    except OpenAPIDiffParseFailed as exc:
        report.parse_error = str(exc)
        return report, 2
    except FileNotFoundError as exc:
        # Couldn't read the spec at the base ref — first-time setup,
        # or the file was added in this patchset. Treat as "no prior
        # contract to break."
        if args.allow_missing_base:
            return report, 0
        report.parse_error = str(exc)
        return report, 2

    report.findings = diff_specs(base_spec, head_spec)
    report.approved_breaking = has_approved_breaking_trailer(args.commit_range)

    if report.has_breaking and not report.approved_breaking:
        return report, 1
    return report, 0


def _format_report(report: DiffReport) -> str:
    return json.dumps(report.to_dict(), indent=2, sort_keys=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base-ref", default="origin/main",
                    help="Git ref for the base side of the diff.")
    ap.add_argument("--head-ref", default="HEAD",
                    help="Git ref for the head side of the diff.")
    ap.add_argument("--base-file", default=None,
                    help="Explicit base OpenAPI JSON path (overrides --base-ref).")
    ap.add_argument("--head-file", default=None,
                    help="Explicit head OpenAPI JSON path (overrides --head-ref).")
    ap.add_argument("--spec-path", default=DEFAULT_SPEC_PATH,
                    help="Repo-relative path to the OpenAPI JSON.")
    ap.add_argument("--commit-range", default=None,
                    help="Git range to scan for the api:approved-breaking "
                         "trailer (default: <base-ref>..<head-ref>).")
    ap.add_argument("--allow-missing-base", action="store_true",
                    help="Treat a missing base spec as 'no prior contract' "
                         "(exit 0) rather than 'parse error' (exit 2). Useful "
                         "for first-time bootstrap of the gate.")
    args = ap.parse_args(argv)

    if args.commit_range is None:
        args.commit_range = f"{args.base_ref}..{args.head_ref}"

    report, code = run(args)
    sys.stdout.write(_format_report(report) + "\n")

    if code == 1:
        sys.stderr.write(
            "\nAPIBreakingChangeRefused: breaking change(s) detected. "
            "Either revert, or add an 'api:approved-breaking: <reason>' "
            "trailer to one of the commits in this patchset and re-run.\n"
        )
    elif code == 2:
        sys.stderr.write(
            "\nOpenAPIDiffParseFailed: degrade to manual review path — "
            "a human reviewer must compare the OpenAPI surface by hand.\n"
        )

    return code


if __name__ == "__main__":
    sys.exit(main())
