"""Phase 63-B — IIS Mitigation Layer."""

from __future__ import annotations

import os
import tempfile

import pytest

from backend import intelligence as iis
from backend import intelligence_mitigation as mit


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fixtures
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.fixture(autouse=True)
def _reset_state():
    iis.reset_for_tests()
    mit._reset_for_tests()
    from backend import decision_engine as de
    de._reset_for_tests()
    yield
    iis.reset_for_tests()
    mit._reset_for_tests()
    de._reset_for_tests()


@pytest.fixture()
async def db_for_audit(monkeypatch):
    """Decision Engine's audit log writes to DB."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "t.db")
        monkeypatch.setenv("OMNISIGHT_DATABASE_PATH", path)
        from backend import config as cfg
        cfg.settings.database_path = path
        from backend import db
        db._DB_PATH = db._resolve_db_path()
        await db.init()
        try:
            yield db
        finally:
            await db.close()


def _drive_alerts(agent_id: str, *, critical: bool = False, warning: bool = False):
    """Push enough records into the IIS window to make `agent_id`
    raise the requested alert level."""
    if critical:
        for _ in range(5):
            iis.record_and_publish(agent_id, code_pass=False)
    elif warning:
        # Ratio under 60% but ≥ 30%: 5 fails, 5 passes → 50%
        for _ in range(5):
            iis.record_and_publish(agent_id, code_pass=False)
        for _ in range(5):
            iis.record_and_publish(agent_id, code_pass=True)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Profile-aware COT length
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.parametrize("strategy,expected", [
    ("cost_saver", 0),
    ("balanced", 200),
    ("sprint", 100),
    ("quality", 500),
])
def test_cot_length_is_profile_aware(monkeypatch, strategy, expected):
    from backend import budget_strategy as bs
    bs._reset_for_tests()
    bs.set_strategy(strategy)
    assert mit.cot_chars_for_current_profile() == expected


def test_cot_falls_back_to_200_on_failure(monkeypatch):
    from backend import budget_strategy as bs
    def boom():
        raise RuntimeError("nope")
    monkeypatch.setattr(bs, "get_strategy", boom)
    assert mit.cot_chars_for_current_profile() == 200


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  map_alerts_to_level
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_map_empty_returns_none():
    assert mit.map_alerts_to_level([]) is None


def test_map_warning_only_is_calibrate():
    alerts = [("warning", "code_pass", "rate 50%")]
    assert mit.map_alerts_to_level(alerts) == "calibrate"


def test_map_critical_wins_over_warning():
    alerts = [
        ("warning", "compliance", "65%"),
        ("critical", "code_pass", "20%"),
    ]
    assert mit.map_alerts_to_level(alerts) == "route"


def test_map_never_returns_contain_directly():
    """Containment requires escalation history; map_alerts_to_level
    must NEVER produce it from a single snapshot."""
    alerts = [("critical", "code_pass", "x"), ("critical", "compliance", "y")]
    assert mit.map_alerts_to_level(alerts) == "route"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  propose_for_agent — happy paths per tier
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_no_alerts_returns_none(db_for_audit):
    assert await mit.propose_for_agent("a-quiet") is None


@pytest.mark.asyncio
async def test_warning_files_calibrate_proposal(db_for_audit):
    from backend import decision_engine as de

    _drive_alerts("a-warn", warning=True)
    dec_id = await mit.propose_for_agent("a-warn", current_model="claude")
    assert dec_id is not None
    dec = de.get(dec_id)
    assert dec.kind == "intelligence/calibrate"
    assert dec.severity == de.DecisionSeverity.routine
    assert dec.default_option_id == "calibrate"
    assert {o["id"] for o in dec.options} == {"calibrate", "skip"}


@pytest.mark.asyncio
async def test_critical_files_route_proposal(db_for_audit):
    from backend import decision_engine as de

    _drive_alerts("a-crit", critical=True)
    dec_id = await mit.propose_for_agent("a-crit", current_model="gpt-4o")
    assert dec_id is not None
    dec = de.get(dec_id)
    assert dec.kind == "intelligence/route"
    assert dec.severity == de.DecisionSeverity.risky
    # Default-safe option is calibrate, NOT switch_model.
    assert dec.default_option_id == "calibrate"
    assert "gpt-4o" in dec.detail


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Dedup
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_second_call_same_level_is_deduped(db_for_audit):
    _drive_alerts("a-dedup", warning=True)
    first = await mit.propose_for_agent("a-dedup")
    assert first is not None
    second = await mit.propose_for_agent("a-dedup")
    assert second is None  # same (agent, calibrate) slot already open


@pytest.mark.asyncio
async def test_different_agents_do_not_dedup_each_other(db_for_audit):
    _drive_alerts("a-one", warning=True)
    _drive_alerts("a-two", warning=True)
    d1 = await mit.propose_for_agent("a-one")
    d2 = await mit.propose_for_agent("a-two")
    assert d1 is not None and d2 is not None
    assert d1 != d2


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Escalation route → contain
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_critical_after_open_route_escalates_to_contain(db_for_audit):
    from backend import decision_engine as de

    _drive_alerts("a-esc", critical=True)
    first = await mit.propose_for_agent("a-esc")
    assert de.get(first).kind == "intelligence/route"

    # Second critical while route is still open → contain.
    second = await mit.propose_for_agent("a-esc")
    assert second is not None
    assert second != first
    assert de.get(second).kind == "intelligence/contain"
    assert de.get(second).severity == de.DecisionSeverity.destructive


@pytest.mark.asyncio
async def test_resolution_callback_frees_dedup_slot(db_for_audit):
    _drive_alerts("a-resolve", warning=True)
    first = await mit.propose_for_agent("a-resolve")
    assert first is not None

    # Without resolution → still deduped.
    assert await mit.propose_for_agent("a-resolve") is None

    # After explicit free → next call may file again.
    mit.on_decision_resolved("a-resolve", "calibrate")
    second = await mit.propose_for_agent("a-resolve")
    assert second is not None
    assert second != first


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  L3 side-effect: notification fires (Jira gated by env)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_contain_emits_critical_notification(db_for_audit, monkeypatch):
    sent = []

    async def fake_notify(level, title, message="", source="", **kw):
        sent.append({"level": level, "title": title, "source": source})
        # Stand-in object (callers don't inspect deeply).
        return type("N", (), {"id": "n", "level": level})()

    from backend import notifications as _n
    monkeypatch.setattr(_n, "notify", fake_notify)

    _drive_alerts("a-contain", critical=True)
    await mit.propose_for_agent("a-contain")  # L2 route
    await mit.propose_for_agent("a-contain")  # escalates to L3 contain

    crit = [s for s in sent if s["level"] == "critical"]
    assert crit, f"expected a critical notification, got {sent}"


@pytest.mark.asyncio
async def test_jira_containment_default_off(db_for_audit, monkeypatch):
    monkeypatch.delenv("OMNISIGHT_IIS_JIRA_CONTAINMENT", raising=False)
    sent = []

    async def fake_notify(level, title, message="", source="", **kw):
        sent.append({"title": title})
        return type("N", (), {"id": "n", "level": level})()

    from backend import notifications as _n
    monkeypatch.setattr(_n, "notify", fake_notify)

    _drive_alerts("a-no-jira", critical=True)
    await mit.propose_for_agent("a-no-jira")  # L2
    await mit.propose_for_agent("a-no-jira")  # L3

    # Should see the critical notification but NOT the [IIS-CONTAIN] tagged
    # Jira-style follow-up.
    titles = [s["title"] for s in sent]
    assert any("L3 containment" in t for t in titles)
    assert not any("[IIS-CONTAIN]" in t for t in titles)


@pytest.mark.asyncio
async def test_jira_containment_fires_when_env_true(db_for_audit, monkeypatch):
    monkeypatch.setenv("OMNISIGHT_IIS_JIRA_CONTAINMENT", "true")
    sent = []

    async def fake_notify(level, title, message="", source="", **kw):
        sent.append({"title": title})
        return type("N", (), {"id": "n", "level": level})()

    from backend import notifications as _n
    monkeypatch.setattr(_n, "notify", fake_notify)

    _drive_alerts("a-jira", critical=True)
    await mit.propose_for_agent("a-jira")  # L2
    await mit.propose_for_agent("a-jira")  # L3

    titles = [s["title"] for s in sent]
    assert any("[IIS-CONTAIN]" in t for t in titles)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Snapshot
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_snapshot_reports_open_proposals_and_counts(db_for_audit):
    _drive_alerts("a-snap", warning=True)
    await mit.propose_for_agent("a-snap")
    snap = mit.get_state_snapshot()
    assert ("a-snap", "calibrate") in snap["open_proposals"]
    assert snap["escalation_count"]["a-snap"]["calibrate"] == 1
