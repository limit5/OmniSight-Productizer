"""RECRUIT C2 (OP-2509) — DB-backed character registry.

Covers the loader policies from
``docs/architecture/2026-07-02-character-recruit-epic-design.md`` §C2 and the
five runner integration scenarios its AC calls out:

1. DB-down cold start → built-ins only (fail-open, no raise).
2. Stale-if-error → a refresh failure keeps the last-good DB snapshot.
3. DB-only character ticket: pickable while fresh, pickup-denied
   ("registry stale") once the snapshot ages past MAX_STALE_AGE_SECONDS.
4. Retired character: pickup denied, but still resolvable for in-flight
   card/skill/XP finalization.
5. Built-in slug collision: the code constant wins — exact seed copy is
   ignored silently, a divergent row is rejected with a field-naming warning.

Plus the filer's dynamic --character validation (no frozen argparse choices,
explicit offline warning) and the create_task active-only filing gate.
"""
from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

import pytest

import backend.agents.jira_dispatch as jd
from backend.agents import character_registry as cr

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "file_jira_ticket.py"

# A DB-only (recruited) character — valid brain/guild/tier.
_BLAZE_ROW = {
    "slug": "blaze",
    "display_name": "Blaze",
    "brain": "subscription-claude",
    "guild": "backend",
    "max_tier": "M",
    "blurb": "Recruited backend hand.",
    "active": True,
}

# A DB-only character that has been retired (active=False).
_EMBER_ROW = {
    "slug": "ember",
    "display_name": "Ember",
    "brain": "subscription-claude",
    "guild": "backend",
    "max_tier": "M",
    "blurb": "Retired backend hand.",
    "active": False,
}


def _seed_row(slug: str) -> dict:
    """Exact-copy row of a built-in — what the 0253 migration seeds."""
    char = cr.CHARACTERS[slug]
    return {f: getattr(char, f) for f in cr._DB_ROW_FIELDS}


class _Clock:
    def __init__(self, start: float = 1_000.0) -> None:
        self.t = start

    def now(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture(autouse=True)
def _fresh_registry():
    cr._SNAPSHOT = None
    yield
    cr._SNAPSHOT = None


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    c = _Clock()
    monkeypatch.setattr(cr, "_now", c.now)
    return c


def _db_returns(monkeypatch, rows: list[dict]) -> None:
    monkeypatch.setattr(
        cr, "_fetch_character_rows", lambda: [dict(r) for r in rows]
    )


def _db_down(monkeypatch) -> None:
    def _boom() -> list[dict]:
        raise RuntimeError("connection refused (simulated DB outage)")

    monkeypatch.setattr(cr, "_fetch_character_rows", _boom)


def _expire_ttl(clock: _Clock) -> None:
    # TTL is 30s ± 10s jitter; +41s always crosses the next refresh point.
    clock.advance(cr._DB_TTL_BASE_SECONDS + cr._DB_TTL_JITTER_SECONDS + 1)


# ── 1. DB-down cold start = built-ins ─────────────────────────────


def test_cold_start_db_down_serves_builtins_and_never_raises(monkeypatch, clock):
    _db_down(monkeypatch)
    roster = cr.load_characters()
    assert roster == cr.CHARACTERS
    assert cr.registry_db_loaded() is False
    # Resolution + denials all fail-open on the built-ins-only snapshot.
    assert cr.resolve_character("nova").brain == "subscription-claude"
    assert cr.character_from_labels(["character:blaze"]) is None
    assert cr.character_retired_denial_from_labels(
        ["character:blaze", "tier:S"]
    ) is None
    assert cr.character_retired_denial_from_labels(
        ["character:nova", "tier:M"]
    ) is None


def test_cold_start_recovers_once_db_comes_back(monkeypatch, clock):
    _db_down(monkeypatch)
    assert "blaze" not in cr.load_characters()
    _db_returns(monkeypatch, [_BLAZE_ROW])
    _expire_ttl(clock)
    assert "blaze" in cr.load_characters()
    assert cr.registry_db_loaded() is True


# ── 2. Stale-if-error keeps the last-good snapshot ─────────────────


def test_stale_if_error_keeps_last_good_db_snapshot(monkeypatch, clock, caplog):
    _db_returns(monkeypatch, [_BLAZE_ROW])
    assert "blaze" in cr.load_characters()

    _db_down(monkeypatch)
    _expire_ttl(clock)
    with caplog.at_level("WARNING", logger="backend.agents.character_registry"):
        roster = cr.load_characters()

    # NEVER replaced by built-ins after a refresh failure.
    assert "blaze" in roster
    assert cr.registry_db_loaded() is True
    stale_logs = [
        r.getMessage() for r in caplog.records
        if "keeping last-good DB snapshot" in r.getMessage()
    ]
    assert stale_logs and "age=" in stale_logs[0]


def test_refresh_failure_retries_and_recovers_next_ttl(monkeypatch, clock):
    _db_returns(monkeypatch, [_BLAZE_ROW])
    cr.load_characters()
    _db_down(monkeypatch)
    _expire_ttl(clock)
    cr.load_characters()  # failed refresh — snapshot kept
    _db_returns(monkeypatch, [_BLAZE_ROW, _EMBER_ROW])
    _expire_ttl(clock)
    assert "ember" in cr.load_characters(include_retired=True)


# ── 3. DB-only character: pickable fresh, denied when stale ───────


def test_db_only_character_pickable_fresh_denied_past_max_stale_age(
    monkeypatch, clock
):
    _db_returns(monkeypatch, [_BLAZE_ROW])
    labels = ["character:blaze", "tier:S", "class:subscription-claude"]
    assert cr.character_retired_denial_from_labels(labels) is None  # fresh

    _db_down(monkeypatch)
    clock.advance(cr.MAX_STALE_AGE_SECONDS + 60)
    reason = cr.character_retired_denial_from_labels(labels)
    assert reason is not None and "registry stale" in reason and "blaze" in reason

    # Built-ins are never stale-denied; in-flight resolution of the DB-only
    # character still works off the stale snapshot (finalization > lost XP).
    assert cr.character_retired_denial_from_labels(
        ["character:nova", "tier:M"]
    ) is None
    assert cr.resolve_character("blaze").slug == "blaze"
    assert cr.character_from_labels(labels).slug == "blaze"


def test_successful_refresh_resets_stale_age(monkeypatch, clock):
    _db_returns(monkeypatch, [_BLAZE_ROW])
    cr.load_characters()
    clock.advance(cr.MAX_STALE_AGE_SECONDS + 60)  # TTL long expired too
    # DB is healthy at re-check → refresh succeeds → no stale denial.
    assert cr.character_retired_denial_from_labels(
        ["character:blaze", "tier:S"]
    ) is None


# ── 4. Retired: pickup denied, still resolvable ────────────────────


def test_retired_character_denied_for_pickup_but_resolvable(monkeypatch, clock):
    _db_returns(monkeypatch, [_EMBER_ROW])
    labels = ["character:ember", "tier:S"]

    reason = cr.character_retired_denial_from_labels(labels)
    assert reason is not None and "retired" in reason and "ember" in reason

    # In-flight resolution paths (card identity, skill/XP finalization).
    assert cr.resolve_character("ember").active is False
    assert cr.character_from_labels(labels).slug == "ember"
    # Tier ceiling still enforced through the retired-inclusive path.
    assert cr.character_tier_denial_from_labels(
        ["character:ember", "tier:X"]
    ) is not None

    # Filing view sees active only.
    assert "ember" not in cr.load_characters()
    assert "ember" in cr.load_characters(include_retired=True)


# ── 5. Built-in collision: code constant wins ──────────────────────


def test_exact_seed_copies_are_ignored_silently(monkeypatch, clock, caplog):
    _db_returns(monkeypatch, [_seed_row(s) for s in sorted(cr.CHARACTERS)])
    with caplog.at_level("WARNING", logger="backend.agents.character_registry"):
        roster = cr.load_characters(include_retired=True)
    assert roster == cr.CHARACTERS
    assert not [r for r in caplog.records if "diverges" in r.getMessage()]


def test_divergent_builtin_row_rejected_with_field_naming_warning(
    monkeypatch, clock, caplog
):
    hijack = dict(_seed_row("nova"), display_name="Nova Prime", max_tier="X")
    _db_returns(monkeypatch, [hijack])
    with caplog.at_level("WARNING", logger="backend.agents.character_registry"):
        roster = cr.load_characters(include_retired=True)
    assert roster["nova"] == cr.CHARACTERS["nova"]  # code constant wins
    warnings = [r.getMessage() for r in caplog.records if "diverges" in r.getMessage()]
    assert warnings
    assert "display_name" in warnings[0] and "max_tier" in warnings[0]


def test_db_cannot_retire_a_builtin(monkeypatch, clock):
    _db_returns(monkeypatch, [dict(_seed_row("nova"), active=False)])
    assert cr.load_characters()["nova"].active is True


# ── Bad rows are skipped, never raise ──────────────────────────────


def test_bad_row_skipped_with_warning_others_load(monkeypatch, clock, caplog):
    bad = dict(_BLAZE_ROW, slug="glitch", brain="subscription-nonexistent")
    _db_returns(monkeypatch, [bad, _BLAZE_ROW])
    with caplog.at_level("WARNING", logger="backend.agents.character_registry"):
        roster = cr.load_characters()
    assert "glitch" not in roster
    assert "blaze" in roster
    assert any("skipping bad character_def row" in r.getMessage() for r in caplog.records)


def test_ttl_jitter_stays_within_bounds():
    for _ in range(50):
        delta = cr._next_refresh_at(0.0)
        assert (
            cr._DB_TTL_BASE_SECONDS - cr._DB_TTL_JITTER_SECONDS
            <= delta
            <= cr._DB_TTL_BASE_SECONDS + cr._DB_TTL_JITTER_SECONDS
        )


# ── Pickup wiring: fetch_pickable_tickets ──────────────────────────


def _claude_client() -> jd.DispatchClient:
    return jd.DispatchClient(
        agent_class="subscription-claude",
        base_url="https://test.invalid/rest/api/3",
        project_key="OP",
        auth_header="Basic dGVzdA==",
        bot_account_id="acc-claude-bot",
        bot_email="claude-bot@example.invalid",
    )


def _stub_jql_response(monkeypatch, issues: list[dict]) -> None:
    def fake_request(client, method, path, body=None, idem_key=None):
        if method == "POST" and path == "/search/jql":
            return {"issues": issues}
        raise AssertionError(f"unexpected jira_dispatch._request: {method} {path}")

    monkeypatch.setattr(jd, "_request", fake_request)


def _issue(key: str, labels: tuple[str, ...]) -> dict:
    return {
        "key": key,
        "fields": {
            "summary": f"{key} test",
            "labels": list(labels),
            "fixVersions": [],
            "created": "2026-07-01T10:00:00.000+0900",
            "components": [],
            "issuetype": {"name": "Story"},
        },
    }


def test_fetch_pickable_drops_retired_character_ticket_with_audit(
    monkeypatch, clock, caplog
):
    _db_returns(monkeypatch, [_EMBER_ROW])
    _stub_jql_response(monkeypatch, [
        _issue("OP-8001", ("class:subscription-claude", "tier:S", "character:ember")),
        _issue("OP-8002", ("class:subscription-claude", "tier:S")),
    ])
    with caplog.at_level("INFO", logger="backend.agents.jira_dispatch"):
        result = jd.fetch_pickable_tickets(_claude_client())

    keys = {issue["key"] for issue in result}
    assert "OP-8001" not in keys
    assert "OP-8002" in keys  # class-only pickup behavior unchanged
    audit = [
        r.getMessage() for r in caplog.records
        if "runner_character_retired_refusal" in r.getMessage()
    ]
    assert audit and "OP-8001" in audit[0] and "retired" in audit[0]


def test_fetch_pickable_denies_db_only_character_when_registry_stale(
    monkeypatch, clock, caplog
):
    _db_returns(monkeypatch, [_BLAZE_ROW])
    cr.load_characters()  # good snapshot
    _db_down(monkeypatch)
    clock.advance(cr.MAX_STALE_AGE_SECONDS + 60)
    _stub_jql_response(monkeypatch, [
        _issue("OP-8003", ("class:subscription-claude", "tier:S", "character:blaze")),
        _issue("OP-8004", ("class:subscription-claude", "tier:M", "character:nova")),
    ])
    with caplog.at_level("INFO", logger="backend.agents.jira_dispatch"):
        result = jd.fetch_pickable_tickets(_claude_client())

    keys = {issue["key"] for issue in result}
    assert "OP-8003" not in keys  # DB-only + stale → waits for DB recovery
    assert "OP-8004" in keys      # built-in never stale-denied
    audit = [
        r.getMessage() for r in caplog.records
        if "runner_character_retired_refusal" in r.getMessage()
    ]
    assert audit and "registry stale" in audit[0]


# ── Filer: dynamic --character validation ──────────────────────────


def _load_filer():
    spec = importlib.util.spec_from_file_location("file_jira_ticket_op2509", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _filer_args(**overrides) -> argparse.Namespace:
    values = {
        "summary": "OP-2509 synthetic check",
        "description_file": "",
        "priority": "Medium",
        "tier": "S",
        "cls": None,
        "character": None,
        "type": "feature",
        "areas": ["backend", "tests"],
        "scope": None,
        "check": True,
        "force": False,
        "no_push_capability": False,
        "capability": [],
        "assignee": None,
        "no_agent_auto": False,
        "skill": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_filer_character_flag_has_no_frozen_choices():
    mod = _load_filer()
    args = mod.build_parser().parse_args([
        "--summary", "x", "--description-file", "d.md", "--priority", "Low",
        "--tier", "S", "--character", "some-future-recruit", "--areas", "backend",
    ])
    # Not rejected at parse time — validation is dynamic in _apply_character.
    assert args.character == "some-future-recruit"


def test_filer_validates_db_recruited_character_dynamically(monkeypatch, clock):
    _db_returns(monkeypatch, [_BLAZE_ROW])
    mod = _load_filer()
    args = _filer_args(character="blaze", tier="M")
    mod._apply_character(args)
    assert args.cls == "subscription-claude"  # brain derived from the DB row


def test_filer_offline_builtin_still_files_with_explicit_warning(
    monkeypatch, clock, capsys
):
    _db_down(monkeypatch)
    mod = _load_filer()
    args = _filer_args(character="nova", tier="M")
    mod._apply_character(args)
    assert args.cls == "subscription-claude"
    err = capsys.readouterr().err
    assert "character registry DB offline" in err
    assert "built-in roster only" in err


def test_filer_offline_unknown_slug_error_names_offline_state(
    monkeypatch, clock
):
    _db_down(monkeypatch)
    mod = _load_filer()
    with pytest.raises(SystemExit) as excinfo:
        mod._apply_character(_filer_args(character="blaze"))
    msg = str(excinfo.value)
    assert "unknown or retired character 'blaze'" in msg
    assert "registry DB offline" in msg
    assert "nova" in msg  # names the known (built-in) set


def test_filer_rejects_retired_character_at_filing(monkeypatch, clock):
    _db_returns(monkeypatch, [_EMBER_ROW])
    mod = _load_filer()
    with pytest.raises(SystemExit) as excinfo:
        mod._apply_character(_filer_args(character="ember"))
    assert "unknown or retired character 'ember'" in str(excinfo.value)


# ── create_task: filing sees active only ───────────────────────────


def test_create_task_rejects_retired_character(monkeypatch, clock):
    import asyncio

    from backend.agents import tools

    _db_returns(monkeypatch, [_EMBER_ROW])
    out = asyncio.run(
        tools.create_task.coroutine("Title", "why", "backend", character="ember")
    )
    assert out.startswith("[ERROR]") and "unknown character" in out


def test_create_task_accepts_db_recruited_character(monkeypatch, clock):
    import asyncio

    import backend.jira_adapter as ja
    from backend.agents import tools

    _db_returns(monkeypatch, [_BLAZE_ROW])

    class _Ref:
        ticket = "OP-TEST"
        url = "http://x/OP-TEST"

    class _CapturingAdapter:
        def __init__(self):
            self.labels = None

        async def create_story(self, *, summary, description, labels, priority):
            self.labels = labels
            return _Ref()

    adapter = _CapturingAdapter()
    monkeypatch.setattr(ja, "build_default_jira_adapter", lambda: adapter)
    out = asyncio.run(
        tools.create_task.coroutine("Title", "why", "backend", character="blaze")
    )
    assert "DISPATCHED" in out
    assert "character:blaze" in adapter.labels
    assert "class:subscription-claude" in adapter.labels
