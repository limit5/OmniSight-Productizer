#!/usr/bin/env python3
"""OP-941 (G5) — Hotfix conductor variant: ``HOTFIX-vX.Y.Z+1`` META + trigger.

Instantiate a ``HOTFIX-vX.Y.Z+1`` META + 4 child JIRA tickets from
``config/hotfix_template.yaml``. The script wires the canonical
``blockedBy`` chain (per L-OP-874: inward=blocker, outward=blocked) and a
``Relates`` link from each child back to the META, then leaves a comment
on the META recording the template version and the ``H-id → OP-key`` map.

It is the G5 counterpart of ``scripts/instantiate_release_meta.py`` (G1)
— the same JIRA-graph-is-the-conductor pattern, narrowed to the
emergency path (D14, OP-886): cherry-pick + smoke → one human +2 →
prod deploy + canary 25% → backport-to-develop check.

Usage::

    scripts/instantiate_hotfix_meta.py --from-change 95001 --target v1.2.4+1 --dry-run
    scripts/instantiate_hotfix_meta.py --from-change 95001 --target v1.2.4+1
    scripts/instantiate_hotfix_meta.py --from-change 95001 --target v1.2.4+1 --force

Error catalog
-------------
* ``HotfixTemplateInvalid``       — template schema validation failed
* ``HotfixSourceChangeNotMerged`` — the ``--from-change`` Gerrit change is
  not ``MERGED``; refuse + alert (you never cherry-pick an unmerged change)
* ``TargetReleaseBranchNotExists``— ``release/<target>`` does not exist
  locally or on the Gerrit remote; refuse + alert (cut it first)
* ``DuplicateHotfix``             — a ``HOTFIX-<target>`` META already
  exists; idempotent skip (exit 0). When it carries the matching
  ``hotfix-source:<change>`` label the message says ``same_source_change``.
* ``JQLQueryFailed``              — pre-create JQL search failed; fail
  closed so a transient JIRA outage never spawns a duplicate META
* ``StateAmbiguous``             — multiple non-Archived ``HOTFIX-<target>``
  METAs exist; alert P0 and refuse to write
* ``HotfixWiringFailed``          — partial state; created keys spilled to
  ``/tmp/hotfix-rollback-<target>.json`` for operator cleanup

State transitions
-----------------
validate target -> Gerrit source change MERGED check -> ``release/<target>``
branch-exists check -> JQL pre-check (HOTFIX-<target>) -> create META ->
create H1..H4 -> wire blockedBy chain + Relates -> comment META -> runner
picks H1.

Recovery / rollback
-------------------
Same shape as G1/G2: on a partial failure the script writes the created
ticket keys to ``/tmp/hotfix-rollback-<target>.json`` before exiting
non-zero; the operator deletes those keys (or runs the G2 cleanup
helper) and re-runs. ``--force`` re-creates only when the prior META is
Archived (workflow-cleaned).
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import subprocess
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

DEFAULT_TEMPLATE_PATH = REPO_ROOT / "config" / "hotfix_template.yaml"
EXPECTED_CHILD_COUNT = 4
# Hotfix target: ``vX.Y.Z+N`` — the ``+N`` is the hotfix counter on the
# release line (mirrors the OP-950 ``hotfix:cherry-pick-to=release/vX.Y.Z+1``
# label and the ``hotfix/vX.Y.Z+1`` branch convention of OP-886).
TARGET_RE = re.compile(
    r"^v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\+[1-9]\d*$"
)
VALID_AREAS = {
    "backend", "frontend", "docs", "tests", "devops",
    "security", "embedded", "tooling", "gerrit", "db",
}
VALID_TIERS = {"S", "M", "L", "X"}
ARCHIVED_STATES = {"Archived", "アーカイブ済み"}
# Default *child* agent-class label (what the YAML usually declares).
DEFAULT_AGENT_CLASS = "subscription-claude"
# Default *runner auth* class — the hotfix flow is codex-driven, matching
# scripts/hotfix_cherry_pick.py and the release-conductor cron.
DEFAULT_RUNNER_CLASS = "subscription-codex"
ROLLBACK_DIR = Path(os.environ.get("OMNISIGHT_HOTFIX_ROLLBACK_DIR", "/tmp"))


# ── Exit codes ───────────────────────────────────────────────────────

EXIT_OK = 0
EXIT_TEMPLATE_INVALID = 2
EXIT_WIRING_FAILED = 3
EXIT_JQL_QUERY_FAILED = 4
EXIT_STATE_AMBIGUOUS = 5
EXIT_SOURCE_NOT_MERGED = 6
EXIT_TARGET_BRANCH_MISSING = 7
# DuplicateHotfix is an idempotent *success*: exit 0.


# ── Custom errors ────────────────────────────────────────────────────


class HotfixTemplateInvalid(ValueError):
    """Template schema validation failed."""


class HotfixSourceChangeNotMerged(RuntimeError):
    """The ``--from-change`` Gerrit change is not in status ``MERGED``."""


class TargetReleaseBranchNotExists(RuntimeError):
    """``release/<target>`` does not exist locally or on the Gerrit remote."""


class DuplicateHotfix(RuntimeError):
    """A ``HOTFIX-<target>`` META already exists — idempotent skip."""


class HotfixWiringFailed(RuntimeError):
    """Partial state: META / children created but link wiring failed."""


class JQLQueryFailed(RuntimeError):
    """Pre-create JQL search failed; fail closed (no duplicate META)."""


class StateAmbiguous(RuntimeError):
    """Multiple non-Archived ``HOTFIX-<target>`` METAs exist (P0)."""


# ── Template loading + validation ────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class ChildSpec:
    h_id: str
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
    """Load + validate a hotfix template YAML file.

    Raises :class:`HotfixTemplateInvalid` with a human-readable reason if
    the file is missing, unparseable, or fails schema validation.
    """
    if not path.is_file():
        raise HotfixTemplateInvalid(f"template not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise HotfixTemplateInvalid(f"YAML parse error in {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise HotfixTemplateInvalid(f"top-level must be a mapping in {path}")
    return _validate_template(raw, path)


def _validate_template(raw: dict, path: Path) -> TemplateSpec:
    version = raw.get("version")
    if not isinstance(version, str) or not version:
        raise HotfixTemplateInvalid(f"{path}: top-level `version` must be a non-empty string")

    meta_raw = raw.get("meta")
    if not isinstance(meta_raw, dict):
        raise HotfixTemplateInvalid(f"{path}: `meta` section is required and must be a mapping")
    summary_template = meta_raw.get("summary_template")
    if not isinstance(summary_template, str) or "{target}" not in summary_template:
        raise HotfixTemplateInvalid(
            f"{path}: meta.summary_template must be a string containing `{{target}}`"
        )
    meta_labels_raw = meta_raw.get("labels", [])
    if not isinstance(meta_labels_raw, list) or not all(isinstance(label, str) for label in meta_labels_raw):
        raise HotfixTemplateInvalid(f"{path}: meta.labels must be a list of strings")
    meta_tier = meta_raw.get("tier", "X")
    if meta_tier not in VALID_TIERS:
        raise HotfixTemplateInvalid(f"{path}: meta.tier {meta_tier!r} not in {sorted(VALID_TIERS)}")
    meta_class = meta_raw.get("class", DEFAULT_AGENT_CLASS)
    if not isinstance(meta_class, str) or not meta_class:
        raise HotfixTemplateInvalid(f"{path}: meta.class must be a non-empty string")
    meta_issuetype = meta_raw.get("issuetype", "Story")

    children_raw = raw.get("children")
    if not isinstance(children_raw, list) or len(children_raw) != EXPECTED_CHILD_COUNT:
        raise HotfixTemplateInvalid(
            f"{path}: `children` must be a list of exactly {EXPECTED_CHILD_COUNT} entries "
            f"(got {len(children_raw) if isinstance(children_raw, list) else type(children_raw).__name__})"
        )

    children: list[ChildSpec] = []
    seen_ids: set[str] = set()
    for idx, child_raw in enumerate(children_raw, start=1):
        if not isinstance(child_raw, dict):
            raise HotfixTemplateInvalid(f"{path}: children[{idx-1}] must be a mapping")
        h_id = child_raw.get("h_id")
        expected_id = f"H{idx}"
        if h_id != expected_id:
            raise HotfixTemplateInvalid(
                f"{path}: children[{idx-1}].h_id must be {expected_id!r} (got {h_id!r}); "
                f"H-ids must be H1..H{EXPECTED_CHILD_COUNT} in order"
            )
        if h_id in seen_ids:
            raise HotfixTemplateInvalid(f"{path}: duplicate h_id {h_id!r}")
        seen_ids.add(h_id)

        name = child_raw.get("name")
        if not isinstance(name, str) or not name:
            raise HotfixTemplateInvalid(f"{path}: {h_id}.name must be a non-empty string")

        blocked_by_raw = child_raw.get("blocked_by", [])
        if not isinstance(blocked_by_raw, list) or not all(isinstance(b, str) for b in blocked_by_raw):
            raise HotfixTemplateInvalid(f"{path}: {h_id}.blocked_by must be a list of H-id strings")
        for blocker in blocked_by_raw:
            if not re.fullmatch(rf"H[1-{EXPECTED_CHILD_COUNT}]", blocker):
                raise HotfixTemplateInvalid(
                    f"{path}: {h_id}.blocked_by entry {blocker!r} is not a valid "
                    f"H1..H{EXPECTED_CHILD_COUNT} id"
                )
            if blocker == h_id:
                raise HotfixTemplateInvalid(f"{path}: {h_id}.blocked_by cannot include itself")
            if blocker not in seen_ids:
                raise HotfixTemplateInvalid(
                    f"{path}: {h_id}.blocked_by references {blocker!r} which is not yet declared. "
                    "Forward references (cycles) are rejected; declare blockers earlier in the list."
                )

        agent_class = child_raw.get("class", DEFAULT_AGENT_CLASS)
        if not isinstance(agent_class, str) or not agent_class:
            raise HotfixTemplateInvalid(f"{path}: {h_id}.class must be a non-empty string")

        tier = child_raw.get("tier")
        if tier not in VALID_TIERS:
            raise HotfixTemplateInvalid(f"{path}: {h_id}.tier {tier!r} not in {sorted(VALID_TIERS)}")

        areas_raw = child_raw.get("areas", [])
        if not isinstance(areas_raw, list) or not areas_raw or not all(isinstance(a, str) for a in areas_raw):
            raise HotfixTemplateInvalid(f"{path}: {h_id}.areas must be a non-empty list of strings")
        for area in areas_raw:
            if area not in VALID_AREAS:
                raise HotfixTemplateInvalid(
                    f"{path}: {h_id} area {area!r} not in {sorted(VALID_AREAS)}"
                )

        ac = child_raw.get("ac")
        if not isinstance(ac, str) or not ac.strip():
            raise HotfixTemplateInvalid(f"{path}: {h_id}.ac must be a non-empty string")

        children.append(ChildSpec(
            h_id=h_id,
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


# ── Target + rendering helpers ───────────────────────────────────────


def validate_target(target: str) -> None:
    if not TARGET_RE.fullmatch(target):
        raise SystemExit(
            f"invalid --target {target!r}: expected vMAJOR.MINOR.PATCH+N "
            f"(the +N hotfix counter), e.g. v1.2.4+1"
        )


def validate_source_change(value: str) -> str:
    text = str(value).strip()
    if not text.isdigit():
        raise SystemExit(
            f"invalid --from-change {value!r}: expected a Gerrit change number"
        )
    return text


def _fmt(text: str, target: str, source_change: str, meta_key: str = "") -> str:
    return text.format(target=target, source_change=source_change, meta_key=meta_key)


def render_meta_summary(template: TemplateSpec, target: str) -> str:
    return template.meta_summary_template.format(target=target)


def render_child_summary(child: ChildSpec, target: str, source_change: str) -> str:
    return f"{child.h_id} ({target}) — " + _fmt(child.name, target, source_change)


def render_meta_labels(template: TemplateSpec, target: str, source_change: str) -> list[str]:
    # Drop any literal ``tier:`` labels in the static list so ``meta.tier``
    # stays the single source of truth.
    labels = [label for label in template.meta_labels if not label.startswith("tier:")]
    labels = list(dict.fromkeys(labels))
    labels.append(f"tier:{template.meta_tier}")
    labels.append(f"HOTFIX-{target}")
    labels.append(f"hotfix-source:{source_change}")
    return list(dict.fromkeys(labels))


def render_child_labels(child: ChildSpec, target: str) -> list[str]:
    labels: list[str] = [
        "agent:auto",
        f"class:{child.agent_class}",
        f"tier:{child.tier}",
        f"HOTFIX-{target}",
        f"hotfix-child:{child.h_id}",
    ]
    for area in child.areas:
        labels.append(f"area:{area}")
    return list(dict.fromkeys(labels))


def render_child_description(
    child: ChildSpec, target: str, source_change: str, meta_key: str,
) -> str:
    ac_text = _fmt(child.ac, target, source_change, meta_key).rstrip()
    return (
        f"## Goal\n"
        f"Hotfix-conductor child `{child.h_id}` for **{target}** "
        f"(cherry-picked from Gerrit change {source_change}).\n"
        f"Parent META: {meta_key}.\n\n"
        f"## Acceptance Criteria\n"
        f"{ac_text}\n\n"
        f"## Spec references\n"
        f"- docs/operations/hotfix-runbook.md (D14, OP-886)\n"
        f"- docs/operations/release-conductor-runbook.md\n"
        f"- Template: config/hotfix_template.yaml\n\n"
        f"## Definition of Done\n"
        f"- [ ] All Acceptance Criteria above checked\n"
        f"- [ ] Gerrit Code-Review +2 per ADR-0003 (one human +2)\n"
        f"- [ ] Commit message references this ticket key\n"
        f"- [ ] Parent META {meta_key} updated via comment when this child closes\n"
    )


def render_meta_description(template: TemplateSpec, target: str, source_change: str) -> str:
    rows = []
    for child in template.children:
        rows.append(
            f"| {child.h_id} | "
            f"{_fmt(child.name, target, source_change)} | "
            f"tier:{child.tier} | "
            f"{', '.join('area:' + a for a in child.areas)} |"
        )
    child_table = "\n".join(rows)
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return (
        f"## HOTFIX-{target} — emergency production hotfix META\n\n"
        f"This ticket is the state machine for the **{target}** emergency "
        f"hotfix (D14, OP-886). It carries no implementation work itself; "
        f"the 4 child tickets `H1`..`H4` (linked below) drive the path "
        f"end-to-end, serialised by a `blockedBy` chain.\n\n"
        f"The JIRA graph IS the conductor. The runner JQL IS the executor. "
        f"There is no external orchestration service — pickup of each child "
        f"depends on its blocker reaching Published, exactly as for any "
        f"other runner ticket. (See `docs/operations/hotfix-runbook.md` and "
        f"`docs/operations/release-conductor-runbook.md`.)\n\n"
        f"## Provenance\n\n"
        f"- Source Gerrit change: {source_change} (must be MERGED)\n"
        f"- Target release branch: `release/{target}`\n"
        f"- Template file: `config/hotfix_template.yaml`\n"
        f"- Template version: `{template.version}`\n"
        f"- Instantiated by: `scripts/instantiate_hotfix_meta.py "
        f"--from-change {source_change} --target {target}`\n"
        f"- Instantiated at (UTC): `{created_at}`\n\n"
        f"## Child map\n\n"
        f"The script comments the resolved `H-id -> OP-key` map onto this "
        f"ticket after all 4 children land. The child list below is the "
        f"static plan; the comment is the live reference.\n\n"
        f"| H-id | Phase | Tier | Areas |\n"
        f"|---|---|---|---|\n"
        f"{child_table}\n\n"
        f"## State machine\n\n"
        f"Pickup gating is enforced by the existing runner pre-pickup gate "
        f"(`backend.agents.file_coordinator.has_unresolved_blockedby`). "
        f"Every child sits in `To Do` until its declared blocker reaches "
        f"Published. The chain is strictly sequential: H1 -> H2 -> H3 -> H4.\n\n"
        f"## Recovery\n\n"
        f"If instantiation fails partway, the script writes the created "
        f"ticket keys to `/tmp/hotfix-rollback-{target}.json` before "
        f"exiting non-zero; delete those keys (or run the G2 cleanup "
        f"helper) and re-run. Re-running without `--force` is an "
        f"idempotent no-op while a `HOTFIX-{target}` META exists; "
        f"`--force` re-creates only when the prior META is Archived.\n\n"
        f"## Acceptance criteria (META)\n\n"
        f"- [ ] All 4 children (`H1`..`H4`) created with correct labels and AC\n"
        f"- [ ] `blockedBy` chain wired in the L-OP-874 direction "
        f"(inward=blocker, outward=blocked)\n"
        f"- [ ] Each child carries a `Relates` link back to this META\n"
        f"- [ ] Operator-readable child map comment landed after META creation\n"
        f"- [ ] Closing H4 transitions this META to Published\n\n"
        f"## DoD\n\n"
        f"- [ ] All H1-H4 children Published\n"
        f"- [ ] Hotfix backported to `develop` (H4 green)\n"
        f"- [ ] This META transitioned to Published, closing the hotfix\n"
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


def _run_jql(
    client: jira_dispatch.DispatchClient,
    jql: str,
    *,
    fields: list[str],
    max_results: int = 20,
) -> list[dict]:
    """Wrap ``/search/jql`` so any failure becomes :class:`JQLQueryFailed`.

    The hotfix engine MUST fail closed on a JQL outage — a transient
    500 / network blip is never a license to create a second META.
    """
    try:
        resp = jira_dispatch._request(
            client,
            "POST",
            "/search/jql",
            {"jql": jql, "fields": fields, "maxResults": max_results},
        )
    except Exception as exc:  # noqa: BLE001 — convert ALL faults
        raise JQLQueryFailed(
            f"JIRA /search/jql failed; refusing to write to avoid duplicate META: {exc}"
        ) from exc
    return list(resp.get("issues", []) or [])


def find_existing_hotfix_matches(
    client: jira_dispatch.DispatchClient,
    target: str,
) -> list[tuple[str, str, list[str]]]:
    """Return ``(key, status_name, labels)`` for every ``HOTFIX-{target}`` META.

    Scopes to the project, the canonical ``HOTFIX-{target}`` tag-style
    label and ``meta:hotfix`` so we never collide on a fuzzy summary
    match. Empty list means no pre-existing META in any state.
    """
    jql = (
        f'project = "{client.project_key}" '
        f'AND labels = "HOTFIX-{target}" '
        f'AND labels = "meta:hotfix"'
    )
    matches: list[tuple[str, str, list[str]]] = []
    seen: set[str] = set()
    for issue in _run_jql(client, jql, fields=["summary", "status", "labels"]):
        key = issue.get("key")
        if not key or key in seen:
            continue
        seen.add(key)
        fields = issue.get("fields") or {}
        status = ((fields.get("status") or {}).get("name") or "")
        labels = [l for l in (fields.get("labels") or []) if isinstance(l, str)]
        matches.append((key, status, labels))
    return matches


def find_existing_hotfix_key(
    client: jira_dispatch.DispatchClient,
    target: str,
) -> tuple[str | None, str | None, list[str]]:
    """Return ``(key, status, labels)`` for an existing META, or ``(None, None, [])``.

    Raises :class:`StateAmbiguous` when more than one matching META
    exists — that is a P0 (the conductor produced duplicate METAs).
    """
    matches = find_existing_hotfix_matches(client, target)
    if not matches:
        return (None, None, [])
    if len(matches) > 1:
        keys = ", ".join(f"{key}(status={status!r})" for key, status, _ in matches)
        raise StateAmbiguous(
            f"multiple HOTFIX-{target} METAs found: [{keys}]. "
            "Reconcile (archive duplicates) before retrying."
        )
    return matches[0]


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


# ── Gerrit preconditions ─────────────────────────────────────────────


def gerrit_change_merged(change_number: str, agent_class: str) -> bool:
    """Return True iff the Gerrit change is in status ``MERGED``.

    A not-found change is treated as "not merged" — either way the
    caller refuses to instantiate a hotfix for it.
    """
    user, ssh_key = jira_dispatch._gerrit_auth_for_instance(agent_class)
    proc = subprocess.run(
        [
            "ssh", "-i", str(ssh_key), "-p", str(jira_dispatch.GERRIT_SSH_PORT),
            f"{user}@{jira_dispatch.GERRIT_SSH_HOST}",
            "gerrit", "query", "--format=JSON", f"change:{change_number}",
        ],
        capture_output=True, text=True, check=True,
    )
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if "project" not in payload:
            continue  # the trailing stats row
        return str(payload.get("status") or "").upper() == "MERGED"
    return False


def release_branch_exists(target: str, agent_class: str, *, repo: Path) -> bool:
    """Return True iff ``release/<target>`` exists locally or on Gerrit."""
    branch = f"release/{target}"
    local = subprocess.run(
        ["git", "rev-parse", "--verify", f"{branch}^{{commit}}"],
        cwd=repo, capture_output=True, text=True,
    )
    if local.returncode == 0:
        return True
    user, ssh_key = jira_dispatch._gerrit_auth_for_instance(agent_class)
    env = os.environ.copy()
    env["GIT_SSH_COMMAND"] = f"ssh -i {ssh_key}"
    remote = subprocess.run(
        ["git", "ls-remote", "--heads", jira_dispatch._gerrit_ssh_url(agent_class), branch],
        cwd=repo, capture_output=True, text=True, env=env,
    )
    return remote.returncode == 0 and bool(remote.stdout.strip())


# ── Planning + dry-run output ────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class InstantiationPlan:
    target: str
    source_change: str
    template_version: str
    meta_summary: str
    meta_labels: tuple[str, ...]
    child_summaries: tuple[tuple[str, str, tuple[str, ...]], ...]  # (h_id, summary, labels)
    blockedby_edges: tuple[tuple[str, str], ...]  # (blocked_h_id, blocker_h_id)


def build_plan(template: TemplateSpec, target: str, source_change: str) -> InstantiationPlan:
    edges: list[tuple[str, str]] = []
    child_rows: list[tuple[str, str, tuple[str, ...]]] = []
    for child in template.children:
        child_rows.append((
            child.h_id,
            render_child_summary(child, target, source_change),
            tuple(render_child_labels(child, target)),
        ))
        for blocker in child.blocked_by:
            edges.append((child.h_id, blocker))
    return InstantiationPlan(
        target=target,
        source_change=source_change,
        template_version=template.version,
        meta_summary=render_meta_summary(template, target),
        meta_labels=tuple(render_meta_labels(template, target, source_change)),
        child_summaries=tuple(child_rows),
        blockedby_edges=tuple(edges),
    )


def render_plan_text(plan: InstantiationPlan) -> str:
    lines = [
        "=== Hotfix META instantiation plan ===",
        f"target:           {plan.target}",
        f"source change:    {plan.source_change}",
        f"template version: {plan.template_version}",
        "",
        "META",
        f"  summary: {plan.meta_summary}",
        f"  labels:  {', '.join(plan.meta_labels)}",
        "",
        f"Children ({len(plan.child_summaries)}):",
    ]
    for h_id, summary, labels in plan.child_summaries:
        lines.append(f"  - {h_id:>3}  {summary}")
        lines.append(f"        labels: {', '.join(labels)}")
    lines.append("")
    lines.append("blockedBy edges (blocked -> blocker; wired via add_blocked_by):")
    for blocked, blocker in plan.blockedby_edges:
        lines.append(f"  {blocked} blockedBy {blocker}")
    lines.append("")
    lines.append("Relates edges (child -> META; wired via jira_create_issue_link link_type=Relates):")
    for h_id, _, _ in plan.child_summaries:
        lines.append(f"  {h_id} relates META")
    lines.append("")
    lines.append("(dry-run: no JIRA writes performed)")
    return "\n".join(lines)


# ── Rollback handling ────────────────────────────────────────────────


def rollback_path(target: str) -> Path:
    return ROLLBACK_DIR / f"hotfix-rollback-{target}.json"


def spill_rollback(target: str, created: dict[str, str], reason: str) -> Path:
    path = rollback_path(target)
    payload = {
        "target": target,
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
    keys_by_h_id: dict[str, str],
) -> int:
    """Wire all blockedBy edges. Returns count of newly created links."""
    created = 0
    for child in children:
        blocked_key = keys_by_h_id[child.h_id]
        for blocker_h_id in child.blocked_by:
            blocker_key = keys_by_h_id[blocker_h_id]
            if file_coordinator.add_blocked_by(
                client,
                blocked_key=blocked_key,
                blocker_key=blocker_key,
                reason=(
                    f"hotfix-conductor template: {child.h_id} blockedBy "
                    f"{blocker_h_id} per config/hotfix_template.yaml "
                    f"(template version recorded on META)"
                ),
            ):
                created += 1
    return created


def wire_relates_to_meta(
    client: jira_dispatch.DispatchClient,
    meta_key: str,
    child_keys: list[str],
) -> int:
    """Wire ``Relates`` links from each child to the META."""
    created = 0
    for child_key in child_keys:
        if file_coordinator.jira_link_exists(
            client, blocked=child_key, blocker=meta_key, link_type="Relates"
        ):
            continue
        file_coordinator.jira_create_issue_link(
            client, inward=child_key, outward=meta_key, link_type="Relates",
        )
        created += 1
    return created


def render_child_map_comment(
    template_version: str,
    meta_key: str,
    target: str,
    source_change: str,
    keys_by_h_id: dict[str, str],
) -> str:
    lines = [
        "[hotfix-template-engine] HOTFIX META instantiated.",
        f"target: {target}",
        f"source change: {source_change}",
        f"template version: {template_version}",
        f"meta: {meta_key}",
        "child map (H-id -> OP-key):",
    ]
    for h_id in sorted(keys_by_h_id, key=lambda s: int(s[1:])):
        lines.append(f"  {h_id}: {keys_by_h_id[h_id]}")
    lines.append(
        "Pickup gating follows the JIRA Blocks graph; "
        "no external conductor service runs against this META."
    )
    return "\n".join(lines)


# ── Apply (live JIRA writes) ─────────────────────────────────────────


def apply(
    client: jira_dispatch.DispatchClient,
    template: TemplateSpec,
    target: str,
    source_change: str,
    *,
    repo: Path,
    agent_class: str = DEFAULT_RUNNER_CLASS,
    force: bool = False,
) -> dict[str, str]:
    """Execute the live instantiation.

    Returns a ``{"META": OP-key, "H1": OP-key, ...}`` map.

    Raises (in check order):

    * :class:`HotfixSourceChangeNotMerged` — ``source_change`` is not MERGED
    * :class:`TargetReleaseBranchNotExists` — ``release/<target>`` missing
    * :class:`StateAmbiguous` — multiple non-Archived ``HOTFIX-<target>`` METAs
    * :class:`DuplicateHotfix` — a ``HOTFIX-<target>`` META already exists
      (idempotent skip; the CLI maps it to exit 0)
    * :class:`HotfixWiringFailed` — a link-wiring step blew up after
      creation (a rollback file is spilled first)
    """
    if not gerrit_change_merged(source_change, agent_class):
        raise HotfixSourceChangeNotMerged(
            f"Gerrit change {source_change} is not MERGED — refusing to "
            f"instantiate HOTFIX-{target}. You never cherry-pick an "
            f"unmerged change."
        )
    if not release_branch_exists(target, agent_class, repo=repo):
        raise TargetReleaseBranchNotExists(
            f"release/{target} does not exist locally or on the Gerrit "
            f"remote — cut the hotfix release branch before retrying."
        )

    existing_key, existing_status, existing_labels = find_existing_hotfix_key(client, target)
    if existing_key is not None:
        if force and existing_status in ARCHIVED_STATES:
            pass  # explicit recovery: re-create over an Archived META
        else:
            same_source = f"hotfix-source:{source_change}" in existing_labels
            raise DuplicateHotfix(
                f"HOTFIX-{target} META already exists: {existing_key} "
                f"(status={existing_status!r}, same_source_change={same_source}); "
                f"idempotent skip"
            )

    created: dict[str, str] = {}
    try:
        meta_key = create_issue(
            client,
            summary=render_meta_summary(template, target),
            description_markdown=render_meta_description(template, target, source_change),
            labels=render_meta_labels(template, target, source_change),
            issuetype=template.meta_issuetype,
        )
        created["META"] = meta_key

        keys_by_h_id: dict[str, str] = {}
        for child in template.children:
            key = create_issue(
                client,
                summary=render_child_summary(child, target, source_change),
                description_markdown=render_child_description(
                    child, target, source_change, meta_key,
                ),
                labels=render_child_labels(child, target),
                issuetype="Story",
            )
            keys_by_h_id[child.h_id] = key
            created[child.h_id] = key
    except Exception as exc:
        if created:
            path = spill_rollback(target, created, f"creation failed: {exc}")
            raise HotfixWiringFailed(
                f"creation aborted partway; rollback list at {path}: {exc}"
            ) from exc
        raise

    try:
        wire_blockedby_chain(client, list(template.children), keys_by_h_id)
        wire_relates_to_meta(client, meta_key, list(keys_by_h_id.values()))
    except Exception as exc:
        path = spill_rollback(target, created, f"link wiring failed: {exc}")
        raise HotfixWiringFailed(
            f"link wiring failed after ticket creation; rollback list at {path}: {exc}"
        ) from exc

    jira_dispatch.add_comment(
        client,
        meta_key,
        render_child_map_comment(template.version, meta_key, target, source_change, keys_by_h_id),
    )
    return created


# ── CLI ──────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--from-change",
        required=True,
        help="Merged Gerrit change number the hotfix is cherry-picked from",
    )
    parser.add_argument(
        "--target",
        required=True,
        help="Hotfix version, e.g. v1.2.4+1 (the +N is the hotfix counter)",
    )
    parser.add_argument(
        "--repo",
        default=".",
        help="Repo path for the release/<target> branch-exists check",
    )
    parser.add_argument(
        "--template",
        default=str(DEFAULT_TEMPLATE_PATH),
        help="Path to a hotfix_template.yaml override (default: config/hotfix_template.yaml)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the instantiation plan; perform NO JIRA writes or Gerrit queries",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Allow re-create even if a META exists (only honoured when status=Archived)",
    )
    parser.add_argument(
        "--agent-class",
        default=DEFAULT_RUNNER_CLASS,
        help="Runner class to authenticate as for Gerrit + JIRA writes",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    validate_target(args.target)
    source_change = validate_source_change(args.from_change)

    template_path = Path(args.template)
    try:
        template = load_template(template_path)
    except HotfixTemplateInvalid as exc:
        print(f"HotfixTemplateInvalid: {exc}", file=sys.stderr)
        return EXIT_TEMPLATE_INVALID

    plan = build_plan(template, args.target, source_change)

    if args.dry_run:
        print(render_plan_text(plan))
        return EXIT_OK

    client = jira_dispatch.make_client(args.agent_class)

    try:
        created = apply(
            client,
            template,
            args.target,
            source_change,
            repo=Path(args.repo).resolve(),
            agent_class=args.agent_class,
            force=args.force,
        )
    except DuplicateHotfix as exc:
        print(f"[hotfix-template-engine] {exc}")
        return EXIT_OK
    except HotfixSourceChangeNotMerged as exc:
        print(f"HotfixSourceChangeNotMerged: {exc}", file=sys.stderr)
        print(
            "ALERT: refusing to instantiate a hotfix META for an unmerged source change.",
            file=sys.stderr,
        )
        return EXIT_SOURCE_NOT_MERGED
    except TargetReleaseBranchNotExists as exc:
        print(f"TargetReleaseBranchNotExists: {exc}", file=sys.stderr)
        print(
            "ALERT: target release branch is missing; cut it before retrying.",
            file=sys.stderr,
        )
        return EXIT_TARGET_BRANCH_MISSING
    except JQLQueryFailed as exc:
        print(f"JQLQueryFailed: {exc}", file=sys.stderr)
        return EXIT_JQL_QUERY_FAILED
    except StateAmbiguous as exc:
        print(f"StateAmbiguous: {exc}", file=sys.stderr)
        print(
            "ALERT: P0 — multiple METAs share this hotfix target label. "
            "Reconcile manually before retrying.",
            file=sys.stderr,
        )
        return EXIT_STATE_AMBIGUOUS
    except HotfixWiringFailed as exc:
        print(f"HotfixWiringFailed: {exc}", file=sys.stderr)
        return EXIT_WIRING_FAILED

    print(f"Created HOTFIX-{args.target} META + {EXPECTED_CHILD_COUNT} children:")
    for h_id, key in created.items():
        print(f"  {h_id}: {key}")
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
