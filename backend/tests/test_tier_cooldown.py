"""OP-807 (G5) -- per-agent Tier-classification cooldown tests.

Locks the four-layer protection's layer-4 contract end-to-end:

* Migration 0205 produces the right SQLite shape for both the
  observation log and the per-agent state row.
* ``record_classification`` writes one observation row.
* ``run_daily_sweep`` triggers a 30-day cooldown for an agent that
  exceeds the threshold of >3 misclassifications in 30 days.
* The 30→90 day escalation rule fires when an agent enters cooldown
  twice in a 90-day window.
* The 90→revoked escalation rule fires when an agent enters cooldown
  three times in a 365-day window.
* ``apply_cooldown_to_tier`` force-promotes Tier S → Tier M while the
  cooldown is active; expired cooldowns no longer promote.
* The synthetic AC-fixture (4 misclassifications by ``test-bot``) ends
  with a state row at level 30d AND a label_callback invocation,
  matching the ticket's ``[ ] Synthetic: trigger 4 misclassifications
  ... -> assert tier-cooldown:30d label appears + classifier returns
  'm' or higher`` line.

Module-global state audit
-------------------------
Every test creates its own ``tmp_path`` SQLite file and points
``sqlite_path=`` keyword args at it (no monkey-patched globals, no
process-wide DSN). Pytest workers therefore share nothing.
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_0205 = BACKEND_ROOT / "alembic" / "versions" / "0205_tier_cooldown.py"

from backend.governance.tier_cooldown import (  # noqa: E402
    THRESHOLD_30D_OVERRIDES,
    THRESHOLD_90D_COOLDOWNS,
    THRESHOLD_365D_COOLDOWNS,
    WINDOW_30D_DAYS,
    WINDOW_90D_DAYS,
    ClassificationRecord,
    apply_cooldown_to_tier,
    evaluate_agent,
    fetch_overrides_in_window,
    get_state,
    record_classification,
    run_daily_sweep,
    upsert_state,
    CooldownState,
)


# ── Test infrastructure ─────────────────────────────────────────────


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "_alembic_0205", MIGRATION_0205,
    )
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["_alembic_0205"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _bootstrap(db_path: Path) -> None:
    """Apply the SQLite arm of migration 0205 to a fresh DB file."""
    m = _load_migration()
    with sqlite3.connect(str(db_path)) as conn:
        conn.executescript(m._SQLITE_OBSERVATION_DDL)
        conn.executescript(m._SQLITE_OBSERVATION_INDEX_AGENT_DATE)
        conn.executescript(m._SQLITE_OBSERVATION_INDEX_DATE)
        conn.executescript(m._SQLITE_STATE_DDL)
        conn.executescript(m._SQLITE_STATE_INDEX_LEVEL)


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "tier_cooldown.db"
    _bootstrap(p)
    return p


# ── Migration shape tests ───────────────────────────────────────────


class TestMigrationShape:
    def test_revision_metadata(self) -> None:
        source = MIGRATION_0205.read_text()
        assert 'revision = "0205"' in source
        assert 'down_revision = "0204"' in source

    def test_observation_columns(self, db_path: Path) -> None:
        with sqlite3.connect(str(db_path)) as conn:
            cols = {
                row[1]: row[2]
                for row in conn.execute("PRAGMA table_info(tier_cooldown_observation)")
            }
        # Mandatory columns the ticket spec calls out.
        for required in (
            "agent_id", "classification_date",
            "computed_tier", "override_tier",
        ):
            assert required in cols, f"missing column {required}"

    def test_state_columns(self, db_path: Path) -> None:
        with sqlite3.connect(str(db_path)) as conn:
            cols = {
                row[1]: row[2]
                for row in conn.execute("PRAGMA table_info(tier_cooldown_state)")
            }
        for required in (
            "agent_id", "cooldown_level", "cooldown_started_at",
            "cooldown_expires_at", "last_evaluation_at", "cooldown_history",
        ):
            assert required in cols, f"missing column {required}"

    def test_pg_branch_uses_jsonb_history(self) -> None:
        m = _load_migration()
        assert "JSONB NOT NULL DEFAULT '[]'::jsonb" in m._PG_STATE_DDL
        # The check constraints lock the cooldown level enum to the
        # documented set so a typo in a writer can't fork the state.
        assert (
            "CHECK (cooldown_level IN ('none','30d','90d','revoked'))"
            in m._PG_STATE_DDL
        )

    def test_observation_check_constraints(self) -> None:
        m = _load_migration()
        for ddl in (m._PG_OBSERVATION_DDL, m._SQLITE_OBSERVATION_DDL):
            assert "CHECK (computed_tier IN ('s','m','l','x'))" in ddl
            assert "CHECK (override_tier IN ('s','m','l','x'))" in ddl


# ── Misclassification detection ─────────────────────────────────────


class TestClassificationRecord:
    def test_promotion_is_misclassification(self) -> None:
        rec = ClassificationRecord(
            agent_id="test-bot",
            classification_date=date(2026, 5, 1),
            computed_tier="s",
            override_tier="m",
        )
        assert rec.is_misclassification() is True

    def test_same_tier_is_not_misclassification(self) -> None:
        rec = ClassificationRecord(
            agent_id="test-bot",
            classification_date=date(2026, 5, 1),
            computed_tier="m",
            override_tier="m",
        )
        assert rec.is_misclassification() is False

    def test_demotion_is_not_misclassification(self) -> None:
        # Reviewer monotonicity (layer 3) should make this impossible
        # in production, but the data model stays robust to the case.
        rec = ClassificationRecord(
            agent_id="test-bot",
            classification_date=date(2026, 5, 1),
            computed_tier="l",
            override_tier="m",
        )
        assert rec.is_misclassification() is False


# ── record_classification + reader round-trip ──────────────────────


class TestRecordRoundTrip:
    def test_insert_and_fetch(self, db_path: Path) -> None:
        rec = ClassificationRecord(
            agent_id="test-bot",
            classification_date=date(2026, 5, 1),
            computed_tier="s",
            override_tier="l",
            ticket="OP-9001",
            change_id="I" + "a" * 40,
            patchset=1,
        )
        assert record_classification(rec, sqlite_path=str(db_path)) is True

        rows = fetch_overrides_in_window(
            agent_id="test-bot",
            since=date(2026, 4, 1),
            until=date(2026, 6, 1),
            sqlite_path=str(db_path),
        )
        assert len(rows) == 1
        assert rows[0].agent_id == "test-bot"
        assert rows[0].computed_tier == "s"
        assert rows[0].override_tier == "l"
        assert rows[0].is_misclassification() is True

    def test_invalid_tier_rejected(self, db_path: Path) -> None:
        rec = ClassificationRecord(
            agent_id="test-bot",
            classification_date=date(2026, 5, 1),
            computed_tier="s",
            override_tier="ZZZ",
        )
        assert record_classification(rec, sqlite_path=str(db_path)) is False

    def test_window_boundaries_are_half_open(self, db_path: Path) -> None:
        # ``until`` is exclusive; ``since`` is inclusive.
        for d in (date(2026, 5, 1), date(2026, 5, 5), date(2026, 5, 10)):
            record_classification(
                ClassificationRecord(
                    agent_id="bot", classification_date=d,
                    computed_tier="s", override_tier="m",
                ),
                sqlite_path=str(db_path),
            )
        rows = fetch_overrides_in_window(
            agent_id="bot",
            since=date(2026, 5, 1),
            until=date(2026, 5, 5),
            sqlite_path=str(db_path),
        )
        # 5/1 included, 5/5 excluded, 5/10 excluded.
        assert [r.classification_date for r in rows] == [date(2026, 5, 1)]


# ── Sweep state-machine ─────────────────────────────────────────────


def _seed_misclassifications(db_path: Path, agent: str, n: int, base: date) -> None:
    """Insert ``n`` distinct misclassification rows for ``agent``."""
    for i in range(n):
        record_classification(
            ClassificationRecord(
                agent_id=agent,
                classification_date=base + timedelta(days=i),
                computed_tier="s",
                override_tier="m",
                ticket=f"OP-FIX-{i}",
            ),
            sqlite_path=str(db_path),
        )


class TestSweepStateMachine:
    def test_below_threshold_no_cooldown(self, db_path: Path) -> None:
        now = datetime(2026, 5, 9, 8, 0, tzinfo=timezone.utc)
        _seed_misclassifications(
            db_path, "test-bot", THRESHOLD_30D_OVERRIDES, now.date() - timedelta(days=5),
        )
        outcomes = run_daily_sweep(now=now, sqlite_path=str(db_path))
        assert len(outcomes) == 1
        assert outcomes[0].misclass_30d == THRESHOLD_30D_OVERRIDES
        assert outcomes[0].new_level == "none"
        assert outcomes[0].transitioned is False

    def test_above_threshold_triggers_30d(self, db_path: Path) -> None:
        now = datetime(2026, 5, 9, 8, 0, tzinfo=timezone.utc)
        # Spec says "more than 3 overrides" → 4 should be the smallest
        # count that triggers the cooldown.
        _seed_misclassifications(
            db_path, "test-bot",
            THRESHOLD_30D_OVERRIDES + 1,
            now.date() - timedelta(days=5),
        )
        outcomes = run_daily_sweep(now=now, sqlite_path=str(db_path))
        assert outcomes[0].new_level == "30d"
        assert outcomes[0].transitioned is True

        state = get_state("test-bot", sqlite_path=str(db_path))
        assert state.cooldown_level == "30d"
        assert state.cooldown_expires_at is not None
        # Window length must match the constant.
        delta = state.cooldown_expires_at - state.cooldown_started_at
        assert delta == timedelta(days=WINDOW_30D_DAYS)

    def test_repeat_in_90d_escalates_to_90d(self, db_path: Path) -> None:
        agent = "test-bot"
        # Pre-seed a previous 30-day cooldown 30 days ago in history so
        # the second trigger is the SECOND cooldown in the trailing 90d.
        prior_started = datetime(2026, 4, 9, 8, 0, tzinfo=timezone.utc)
        prior_history = [{
            "started_at": prior_started.isoformat(),
            "level": "30d",
            "misclass_count": THRESHOLD_30D_OVERRIDES + 1,
        }]
        upsert_state(
            CooldownState(
                agent_id=agent,
                cooldown_level="none",
                cooldown_history=prior_history,
            ),
            sqlite_path=str(db_path),
        )

        # New misclassification streak 5 days ago.
        now = datetime(2026, 5, 9, 8, 0, tzinfo=timezone.utc)
        _seed_misclassifications(
            db_path, agent,
            THRESHOLD_30D_OVERRIDES + 1,
            now.date() - timedelta(days=5),
        )
        outcome = evaluate_agent(agent, now=now, sqlite_path=str(db_path))
        assert outcome.cooldowns_in_90d == 1  # the prior one
        assert outcome.new_level == "90d", (
            f"second cooldown in 90d should escalate to 90d, got {outcome.new_level}"
        )

        state = get_state(agent, sqlite_path=str(db_path))
        assert state.cooldown_level == "90d"
        delta = state.cooldown_expires_at - state.cooldown_started_at
        assert delta == timedelta(days=WINDOW_90D_DAYS)

    def test_third_in_year_promotes_to_revoked(self, db_path: Path) -> None:
        agent = "test-bot"
        # Two prior cooldowns within the past year -- one at -200d, one
        # at -100d. The new trigger today makes it the third.
        now = datetime(2026, 5, 9, 8, 0, tzinfo=timezone.utc)
        history = [
            {"started_at": (now - timedelta(days=200)).isoformat(), "level": "30d",
             "misclass_count": 4},
            {"started_at": (now - timedelta(days=100)).isoformat(), "level": "90d",
             "misclass_count": 5},
        ]
        upsert_state(
            CooldownState(
                agent_id=agent, cooldown_level="none",
                cooldown_history=history,
            ),
            sqlite_path=str(db_path),
        )
        _seed_misclassifications(
            db_path, agent, THRESHOLD_30D_OVERRIDES + 1,
            now.date() - timedelta(days=5),
        )
        outcome = evaluate_agent(agent, now=now, sqlite_path=str(db_path))
        assert outcome.cooldowns_in_365d == 2
        assert outcome.new_level == "revoked"
        state = get_state(agent, sqlite_path=str(db_path))
        assert state.cooldown_level == "revoked"
        # Revoked agents have no automatic expiry — operator must lift.
        assert state.cooldown_expires_at is None

    def test_active_cooldown_not_re_triggered(self, db_path: Path) -> None:
        agent = "test-bot"
        now = datetime(2026, 5, 9, 8, 0, tzinfo=timezone.utc)
        # Agent is already mid-cooldown (started 5 days ago, 25 days
        # left to run). A new sweep with no fresh misclassifications
        # should NOT step the level up.
        upsert_state(
            CooldownState(
                agent_id=agent,
                cooldown_level="30d",
                cooldown_started_at=now - timedelta(days=5),
                cooldown_expires_at=now + timedelta(days=25),
                cooldown_history=[
                    {"started_at": (now - timedelta(days=5)).isoformat(),
                     "level": "30d", "misclass_count": 4},
                ],
            ),
            sqlite_path=str(db_path),
        )
        outcome = evaluate_agent(agent, now=now, sqlite_path=str(db_path))
        assert outcome.previous_level == "30d"
        assert outcome.new_level == "30d"
        assert outcome.transitioned is False

    def test_expired_cooldown_steps_down(self, db_path: Path) -> None:
        agent = "test-bot"
        now = datetime(2026, 5, 9, 8, 0, tzinfo=timezone.utc)
        upsert_state(
            CooldownState(
                agent_id=agent,
                cooldown_level="30d",
                cooldown_started_at=now - timedelta(days=31),
                cooldown_expires_at=now - timedelta(days=1),  # expired
                cooldown_history=[
                    {"started_at": (now - timedelta(days=31)).isoformat(),
                     "level": "30d", "misclass_count": 4},
                ],
            ),
            sqlite_path=str(db_path),
        )
        outcome = evaluate_agent(agent, now=now, sqlite_path=str(db_path))
        assert outcome.new_level == "none"
        # History row stays so the next trigger can still escalate.
        state = get_state(agent, sqlite_path=str(db_path))
        assert len(state.cooldown_history) == 1


# ── Classifier integration hook ─────────────────────────────────────


class TestApplyCooldownToTier:
    def test_no_cooldown_pass_through(self, db_path: Path) -> None:
        result = apply_cooldown_to_tier(
            "fresh-bot", "s", sqlite_path=str(db_path),
        )
        assert result == "s"

    def test_30d_cooldown_promotes_s_to_m(self, db_path: Path) -> None:
        agent = "test-bot"
        now = datetime(2026, 5, 9, 8, 0, tzinfo=timezone.utc)
        upsert_state(
            CooldownState(
                agent_id=agent,
                cooldown_level="30d",
                cooldown_started_at=now - timedelta(days=2),
                cooldown_expires_at=now + timedelta(days=28),
            ),
            sqlite_path=str(db_path),
        )
        # Tier S becomes M; higher tiers untouched.
        assert apply_cooldown_to_tier(agent, "s", now=now, sqlite_path=str(db_path)) == "m"
        assert apply_cooldown_to_tier(agent, "m", now=now, sqlite_path=str(db_path)) == "m"
        assert apply_cooldown_to_tier(agent, "l", now=now, sqlite_path=str(db_path)) == "l"
        assert apply_cooldown_to_tier(agent, "x", now=now, sqlite_path=str(db_path)) == "x"

    def test_revoked_always_promotes_to_m(self, db_path: Path) -> None:
        agent = "broken-bot"
        upsert_state(
            CooldownState(
                agent_id=agent,
                cooldown_level="revoked",
            ),
            sqlite_path=str(db_path),
        )
        # Even Tier S → M; Tier M / L / X stay where they are because
        # revoked is a *floor*, not a ceiling.
        assert apply_cooldown_to_tier(agent, "s", sqlite_path=str(db_path)) == "m"
        assert apply_cooldown_to_tier(agent, "m", sqlite_path=str(db_path)) == "m"
        assert apply_cooldown_to_tier(agent, "x", sqlite_path=str(db_path)) == "x"

    def test_expired_cooldown_no_longer_promotes(self, db_path: Path) -> None:
        agent = "test-bot"
        now = datetime(2026, 5, 9, 8, 0, tzinfo=timezone.utc)
        upsert_state(
            CooldownState(
                agent_id=agent,
                cooldown_level="30d",
                cooldown_started_at=now - timedelta(days=31),
                cooldown_expires_at=now - timedelta(days=1),
            ),
            sqlite_path=str(db_path),
        )
        # The state row hasn't been swept yet, but the classifier still
        # respects the expiry timestamp -- so a fresh PS classification
        # is not penalised by a stale-but-expired cooldown.
        assert apply_cooldown_to_tier(
            agent, "s", now=now, sqlite_path=str(db_path),
        ) == "s"


# ── Synthetic AC fixture: 4 misclass by test-bot → label + classifier ─


class TestSyntheticAcFixture:
    """Mirror of the AC line:
       'Synthetic: trigger 4 misclassifications by `test-bot` in test
        fixtures → cron run → assert `tier-cooldown:30d` label appears
        + classifier returns "m" or higher for any of test-bot's future
        tickets'.
    """

    def test_full_flow(self, db_path: Path) -> None:
        agent = "test-bot"
        now = datetime(2026, 5, 9, 8, 0, tzinfo=timezone.utc)

        # 1) Trigger 4 misclassifications (the smallest count strictly
        #    greater than the spec's threshold of 3).
        _seed_misclassifications(
            db_path, agent, 4, now.date() - timedelta(days=5),
        )

        # 2) Cron run. Capture the label callback so we can assert that
        #    the cron WOULD have written the JIRA label without
        #    needing a real JIRA connection in the test environment.
        recorded_labels: list[tuple[str, str]] = []

        def recorder(agent_id: str, level: str, ticket: str | None) -> None:
            recorded_labels.append((agent_id, level))

        outcomes = run_daily_sweep(
            now=now, label_callback=recorder, sqlite_path=str(db_path),
        )

        # 3) The state row must show level=30d.
        assert any(
            o.agent_id == agent and o.new_level == "30d" and o.transitioned
            for o in outcomes
        ), f"sweep outcomes={outcomes}"
        state = get_state(agent, sqlite_path=str(db_path))
        assert state.cooldown_level == "30d"

        # 4) The cron must have invoked the label callback with the
        #    canonical `tier-cooldown:30d` payload (the cron's
        #    callback formats the label `tier-cooldown:<level>`).
        assert (agent, "30d") in recorded_labels

        # 5) Classifier returns 'm' for any future Tier S call.
        assert apply_cooldown_to_tier(
            agent, "s", now=now, sqlite_path=str(db_path),
        ) == "m"
        # And 'l' / 'x' calls remain unaffected (cooldown is a floor at M).
        assert apply_cooldown_to_tier(
            agent, "l", now=now, sqlite_path=str(db_path),
        ) == "l"


# ── Threshold constants are wired to ADR-0005 §4 layer 4 ───────────


class TestThresholdConstants:
    def test_constants_match_adr(self) -> None:
        # ADR-0005 §4 layer 4: > 3 overrides in 30d, 2 cooldowns in 90d
        # promotes to 90d, 3 cooldowns in 365d revokes. Pinned here so
        # an accidental tweak in the module fails this test.
        assert THRESHOLD_30D_OVERRIDES == 3
        assert THRESHOLD_90D_COOLDOWNS == 2
        assert THRESHOLD_365D_COOLDOWNS == 3
        assert WINDOW_30D_DAYS == 30
        assert WINDOW_90D_DAYS == 90
