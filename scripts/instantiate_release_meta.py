#!/usr/bin/env python3
"""OP-937 (G1) — Release template engine.

Instantiate a RELEASE-vX.Y.Z META + 13 child JIRA tickets from
``config/release_template.yaml``. The script wires the canonical
``blockedBy`` chain (per L-OP-874: inward=blocker, outward=blocked)
and a ``Relates`` link from each child back to the META, then leaves
a comment on the META recording the template version and the
``R-id → OP-key`` map.

Usage::

    scripts/instantiate_release_meta.py --version v0.5.1-rc1 --dry-run
    scripts/instantiate_release_meta.py --version v0.5.1-rc1
    scripts/instantiate_release_meta.py --version v0.5.1-rc1 --force

Error catalog
-------------
* ``TemplateYAMLInvalid``  — template schema validation failed
* ``MetaAlreadyExists``    — idempotency guard refused re-creation
* ``BlockedByWiringFailed``— partial state; created keys spilled to
  ``/tmp/release-rollback-<version>.json`` for operator cleanup

State transitions
-----------------
validate template -> check existing -> create META -> create children
-> wire blockedBy + Relates -> comment META + return summary
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.agents import file_coordinator, jira_dispatch  # noqa: E402

# ── Constants ────────────────────────────────────────────────────────

DEFAULT_TEMPLATE_PATH = REPO_ROOT / "config" / "release_template.yaml"
DEFAULT_META_DESC_TEMPLATE_PATH = (
    REPO_ROOT / "config" / "release_meta_description.md.template"
)
EXPECTED_CHILD_COUNT = 13
SEMVER_VERSION_RE = re.compile(
    r"^v\d+\.\d+\.\d+(?:-(?:rc|beta|alpha)\d+)?$"
)
VALID_AREAS = {
    "backend", "frontend", "docs", "tests", "devops",
    "security", "embedded", "tooling", "gerrit", "db",
}
VALID_TIERS = {"S", "M", "L", "X"}
ARCHIVED_STATES = {"Archived", "アーカイブ済み"}
DEFAULT_AGENT_CLASS = "subscription-claude"
ROLLBACK_DIR = Path(os.environ.get("OMNISIGHT_RELEASE_ROLLBACK_DIR", "/tmp"))


# ── Exit codes ───────────────────────────────────────────────────────

EXIT_OK = 0
EXIT_META_ALREADY_EXISTS = 1
EXIT_TEMPLATE_INVALID = 2
EXIT_WIRING_FAILED = 3


# ── Custom errors ────────────────────────────────────────────────────


class TemplateYAMLInvalid(ValueError):
    """Template schema validation failed."""


class MetaAlreadyExists(RuntimeError):
    """Idempotency guard: META already exists for the requested version."""


class BlockedByWiringFailed(RuntimeError):
    """Partial state: META / children created but link wiring failed."""


# ── Template loading + validation ────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class ChildSpec:
    r_id: str
    name: str
    blocked_by: tuple[str, ...]
    agent_class: str
    tier: str
    areas: tuple[str, ...]
    ac: str


@dataclasses.dataclass(frozen=True)
class TemplateSpec:
    version: str
    meta_summary_template: str
    meta_labels: tuple[str, ...]
    meta_tier: str
    meta_class: str
    meta_issuetype: str
    children: tuple[ChildSpec, ...]


def load_template(path: Path) -> TemplateSpec:
    """Load + validate a release template YAML file.

    Raises :class:`TemplateYAMLInvalid` with a human-readable reason if
    the file is missing, unparseable, or fails schema validation.
    """
    if not path.is_file():
        raise TemplateYAMLInvalid(f"template not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise TemplateYAMLInvalid(f"YAML parse error in {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise TemplateYAMLInvalid(f"top-level must be a mapping in {path}")
    return _validate_template(raw, path)


def _validate_template(raw: dict, path: Path) -> TemplateSpec:
    version = raw.get("version")
    if not isinstance(version, str) or not version:
        raise TemplateYAMLInvalid(f"{path}: top-level `version` must be a non-empty string")

    meta_raw = raw.get("meta")
    if not isinstance(meta_raw, dict):
        raise TemplateYAMLInvalid(f"{path}: `meta` section is required and must be a mapping")
    summary_template = meta_raw.get("summary_template")
    if not isinstance(summary_template, str) or "{version}" not in summary_template:
        raise TemplateYAMLInvalid(
            f"{path}: meta.summary_template must be a string containing `{{version}}`"
        )
    meta_labels_raw = meta_raw.get("labels", [])
    if not isinstance(meta_labels_raw, list) or not all(isinstance(label, str) for label in meta_labels_raw):
        raise TemplateYAMLInvalid(f"{path}: meta.labels must be a list of strings")
    meta_tier = meta_raw.get("tier", "M")
    if meta_tier not in VALID_TIERS:
        raise TemplateYAMLInvalid(f"{path}: meta.tier {meta_tier!r} not in {sorted(VALID_TIERS)}")
    meta_class = meta_raw.get("class", DEFAULT_AGENT_CLASS)
    if not isinstance(meta_class, str) or not meta_class:
        raise TemplateYAMLInvalid(f"{path}: meta.class must be a non-empty string")
    meta_issuetype = meta_raw.get("issuetype", "Story")

    children_raw = raw.get("children")
    if not isinstance(children_raw, list) or len(children_raw) != EXPECTED_CHILD_COUNT:
        raise TemplateYAMLInvalid(
            f"{path}: `children` must be a list of exactly {EXPECTED_CHILD_COUNT} entries "
            f"(got {len(children_raw) if isinstance(children_raw, list) else type(children_raw).__name__})"
        )

    children: list[ChildSpec] = []
    seen_ids: set[str] = set()
    for idx, child_raw in enumerate(children_raw, start=1):
        if not isinstance(child_raw, dict):
            raise TemplateYAMLInvalid(f"{path}: children[{idx-1}] must be a mapping")
        r_id = child_raw.get("r_id")
        expected_id = f"R{idx}"
        if r_id != expected_id:
            raise TemplateYAMLInvalid(
                f"{path}: children[{idx-1}].r_id must be {expected_id!r} (got {r_id!r}); "
                f"R-ids must be R1..R{EXPECTED_CHILD_COUNT} in order"
            )
        if r_id in seen_ids:
            raise TemplateYAMLInvalid(f"{path}: duplicate r_id {r_id!r}")
        seen_ids.add(r_id)

        name = child_raw.get("name")
        if not isinstance(name, str) or not name:
            raise TemplateYAMLInvalid(f"{path}: {r_id}.name must be a non-empty string")

        blocked_by_raw = child_raw.get("blocked_by", [])
        if not isinstance(blocked_by_raw, list) or not all(isinstance(b, str) for b in blocked_by_raw):
            raise TemplateYAMLInvalid(f"{path}: {r_id}.blocked_by must be a list of R-id strings")
        for blocker in blocked_by_raw:
            if not re.fullmatch(r"R([1-9]|1[0-3])", blocker):
                raise TemplateYAMLInvalid(
                    f"{path}: {r_id}.blocked_by entry {blocker!r} is not a valid R1..R13 id"
                )
            if blocker == r_id:
                raise TemplateYAMLInvalid(f"{path}: {r_id}.blocked_by cannot include itself")
            if blocker not in seen_ids:
                # Forward references would form a cycle; require chain order.
                raise TemplateYAMLInvalid(
                    f"{path}: {r_id}.blocked_by references {blocker!r} which is not yet declared. "
                    "Forward references (cycles) are rejected; declare blockers earlier in the list."
                )

        agent_class = child_raw.get("class", DEFAULT_AGENT_CLASS)
        if not isinstance(agent_class, str) or not agent_class:
            raise TemplateYAMLInvalid(f"{path}: {r_id}.class must be a non-empty string")

        tier = child_raw.get("tier")
        if tier not in VALID_TIERS:
            raise TemplateYAMLInvalid(f"{path}: {r_id}.tier {tier!r} not in {sorted(VALID_TIERS)}")

        areas_raw = child_raw.get("areas", [])
        if not isinstance(areas_raw, list) or not areas_raw or not all(isinstance(a, str) for a in areas_raw):
            raise TemplateYAMLInvalid(f"{path}: {r_id}.areas must be a non-empty list of strings")
        for area in areas_raw:
            if area not in VALID_AREAS:
                raise TemplateYAMLInvalid(
                    f"{path}: {r_id} area {area!r} not in {sorted(VALID_AREAS)}"
                )

        ac = child_raw.get("ac")
        if not isinstance(ac, str) or not ac.strip():
            raise TemplateYAMLInvalid(f"{path}: {r_id}.ac must be a non-empty string")

        children.append(ChildSpec(
            r_id=r_id,
            name=name,
            blocked_by=tuple(blocked_by_raw),
            agent_class=agent_class,
            tier=tier,
            areas=tuple(areas_raw),
            ac=ac,
        ))

    return TemplateSpec(
        version=version,
        meta_summary_template=summary_template,
        meta_labels=tuple(meta_labels_raw),
        meta_tier=meta_tier,
        meta_class=meta_class,
        meta_issuetype=meta_issuetype,
        children=tuple(children),
    )


# ── Version + summary helpers ────────────────────────────────────────


def validate_version(version: str) -> None:
    if not SEMVER_VERSION_RE.fullmatch(version):
        raise SystemExit(
            f"invalid --version {version!r}: expected vMAJOR.MINOR.PATCH "
            f"with optional -rc/-beta/-alpha suffix (e.g. v0.5.1, v0.5.1-rc1)"
        )


def render_meta_summary(template: TemplateSpec, version: str) -> str:
    return template.meta_summary_template.format(version=version)


def render_child_summary(child: ChildSpec, version: str) -> str:
    return f"{child.r_id} ({version}) — " + child.name.format(version=version)


def render_meta_labels(template: TemplateSpec, version: str) -> list[str]:
    # Drop any pre-existing `tier:` labels in the static list so the
    # canonical `meta.tier` value is the single source of truth (avoids
    # ``tier:M`` + ``tier:X`` ending up on the same META after a template
    # edit lands a mismatched literal).
    labels = [label for label in template.meta_labels if not label.startswith("tier:")]
    labels = list(dict.fromkeys(labels))
    labels.append(f"tier:{template.meta_tier}")
    labels.append(f"RELEASE-{version}")
    return list(dict.fromkeys(labels))


def render_child_labels(child: ChildSpec, version: str) -> list[str]:
    labels: list[str] = [
        "agent:auto",
        f"class:{child.agent_class}",
        f"tier:{child.tier}",
        f"RELEASE-{version}",
        f"release-child:{child.r_id}",
    ]
    for area in child.areas:
        labels.append(f"area:{area}")
    return list(dict.fromkeys(labels))


def render_child_description(child: ChildSpec, version: str, meta_key: str) -> str:
    ac_text = child.ac.format(version=version).rstrip()
    return (
        f"## Goal\n"
        f"Release-conductor child `{child.r_id}` for **{version}**.\n"
        f"Parent META: {meta_key}.\n\n"
        f"## Acceptance Criteria\n"
        f"{ac_text}\n\n"
        f"## Spec references\n"
        f"- docs/operations/release-conductor-pattern.md\n"
        f"- docs/operations/release-conductor-runbook.md\n"
        f"- Template: config/release_template.yaml\n\n"
        f"## Definition of Done\n"
        f"- [ ] All Acceptance Criteria above checked\n"
        f"- [ ] Gerrit Code-Review +2 per ADR-0003\n"
        f"- [ ] Commit message references this ticket key\n"
        f"- [ ] Parent META {meta_key} updated via comment when this child closes\n"
    )


def render_meta_description(
    template: TemplateSpec,
    version: str,
    template_path: Path,
    template_desc_path: Path,
) -> str:
    desc_template = template_desc_path.read_text(encoding="utf-8")
    rows = []
    for child in template.children:
        rows.append(
            f"| {child.r_id} | "
            f"{child.name.format(version=version)} | "
            f"tier:{child.tier} | "
            f"{', '.join('area:' + a for a in child.areas)} |"
        )
    child_table = "\n".join(rows)
    return desc_template.format(
        version=version,
        template_version=template.version,
        created_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        child_table=child_table,
    )


# ── JIRA client wrapper ──────────────────────────────────────────────


def _adf_codeblock(markdown_text: str) -> dict[str, Any]:
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "codeBlock",
                "attrs": {"language": "markdown"},
                "content": [{"type": "text", "text": markdown_text}],
            }
        ],
    }


def find_existing_meta_key(
    client: jira_dispatch.DispatchClient,
    version: str,
) -> tuple[str | None, str | None]:
    """Return (key, status_name) for an existing META, or (None, None).

    Detection scopes to the project and the canonical
    ``RELEASE-<version>`` label so we never collide on a fuzzy summary
    match.
    """
    label = f"RELEASE-{version}"
    jql = (
        f'project = "{client.project_key}" '
        f'AND labels = "{label}" '
        f'AND labels = "meta:release"'
    )
    resp = jira_dispatch._request(
        client,
        "POST",
        "/search/jql",
        {"jql": jql, "fields": ["summary", "status"], "maxResults": 5},
    )
    for issue in resp.get("issues", []) or []:
        return (
            issue.get("key"),
            (((issue.get("fields") or {}).get("status") or {}).get("name") or ""),
        )
    return (None, None)


def create_issue(
    client: jira_dispatch.DispatchClient,
    *,
    summary: str,
    description_markdown: str,
    labels: list[str],
    issuetype: str = "Story",
) -> str:
    """POST one JIRA issue and return its key. Caller handles rollback."""
    body = {
        "fields": {
            "project": {"key": client.project_key},
            "summary": summary,
            "description": _adf_codeblock(description_markdown),
            "issuetype": {"name": issuetype},
            "labels": labels,
        }
    }
    resp = jira_dispatch._request(client, "POST", "/issue", body)
    key = resp.get("key")
    if not key:
        raise RuntimeError(f"JIRA POST /issue returned no key: {resp!r}")
    return key


# ── Planning + dry-run output ────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class InstantiationPlan:
    version: str
    template_version: str
    meta_summary: str
    meta_labels: tuple[str, ...]
    child_summaries: tuple[tuple[str, str, tuple[str, ...]], ...]  # (r_id, summary, labels)
    blockedby_edges: tuple[tuple[str, str], ...]  # (blocked_r_id, blocker_r_id)


def build_plan(template: TemplateSpec, version: str) -> InstantiationPlan:
    edges: list[tuple[str, str]] = []
    child_rows: list[tuple[str, str, tuple[str, ...]]] = []
    for child in template.children:
        child_rows.append((
            child.r_id,
            render_child_summary(child, version),
            tuple(render_child_labels(child, version)),
        ))
        for blocker in child.blocked_by:
            edges.append((child.r_id, blocker))
    return InstantiationPlan(
        version=version,
        template_version=template.version,
        meta_summary=render_meta_summary(template, version),
        meta_labels=tuple(render_meta_labels(template, version)),
        child_summaries=tuple(child_rows),
        blockedby_edges=tuple(edges),
    )


def render_plan_text(plan: InstantiationPlan) -> str:
    lines = [
        "=== Release META instantiation plan ===",
        f"version:          {plan.version}",
        f"template version: {plan.template_version}",
        "",
        "META",
        f"  summary: {plan.meta_summary}",
        f"  labels:  {', '.join(plan.meta_labels)}",
        "",
        f"Children ({len(plan.child_summaries)}):",
    ]
    for r_id, summary, labels in plan.child_summaries:
        lines.append(f"  - {r_id:>3}  {summary}")
        lines.append(f"        labels: {', '.join(labels)}")
    lines.append("")
    lines.append("blockedBy edges (blocked → blocker; wired via add_blocked_by):")
    for blocked, blocker in plan.blockedby_edges:
        lines.append(f"  {blocked} blockedBy {blocker}")
    lines.append("")
    lines.append("Relates edges (child → META; wired via jira_create_issue_link link_type=Relates):")
    for r_id, _, _ in plan.child_summaries:
        lines.append(f"  {r_id} relates META")
    lines.append("")
    lines.append("(dry-run: no JIRA writes performed)")
    return "\n".join(lines)


# ── Rollback handling ────────────────────────────────────────────────


def rollback_path(version: str) -> Path:
    return ROLLBACK_DIR / f"release-rollback-{version}.json"


def spill_rollback(version: str, created: dict[str, str], reason: str) -> Path:
    path = rollback_path(version)
    payload = {
        "version": version,
        "reason": reason,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "ticket_keys": created,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


# ── Wiring ───────────────────────────────────────────────────────────


def wire_blockedby_chain(
    client: jira_dispatch.DispatchClient,
    children: list[ChildSpec],
    keys_by_r_id: dict[str, str],
) -> int:
    """Wire all blockedBy edges. Returns count of newly created links."""
    created = 0
    for child in children:
        blocked_key = keys_by_r_id[child.r_id]
        for blocker_r_id in child.blocked_by:
            blocker_key = keys_by_r_id[blocker_r_id]
            new_link = file_coordinator.add_blocked_by(
                client,
                blocked_key=blocked_key,
                blocker_key=blocker_key,
                reason=(
                    f"release-conductor template: "
                    f"{child.r_id} blockedBy {blocker_r_id} per "
                    f"config/release_template.yaml (template version "
                    f"recorded on META)"
                ),
            )
            if new_link:
                created += 1
    return created


def wire_relates_to_meta(
    client: jira_dispatch.DispatchClient,
    meta_key: str,
    child_keys: list[str],
) -> int:
    """Wire ``Relates`` links from each child to the META.

    Uses ``file_coordinator.jira_create_issue_link`` so the raw POST
    ``/issueLink`` schema stays inside the allowlisted file (see
    ``scripts/check_issuelink_post_callers.py``).
    """
    created = 0
    for child_key in child_keys:
        if file_coordinator.jira_link_exists(
            client, blocked=child_key, blocker=meta_key, link_type="Relates"
        ):
            continue
        file_coordinator.jira_create_issue_link(
            client,
            inward=child_key,
            outward=meta_key,
            link_type="Relates",
        )
        created += 1
    return created


def render_child_map_comment(
    template_version: str,
    meta_key: str,
    version: str,
    keys_by_r_id: dict[str, str],
) -> str:
    lines = [
        "[release-template-engine] META instantiated.",
        f"version: {version}",
        f"template version: {template_version}",
        f"meta: {meta_key}",
        "child map (R-id -> OP-key):",
    ]
    for r_id in sorted(keys_by_r_id, key=lambda s: int(s[1:])):
        lines.append(f"  {r_id}: {keys_by_r_id[r_id]}")
    lines.append(
        "Pickup gating follows the JIRA Blocks graph; "
        "no external conductor service runs against this META."
    )
    return "\n".join(lines)


# ── Apply (live JIRA writes) ─────────────────────────────────────────


def apply(
    client: jira_dispatch.DispatchClient,
    template: TemplateSpec,
    version: str,
    template_path: Path,
    template_desc_path: Path,
    *,
    force: bool = False,
) -> dict[str, str]:
    """Execute the live instantiation.

    Returns a ``{"META": OP-key, "R1": OP-key, ...}`` map. Raises
    :class:`MetaAlreadyExists` if the version already has a non-Archived
    META and ``force`` is False; raises :class:`BlockedByWiringFailed`
    if a link-wiring step blows up after creation (with a rollback
    file already spilled).
    """
    existing_key, existing_status = find_existing_meta_key(client, version)
    if existing_key is not None:
        if not force or existing_status not in ARCHIVED_STATES:
            raise MetaAlreadyExists(
                f"RELEASE-{version} META already exists: "
                f"{existing_key} (status={existing_status!r}). "
                f"Use --force only when the META was Archived."
            )

    created: dict[str, str] = {}
    try:
        meta_summary = render_meta_summary(template, version)
        meta_description = render_meta_description(
            template, version, template_path, template_desc_path,
        )
        meta_labels = render_meta_labels(template, version)
        meta_key = create_issue(
            client,
            summary=meta_summary,
            description_markdown=meta_description,
            labels=meta_labels,
            issuetype=template.meta_issuetype,
        )
        created["META"] = meta_key

        keys_by_r_id: dict[str, str] = {}
        for child in template.children:
            child_summary = render_child_summary(child, version)
            child_description = render_child_description(child, version, meta_key)
            child_labels = render_child_labels(child, version)
            key = create_issue(
                client,
                summary=child_summary,
                description_markdown=child_description,
                labels=child_labels,
                issuetype="Story",
            )
            keys_by_r_id[child.r_id] = key
            created[child.r_id] = key
    except Exception as exc:
        if created:
            path = spill_rollback(version, created, f"creation failed: {exc}")
            raise BlockedByWiringFailed(
                f"creation aborted partway; rollback list at {path}: {exc}"
            ) from exc
        raise

    try:
        wire_blockedby_chain(client, list(template.children), keys_by_r_id)
        wire_relates_to_meta(client, meta_key, list(keys_by_r_id.values()))
    except Exception as exc:
        path = spill_rollback(version, created, f"link wiring failed: {exc}")
        raise BlockedByWiringFailed(
            f"link wiring failed after ticket creation; "
            f"rollback list at {path}: {exc}"
        ) from exc

    comment_text = render_child_map_comment(
        template.version, meta_key, version, keys_by_r_id,
    )
    jira_dispatch.add_comment(client, meta_key, comment_text)
    return created


# ── CLI ──────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--version",
        required=True,
        help="SemVer release version, e.g. v0.5.1 or v0.5.1-rc1",
    )
    parser.add_argument(
        "--template",
        default=str(DEFAULT_TEMPLATE_PATH),
        help="Path to a release_template.yaml override (default: config/release_template.yaml)",
    )
    parser.add_argument(
        "--meta-description-template",
        default=str(DEFAULT_META_DESC_TEMPLATE_PATH),
        help="Path to the META description markdown template",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the instantiation plan; perform NO JIRA writes",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Allow re-create even if a META exists (only honoured when status=Archived)",
    )
    parser.add_argument(
        "--agent-class",
        default=DEFAULT_AGENT_CLASS,
        help="Runner class to authenticate as when creating tickets",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    validate_version(args.version)

    template_path = Path(args.template)
    template_desc_path = Path(args.meta_description_template)

    try:
        template = load_template(template_path)
    except TemplateYAMLInvalid as exc:
        print(f"TemplateYAMLInvalid: {exc}", file=sys.stderr)
        return EXIT_TEMPLATE_INVALID

    plan = build_plan(template, args.version)

    if args.dry_run:
        print(render_plan_text(plan))
        return EXIT_OK

    client = jira_dispatch.make_client(args.agent_class)

    try:
        created = apply(
            client,
            template,
            args.version,
            template_path,
            template_desc_path,
            force=args.force,
        )
    except MetaAlreadyExists as exc:
        print(f"MetaAlreadyExists: {exc}", file=sys.stderr)
        return EXIT_META_ALREADY_EXISTS
    except BlockedByWiringFailed as exc:
        print(f"BlockedByWiringFailed: {exc}", file=sys.stderr)
        return EXIT_WIRING_FAILED

    print(f"Created RELEASE-{args.version} META + {EXPECTED_CHILD_COUNT} children:")
    for r_id, key in created.items():
        print(f"  {r_id}: {key}")
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
