"""Tests for ``scripts/refine_seed_tickets.py`` (OP-781).

Pins:
- generate_proposal returns a Proposal with ≥1 AC + Files + Prerequisites
  on a synthetic 5-ticket batch (acceptance criterion #4).
- save_proposal / load_proposal round-trip with full token-usage payload.
- classify_confidence picks the right tier for the canonical examples
  in the OP-781 spec (high / medium / low).
- render_refined_description produces a Story-shaped markdown body that
  contains every required ## section.
- cmd_propose / cmd_apply call the right JIRA helpers (label flip,
  issuetype Task → Story, audit-log append) without hitting the network.
- audit-log append writes one JSONL line per applied refinement.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import refine_seed_tickets as rst  # noqa: E402


# ── Fakes ─────────────────────────────────────────────────────────


def _fake_issue(
    key: str,
    summary: str,
    description: str = "",
    labels: tuple[str, ...] = (rst.NEEDS_REFINEMENT_LABEL, "tier:M", "class:subscription-claude"),
    issuetype: str = "Task",
    parent_key: str | None = None,
) -> dict:
    fields: dict = {
        "summary": summary,
        "labels": list(labels),
        "issuetype": {"name": issuetype},
        "status": {"name": "To Do"},
    }
    if description:
        fields["description"] = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "codeBlock",
                    "attrs": {"language": "markdown"},
                    "content": [{"type": "text", "text": description}],
                }
            ],
        }
    if parent_key:
        fields["parent"] = {"key": parent_key, "fields": {"summary": "parent wave"}}
    return {"key": key, "fields": fields}


def _stub_llm_proposer(payload: dict, in_tokens: int = 100, out_tokens: int = 200):
    """Return a callable matching :data:`rst.LlmProposer` with a fixed payload."""
    text = json.dumps(payload)

    def _call(system: str, user: str) -> tuple[str, rst.TokenUsage]:
        return text, rst.TokenUsage(input_tokens=in_tokens, output_tokens=out_tokens)

    return _call


_GOOD_PAYLOAD = {
    "acceptance_criteria": [
        "Function `quota_tracker.add()` rejects negative values (test: test_quota_negative)",
        "Migration adds `quota_tracker` table with usage_id PK + per-tenant index",
        "REST endpoint /api/quota returns 429 when over cap",
    ],
    "files": [
        "backend/agents/quota_tracker.py",
        "backend/alembic/versions/0210_quota_tracker.py",
        "backend/tests/test_quota_tracker.py",
    ],
    "prerequisites": {
        "blocks_on": [],
        "soft_prereqs": [],
        "mutex_with": ["mutex:alembic-chain-head"],
        "schema_locks": [],
        "live_state_requires": [],
        "external_blockers": [],
    },
    "notes": "",
}


# ── classify_confidence ────────────────────────────────────────────


def test_classify_confidence_high_for_concrete_keyword() -> None:
    tier, _ = rst.classify_confidence(
        "Add aria-label to N components", "aria-label coverage on focusable components",
    )
    assert tier == "high"


def test_classify_confidence_low_for_iso_oneliner() -> None:
    tier, _ = rst.classify_confidence(
        "L4.7.5 — 與 ISO 26262 / IEC 62304 / DO-178C 對齊", "",
    )
    assert tier == "low"


def test_classify_confidence_low_for_short_summary_and_description() -> None:
    tier, _ = rst.classify_confidence("Short goal", "tiny body")
    assert tier == "low"


def test_classify_confidence_medium_default() -> None:
    tier, _ = rst.classify_confidence(
        "Some moderately long ticket about quota plumbing across services",
        "We want a quota tracker with caps and reset windows for tenant-scoped usage."
        " It must not interfere with the existing rate limiter and should expose a"
        " clean REST surface for dashboards.",
    )
    assert tier == "medium"


# ── parse_llm_json ─────────────────────────────────────────────────


def test_parse_llm_json_strips_fences() -> None:
    text = '```json\n{"acceptance_criteria": ["a"]}\n```'
    out = rst.parse_llm_json(text)
    assert out == {"acceptance_criteria": ["a"]}


def test_parse_llm_json_recovers_from_extra_prose() -> None:
    text = 'Sure, here is the JSON: {"acceptance_criteria": ["a"], "files": []}'
    out = rst.parse_llm_json(text)
    assert out["acceptance_criteria"] == ["a"]


def test_parse_llm_json_raises_on_garbage() -> None:
    with pytest.raises(ValueError):
        rst.parse_llm_json("definitely not json")


# ── generate_proposal — synthetic 5-ticket batch (AC #4) ───────────


@pytest.fixture
def synthetic_tickets() -> list[dict]:
    return [
        _fake_issue("OP-901", "MP.W1.2 — quota tracker", "Add quota tracking for tenant API calls."),
        _fake_issue("OP-902", "RPG.W2.1 — alembic migration agent_card", "Create agent_card table."),
        _fake_issue("OP-903", "WP.X — investigate ratelimit drift", ""),
        _fake_issue("OP-904", "Z.42 — extract circuit breaker module", "Pull out into its own module."),
        _fake_issue("OP-905", "L4.7.5 — 與 ISO 26262 / IEC 62304 對齊", ""),
    ]


def test_generate_proposal_synthetic_batch(synthetic_tickets, tmp_path) -> None:
    """AC #4: 5 sample placeholder tickets → propose → all 5 have valid
    AC + Paths + Prerequisites.
    """
    proposer = _stub_llm_proposer(_GOOD_PAYLOAD)
    proposals = []
    for issue in synthetic_tickets:
        p = rst.generate_proposal(issue, llm_proposer=proposer, model="haiku-test")
        rst.save_proposal(p, tmp_path)
        proposals.append(p)

    assert len(proposals) == 5
    for p in proposals:
        assert len(p.proposed_acceptance_criteria) >= 3
        assert len(p.proposed_files) >= 1
        assert "blocks_on" in p.proposed_prerequisites
        assert "mutex_with" in p.proposed_prerequisites
        assert (tmp_path / f"{p.key}.json").exists()


def test_generate_proposal_no_ac_downgrades_to_low(synthetic_tickets) -> None:
    proposer = _stub_llm_proposer({"acceptance_criteria": [], "files": [], "prerequisites": {}})
    p = rst.generate_proposal(synthetic_tickets[0], llm_proposer=proposer)
    assert p.confidence == "low"
    assert "no acceptance criteria parsed" in p.confidence_reason


def test_generate_proposal_records_token_usage(synthetic_tickets) -> None:
    proposer = _stub_llm_proposer(_GOOD_PAYLOAD, in_tokens=1234, out_tokens=567)
    p = rst.generate_proposal(synthetic_tickets[0], llm_proposer=proposer)
    assert p.token_usage.input_tokens == 1234
    assert p.token_usage.output_tokens == 567
    # Cost > 0 — Haiku rates pinned in module
    assert p.cost_usd > 0


def test_generate_proposal_handles_malformed_json(synthetic_tickets) -> None:
    def _bad_proposer(system: str, user: str):
        return "not-json-at-all", rst.TokenUsage(input_tokens=10, output_tokens=20)

    p = rst.generate_proposal(synthetic_tickets[0], llm_proposer=_bad_proposer)
    assert p.proposed_acceptance_criteria == []
    assert p.confidence == "low"
    assert any("JSON parse failed" in n for n in p.notes)


# ── save / load round-trip ────────────────────────────────────────


def test_proposal_round_trip(tmp_path, synthetic_tickets) -> None:
    proposer = _stub_llm_proposer(_GOOD_PAYLOAD)
    p = rst.generate_proposal(synthetic_tickets[0], llm_proposer=proposer)
    rst.save_proposal(p, tmp_path)
    p2 = rst.load_proposal(p.key, tmp_path)
    assert p2.key == p.key
    assert p2.proposed_acceptance_criteria == p.proposed_acceptance_criteria
    assert p2.proposed_files == p.proposed_files
    assert p2.token_usage.input_tokens == p.token_usage.input_tokens
    assert p2.status == "pending"


def test_load_proposal_missing_raises(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        rst.load_proposal("OP-9999", tmp_path)


# ── render_refined_description ────────────────────────────────────


def test_render_refined_description_contains_required_sections(synthetic_tickets) -> None:
    p = rst.generate_proposal(
        synthetic_tickets[0],
        llm_proposer=_stub_llm_proposer(_GOOD_PAYLOAD),
    )
    md = rst.render_refined_description(p)
    assert "## Goal" in md
    assert "## Acceptance Criteria" in md
    assert "## Files / Paths" in md
    assert "## Prerequisites" in md
    assert "```yaml" in md
    assert "blocks_on:" in md
    assert "mutex_with:" in md
    assert "Refined by ai-assisted proposal" in md


def test_render_prerequisites_yaml_renders_empty_arrays() -> None:
    out = rst.render_prerequisites_yaml({})
    for key in ("blocks_on", "soft_prereqs", "mutex_with", "schema_locks",
                "live_state_requires", "external_blockers"):
        assert f"{key}: []" in out


def test_render_prerequisites_yaml_renders_items() -> None:
    out = rst.render_prerequisites_yaml({"blocks_on": ["OP-1", "OP-2"]})
    assert "blocks_on:" in out
    assert "  - OP-1" in out
    assert "  - OP-2" in out


# ── cmd_propose / cmd_apply with fakes (no network) ───────────────


class _FakeClient:
    """In-memory stand-in for ``jd.DispatchClient`` operations.

    Records each PUT / POST / label / comment so tests can assert the
    apply flow hits the right endpoints. The script never instantiates
    this directly — the cmd_propose / cmd_apply helpers accept a
    pre-built client argument.
    """

    def __init__(self) -> None:
        self.agent_class = "subscription-claude"
        self.project_key = "OP"
        self.bot_email = "rt3628+claude-bot@gmail.com"
        self.bot_account_id = "fake-account-id"
        self.base_url = "https://example.invalid/rest/api/3"
        self.auth_header = "Basic fake"
        self.label_changes: list[tuple[str, str, str]] = []  # (key, action, label)
        self.comments: list[tuple[str, str]] = []
        self.descriptions: list[tuple[str, str]] = []
        self.issuetype_changes: list[tuple[str, str]] = []


@pytest.fixture
def fake_client_module(monkeypatch):
    """Patch ``rst.jd._request`` / helpers to record calls instead of HTTP."""
    fake = _FakeClient()
    calls: dict[str, list] = {"requests": []}

    def fake_request(client, method, path, body=None, idem_key=None):
        calls["requests"].append((method, path, body))
        return {}

    def fake_request_idempotent(client, method, path, body, idem_key):
        calls["requests"].append((method, path, body))
        if "/issue/" in path and method == "PUT" and body and "fields" in body:
            fields = body["fields"]
            if "description" in fields:
                fake.descriptions.append((path, json.dumps(fields["description"])[:200]))
            if "issuetype" in fields:
                fake.issuetype_changes.append((path, fields["issuetype"]["name"]))
        return {}

    def fake_add_label(client, key, label, idem_key=None):
        fake.label_changes.append((key, "add", label))

    def fake_remove_label(client, key, label, idem_key=None):
        fake.label_changes.append((key, "remove", label))

    def fake_add_comment(client, key, text, idem_key=None):
        fake.comments.append((key, text))

    monkeypatch.setattr(rst.jd, "_request", fake_request)
    monkeypatch.setattr(rst.jd, "_request_idempotent", fake_request_idempotent)
    monkeypatch.setattr(rst.jd, "add_label", fake_add_label)
    monkeypatch.setattr(rst.jd, "remove_label", fake_remove_label)
    monkeypatch.setattr(rst.jd, "add_comment", fake_add_comment)
    return fake, calls


def test_cmd_propose_writes_proposals_for_each_ticket(
    monkeypatch, tmp_path, fake_client_module, synthetic_tickets,
) -> None:
    fake, calls = fake_client_module

    def _fake_fetch(client, max_results=100):
        return synthetic_tickets

    monkeypatch.setattr(rst, "fetch_needs_refinement", _fake_fetch)
    monkeypatch.setattr(rst, "PROPOSALS_DIR", tmp_path)

    args = SimpleNamespace(
        agent_class="subscription-claude",
        operator=None,
        limit=None,
        dry_run=False,
        overwrite=False,
        post_comment=False,
        model="haiku-test",
        cmd="--propose",
    )

    rc = rst.cmd_propose(
        args,
        client=fake,
        llm_proposer=_stub_llm_proposer(_GOOD_PAYLOAD),
        base_dir=tmp_path,
    )
    assert rc == 0
    for issue in synthetic_tickets:
        assert (tmp_path / f"{issue['key']}.json").exists()
    audit = (tmp_path / "audit.log").read_text().splitlines()
    assert len(audit) == 5
    for line in audit:
        entry = json.loads(line)
        assert entry["action"] == "propose"


def test_cmd_propose_dry_run_does_not_write(monkeypatch, tmp_path, fake_client_module, synthetic_tickets) -> None:
    fake, _ = fake_client_module
    monkeypatch.setattr(rst, "fetch_needs_refinement", lambda c, max_results=100: synthetic_tickets)

    args = SimpleNamespace(
        agent_class="subscription-claude", operator=None, limit=None,
        dry_run=True, overwrite=False, post_comment=False, model="haiku-test",
        cmd="--propose",
    )
    rc = rst.cmd_propose(args, client=fake, llm_proposer=_stub_llm_proposer(_GOOD_PAYLOAD), base_dir=tmp_path)
    assert rc == 0
    assert not list(tmp_path.glob("OP-*.json"))


def test_cmd_review_accept_flips_status(tmp_path, synthetic_tickets) -> None:
    proposer = _stub_llm_proposer(_GOOD_PAYLOAD)
    p = rst.generate_proposal(synthetic_tickets[0], llm_proposer=proposer)
    rst.save_proposal(p, tmp_path)

    args = SimpleNamespace(key=p.key, accept=True, reject=False)
    rc = rst.cmd_review(args, base_dir=tmp_path)
    assert rc == 0
    assert rst.load_proposal(p.key, tmp_path).status == "accepted"


def test_cmd_review_reject_flips_status(tmp_path, synthetic_tickets) -> None:
    proposer = _stub_llm_proposer(_GOOD_PAYLOAD)
    p = rst.generate_proposal(synthetic_tickets[0], llm_proposer=proposer)
    rst.save_proposal(p, tmp_path)

    args = SimpleNamespace(key=p.key, accept=False, reject=True)
    rst.cmd_review(args, base_dir=tmp_path)
    assert rst.load_proposal(p.key, tmp_path).status == "rejected"


def test_cmd_apply_refuses_unaccepted_proposal(tmp_path, fake_client_module, synthetic_tickets) -> None:
    fake, _ = fake_client_module
    proposer = _stub_llm_proposer(_GOOD_PAYLOAD)
    p = rst.generate_proposal(synthetic_tickets[0], llm_proposer=proposer)
    rst.save_proposal(p, tmp_path)

    args = SimpleNamespace(
        agent_class="subscription-claude", operator="op@example.invalid",
        key=p.key, dry_run=False, force=False, cmd="--apply",
    )
    rc = rst.cmd_apply(args, client=fake, base_dir=tmp_path)
    assert rc == 2
    assert not fake.label_changes
    assert not fake.comments


def test_cmd_apply_runs_full_flow(tmp_path, fake_client_module, synthetic_tickets) -> None:
    """AC #5: apply flips Task → Story, removes runner-needs-refinement,
    adds refined-by:ai-assisted.
    """
    fake, calls = fake_client_module
    proposer = _stub_llm_proposer(_GOOD_PAYLOAD)
    p = rst.generate_proposal(synthetic_tickets[0], llm_proposer=proposer)
    p.status = "accepted"
    rst.save_proposal(p, tmp_path)

    args = SimpleNamespace(
        agent_class="subscription-claude", operator="sora@example.invalid",
        key=p.key, dry_run=False, force=False, cmd="--apply",
    )
    rc = rst.cmd_apply(args, client=fake, base_dir=tmp_path)
    assert rc == 0

    # Issuetype Task → Story
    issuetypes = [name for _, name in fake.issuetype_changes]
    assert "Story" in issuetypes
    # Description PUT happened
    assert any("description" in body for body in [json.loads(d[1]) if False else d[1] for d in fake.descriptions]) or fake.descriptions
    # Label flip
    assert (p.key, "remove", rst.NEEDS_REFINEMENT_LABEL) in fake.label_changes
    assert (p.key, "add", rst.REFINED_LABEL) in fake.label_changes
    # Comment with [ai-refined] tag
    assert any("[ai-refined]" in text for _, text in fake.comments)
    # Audit log entry
    audit_lines = (tmp_path / "audit.log").read_text().splitlines()
    apply_entries = [json.loads(l) for l in audit_lines if json.loads(l).get("action") == "apply"]
    assert len(apply_entries) == 1
    assert apply_entries[0]["operator"] == "sora@example.invalid"
    assert "token_usage" in apply_entries[0]
    # Proposal status is updated to applied
    assert rst.load_proposal(p.key, tmp_path).status == "applied"


def test_cmd_apply_dry_run_does_not_call_jira(tmp_path, fake_client_module, synthetic_tickets) -> None:
    fake, _ = fake_client_module
    proposer = _stub_llm_proposer(_GOOD_PAYLOAD)
    p = rst.generate_proposal(synthetic_tickets[0], llm_proposer=proposer)
    p.status = "accepted"
    rst.save_proposal(p, tmp_path)

    args = SimpleNamespace(
        agent_class="subscription-claude", operator=None,
        key=p.key, dry_run=True, force=False, cmd="--apply",
    )
    rc = rst.cmd_apply(args, client=fake, base_dir=tmp_path)
    assert rc == 0
    assert not fake.label_changes
    assert not fake.comments
    assert not fake.issuetype_changes


# ── audit log ──────────────────────────────────────────────────────


def test_append_audit_log_writes_jsonl(tmp_path) -> None:
    log = tmp_path / "audit.log"
    rst.append_audit_log(
        operator="op@example.invalid",
        action="apply",
        key="OP-901",
        token_usage=rst.TokenUsage(input_tokens=10, output_tokens=20),
        extra={"confidence": "high"},
        log_path=log,
    )
    rst.append_audit_log(
        operator="op@example.invalid",
        action="propose",
        key="OP-902",
        token_usage=rst.TokenUsage(input_tokens=5, output_tokens=8),
        log_path=log,
    )
    lines = log.read_text().splitlines()
    assert len(lines) == 2
    e0 = json.loads(lines[0])
    assert e0["key"] == "OP-901"
    assert e0["action"] == "apply"
    assert e0["token_usage"]["input_tokens"] == 10
    assert "cost_usd" in e0
    e1 = json.loads(lines[1])
    assert e1["key"] == "OP-902"
    assert e1["action"] == "propose"


# ── CLI argv handling ─────────────────────────────────────────────


def test_main_review_default_prints(tmp_path, capsys, synthetic_tickets) -> None:
    proposer = _stub_llm_proposer(_GOOD_PAYLOAD)
    p = rst.generate_proposal(synthetic_tickets[0], llm_proposer=proposer)
    rst.save_proposal(p, tmp_path)

    with patch.object(rst, "PROPOSALS_DIR", tmp_path):
        rc = rst.main(["--review", p.key])
    out = capsys.readouterr().out
    assert rc == 0
    assert p.key in out
    assert "Proposed Acceptance Criteria" in out


def test_main_review_accept_and_reject_mutually_exclusive(tmp_path, synthetic_tickets, capsys) -> None:
    proposer = _stub_llm_proposer(_GOOD_PAYLOAD)
    p = rst.generate_proposal(synthetic_tickets[0], llm_proposer=proposer)
    rst.save_proposal(p, tmp_path)

    with patch.object(rst, "PROPOSALS_DIR", tmp_path):
        rc = rst.main(["--review", p.key, "--accept", "--reject"])
    assert rc == 2


# ── ADF helpers ────────────────────────────────────────────────────


def test_adf_to_text_extracts_codeblock_content() -> None:
    adf = {
        "type": "doc", "version": 1,
        "content": [
            {
                "type": "codeBlock",
                "attrs": {"language": "markdown"},
                "content": [{"type": "text", "text": "## Goal\nplaceholder"}],
            }
        ],
    }
    assert "## Goal" in rst._adf_to_text(adf)
    assert "placeholder" in rst._adf_to_text(adf)


def test_adf_codeblock_round_trip() -> None:
    md = "## Goal\nrefined"
    adf = rst._adf_codeblock(md)
    assert adf["type"] == "doc"
    assert adf["content"][0]["type"] == "codeBlock"
    assert adf["content"][0]["content"][0]["text"] == md
