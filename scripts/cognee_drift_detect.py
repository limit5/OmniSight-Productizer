#!/usr/bin/env python3
"""OP-907 F9 — Cognee ontology drift detection.

Daily read-only audit that compares the live Cognee KG against
ground-truth artefacts (curated entity-class YAML, ``git ls-files``,
JIRA), flags three drift types and renders a report under
``docs/audit/cognee-drift-YYYY-MM-DD.md``. Drift items that persist
unresolved for more than seven days escalate via the OP-721 operator
notification bridge.

State machine (per OP-907 description)::

    daily cron → KG snapshot → reality snapshot
      → diff:
        stale              = (in_KG - in_reality)
        missing            = (in_reality - in_KG)
        schema_violations  = (KG_classes - approved_classes)
      → render report → write to docs/audit/
      → if unresolved drift > 7d: escalate

Error catalog
-------------

* ``DriftDetectKGUnreachable`` — Neo4j refused the snapshot query;
  the run is skipped (cron retries tomorrow) and the failure is logged.
* ``DriftAuditWriteFailed`` — could not write the audit Markdown file;
  the report falls through to stdout so the operator still sees it.
* ``EscalationBridgeDown`` — the OP-721 ``operator_notifier.notify``
  call raised; the caller falls back to a logged ``WARN`` and continues
  so a wedged notifier never silences the detector.

Inputs and injection
--------------------
The runtime accepts three "snapshot collectors", all injectable so
tests can drive the full pipeline without a live Neo4j / git / JIRA:

* ``kg_collector(config) -> KGSnapshot``      — default queries Neo4j
* ``reality_collector(...) -> RealitySnapshot`` — default reads YAML +
  ``git ls-files`` + JIRA via :mod:`backend.agents.jira_dispatch`
* ``notifier_notify(severity, code, **fields)`` — default is the
  module-level ``operator_notifier.notify``

The per-entity allowlist (``config/cognee_drift_ignore.yaml``)
suppresses false-positives without code changes — operator-edited,
read every run.

CLI
---
Default invocation (matches the systemd timer)::

    python -m scripts.cognee_drift_detect

Common overrides::

    --repo-root /opt/omnisight       # default: cwd
    --out-dir docs/audit             # default: <repo>/docs/audit
    --no-jira                        # skip JIRA collection (offline)
    --no-escalation                  # never invoke the notifier
    --state-file var/cognee_drift_state.json
    --jira-window-days 30            # JIRA "recently merged" window

Exit codes
----------
* 0 — drift detection ran, report written (drift may or may not exist;
  exit code does not signal drift presence — read the report)
* 1 — unexpected fatal error (stack-trace logged)
* 2 — KG was unreachable; skipped per error catalog
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

logger = logging.getLogger("cognee_drift_detect")

ESCALATION_AGE_DAYS = 7
DEFAULT_JIRA_WINDOW_DAYS = 30

DRIFT_CODE_STALE = "cognee_drift_stale_entity"
DRIFT_CODE_MISSING = "cognee_drift_missing_entity"
DRIFT_CODE_SCHEMA = "cognee_drift_schema_mismatch"
DRIFT_CODE_ESCALATION = "cognee_drift_unresolved_7d"


# ── Error catalog ────────────────────────────────────────────────


class DriftDetectKGUnreachable(RuntimeError):
    """Cognee/Neo4j refused the snapshot query — skip today's run."""


class DriftAuditWriteFailed(RuntimeError):
    """Audit report could not be written; caller degrades to stdout."""


class EscalationBridgeDown(RuntimeError):
    """OP-721 operator_notifier failed; caller logs + continues."""


# ── Value objects ────────────────────────────────────────────────


@dataclass(frozen=True)
class KGSnapshot:
    """What the live Cognee KG currently believes exists.

    ``classes`` is the set of every entity-class label appearing in the
    graph; ``entities`` maps ``class_name -> set[entity_key]``. The keys
    use the same dedup-friendly identifiers the F5 ingestion pipeline
    writes (PythonModule = dotted path, JiraTicket = issue key).
    """

    classes: frozenset[str]
    entities: Mapping[str, frozenset[str]]

    @classmethod
    def empty(cls) -> "KGSnapshot":
        return cls(classes=frozenset(), entities={})


@dataclass(frozen=True)
class RealitySnapshot:
    """Ground truth — what SHOULD be in the KG right now.

    * ``approved_classes`` — operator-curated YAML, the authoritative
      schema.
    * ``python_modules`` — dotted module path for every git-tracked
      ``*.py`` under the configured roots (mirrors the F5 extractor).
    * ``jira_keys`` — open + recently-merged issue keys.
    """

    approved_classes: frozenset[str]
    python_modules: frozenset[str]
    jira_keys: frozenset[str]


@dataclass(frozen=True)
class DriftItem:
    """One observed drift event keyed by ``(kind, class_name, key)``.

    ``kind`` is one of the three drift types (stale / missing / schema).
    ``key`` is the entity key for stale/missing; for schema mismatches
    the key is the offending class name itself.
    """

    kind: str
    class_name: str
    key: str

    def state_key(self) -> str:
        return f"{self.kind}::{self.class_name}::{self.key}"


@dataclass
class DriftReport:
    stale: list[DriftItem] = field(default_factory=list)
    missing: list[DriftItem] = field(default_factory=list)
    schema_violations: list[DriftItem] = field(default_factory=list)
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def total(self) -> int:
        return len(self.stale) + len(self.missing) + len(self.schema_violations)

    def all_items(self) -> list[DriftItem]:
        return [*self.stale, *self.missing, *self.schema_violations]


# ── Reality collectors ───────────────────────────────────────────


def load_approved_classes(yaml_path: Path) -> frozenset[str]:
    """Read ``config/cognee_entity_classes.yaml`` and return the set of
    approved class names. Missing/empty file → empty set (which causes
    every KG class to register as a schema mismatch, which is the
    correct shouty failure mode)."""
    if not yaml_path.exists():
        return frozenset()
    import yaml  # local import — keeps --help cheap
    data = yaml.safe_load(yaml_path.read_text()) or {}
    return frozenset(
        str(entry.get("name", "")).strip()
        for entry in (data.get("classes") or [])
        if entry.get("name")
    )


def load_drift_ignore(yaml_path: Path) -> set[str]:
    """Load the per-entity allowlist. Returns a set of state-key
    strings (``"<kind>::<class>::<key>"``). Empty/missing file → empty
    set (no suppression)."""
    if not yaml_path.exists():
        return set()
    import yaml
    data = yaml.safe_load(yaml_path.read_text()) or {}
    out: set[str] = set()
    for entry in (data.get("ignore") or []):
        kind = str(entry.get("kind", "")).strip()
        class_name = str(entry.get("class", "")).strip()
        key = str(entry.get("key", "")).strip()
        if not (kind and class_name and key):
            continue
        out.add(f"{kind}::{class_name}::{key}")
    return out


def collect_python_modules(
    repo_root: Path,
    *,
    roots: tuple[str, ...] = ("backend", "scripts"),
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> frozenset[str]:
    """Enumerate dotted module names for every git-tracked .py under
    ``roots``. Mirrors the dotted-path key F5 writes for
    ``PythonModule`` entities so set-diff is meaningful."""
    invoker = runner or subprocess.run
    modules: set[str] = set()
    for root in roots:
        try:
            proc = invoker(
                ["git", "-C", str(repo_root), "ls-files", f"{root}/*.py"],
                text=True,
                capture_output=True,
                check=True,
                timeout=30,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            continue
        for line in proc.stdout.splitlines():
            rel = line.strip()
            if not rel or not rel.endswith(".py"):
                continue
            modules.add(rel[:-3].replace("/", "."))
    return frozenset(modules)


JiraKeyFetcher = Callable[[int], Iterable[str]]


def default_jira_fetcher(agent_class: str = "claude") -> JiraKeyFetcher:
    """Build a JIRA fetcher that returns open + recently-merged keys.

    Mirrors :func:`scripts.cognee_initial_ingest.default_jira_reader`
    but yields keys only — the drift detector does not need the full
    issue payload. A live JIRA outage raises ``RuntimeError``, which
    the caller maps to a "skip JIRA dimension" branch so KG / file
    drift still surfaces.
    """
    from backend.agents import jira_dispatch  # local import — cold-start cost

    def _fetcher(window_days: int) -> Iterable[str]:
        client = jira_dispatch.make_client(agent_class)
        jql = (
            f"project = {client.project_key} AND "
            f"(status != Done OR updated >= -{window_days}d) "
            "ORDER BY updated DESC"
        )
        request = jira_dispatch._request  # noqa: SLF001 — reuses the auth client
        page = request(
            client,
            "POST",
            "/search/jql",
            {"jql": jql, "fields": ["status"], "maxResults": 500},
        )
        for issue in page.get("issues", []) or []:
            key = issue.get("key")
            if key:
                yield str(key)

    return _fetcher


def collect_reality_snapshot(
    repo_root: Path,
    *,
    classes_yaml: Path,
    jira_fetcher: JiraKeyFetcher | None,
    jira_window_days: int = DEFAULT_JIRA_WINDOW_DAYS,
    code_roots: tuple[str, ...] = ("backend", "scripts"),
) -> RealitySnapshot:
    """Build the full reality snapshot. JIRA failures degrade to an
    empty key set so the rest of the pipeline keeps going; the operator
    sees a logged ``jira_fetch_failed`` line on the way past."""
    approved = load_approved_classes(classes_yaml)
    modules = collect_python_modules(repo_root, roots=code_roots)
    jira_keys: frozenset[str] = frozenset()
    if jira_fetcher is not None:
        try:
            jira_keys = frozenset(jira_fetcher(jira_window_days))
        except Exception as exc:  # noqa: BLE001 — outage degrades, never crashes
            logger.warning("jira_fetch_failed err=%s", exc)
            jira_keys = frozenset()
    return RealitySnapshot(
        approved_classes=approved,
        python_modules=modules,
        jira_keys=jira_keys,
    )


# ── KG collector (Neo4j) ─────────────────────────────────────────


# Classes whose entity instances are checked against reality. Other
# ontology classes (PythonClass, PythonFunction, JiraComponent, ...)
# live in the KG but have no enumerable reality counterpart that the
# F9 spec requires us to verify, so we intentionally do not raise
# stale/missing alerts on them. Schema-mismatch still covers the
# class-set drift universally.
REALITY_CHECKED_CLASSES: tuple[str, ...] = ("PythonModule", "JiraTicket")


KGCollector = Callable[[], KGSnapshot]


def neo4j_kg_collector() -> KGSnapshot:
    """Default KG collector — talks to Neo4j via the same config
    :func:`backend.agents.cognee_integration.healthcheck` uses.

    Returns the union of every entity-class label and the full
    ``(class_name, key)`` set so the diff can compute schema /
    stale / missing in one pass. Cognee writes nodes with the
    ontology class as a label and ``name`` / ``key`` / ``identifier``
    as the natural-key property — we accept any of those so we are
    robust against the upstream SDK renaming the field.
    """
    from backend.agents.cognee_integration import (
        CogneeConfig,
        CogneeNeo4jUnavailable,
        Neo4jPasswordDefault,
    )

    import importlib
    try:
        neo4j = importlib.import_module("neo4j")
    except ImportError as exc:
        raise DriftDetectKGUnreachable(f"neo4j driver not installed: {exc}") from exc

    config = CogneeConfig.from_env()
    try:
        config.validate_password()
    except Neo4jPasswordDefault as exc:
        raise DriftDetectKGUnreachable(str(exc)) from exc

    try:
        driver = neo4j.GraphDatabase.driver(
            config.neo4j_uri,
            auth=(config.neo4j_user, config.neo4j_password),
        )
    except Exception as exc:  # noqa: BLE001 — driver raises many connection classes
        raise DriftDetectKGUnreachable(f"neo4j driver init failed: {exc}") from exc

    try:
        try:
            with driver.session() as session:
                rows = list(session.run(
                    "MATCH (n) "
                    "RETURN labels(n) AS labels, "
                    "coalesce(n.key, n.name, n.identifier, n.id) AS key"
                ))
        finally:
            driver.close()
    except CogneeNeo4jUnavailable as exc:
        raise DriftDetectKGUnreachable(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — connection/auth/timeout all surface here
        raise DriftDetectKGUnreachable(f"neo4j snapshot query failed: {exc}") from exc

    classes: set[str] = set()
    entities: dict[str, set[str]] = {}
    for row in rows:
        labels = row.get("labels") if isinstance(row, Mapping) else row["labels"]
        key = row.get("key") if isinstance(row, Mapping) else row["key"]
        if not labels:
            continue
        # Cognee stores the ontology class as the FIRST label; the
        # remaining labels are framework-internal (Document, Chunk).
        # We accept all non-internal labels conservatively.
        for label in labels:
            if label in _COGNEE_INTERNAL_LABELS:
                continue
            classes.add(label)
            if key is None or key == "":
                continue
            entities.setdefault(label, set()).add(str(key))
    return KGSnapshot(
        classes=frozenset(classes),
        entities={k: frozenset(v) for k, v in entities.items()},
    )


_COGNEE_INTERNAL_LABELS: frozenset[str] = frozenset({
    "Document", "DocumentChunk", "Chunk", "Entity", "Node",
})


# ── Diff engine ──────────────────────────────────────────────────


def diff_snapshots(
    kg: KGSnapshot,
    reality: RealitySnapshot,
    ignore: set[str],
) -> DriftReport:
    """Compute the three drift sets, honouring the allowlist.

    The ignore set is consulted on the state-key form
    ``<kind>::<class>::<key>``; any drift item whose state-key is in
    the set is dropped silently."""
    report = DriftReport()

    # Reality keys per class
    reality_keys_by_class: dict[str, frozenset[str]] = {
        "PythonModule": reality.python_modules,
        "JiraTicket": reality.jira_keys,
    }

    # Schema mismatch — every KG class not in the approved YAML.
    for cls in sorted(kg.classes):
        if cls in reality.approved_classes:
            continue
        item = DriftItem(kind="schema", class_name=cls, key=cls)
        if item.state_key() in ignore:
            continue
        report.schema_violations.append(item)

    # Stale + missing for the reality-checked classes.
    for cls in REALITY_CHECKED_CLASSES:
        kg_keys = kg.entities.get(cls, frozenset())
        truth_keys = reality_keys_by_class.get(cls, frozenset())

        # JIRA defaults to an empty set when the fetcher is disabled or
        # the API is down — comparing against an empty truth would mark
        # every KG ticket as stale. Guard that case so a JIRA outage
        # cannot manufacture a stale-ticket alert storm.
        if cls == "JiraTicket" and not truth_keys:
            continue

        for stale_key in sorted(kg_keys - truth_keys):
            item = DriftItem(kind="stale", class_name=cls, key=stale_key)
            if item.state_key() in ignore:
                continue
            report.stale.append(item)
        for missing_key in sorted(truth_keys - kg_keys):
            item = DriftItem(kind="missing", class_name=cls, key=missing_key)
            if item.state_key() in ignore:
                continue
            report.missing.append(item)

    return report


# ── Report rendering ─────────────────────────────────────────────


_SUGGESTED_ACTION: dict[str, str] = {
    "stale": "Re-ingest the source corpus — KG holds an entity whose backing artefact is gone.",
    "missing": "Investigate ECL pipeline — backing artefact exists but never landed in KG.",
    "schema": "Update `config/cognee_entity_classes.yaml` or rebuild KG with the approved schema.",
}


def render_report_markdown(report: DriftReport, *, generated_for: str) -> str:
    """Render the audit report as Markdown. Stable shape — operators
    grep for the count line; tests assert the table headers."""
    lines: list[str] = []
    lines.append(f"# Cognee ontology drift — {generated_for}")
    lines.append("")
    lines.append(f"Generated: {report.generated_at}")
    lines.append("")
    lines.append("## Counts")
    lines.append("")
    lines.append(f"- Stale entities: {len(report.stale)}")
    lines.append(f"- Missing entities: {len(report.missing)}")
    lines.append(f"- Schema mismatches: {len(report.schema_violations)}")
    lines.append(f"- Total drift items: {report.total}")
    lines.append("")

    def _table(title: str, kind: str, items: list[DriftItem]) -> None:
        lines.append(f"## {title}")
        lines.append("")
        if not items:
            lines.append("_None._")
            lines.append("")
            return
        lines.append(f"Suggested action: {_SUGGESTED_ACTION[kind]}")
        lines.append("")
        lines.append("| Class | Key |")
        lines.append("|---|---|")
        for item in items:
            # Markdown table cells: escape pipes only — the keys are
            # bounded identifiers so we do not need full HTML escaping.
            cls = item.class_name.replace("|", "\\|")
            key = item.key.replace("|", "\\|")
            lines.append(f"| {cls} | {key} |")
        lines.append("")

    _table("Stale entities (in KG, absent from reality)", "stale", report.stale)
    _table("Missing entities (in reality, absent from KG)", "missing", report.missing)
    _table("Schema mismatches (class in KG, not in approved YAML)", "schema", report.schema_violations)

    lines.append("## Allowlist")
    lines.append("")
    lines.append(
        "False-positives can be suppressed in `config/cognee_drift_ignore.yaml`. "
        "Add one entry per false-positive with `kind`, `class`, `key`, and a "
        "rationale comment. The detector reads the file on every run."
    )
    lines.append("")
    return "\n".join(lines)


def write_report(report_md: str, out_dir: Path, day: str) -> Path:
    """Persist the report to ``<out_dir>/cognee-drift-<day>.md``.

    Raises :class:`DriftAuditWriteFailed` on any I/O error so callers
    can fall back to stdout per the error catalog."""
    target = out_dir / f"cognee-drift-{day}.md"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(report_md)
        tmp.replace(target)
    except OSError as exc:
        raise DriftAuditWriteFailed(f"write {target} failed: {exc}") from exc
    return target


# ── Escalation state ─────────────────────────────────────────────


@dataclass
class DriftState:
    """Per state-key first-seen timestamp store.

    Persisted as JSON between runs so the detector can compute
    ``unresolved age`` without re-querying historical reports.
    """

    first_seen: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps({"first_seen": dict(sorted(self.first_seen.items()))}, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "DriftState":
        try:
            data = json.loads(text) or {}
        except json.JSONDecodeError:
            logger.warning("drift_state_corrupt: starting fresh")
            return cls()
        return cls(first_seen=dict(data.get("first_seen") or {}))


def load_drift_state(path: Path) -> DriftState:
    if not path.exists():
        return DriftState()
    return DriftState.from_json(path.read_text())


def save_drift_state(state: DriftState, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(state.to_json())
    tmp.replace(path)


def update_state_with_report(
    state: DriftState,
    report: DriftReport,
    *,
    now: datetime,
) -> tuple[DriftState, list[tuple[DriftItem, datetime]]]:
    """Reconcile state with the current report.

    * New drift items get a fresh ``first_seen`` set to ``now``.
    * Resolved items (in state but not in report) get removed.
    * Returns the updated state plus a list of items whose age >=
      :data:`ESCALATION_AGE_DAYS`, paired with their first-seen
      timestamp so the escalation message has receipt data.
    """
    seen_now = {item.state_key(): item for item in report.all_items()}
    new_first_seen: dict[str, str] = {}
    for sk in seen_now:
        new_first_seen[sk] = state.first_seen.get(sk, now.isoformat())
    updated = DriftState(first_seen=new_first_seen)

    threshold = now.timestamp() - ESCALATION_AGE_DAYS * 86400
    escalations: list[tuple[DriftItem, datetime]] = []
    for sk, item in seen_now.items():
        first_seen_iso = updated.first_seen[sk]
        try:
            first_seen_dt = datetime.fromisoformat(first_seen_iso)
        except ValueError:
            continue
        if first_seen_dt.timestamp() <= threshold:
            escalations.append((item, first_seen_dt))
    return updated, escalations


# ── Escalation ───────────────────────────────────────────────────


NotifierCallable = Callable[..., Any]


def escalate(
    items: list[tuple[DriftItem, datetime]],
    *,
    notifier: NotifierCallable | None,
    report_path: Path | None,
) -> bool:
    """Page the operator about unresolved drift.

    Returns True iff a notification was successfully dispatched.
    On notifier failure raises :class:`EscalationBridgeDown` so the
    caller can degrade to the email-fallback rung of the error
    catalog. The caller is responsible for swallowing the exception
    after switching transports.
    """
    if not items:
        return False
    if notifier is None:
        logger.info("escalation_skipped: notifier disabled (count=%d)", len(items))
        return False
    sample_item, sample_first_seen = items[0]
    age_days = (datetime.now(timezone.utc) - sample_first_seen).total_seconds() / 86400.0
    context = {
        "drift_count": len(items),
        "sample_kind": sample_item.kind,
        "sample_class": sample_item.class_name,
        "sample_key": sample_item.key,
        "sample_age_days": round(age_days, 2),
        "report_path": str(report_path) if report_path else "stdout",
    }
    try:
        notifier(
            "DEGRADED",
            code=DRIFT_CODE_ESCALATION,
            message=(
                f"{len(items)} cognee drift item(s) unresolved >7d "
                f"(sample {sample_item.kind} {sample_item.class_name}/{sample_item.key})"
            ),
            **context,
        )
    except Exception as exc:  # noqa: BLE001 — surface as typed escalation error
        raise EscalationBridgeDown(f"notifier dispatch failed: {exc}") from exc
    return True


def _email_fallback(
    items: list[tuple[DriftItem, datetime]],
    *,
    report_path: Path | None,
) -> None:
    """Best-effort email fallback when the OP-721 bridge is down.

    Uses the same SMTP env vars as :mod:`backend.agents.operator_notifier`
    so the operator has one place to configure the relay. Silently
    no-ops when no relay is configured — caller already logged the
    bridge failure, so this is purely additive recovery.
    """
    host = os.environ.get("OMNISIGHT_NOTIFIER_SMTP_HOST")
    sender = os.environ.get("OMNISIGHT_NOTIFIER_SMTP_FROM")
    recipients_raw = os.environ.get("OMNISIGHT_NOTIFIER_EMAIL_RECIPIENTS", "")
    recipients = [r.strip() for r in recipients_raw.split(",") if r.strip()]
    if not (host and sender and recipients):
        logger.info("email_fallback_skipped: SMTP env not configured")
        return
    try:
        import smtplib
        import ssl
        from email.message import EmailMessage
    except ImportError:  # pragma: no cover — stdlib
        return
    msg = EmailMessage()
    msg["Subject"] = f"[DEGRADED] {DRIFT_CODE_ESCALATION}"
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    body_lines = [
        f"{len(items)} cognee drift item(s) unresolved >{ESCALATION_AGE_DAYS}d.",
        f"Report: {report_path or 'stdout'}",
        "",
        "Sample:",
    ]
    for item, first_seen in items[:10]:
        age = (datetime.now(timezone.utc) - first_seen).days
        body_lines.append(f"  {item.kind} {item.class_name}/{item.key} (first_seen={first_seen.isoformat()}, age={age}d)")
    msg.set_content("\n".join(body_lines))
    host_part, _, port_part = host.partition(":")
    port = int(port_part) if port_part else 587
    try:
        with smtplib.SMTP(host_part, port, timeout=15) as s:
            s.ehlo()
            if port != 25:
                s.starttls(context=ssl.create_default_context())
                s.ehlo()
            user = os.environ.get("OMNISIGHT_NOTIFIER_SMTP_USER")
            password = os.environ.get("OMNISIGHT_NOTIFIER_SMTP_PASSWORD")
            if user and password:
                s.login(user, password)
            s.send_message(msg)
        logger.info("email_fallback_sent recipients=%d", len(recipients))
    except Exception as exc:  # noqa: BLE001 — fallback may itself fail
        logger.warning("email_fallback_failed err=%s", exc)


# ── Top-level runner ─────────────────────────────────────────────


@dataclass
class DriftDetectConfig:
    repo_root: Path
    out_dir: Path
    classes_yaml: Path
    ignore_yaml: Path
    state_file: Path
    jira_window_days: int = DEFAULT_JIRA_WINDOW_DAYS
    code_roots: tuple[str, ...] = ("backend", "scripts")
    use_jira: bool = True
    use_escalation: bool = True
    jira_agent: str = "claude"


def run_drift_detect(
    config: DriftDetectConfig,
    *,
    kg_collector: KGCollector | None = None,
    jira_fetcher: JiraKeyFetcher | None = None,
    notifier: NotifierCallable | None = None,
    now: datetime | None = None,
) -> tuple[DriftReport, Path | None]:
    """Run the full detect-diff-report-escalate pipeline.

    Returns ``(report, report_path)`` — ``report_path`` is ``None`` when
    the audit write failed and the report fell through to stdout.

    Test injection: pass ``kg_collector``, ``jira_fetcher`` and
    ``notifier`` to short-circuit Neo4j / JIRA / notifier I/O.
    """
    now = now or datetime.now(timezone.utc)
    day = now.strftime("%Y-%m-%d")

    # KG snapshot — bubble the typed error up to the CLI so cron retries
    # tomorrow rather than mis-reporting an empty KG as "everything gone".
    collector = kg_collector or neo4j_kg_collector
    kg_snapshot = collector()  # raises DriftDetectKGUnreachable on failure

    # Reality snapshot
    fetcher = jira_fetcher
    if fetcher is None and config.use_jira:
        try:
            fetcher = default_jira_fetcher(config.jira_agent)
        except Exception as exc:  # noqa: BLE001 — JIRA optional; degrade
            logger.warning("jira_fetcher_init_failed err=%s", exc)
            fetcher = None
    reality_snapshot = collect_reality_snapshot(
        config.repo_root,
        classes_yaml=config.classes_yaml,
        jira_fetcher=fetcher,
        jira_window_days=config.jira_window_days,
        code_roots=config.code_roots,
    )

    # Allowlist + diff
    ignore = load_drift_ignore(config.ignore_yaml)
    report = diff_snapshots(kg_snapshot, reality_snapshot, ignore)

    # Report write — write-failure degrades to stdout per error catalog.
    report_md = render_report_markdown(report, generated_for=day)
    report_path: Path | None = None
    try:
        report_path = write_report(report_md, config.out_dir, day)
    except DriftAuditWriteFailed as exc:
        logger.warning("drift_audit_write_failed err=%s — falling back to stdout", exc)
        sys.stdout.write(report_md)
        sys.stdout.flush()

    # State + escalation
    state = load_drift_state(config.state_file)
    updated_state, escalations = update_state_with_report(state, report, now=now)
    save_drift_state(updated_state, config.state_file)

    if escalations and config.use_escalation:
        try:
            escalate(escalations, notifier=notifier or _default_notifier_notify(), report_path=report_path)
        except EscalationBridgeDown as exc:
            logger.warning("escalation_bridge_down err=%s — falling back to email", exc)
            _email_fallback(escalations, report_path=report_path)

    return report, report_path


def _default_notifier_notify() -> NotifierCallable:
    """Lazy default — import the operator_notifier shim only when
    escalation is actually needed. Keeps offline / dry-run paths from
    pulling in the full notifier stack."""
    from backend.agents.operator_notifier import notify
    return notify  # type: ignore[return-value]


# ── CLI ──────────────────────────────────────────────────────────


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cognee_drift_detect",
        description="OP-907 — daily Cognee KG ontology drift detection.",
    )
    parser.add_argument(
        "--repo-root", type=Path, default=Path.cwd(),
        help="Repository root (default: cwd).",
    )
    parser.add_argument(
        "--out-dir", type=Path, default=None,
        help="Audit output dir (default: <repo>/docs/audit).",
    )
    parser.add_argument(
        "--classes-yaml", type=Path, default=None,
        help="Approved entity-classes YAML (default: <repo>/config/cognee_entity_classes.yaml).",
    )
    parser.add_argument(
        "--ignore-yaml", type=Path, default=None,
        help="Drift ignore YAML (default: <repo>/config/cognee_drift_ignore.yaml).",
    )
    parser.add_argument(
        "--state-file", type=Path, default=None,
        help="Persisted drift-state JSON (default: <repo>/var/cognee_drift_state.json).",
    )
    parser.add_argument(
        "--jira-window-days", type=int, default=DEFAULT_JIRA_WINDOW_DAYS,
        help="JIRA recent-merged window in days (default: 30).",
    )
    parser.add_argument(
        "--jira-agent", default="claude",
        help="agent_class for jira_dispatch.make_client (default: claude).",
    )
    parser.add_argument(
        "--no-jira", action="store_true",
        help="Skip the JIRA dimension entirely (offline mode).",
    )
    parser.add_argument(
        "--no-escalation", action="store_true",
        help="Never invoke the operator notifier even if drift is >7d old.",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    repo_root = args.repo_root.resolve()
    cfg = DriftDetectConfig(
        repo_root=repo_root,
        out_dir=(args.out_dir or repo_root / "docs" / "audit").resolve(),
        classes_yaml=(args.classes_yaml or repo_root / "config" / "cognee_entity_classes.yaml").resolve(),
        ignore_yaml=(args.ignore_yaml or repo_root / "config" / "cognee_drift_ignore.yaml").resolve(),
        state_file=(args.state_file or repo_root / "var" / "cognee_drift_state.json").resolve(),
        jira_window_days=args.jira_window_days,
        use_jira=not args.no_jira,
        use_escalation=not args.no_escalation,
        jira_agent=args.jira_agent,
    )

    try:
        report, report_path = run_drift_detect(cfg)
    except DriftDetectKGUnreachable as exc:
        logger.error("drift_detect_kg_unreachable err=%s — skip today, cron retries", exc)
        return 2
    except Exception:  # noqa: BLE001 — surface stack trace for the operator
        logger.exception("drift_detect_fatal")
        return 1

    logger.info(
        "drift_detect_complete stale=%d missing=%d schema=%d total=%d report=%s",
        len(report.stale), len(report.missing), len(report.schema_violations),
        report.total, report_path or "stdout",
    )
    return 0


if __name__ == "__main__":  # pragma: no cover — CLI entry
    sys.exit(main())
