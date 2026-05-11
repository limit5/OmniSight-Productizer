"""OP-937 (G1) tests for ``scripts/instantiate_release_meta.py``.

Covers the seven test cases from the ticket description:

1. Happy path — creates META + 13 children with correct labels.
2. Idempotency — refuses re-creation when META already exists.
3. ``--dry-run`` — emits a plan and performs zero JIRA writes.
4. ``--force`` — allows re-creation only when META status is Archived.
5. Template YAML invalid — refuses with a TemplateYAMLInvalid error.
6. ``blockedBy`` wiring failure — rollback file lands, error raised.
7. Child label drift detector — each child carries the expected label set.

JIRA writes are intercepted via ``monkeypatch`` of the dispatch-layer
helpers; the suite never hits the network.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

# ``scripts/instantiate_release_meta.py`` lives outside the
# importable ``backend.*`` package tree, so load it from path so this
# test suite stays under ``backend/tests/`` per the ticket file list.
_ENGINE_PATH = REPO_ROOT / "scripts" / "instantiate_release_meta.py"
_spec = importlib.util.spec_from_file_location(
    "instantiate_release_meta", _ENGINE_PATH,
)
engine = importlib.util.module_from_spec(_spec)
sys.modules["instantiate_release_meta"] = engine
_spec.loader.exec_module(engine)  # type: ignore[union-attr]

from backend.agents import file_coordinator, jira_dispatch  # noqa: E402


# ── Fixtures ─────────────────────────────────────────────────────────


def _fake_client() -> jira_dispatch.DispatchClient:
    return jira_dispatch.DispatchClient(
        agent_class="subscription-claude",
        base_url="https://test.invalid/rest/api/3",
        project_key="OP",
        auth_header="Basic dGVzdA==",
        bot_account_id="acc-test",
        bot_email="bot@example.invalid",
    )


@pytest.fixture
def template_path() -> Path:
    return REPO_ROOT / "config" / "release_template.yaml"


@pytest.fixture
def meta_desc_path() -> Path:
    return REPO_ROOT / "config" / "release_meta_description.md.template"


@pytest.fixture
def patch_jira(monkeypatch, tmp_path):
    """Intercept every JIRA helper called by ``engine`` + return a recorder."""
    state = {
        "issues_created": [],   # list[(summary, labels, issuetype)]
        "blocked_by_calls": [],  # list[(blocked_key, blocker_key)]
        "relates_calls": [],     # list[(inward, outward, link_type)]
        "comments": [],          # list[(key, text)]
        "existing_meta": None,   # (key, status) or None
        "next_issue_seq": 1000,
        "link_exists": False,
    }

    # Redirect rollback files into tmp_path so tests stay sandboxed.
    monkeypatch.setattr(engine, "ROLLBACK_DIR", tmp_path)

    def fake_find_existing(client, version):  # noqa: ARG001
        return state["existing_meta"] or (None, None)

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
            "blocked": blocked_key,
            "blocker": blocker_key,
            "reason": reason,
            "link_type": link_type,
        })
        return True

    def fake_link_exists(client, *, blocked, blocker, link_type="Blocks"):  # noqa: ARG001
        return state["link_exists"]

    def fake_create_issue_link(client, *, inward, outward, link_type="Blocks"):  # noqa: ARG001
        state["relates_calls"].append({
            "inward": inward,
            "outward": outward,
            "link_type": link_type,
        })

    def fake_add_comment(client, key, text, idem_key=None):  # noqa: ARG001
        state["comments"].append({"key": key, "text": text})

    monkeypatch.setattr(engine, "find_existing_meta_key", fake_find_existing)
    monkeypatch.setattr(engine, "create_issue", fake_create_issue)
    monkeypatch.setattr(file_coordinator, "add_blocked_by", fake_add_blocked_by)
    monkeypatch.setattr(file_coordinator, "jira_link_exists", fake_link_exists)
    monkeypatch.setattr(
        file_coordinator, "jira_create_issue_link", fake_create_issue_link,
    )
    monkeypatch.setattr(jira_dispatch, "add_comment", fake_add_comment)
    return state


# ── Test 1: happy path ───────────────────────────────────────────────


def test_happy_path_creates_meta_plus_thirteen_children(
    patch_jira, template_path, meta_desc_path,
) -> None:
    template = engine.load_template(template_path)
    created = engine.apply(
        _fake_client(),
        template,
        "v0.5.1-rc1",
        template_path,
        meta_desc_path,
    )

    # 14 total tickets — 1 META + 13 children
    assert len(patch_jira["issues_created"]) == 14
    assert set(created.keys()) == {"META"} | {f"R{i}" for i in range(1, 14)}

    # First created issue is the META (carries the meta:release label).
    meta_record = patch_jira["issues_created"][0]
    assert "meta:release" in meta_record["labels"]
    assert "RELEASE-v0.5.1-rc1" in meta_record["labels"]
    assert "tier:X" in meta_record["labels"]
    assert meta_record["summary"].startswith("RELEASE-v0.5.1-rc1 META")

    # Each child carries the release label + a release-child:<R-id> marker.
    for idx, child_record in enumerate(patch_jira["issues_created"][1:], start=1):
        assert "RELEASE-v0.5.1-rc1" in child_record["labels"]
        assert f"release-child:R{idx}" in child_record["labels"]
        assert any(label.startswith("class:") for label in child_record["labels"])
        assert any(label.startswith("area:") for label in child_record["labels"])

    # blockedBy chain: 12 edges, sequential.
    edges = [(c["blocked"], c["blocker"]) for c in patch_jira["blocked_by_calls"]]
    expected_blockers = [created[f"R{i}"] for i in range(1, 13)]
    expected_blocked = [created[f"R{i}"] for i in range(2, 14)]
    assert edges == list(zip(expected_blocked, expected_blockers))

    # Relates from each child to META, with link_type=Relates.
    assert len(patch_jira["relates_calls"]) == 13
    for call in patch_jira["relates_calls"]:
        assert call["link_type"] == "Relates"
        assert call["outward"] == created["META"]
        assert call["inward"] in {created[f"R{i}"] for i in range(1, 14)}

    # META comment carries template version + child key map.
    assert len(patch_jira["comments"]) == 1
    comment = patch_jira["comments"][0]
    assert comment["key"] == created["META"]
    assert "template version: v1" in comment["text"]
    assert created["R1"] in comment["text"]
    assert created["R13"] in comment["text"]


# ── Test 2: idempotency guard ───────────────────────────────────────


def test_idempotency_refuses_when_meta_exists(
    patch_jira, template_path, meta_desc_path,
) -> None:
    template = engine.load_template(template_path)
    patch_jira["existing_meta"] = ("OP-9999", "In Progress")

    with pytest.raises(engine.MetaAlreadyExists) as exc_info:
        engine.apply(
            _fake_client(),
            template,
            "v0.5.1-rc1",
            template_path,
            meta_desc_path,
        )
    assert "RELEASE-v0.5.1-rc1" in str(exc_info.value)
    assert "OP-9999" in str(exc_info.value)
    # Zero JIRA writes performed.
    assert patch_jira["issues_created"] == []
    assert patch_jira["blocked_by_calls"] == []


def test_idempotency_main_exit_code_is_one(
    monkeypatch, patch_jira, template_path, meta_desc_path,
) -> None:
    patch_jira["existing_meta"] = ("OP-9999", "In Progress")
    monkeypatch.setattr(engine.jira_dispatch, "make_client", lambda cls: _fake_client())
    rc = engine.main([
        "--version", "v0.5.1-rc1",
        "--template", str(template_path),
        "--meta-description-template", str(meta_desc_path),
    ])
    assert rc == engine.EXIT_META_ALREADY_EXISTS == 1


# ── Test 3: --dry-run is a no-op ─────────────────────────────────────


def test_dry_run_emits_plan_and_performs_no_writes(
    capsys, monkeypatch, patch_jira, template_path, meta_desc_path,
) -> None:
    # No JIRA client should be constructed under dry-run.
    def fail_make_client(cls):  # noqa: ARG001
        raise AssertionError("make_client must not be called under --dry-run")

    monkeypatch.setattr(engine.jira_dispatch, "make_client", fail_make_client)
    rc = engine.main([
        "--version", "v0.5.1-rc1",
        "--dry-run",
        "--template", str(template_path),
        "--meta-description-template", str(meta_desc_path),
    ])
    assert rc == 0
    stdout = capsys.readouterr().out
    assert "Release META instantiation plan" in stdout
    assert "dry-run: no JIRA writes performed" in stdout
    # The plan lists exactly 13 children + 12 blockedBy edges.
    assert stdout.count("blockedBy R") == 12
    # No live writes touched the recorder.
    assert patch_jira["issues_created"] == []
    assert patch_jira["blocked_by_calls"] == []
    assert patch_jira["comments"] == []


# ── Test 4: --force allows recreate only when Archived ──────────────


def test_force_refuses_when_meta_is_not_archived(
    patch_jira, template_path, meta_desc_path,
) -> None:
    template = engine.load_template(template_path)
    patch_jira["existing_meta"] = ("OP-9999", "In Progress")
    with pytest.raises(engine.MetaAlreadyExists):
        engine.apply(
            _fake_client(),
            template,
            "v0.5.1-rc1",
            template_path,
            meta_desc_path,
            force=True,
        )


def test_force_allows_recreate_when_meta_is_archived(
    patch_jira, template_path, meta_desc_path,
) -> None:
    template = engine.load_template(template_path)
    patch_jira["existing_meta"] = ("OP-9999", "Archived")
    created = engine.apply(
        _fake_client(),
        template,
        "v0.5.1-rc1",
        template_path,
        meta_desc_path,
        force=True,
    )
    assert len(created) == 14
    assert set(created.keys()) == {"META"} | {f"R{i}" for i in range(1, 14)}


# ── Test 5: template YAML invalid ────────────────────────────────────


def test_template_yaml_invalid_refuses_with_clear_error(tmp_path) -> None:
    bad = tmp_path / "bad_template.yaml"
    bad.write_text(textwrap.dedent("""\
        version: "v1"
        meta:
          summary_template: "missing version placeholder"
          labels: []
          tier: "X"
        children: []
    """))
    with pytest.raises(engine.TemplateYAMLInvalid) as exc_info:
        engine.load_template(bad)
    assert "summary_template" in str(exc_info.value)


def test_template_yaml_invalid_rejects_wrong_child_count(tmp_path) -> None:
    bad = tmp_path / "bad_template.yaml"
    bad.write_text(textwrap.dedent("""\
        version: "v1"
        meta:
          summary_template: "RELEASE-{version} META"
          labels: []
          tier: "X"
        children:
          - r_id: "R1"
            name: "only one"
            class: "subscription-claude"
            tier: "S"
            areas: ["docs"]
            ac: "x"
    """))
    with pytest.raises(engine.TemplateYAMLInvalid) as exc_info:
        engine.load_template(bad)
    assert "exactly 13" in str(exc_info.value)


def test_template_yaml_invalid_rejects_bad_blocked_by_reference(tmp_path) -> None:
    """A forward reference (cycle) must be refused so chains stay acyclic."""
    children = []
    for i in range(1, 14):
        blocked_by = ["R13"] if i == 1 else [f"R{i-1}"]
        children.append({
            "r_id": f"R{i}",
            "name": f"step {i}",
            "blocked_by": blocked_by,
            "class": "subscription-claude",
            "tier": "S",
            "areas": ["docs"],
            "ac": "x",
        })
    import yaml as _yaml
    bad = tmp_path / "bad_template.yaml"
    bad.write_text(_yaml.safe_dump({
        "version": "v1",
        "meta": {"summary_template": "RELEASE-{version} META", "labels": [], "tier": "X"},
        "children": children,
    }))
    with pytest.raises(engine.TemplateYAMLInvalid) as exc_info:
        engine.load_template(bad)
    assert "Forward references" in str(exc_info.value)


def test_template_yaml_invalid_via_main_exits_two(
    monkeypatch, tmp_path, meta_desc_path,
) -> None:
    bad = tmp_path / "bad_template.yaml"
    bad.write_text("version: 5\n")  # top-level version must be a string
    rc = engine.main([
        "--version", "v0.5.1-rc1",
        "--dry-run",
        "--template", str(bad),
        "--meta-description-template", str(meta_desc_path),
    ])
    assert rc == engine.EXIT_TEMPLATE_INVALID == 2


# ── Test 6: blockedBy wiring failure rolls back ──────────────────────


def test_blocked_by_wiring_failure_writes_rollback_file(
    monkeypatch, patch_jira, template_path, meta_desc_path, tmp_path,
) -> None:
    template = engine.load_template(template_path)

    def explode(*args, **kwargs):  # noqa: ARG001
        raise RuntimeError("simulated JIRA 500 on issueLink")

    monkeypatch.setattr(file_coordinator, "add_blocked_by", explode)

    with pytest.raises(engine.BlockedByWiringFailed) as exc_info:
        engine.apply(
            _fake_client(),
            template,
            "v0.5.1-rc1",
            template_path,
            meta_desc_path,
        )
    rollback_file = tmp_path / "release-rollback-v0.5.1-rc1.json"
    assert rollback_file.is_file()
    payload = json.loads(rollback_file.read_text())
    # All 14 created keys must be recorded so the operator can clean up.
    assert set(payload["ticket_keys"].keys()) == {"META"} | {f"R{i}" for i in range(1, 14)}
    assert payload["version"] == "v0.5.1-rc1"
    assert "simulated JIRA 500" in payload["reason"]
    assert str(rollback_file) in str(exc_info.value)


def test_blocked_by_wiring_failure_main_exit_code_is_three(
    monkeypatch, patch_jira, template_path, meta_desc_path,
) -> None:
    def explode(*args, **kwargs):  # noqa: ARG001
        raise RuntimeError("simulated JIRA 500 on issueLink")

    monkeypatch.setattr(file_coordinator, "add_blocked_by", explode)
    monkeypatch.setattr(engine.jira_dispatch, "make_client", lambda cls: _fake_client())
    rc = engine.main([
        "--version", "v0.5.1-rc1",
        "--template", str(template_path),
        "--meta-description-template", str(meta_desc_path),
    ])
    assert rc == engine.EXIT_WIRING_FAILED == 3


# ── Test 7: child label drift detector ───────────────────────────────


def test_child_label_drift_detector_matches_template(template_path) -> None:
    """Every rendered child label set must include the canonical labels.

    Drift catcher: if someone changes the template or the label
    renderer, this test surfaces it as a concrete diff instead of a
    silent prod regression.
    """
    template = engine.load_template(template_path)
    version = "v0.5.1-rc1"
    expected_required = {"agent:auto", f"RELEASE-{version}"}
    for child in template.children:
        labels = set(engine.render_child_labels(child, version))
        assert expected_required.issubset(labels), (
            f"{child.r_id} missing required labels: "
            f"expected {expected_required}, got {labels}"
        )
        assert f"release-child:{child.r_id}" in labels
        assert f"class:{child.agent_class}" in labels
        assert f"tier:{child.tier}" in labels
        for area in child.areas:
            assert f"area:{area}" in labels
        # No duplicates and no untagged area labels.
        assert len(labels) == len(engine.render_child_labels(child, version))


def test_default_template_has_exactly_thirteen_children(template_path) -> None:
    template = engine.load_template(template_path)
    assert len(template.children) == 13
    assert [c.r_id for c in template.children] == [f"R{i}" for i in range(1, 14)]


def test_default_template_blocked_by_chain_is_sequential(template_path) -> None:
    template = engine.load_template(template_path)
    assert template.children[0].blocked_by == ()  # R1 has no blocker
    for idx, child in enumerate(template.children[1:], start=2):
        assert child.blocked_by == (f"R{idx-1}",), (
            f"{child.r_id} expected blocked_by=('R{idx-1}',), got {child.blocked_by}"
        )


# ── Plan / formatting helpers ────────────────────────────────────────


def test_build_plan_records_twelve_blockedby_edges(template_path) -> None:
    template = engine.load_template(template_path)
    plan = engine.build_plan(template, "v0.5.1-rc1")
    assert len(plan.blockedby_edges) == 12
    assert plan.blockedby_edges[0] == ("R2", "R1")
    assert plan.blockedby_edges[-1] == ("R13", "R12")


def test_version_regex_accepts_rc_and_stable() -> None:
    assert engine.SEMVER_VERSION_RE.fullmatch("v0.5.1")
    assert engine.SEMVER_VERSION_RE.fullmatch("v0.5.1-rc1")
    assert engine.SEMVER_VERSION_RE.fullmatch("v0.5.1-beta2")
    assert engine.SEMVER_VERSION_RE.fullmatch("v1.0.0-alpha10")
    assert not engine.SEMVER_VERSION_RE.fullmatch("0.5.1")
    assert not engine.SEMVER_VERSION_RE.fullmatch("v0.5")
    assert not engine.SEMVER_VERSION_RE.fullmatch("v0.5.1-snapshot")


def test_render_plan_text_lists_relates_and_blocked_by(template_path) -> None:
    template = engine.load_template(template_path)
    plan = engine.build_plan(template, "v0.5.1-rc1")
    text = engine.render_plan_text(plan)
    assert "blockedBy edges" in text
    assert "Relates edges" in text
    # Operator can grep dry-run output for the exact link wiring.
    assert "R13 blockedBy R12" in text
    assert "R1 relates META" in text
    assert text.count(re.escape("v0.5.1-rc1")) or "v0.5.1-rc1" in text
