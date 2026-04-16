"""O5 (#268) — IntentSource registry + audit helper tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend import intent_source as isrc


@pytest.fixture(autouse=True)
def _reset_registry():
    isrc.reset_registry_for_tests()
    yield
    isrc.reset_registry_for_tests()


# ──────────────────────────────────────────────────────────────
#  Registry
# ──────────────────────────────────────────────────────────────


def test_register_and_get_direct_instance():
    src = SimpleNamespace(vendor="fake")
    isrc.register_source(src)
    assert isrc.get_source("fake") is src
    assert "fake" in isrc.list_vendors()


def test_factory_lazy_init():
    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        return SimpleNamespace(vendor="lazy")

    isrc.register_factory("lazy", factory)
    assert calls["n"] == 0
    inst1 = isrc.get_source("lazy")
    inst2 = isrc.get_source("lazy")
    assert inst1 is inst2       # cached
    assert calls["n"] == 1      # factory called once


def test_get_source_unknown_raises():
    with pytest.raises(KeyError):
        isrc.get_source("nope")


def test_factory_failure_propagates_as_keyerror():
    def bad():
        raise RuntimeError("boom")
    isrc.register_factory("bad", bad)
    with pytest.raises(KeyError):
        isrc.get_source("bad")


def test_default_vendor_prefers_jira():
    isrc.register_source(SimpleNamespace(vendor="github"))
    isrc.register_source(SimpleNamespace(vendor="jira"))
    assert isrc.default_vendor() == "jira"


def test_default_vendor_env_override(monkeypatch):
    isrc.register_source(SimpleNamespace(vendor="github"))
    isrc.register_source(SimpleNamespace(vendor="gitlab"))
    monkeypatch.setenv("OMNISIGHT_INTENT_VENDOR", "gitlab")
    assert isrc.default_vendor() == "gitlab"


# ──────────────────────────────────────────────────────────────
#  Vendor detection
# ──────────────────────────────────────────────────────────────


class TestDetectVendor:
    def test_github_headers(self):
        assert isrc.detect_vendor(
            {"X-GitHub-Event": "issues"}, b"{}",
        ) == "github"

    def test_gitlab_headers(self):
        assert isrc.detect_vendor(
            {"X-Gitlab-Event": "Issue Hook"}, b"{}",
        ) == "gitlab"

    def test_jira_header(self):
        assert isrc.detect_vendor(
            {"X-Jira-Webhook-Secret": "s"}, b"{}",
        ) == "jira"

    def test_jira_body(self):
        import json
        body = json.dumps({"issue": {"key": "PROJ-42"}}).encode()
        assert isrc.detect_vendor({}, body) == "jira"

    def test_gitlab_body(self):
        import json
        body = json.dumps({"object_kind": "issue"}).encode()
        assert isrc.detect_vendor({}, body) == "gitlab"

    def test_unknown(self):
        assert isrc.detect_vendor({}, b"{}") is None


# ──────────────────────────────────────────────────────────────
#  payload_hash — stability + canonicalisation
# ──────────────────────────────────────────────────────────────


class TestPayloadHash:
    def test_bytes(self):
        h1 = isrc.payload_hash(b"hello")
        h2 = isrc.payload_hash(b"hello")
        assert h1 == h2 and len(h1) == 64

    def test_dict_stable_across_key_order(self):
        h1 = isrc.payload_hash({"a": 1, "b": 2})
        h2 = isrc.payload_hash({"b": 2, "a": 1})
        assert h1 == h2

    def test_different_payloads_differ(self):
        assert isrc.payload_hash({"a": 1}) != isrc.payload_hash({"a": 2})

    def test_nested(self):
        obj = {"issue": {"key": "PROJ-1"}, "fields": {"summary": "x"}}
        assert len(isrc.payload_hash(obj)) == 64


# ──────────────────────────────────────────────────────────────
#  SubtaskPayload.from_task_card round-trip
# ──────────────────────────────────────────────────────────────


def test_subtask_payload_from_task_card():
    from backend.catc import TaskCard
    card = TaskCard.from_dict({
        "jira_ticket": "PROJ-1001",
        "acceptance_criteria": "Do the thing",
        "navigation": {
            "entry_point": "src/foo.c",
            "impact_scope": {
                "allowed": ["src/foo/**"],
                "forbidden": ["test_assets/**"],
            },
        },
        "domain_context": "camera-subsystem",
        "handoff_protocol": ["Run tests", "Push to Gerrit"],
    })
    p = isrc.SubtaskPayload.from_task_card(card)
    assert p.title == "PROJ-1001"
    assert p.acceptance_criteria == "Do the thing"
    assert p.impact_scope_allowed == ["src/foo/**"]
    assert p.impact_scope_forbidden == ["test_assets/**"]
    assert p.handoff_protocol == ["Run tests", "Push to Gerrit"]
    assert p.domain_context == "camera-subsystem"


# ──────────────────────────────────────────────────────────────
#  audit_outbound — hashes land in the audit row
# ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_audit_outbound_records_hashes(monkeypatch):
    captured: dict[str, object] = {}

    async def fake_log(**kwargs):
        captured.update(kwargs)
        return 42

    # Patch ``backend.audit.log`` directly — the alternative of replacing
    # ``sys.modules['backend.audit']`` doesn't work once the module is
    # already bound as ``backend.audit`` (`from backend import audit`
    # picks up the existing attribute, not sys.modules).
    from backend import audit as _audit
    monkeypatch.setattr(_audit, "log", fake_log)

    rid = await isrc.audit_outbound(
        vendor="jira", action="create_subtasks", ticket="PROJ-1",
        request={"payload": "rq"}, response={"payload": "rs"},
        status_code=201,
    )
    assert rid == 42
    assert captured["entity_kind"] == "intent_source"
    assert captured["entity_id"] == "jira:PROJ-1"
    assert captured["action"] == "intent_source:jira:create_subtasks"
    assert "request_hash" in captured["before"]
    assert "response_hash" in captured["after"]
    assert captured["after"]["http_status"] == 201
