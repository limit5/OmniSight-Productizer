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
    monkeypatch.setattr(
        engine.jira_dispatch,
        "make_client",
        lambda cls, instance_id=None: _fake_client(),
    )
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
    monkeypatch.setattr(
        engine.jira_dispatch,
        "make_client",
        lambda cls, instance_id=None: _fake_client(),
    )
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


# ─────────────────────────────────────────────────────────────────────
# OP-938 (G2) — Idempotency guard extension + release_status.py
# ─────────────────────────────────────────────────────────────────────


# Load release_status.py the same way we load the engine — it sits
# outside the backend.* package tree.
_STATUS_PATH = REPO_ROOT / "scripts" / "release_status.py"
_status_spec = importlib.util.spec_from_file_location(
    "release_status", _STATUS_PATH,
)
release_status = importlib.util.module_from_spec(_status_spec)
sys.modules["release_status"] = release_status
_status_spec.loader.exec_module(release_status)  # type: ignore[union-attr]


# ── G2 fixture: stub /search/jql at the jira_dispatch._request seam ──


@pytest.fixture
def stub_jql(monkeypatch):
    """Return a recorder that controls every JIRA /search/jql response.

    Tests append callable(jql, fields, max_results) → response dict
    entries to ``state["responses"]``. Each call pops the next handler;
    if the list is empty the test fails loudly so we never silently
    fall back to a generic empty payload.
    """
    state = {
        "responses": [],  # list[Callable[(jql, fields, max_results)] -> dict]
        "calls": [],      # list[(jql, fields, max_results)]
    }

    def fake_request(client, method, path, body=None, idem_key=None):  # noqa: ARG001
        if path != "/search/jql" or method != "POST":
            raise AssertionError(
                f"unexpected request in stub_jql: {method} {path}"
            )
        jql = (body or {}).get("jql", "")
        fields = (body or {}).get("fields", [])
        max_results = (body or {}).get("maxResults", 0)
        state["calls"].append((jql, list(fields), max_results))
        if not state["responses"]:
            raise AssertionError(
                f"stub_jql ran out of responses; received call jql={jql!r}"
            )
        handler = state["responses"].pop(0)
        return handler(jql, list(fields), max_results)

    monkeypatch.setattr(jira_dispatch, "_request", fake_request)
    return state


def _issue_record(key: str, status: str, labels: list[str]) -> dict:
    """Shape one JQL `issues[]` entry as the JIRA REST API returns it."""
    return {
        "key": key,
        "fields": {
            "summary": f"summary for {key}",
            "status": {"name": status},
            "labels": labels,
        },
    }


# ── G2.1 — duplicate META refused (RELEASE label match) ─────────────


def test_g2_duplicate_release_meta_refused(
    stub_jql, template_path, meta_desc_path,
) -> None:
    """``find_existing_meta_matches`` must surface a RELEASE-label match."""
    stub_jql["responses"] = [
        lambda jql, fields, mr: {  # noqa: ARG005
            "issues": [_issue_record(
                "OP-9999", "In Progress",
                ["meta:release", "RELEASE-v0.5.1-rc1"],
            )],
        },
        lambda jql, fields, mr: {"issues": []},  # noqa: ARG005 — HOTFIX scan
    ]
    template = engine.load_template(template_path)
    with pytest.raises(engine.MetaAlreadyExists) as exc_info:
        engine.apply(
            _fake_client(),
            template,
            "v0.5.1-rc1",
            template_path,
            meta_desc_path,
        )
    assert "OP-9999" in str(exc_info.value)


# ── G2.2 — duplicate HOTFIX META refused (HOTFIX-{version}+* match) ──


def test_g2_duplicate_hotfix_meta_refused(
    stub_jql, template_path, meta_desc_path,
) -> None:
    """A pre-existing HOTFIX-{version}+N META must block re-creation.

    The JQL pre-check covers RELEASE-{version} AND HOTFIX-{version}+*,
    so an in-flight hotfix for the same version stops a duplicate
    RELEASE META from being created.
    """
    stub_jql["responses"] = [
        lambda jql, fields, mr: {"issues": []},  # noqa: ARG005 — RELEASE
        lambda jql, fields, mr: {  # noqa: ARG005 — HOTFIX scan
            "issues": [_issue_record(
                "OP-7777", "In Progress",
                ["meta:release", "HOTFIX-v0.5.1-rc1+1"],
            )],
        },
    ]
    template = engine.load_template(template_path)
    with pytest.raises(engine.MetaAlreadyExists) as exc_info:
        engine.apply(
            _fake_client(),
            template,
            "v0.5.1-rc1",
            template_path,
            meta_desc_path,
        )
    assert "OP-7777" in str(exc_info.value)


# ── G2.3 — archived + --force allowed ───────────────────────────────


def test_g2_archived_meta_with_force_allows_recreation(
    stub_jql, monkeypatch, template_path, meta_desc_path, tmp_path,
) -> None:
    """An Archived META + ``--force`` is the explicit recovery path."""
    monkeypatch.setattr(engine, "ROLLBACK_DIR", tmp_path)

    issue_seq = {"n": 1000}

    def fake_create_issue(client, *, summary, description_markdown, labels, issuetype="Story"):  # noqa: ARG001
        issue_seq["n"] += 1
        return f"OP-{issue_seq['n']}"

    monkeypatch.setattr(engine, "create_issue", fake_create_issue)
    monkeypatch.setattr(
        file_coordinator, "add_blocked_by",
        lambda *a, **kw: True,
    )
    monkeypatch.setattr(
        file_coordinator, "jira_link_exists",
        lambda *a, **kw: False,
    )
    monkeypatch.setattr(
        file_coordinator, "jira_create_issue_link",
        lambda *a, **kw: None,
    )
    monkeypatch.setattr(jira_dispatch, "add_comment", lambda *a, **kw: None)

    stub_jql["responses"] = [
        lambda jql, fields, mr: {  # noqa: ARG005 — RELEASE
            "issues": [_issue_record(
                "OP-9999", "Archived",
                ["meta:release", "RELEASE-v0.5.1-rc1"],
            )],
        },
        lambda jql, fields, mr: {"issues": []},  # noqa: ARG005 — HOTFIX
    ]

    template = engine.load_template(template_path)
    created = engine.apply(
        _fake_client(),
        template,
        "v0.5.1-rc1",
        template_path,
        meta_desc_path,
        force=True,
    )
    assert set(created.keys()) == {"META"} | {f"R{i}" for i in range(1, 14)}


# ── G2.4 — JQLQueryFailed → fail closed, never create META ──────────


def test_g2_jql_query_failed_refuses_to_create(
    stub_jql, template_path, meta_desc_path,
) -> None:
    """Any /search/jql failure must become JQLQueryFailed; no write."""
    def explode(jql, fields, mr):  # noqa: ARG001
        raise RuntimeError("simulated JIRA 500 on /search/jql")

    stub_jql["responses"] = [explode]
    template = engine.load_template(template_path)
    with pytest.raises(engine.JQLQueryFailed) as exc_info:
        engine.apply(
            _fake_client(),
            template,
            "v0.5.1-rc1",
            template_path,
            meta_desc_path,
        )
    assert "simulated JIRA 500" in str(exc_info.value)
    # find_existing only got as far as one call before failing.
    assert len(stub_jql["calls"]) == 1


def test_g2_jql_query_failed_main_exit_code_is_four(
    monkeypatch, stub_jql, template_path, meta_desc_path,
) -> None:
    def explode(jql, fields, mr):  # noqa: ARG001
        raise RuntimeError("simulated JIRA 500 on /search/jql")

    stub_jql["responses"] = [explode]
    monkeypatch.setattr(
        engine.jira_dispatch,
        "make_client",
        lambda cls, instance_id=None: _fake_client(),
    )
    rc = engine.main([
        "--version", "v0.5.1-rc1",
        "--template", str(template_path),
        "--meta-description-template", str(meta_desc_path),
    ])
    assert rc == engine.EXIT_JQL_QUERY_FAILED == 4


# ── G2.5 — StateAmbiguous: multiple METAs for same version ──────────


def test_g2_state_ambiguous_alerts_p0(
    stub_jql, template_path, meta_desc_path,
) -> None:
    """Two METAs sharing the version label is a P0; refuse to write."""
    stub_jql["responses"] = [
        lambda jql, fields, mr: {  # noqa: ARG005 — RELEASE label finds 2
            "issues": [
                _issue_record("OP-5001", "In Progress",
                              ["meta:release", "RELEASE-v0.5.1-rc1"]),
                _issue_record("OP-5002", "In Progress",
                              ["meta:release", "RELEASE-v0.5.1-rc1"]),
            ],
        },
        lambda jql, fields, mr: {"issues": []},  # noqa: ARG005 — HOTFIX
    ]
    template = engine.load_template(template_path)
    with pytest.raises(engine.StateAmbiguous) as exc_info:
        engine.apply(
            _fake_client(),
            template,
            "v0.5.1-rc1",
            template_path,
            meta_desc_path,
        )
    msg = str(exc_info.value)
    assert "OP-5001" in msg and "OP-5002" in msg


def test_g2_state_ambiguous_main_exit_code_is_five(
    monkeypatch, stub_jql, template_path, meta_desc_path,
) -> None:
    stub_jql["responses"] = [
        lambda jql, fields, mr: {  # noqa: ARG005
            "issues": [
                _issue_record("OP-5001", "In Progress",
                              ["meta:release", "RELEASE-v0.5.1-rc1"]),
                _issue_record("OP-5002", "In Progress",
                              ["meta:release", "RELEASE-v0.5.1-rc1"]),
            ],
        },
        lambda jql, fields, mr: {"issues": []},  # noqa: ARG005
    ]
    monkeypatch.setattr(
        engine.jira_dispatch,
        "make_client",
        lambda cls, instance_id=None: _fake_client(),
    )
    rc = engine.main([
        "--version", "v0.5.1-rc1",
        "--template", str(template_path),
        "--meta-description-template", str(meta_desc_path),
    ])
    assert rc == engine.EXIT_STATE_AMBIGUOUS == 5


# ── G2.6 — release_status.py happy path ─────────────────────────────


def test_g2_release_status_happy_path(stub_jql, capsys, monkeypatch) -> None:
    """``release_status.py --version v0.5.1-rc1`` reports cursor + counts.

    Layout: R1..R5 Published, R6 In Progress, R7..R13 To Do.
    Cursor should be R6.
    """
    children = []
    for i in range(1, 14):
        if i <= 5:
            status = "Published"
        elif i == 6:
            status = "In Progress"
        else:
            status = "To Do"
        children.append(_issue_record(
            f"OP-{2000 + i}",
            status,
            ["meta:release-child", "RELEASE-v0.5.1-rc1", f"release-child:R{i}"],
        ))

    stub_jql["responses"] = [
        lambda jql, fields, mr: {  # noqa: ARG005 — RELEASE META lookup
            "issues": [_issue_record(
                "OP-1999", "In Progress",
                ["meta:release", "RELEASE-v0.5.1-rc1"],
            )],
        },
        lambda jql, fields, mr: {"issues": []},  # noqa: ARG005 — HOTFIX scan
        lambda jql, fields, mr: {  # noqa: ARG005 — child fetch
            "issues": children,
        },
    ]

    monkeypatch.setattr(
        release_status.jira_dispatch, "make_client",
        lambda cls: _fake_client(),
    )
    rc = release_status.main(["--version", "v0.5.1-rc1"])
    assert rc == release_status.EXIT_OK == 0
    out = capsys.readouterr().out
    assert "Release status — v0.5.1-rc1" in out
    assert "META:           OP-1999" in out
    assert "in progress:    R6" in out
    # Completed list reflects R1..R5; pending reflects R6..R13.
    assert "completed:      R1, R2, R3, R4, R5" in out
    assert "pending:        R6, R7, R8, R9, R10, R11, R12, R13" in out


def test_g2_release_status_happy_path_json(stub_jql, capsys, monkeypatch) -> None:
    children = [
        _issue_record(
            f"OP-{3000 + i}",
            "Published" if i < 7 else ("In Progress" if i == 7 else "To Do"),
            ["meta:release-child", "RELEASE-v0.5.1-rc1", f"release-child:R{i}"],
        )
        for i in range(1, 14)
    ]
    stub_jql["responses"] = [
        lambda jql, fields, mr: {  # noqa: ARG005
            "issues": [_issue_record(
                "OP-2999", "In Progress",
                ["meta:release", "RELEASE-v0.5.1-rc1"],
            )],
        },
        lambda jql, fields, mr: {"issues": []},  # noqa: ARG005
        lambda jql, fields, mr: {"issues": children},  # noqa: ARG005
    ]

    monkeypatch.setattr(
        release_status.jira_dispatch, "make_client",
        lambda cls: _fake_client(),
    )
    rc = release_status.main(["--version", "v0.5.1-rc1", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["version"] == "v0.5.1-rc1"
    assert payload["meta"]["key"] == "OP-2999"
    assert payload["in_progress"] == "R7"
    assert payload["completed"] == [f"R{i}" for i in range(1, 7)]
    assert payload["pending"] == [f"R{i}" for i in range(7, 14)]
    assert payload["child_count"] == 13


# ── G2.7 — release_status.py no-such-version → exit 1 ───────────────


def test_g2_release_status_no_such_version(
    stub_jql, capsys, monkeypatch,
) -> None:
    stub_jql["responses"] = [
        lambda jql, fields, mr: {"issues": []},  # noqa: ARG005 — RELEASE empty
        lambda jql, fields, mr: {"issues": []},  # noqa: ARG005 — HOTFIX empty
    ]
    monkeypatch.setattr(
        release_status.jira_dispatch, "make_client",
        lambda cls: _fake_client(),
    )
    rc = release_status.main(["--version", "v9.9.9"])
    assert rc == release_status.EXIT_NO_SUCH_VERSION == 1
    captured = capsys.readouterr()
    assert "no META exists for 'v9.9.9'" in captured.err


# ── G2.8 — release_status.py JQL outage → exit 2 ────────────────────


def test_g2_release_status_jql_failure_exits_two(
    stub_jql, capsys, monkeypatch,
) -> None:
    def explode(jql, fields, mr):  # noqa: ARG001
        raise RuntimeError("JIRA 503 on /search/jql")

    stub_jql["responses"] = [explode]
    monkeypatch.setattr(
        release_status.jira_dispatch, "make_client",
        lambda cls: _fake_client(),
    )
    rc = release_status.main(["--version", "v0.5.1-rc1"])
    assert rc == release_status.EXIT_JQL_QUERY_FAILED == 2
    err = capsys.readouterr().err
    assert "JQLQueryFailed" in err


# ── G2.9 — release_status.py state ambiguity → exit 3 ───────────────


def test_g2_release_status_state_ambiguous_exits_three(
    stub_jql, capsys, monkeypatch,
) -> None:
    stub_jql["responses"] = [
        lambda jql, fields, mr: {  # noqa: ARG005 — two active RELEASE METAs
            "issues": [
                _issue_record("OP-6001", "In Progress",
                              ["meta:release", "RELEASE-v0.5.1-rc1"]),
                _issue_record("OP-6002", "In Progress",
                              ["meta:release", "RELEASE-v0.5.1-rc1"]),
            ],
        },
        lambda jql, fields, mr: {"issues": []},  # noqa: ARG005 — HOTFIX
    ]
    monkeypatch.setattr(
        release_status.jira_dispatch, "make_client",
        lambda cls: _fake_client(),
    )
    rc = release_status.main(["--version", "v0.5.1-rc1"])
    assert rc == release_status.EXIT_STATE_AMBIGUOUS == 3
    err = capsys.readouterr().err
    assert "OP-6001" in err and "OP-6002" in err


# ── G2.10 — idempotency proven across re-runs ───────────────────────


def test_g2_idempotency_proven_across_reruns(
    stub_jql, monkeypatch, template_path, meta_desc_path, tmp_path,
) -> None:
    """First run creates everything; second run refuses with no writes.

    Wires the same fakes used for the happy path to verify *zero* JIRA
    writes land on the second invocation — proving the JQL guard is
    actually the gate, not a side-effect of the create path.
    """
    monkeypatch.setattr(engine, "ROLLBACK_DIR", tmp_path)

    issue_seq = {"n": 4000}
    created_keys: list[str] = []

    def fake_create_issue(client, *, summary, description_markdown, labels, issuetype="Story"):  # noqa: ARG001
        issue_seq["n"] += 1
        key = f"OP-{issue_seq['n']}"
        created_keys.append(key)
        return key

    blocked_by_calls: list[tuple] = []
    relates_calls: list[tuple] = []

    monkeypatch.setattr(engine, "create_issue", fake_create_issue)
    monkeypatch.setattr(
        file_coordinator, "add_blocked_by",
        lambda *a, **kw: (blocked_by_calls.append((a, kw)) or True),
    )
    monkeypatch.setattr(
        file_coordinator, "jira_link_exists",
        lambda *a, **kw: False,
    )
    monkeypatch.setattr(
        file_coordinator, "jira_create_issue_link",
        lambda *a, **kw: relates_calls.append((a, kw)),
    )
    monkeypatch.setattr(jira_dispatch, "add_comment", lambda *a, **kw: None)

    # Run 1 — nothing exists; expect 14 creates + 12 blockedBy edges.
    stub_jql["responses"] = [
        lambda jql, fields, mr: {"issues": []},  # noqa: ARG005 — RELEASE empty
        lambda jql, fields, mr: {"issues": []},  # noqa: ARG005 — HOTFIX empty
    ]
    template = engine.load_template(template_path)
    created = engine.apply(
        _fake_client(),
        template,
        "v0.5.1-rc1",
        template_path,
        meta_desc_path,
    )
    assert len(created) == 14
    assert len(created_keys) == 14
    assert len(blocked_by_calls) == 12

    # Run 2 — RELEASE label now finds the META created in run 1 →
    # idempotency guard must refuse. No new create_issue calls fire.
    before_keys = list(created_keys)
    before_blocked = list(blocked_by_calls)
    stub_jql["responses"] = [
        lambda jql, fields, mr: {  # noqa: ARG005 — RELEASE finds run-1 META
            "issues": [_issue_record(
                created["META"], "In Progress",
                ["meta:release", "RELEASE-v0.5.1-rc1"],
            )],
        },
        lambda jql, fields, mr: {"issues": []},  # noqa: ARG005 — HOTFIX
    ]
    with pytest.raises(engine.MetaAlreadyExists):
        engine.apply(
            _fake_client(),
            template,
            "v0.5.1-rc1",
            template_path,
            meta_desc_path,
        )
    # Strict invariant: nothing about the second run touched JIRA.
    assert created_keys == before_keys
    assert blocked_by_calls == before_blocked
