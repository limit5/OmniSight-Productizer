"""OP-941 (G5) tests for ``scripts/instantiate_hotfix_meta.py``.

Covers the five test-plan cases from the ticket description:

1. Hotfix creates correctly — META + 4 children with correct labels +
   blockedBy chain + Relates links + child-map comment.
2. Source change not merged — refused with ``HotfixSourceChangeNotMerged``;
   no JIRA writes; CLI exit 6.
3. Target release branch missing — refused with
   ``TargetReleaseBranchNotExists``; no JIRA writes; CLI exit 7.
4. Duplicate hotfix — a pre-existing ``HOTFIX-<target>`` META makes the
   run an idempotent skip (``DuplicateHotfix``); no new JIRA writes;
   CLI exit 0.
5. Backport gate fires — the template carries the H4
   backport-to-develop check, wired blockedBy H3, citing
   ``scripts/hotfix_backport_check.py`` and ``develop``.

Plus a handful of schema / regex / dry-run / force / fail-closed guards.

Gerrit + JIRA effects are intercepted via ``monkeypatch``; the suite
never hits the network.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

# ``scripts/instantiate_hotfix_meta.py`` lives outside the importable
# ``backend.*`` package tree, so load it from path (same pattern as the
# G1 release-engine suite).
_ENGINE_PATH = REPO_ROOT / "scripts" / "instantiate_hotfix_meta.py"
_spec = importlib.util.spec_from_file_location("instantiate_hotfix_meta", _ENGINE_PATH)
engine = importlib.util.module_from_spec(_spec)
sys.modules["instantiate_hotfix_meta"] = engine
_spec.loader.exec_module(engine)  # type: ignore[union-attr]

from backend.agents import file_coordinator, jira_dispatch  # noqa: E402


TEMPLATE_PATH = REPO_ROOT / "config" / "hotfix_template.yaml"
TARGET = "v1.2.4+1"
SOURCE_CHANGE = "95001"


# ── Fixtures ─────────────────────────────────────────────────────────


def _fake_client() -> jira_dispatch.DispatchClient:
    return jira_dispatch.DispatchClient(
        agent_class="subscription-codex",
        base_url="https://test.invalid/rest/api/3",
        project_key="OP",
        auth_header="Basic dGVzdA==",
        bot_account_id="acc-test",
        bot_email="bot@example.invalid",
    )


@pytest.fixture
def template_path() -> Path:
    return TEMPLATE_PATH


@pytest.fixture
def patch_engine(monkeypatch, tmp_path):
    """Intercept Gerrit + JIRA effects; return a recorder dict."""
    state = {
        "issues_created": [],     # list[dict]
        "blocked_by_calls": [],   # list[dict]
        "relates_calls": [],      # list[dict]
        "comments": [],           # list[dict]
        "existing_meta": (None, None, []),  # (key, status, labels)
        "merged": True,
        "branch_exists": True,
        "next_issue_seq": 1000,
        "link_exists": False,
    }
    monkeypatch.setattr(engine, "ROLLBACK_DIR", tmp_path)

    monkeypatch.setattr(
        engine, "gerrit_change_merged",
        lambda change, agent_class: state["merged"],  # noqa: ARG005
    )
    monkeypatch.setattr(
        engine, "release_branch_exists",
        lambda target, agent_class, *, repo: state["branch_exists"],  # noqa: ARG005
    )
    monkeypatch.setattr(
        engine, "find_existing_hotfix_key",
        lambda client, target: state["existing_meta"],  # noqa: ARG005
    )

    def fake_create_issue(client, *, summary, description_markdown, labels, issuetype="Story"):  # noqa: ARG001
        state["next_issue_seq"] += 1
        key = f"OP-{state['next_issue_seq']}"
        state["issues_created"].append({
            "key": key,
            "summary": summary,
            "labels": list(labels),
            "issuetype": issuetype,
            "description": description_markdown,
        })
        return key

    def fake_add_blocked_by(client, blocked_key, blocker_key, *, reason=None, link_type="Blocks"):  # noqa: ARG001
        state["blocked_by_calls"].append({
            "blocked": blocked_key, "blocker": blocker_key,
            "reason": reason, "link_type": link_type,
        })
        return True

    def fake_link_exists(client, *, blocked, blocker, link_type="Blocks"):  # noqa: ARG001
        return state["link_exists"]

    def fake_create_issue_link(client, *, inward, outward, link_type="Blocks"):  # noqa: ARG001
        state["relates_calls"].append({"inward": inward, "outward": outward, "link_type": link_type})

    def fake_add_comment(client, key, text, idem_key=None):  # noqa: ARG001
        state["comments"].append({"key": key, "text": text})

    monkeypatch.setattr(engine, "create_issue", fake_create_issue)
    monkeypatch.setattr(file_coordinator, "add_blocked_by", fake_add_blocked_by)
    monkeypatch.setattr(file_coordinator, "jira_link_exists", fake_link_exists)
    monkeypatch.setattr(file_coordinator, "jira_create_issue_link", fake_create_issue_link)
    monkeypatch.setattr(jira_dispatch, "add_comment", fake_add_comment)
    monkeypatch.setattr(
        engine.jira_dispatch,
        "make_client",
        lambda cls, instance_id=None: _fake_client(),
    )
    return state


# ── Case 1: hotfix creates correctly ─────────────────────────────────


def test_hotfix_creates_meta_plus_four_children(patch_engine, template_path) -> None:
    template = engine.load_template(template_path)
    created = engine.apply(
        _fake_client(), template, TARGET, SOURCE_CHANGE, repo=Path("."),
    )

    # 5 tickets — 1 META + 4 children (H1..H4).
    assert len(patch_engine["issues_created"]) == 5
    assert set(created.keys()) == {"META", "H1", "H2", "H3", "H4"}

    meta_record = patch_engine["issues_created"][0]
    assert "meta:hotfix" in meta_record["labels"]
    assert f"HOTFIX-{TARGET}" in meta_record["labels"]
    assert "tier:X" in meta_record["labels"]
    assert f"hotfix-source:{SOURCE_CHANGE}" in meta_record["labels"]
    assert meta_record["summary"].startswith(f"HOTFIX-{TARGET} META")

    for idx, child_record in enumerate(patch_engine["issues_created"][1:], start=1):
        assert f"HOTFIX-{TARGET}" in child_record["labels"]
        assert f"hotfix-child:H{idx}" in child_record["labels"]
        assert any(label.startswith("class:") for label in child_record["labels"])
        assert any(label.startswith("area:") for label in child_record["labels"])
        assert created["META"] in child_record["description"]

    # blockedBy chain: H2←H1, H3←H2, H4←H3 (3 edges, sequential).
    edges = [(c["blocked"], c["blocker"]) for c in patch_engine["blocked_by_calls"]]
    assert edges == [
        (created["H2"], created["H1"]),
        (created["H3"], created["H2"]),
        (created["H4"], created["H3"]),
    ]

    # Relates: each child → META, link_type=Relates.
    assert len(patch_engine["relates_calls"]) == 4
    for call in patch_engine["relates_calls"]:
        assert call["link_type"] == "Relates"
        assert call["outward"] == created["META"]
        assert call["inward"] in {created[f"H{i}"] for i in range(1, 5)}

    # Child-map comment on the META.
    assert len(patch_engine["comments"]) == 1
    comment = patch_engine["comments"][0]
    assert comment["key"] == created["META"]
    assert "template version: v1" in comment["text"]
    assert f"source change: {SOURCE_CHANGE}" in comment["text"]
    assert created["H1"] in comment["text"]
    assert created["H4"] in comment["text"]


def test_hotfix_main_happy_path_exit_zero(patch_engine, capsys) -> None:
    rc = engine.main([
        "--from-change", SOURCE_CHANGE,
        "--target", TARGET,
        "--template", str(TEMPLATE_PATH),
    ])
    assert rc == engine.EXIT_OK == 0
    out = capsys.readouterr().out
    assert f"Created HOTFIX-{TARGET} META + 4 children" in out
    assert len(patch_engine["issues_created"]) == 5


# ── Case 2: source change not merged → refused ───────────────────────


def test_source_change_not_merged_refused(patch_engine, template_path) -> None:
    patch_engine["merged"] = False
    template = engine.load_template(template_path)
    with pytest.raises(engine.HotfixSourceChangeNotMerged) as exc_info:
        engine.apply(_fake_client(), template, TARGET, SOURCE_CHANGE, repo=Path("."))
    assert SOURCE_CHANGE in str(exc_info.value)
    assert patch_engine["issues_created"] == []
    assert patch_engine["blocked_by_calls"] == []


def test_source_change_not_merged_main_exit_six(patch_engine) -> None:
    patch_engine["merged"] = False
    rc = engine.main([
        "--from-change", SOURCE_CHANGE, "--target", TARGET,
        "--template", str(TEMPLATE_PATH),
    ])
    assert rc == engine.EXIT_SOURCE_NOT_MERGED == 6
    assert patch_engine["issues_created"] == []


# ── Case 3: target release branch missing → refused ──────────────────


def test_target_release_branch_missing_refused(patch_engine, template_path) -> None:
    patch_engine["branch_exists"] = False
    template = engine.load_template(template_path)
    with pytest.raises(engine.TargetReleaseBranchNotExists) as exc_info:
        engine.apply(_fake_client(), template, TARGET, SOURCE_CHANGE, repo=Path("."))
    assert f"release/{TARGET}" in str(exc_info.value)
    assert patch_engine["issues_created"] == []


def test_target_release_branch_missing_main_exit_seven(patch_engine) -> None:
    patch_engine["branch_exists"] = False
    rc = engine.main([
        "--from-change", SOURCE_CHANGE, "--target", TARGET,
        "--template", str(TEMPLATE_PATH),
    ])
    assert rc == engine.EXIT_TARGET_BRANCH_MISSING == 7
    assert patch_engine["issues_created"] == []


# ── Case 4: duplicate hotfix → idempotent skip ───────────────────────


def test_duplicate_hotfix_skipped(patch_engine, template_path) -> None:
    patch_engine["existing_meta"] = (
        "OP-9999", "In Progress",
        ["meta:hotfix", f"HOTFIX-{TARGET}", f"hotfix-source:{SOURCE_CHANGE}"],
    )
    template = engine.load_template(template_path)
    with pytest.raises(engine.DuplicateHotfix) as exc_info:
        engine.apply(_fake_client(), template, TARGET, SOURCE_CHANGE, repo=Path("."))
    msg = str(exc_info.value)
    assert "OP-9999" in msg
    assert "same_source_change=True" in msg
    # Idempotent: zero new JIRA writes.
    assert patch_engine["issues_created"] == []
    assert patch_engine["blocked_by_calls"] == []
    assert patch_engine["comments"] == []


def test_duplicate_hotfix_main_exit_zero(patch_engine, capsys) -> None:
    patch_engine["existing_meta"] = (
        "OP-9999", "In Progress", ["meta:hotfix", f"HOTFIX-{TARGET}"],
    )
    rc = engine.main([
        "--from-change", SOURCE_CHANGE, "--target", TARGET,
        "--template", str(TEMPLATE_PATH),
    ])
    # Idempotent skip is a *success* — the cron must not treat it as a failure.
    assert rc == engine.EXIT_OK == 0
    out = capsys.readouterr().out
    assert "idempotent skip" in out
    assert "OP-9999" in out
    assert patch_engine["issues_created"] == []


def test_duplicate_archived_meta_with_force_recreates(patch_engine, template_path) -> None:
    patch_engine["existing_meta"] = ("OP-9999", "Archived", ["meta:hotfix", f"HOTFIX-{TARGET}"])
    template = engine.load_template(template_path)
    created = engine.apply(
        _fake_client(), template, TARGET, SOURCE_CHANGE, repo=Path("."), force=True,
    )
    assert set(created.keys()) == {"META", "H1", "H2", "H3", "H4"}


def test_force_refuses_when_meta_not_archived(patch_engine, template_path) -> None:
    patch_engine["existing_meta"] = ("OP-9999", "In Progress", ["meta:hotfix", f"HOTFIX-{TARGET}"])
    template = engine.load_template(template_path)
    with pytest.raises(engine.DuplicateHotfix):
        engine.apply(_fake_client(), template, TARGET, SOURCE_CHANGE, repo=Path("."), force=True)


# ── Case 5: backport gate fires (H4 in the template) ─────────────────


def test_backport_gate_child_present(template_path) -> None:
    template = engine.load_template(template_path)
    assert [c.h_id for c in template.children] == ["H1", "H2", "H3", "H4"]

    h1, h2, h3, h4 = template.children
    # The 4 children match the ticket's H1..H4 spec.
    assert "cherry-pick" in h1.name.lower() and "smoke" in h1.name.lower()
    assert h1.blocked_by == ()
    assert "approval" in h2.name.lower()
    assert h2.blocked_by == ("H1",)
    assert "deploy" in h3.name.lower() and "canary 25%" in h3.name.lower()
    assert h3.blocked_by == ("H2",)

    # H4 — the D14 backport-to-develop gate, wired blockedBy H3.
    assert "backport" in h4.name.lower()
    assert h4.blocked_by == ("H3",)
    ac = h4.ac.lower()
    assert "hotfix_backport_check.py" in ac
    assert "develop" in ac
    # Rendered child labels carry the H4 marker.
    assert "hotfix-child:H4" in engine.render_child_labels(h4, TARGET)


# ── Schema / regex / dry-run / fail-closed guards ────────────────────


def test_default_template_loads_with_four_children(template_path) -> None:
    template = engine.load_template(template_path)
    assert len(template.children) == engine.EXPECTED_CHILD_COUNT == 4


def test_target_regex_accepts_hotfix_counter_form() -> None:
    assert engine.TARGET_RE.fullmatch("v1.2.4+1")
    assert engine.TARGET_RE.fullmatch("v0.3.0+2")
    assert engine.TARGET_RE.fullmatch("v10.0.1+12")
    assert not engine.TARGET_RE.fullmatch("v1.2.4")        # missing +N
    assert not engine.TARGET_RE.fullmatch("v1.2.4+0")      # counter must be >= 1
    assert not engine.TARGET_RE.fullmatch("1.2.4+1")       # missing leading v
    assert not engine.TARGET_RE.fullmatch("v1.2+1")        # not three components


def test_template_yaml_invalid_missing_target_placeholder(tmp_path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(textwrap.dedent("""\
        version: "v1"
        meta:
          summary_template: "no placeholder here"
          labels: []
          tier: "X"
        children: []
    """))
    with pytest.raises(engine.HotfixTemplateInvalid) as exc_info:
        engine.load_template(bad)
    assert "summary_template" in str(exc_info.value)


def test_template_yaml_invalid_wrong_child_count(tmp_path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(textwrap.dedent("""\
        version: "v1"
        meta:
          summary_template: "HOTFIX-{target} META"
          labels: []
          tier: "X"
        children:
          - h_id: "H1"
            name: "only one"
            class: "subscription-claude"
            tier: "S"
            areas: ["tooling"]
            ac: "x"
    """))
    with pytest.raises(engine.HotfixTemplateInvalid) as exc_info:
        engine.load_template(bad)
    assert "exactly 4" in str(exc_info.value)


def test_template_yaml_invalid_forward_blocked_by_reference(tmp_path) -> None:
    import yaml as _yaml
    children = []
    for i in range(1, 5):
        children.append({
            "h_id": f"H{i}",
            "name": f"step {i}",
            "blocked_by": ["H4"] if i == 1 else [f"H{i-1}"],
            "class": "subscription-claude",
            "tier": "S",
            "areas": ["tooling"],
            "ac": "x",
        })
    bad = tmp_path / "bad.yaml"
    bad.write_text(_yaml.safe_dump({
        "version": "v1",
        "meta": {"summary_template": "HOTFIX-{target} META", "labels": [], "tier": "X"},
        "children": children,
    }))
    with pytest.raises(engine.HotfixTemplateInvalid) as exc_info:
        engine.load_template(bad)
    assert "Forward references" in str(exc_info.value)


def test_template_yaml_invalid_via_main_exits_two(tmp_path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("version: 5\n")  # top-level version must be a string
    rc = engine.main([
        "--from-change", SOURCE_CHANGE, "--target", TARGET, "--dry-run",
        "--template", str(bad),
    ])
    assert rc == engine.EXIT_TEMPLATE_INVALID == 2


def test_dry_run_emits_plan_and_no_writes(patch_engine, capsys) -> None:
    rc = engine.main([
        "--from-change", SOURCE_CHANGE, "--target", TARGET, "--dry-run",
        "--template", str(TEMPLATE_PATH),
    ])
    assert rc == engine.EXIT_OK
    out = capsys.readouterr().out
    assert "Hotfix META instantiation plan" in out
    assert "dry-run: no JIRA writes performed" in out
    assert out.count("blockedBy H") == 3
    assert patch_engine["issues_created"] == []
    assert patch_engine["comments"] == []


def test_wiring_failure_spills_rollback_file(monkeypatch, patch_engine, template_path, tmp_path) -> None:
    template = engine.load_template(template_path)

    def explode(*args, **kwargs):  # noqa: ARG001
        raise RuntimeError("simulated JIRA 500 on issueLink")

    monkeypatch.setattr(file_coordinator, "add_blocked_by", explode)
    with pytest.raises(engine.HotfixWiringFailed) as exc_info:
        engine.apply(_fake_client(), template, TARGET, SOURCE_CHANGE, repo=Path("."))
    rollback_file = tmp_path / f"hotfix-rollback-{TARGET}.json"
    assert rollback_file.is_file()
    payload = json.loads(rollback_file.read_text())
    assert set(payload["ticket_keys"].keys()) == {"META", "H1", "H2", "H3", "H4"}
    assert payload["target"] == TARGET
    assert "simulated JIRA 500" in payload["reason"]
    assert str(rollback_file) in str(exc_info.value)


def test_jql_outage_fails_closed(monkeypatch, template_path) -> None:
    """A /search/jql failure must become JQLQueryFailed — never create a META."""
    template = engine.load_template(template_path)

    def explode(client, method, path, body=None, idem_key=None):  # noqa: ARG001
        if path == "/search/jql":
            raise RuntimeError("simulated JIRA 503 on /search/jql")
        raise AssertionError(f"unexpected request: {method} {path}")

    monkeypatch.setattr(jira_dispatch, "_request", explode)
    monkeypatch.setattr(engine, "gerrit_change_merged", lambda *a, **k: True)
    monkeypatch.setattr(engine, "release_branch_exists", lambda *a, **k: True)
    with pytest.raises(engine.JQLQueryFailed) as exc_info:
        engine.apply(_fake_client(), template, TARGET, SOURCE_CHANGE, repo=Path("."))
    assert "503" in str(exc_info.value)


def test_state_ambiguous_when_two_metas(monkeypatch, template_path) -> None:
    template = engine.load_template(template_path)

    def two_matches(client, method, path, body=None, idem_key=None):  # noqa: ARG001
        assert path == "/search/jql"
        return {"issues": [
            {"key": "OP-5001", "fields": {"status": {"name": "In Progress"}, "labels": ["meta:hotfix", f"HOTFIX-{TARGET}"]}},
            {"key": "OP-5002", "fields": {"status": {"name": "In Progress"}, "labels": ["meta:hotfix", f"HOTFIX-{TARGET}"]}},
        ]}

    monkeypatch.setattr(jira_dispatch, "_request", two_matches)
    monkeypatch.setattr(engine, "gerrit_change_merged", lambda *a, **k: True)
    monkeypatch.setattr(engine, "release_branch_exists", lambda *a, **k: True)
    with pytest.raises(engine.StateAmbiguous) as exc_info:
        engine.apply(_fake_client(), template, TARGET, SOURCE_CHANGE, repo=Path("."))
    assert "OP-5001" in str(exc_info.value) and "OP-5002" in str(exc_info.value)


def test_find_existing_hotfix_matches_scopes_query(monkeypatch) -> None:
    seen = {}

    def fake_request(client, method, path, body=None, idem_key=None):  # noqa: ARG001
        seen["path"] = path
        seen["jql"] = (body or {}).get("jql", "")
        return {"issues": []}

    monkeypatch.setattr(jira_dispatch, "_request", fake_request)
    matches = engine.find_existing_hotfix_matches(_fake_client(), TARGET)
    assert matches == []
    assert seen["path"] == "/search/jql"
    assert f'labels = "HOTFIX-{TARGET}"' in seen["jql"]
    assert 'labels = "meta:hotfix"' in seen["jql"]
