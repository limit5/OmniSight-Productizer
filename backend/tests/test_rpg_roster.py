"""OP-2512 — scripts/rpg_roster.py: merged roster CLI.

Covers the 4-AC spec:

* no-DB path: built-ins-only table + stderr notice, exit 0 (mock/fake store);
* DB path (fake rows): a DB-recruited character (vega) lists alongside the
  4 built-ins, joined with card level/xp and "skill:LvN(branch)" skills;
* --json emits the same data as JSON (shape pinned);
* read-only: the progression fetch issues SELECTs only.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

from backend.agents import character_registry as cr

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "rpg_roster.py"

# The first DB-recruited character (RECRUIT C2 live proof).
_VEGA_ROW = {
    "slug": "vega",
    "display_name": "Vega",
    "brain": "subscription-claude",
    "guild": "backend",
    "max_tier": "M",
    "blurb": "First DB-recruited character.",
    "active": True,
}

# A retired DB-recruited character — listed (include_retired=True), active=no.
_EMBER_ROW = {
    "slug": "ember",
    "display_name": "Ember",
    "brain": "subscription-claude",
    "guild": "backend",
    "max_tier": "M",
    "blurb": "Retired recruit.",
    "active": False,
}

_CARDS = {
    "vega": {"level": 3, "xp": 120},
    "nova": {"level": 5, "xp": 900},
}

_SKILLS = {
    "vega": [
        {"skill_id": "enterprise_web", "level": 2, "branch": None},
        {"skill_id": "rpg_systems", "level": 3, "branch": "scale_out"},
    ],
}


@pytest.fixture(autouse=True)
def _fresh_registry():
    cr._SNAPSHOT = None
    yield
    cr._SNAPSHOT = None


@pytest.fixture
def roster_mod():
    spec = importlib.util.spec_from_file_location("rpg_roster_op2512", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _db_returns(monkeypatch, rows: list[dict]) -> None:
    monkeypatch.setattr(cr, "_fetch_character_rows", lambda: [dict(r) for r in rows])


def _db_down(monkeypatch) -> None:
    def _boom() -> list[dict]:
        raise RuntimeError("connection refused (simulated DB outage)")

    monkeypatch.setattr(cr, "_fetch_character_rows", _boom)


def _progression_returns(monkeypatch, roster_mod, cards: dict, skills: dict) -> None:
    monkeypatch.setattr(roster_mod, "_fetch_progression", lambda: (cards, skills))


def _progression_down(monkeypatch, roster_mod) -> None:
    def _boom():
        raise RuntimeError("connection refused (simulated DB outage)")

    monkeypatch.setattr(roster_mod, "_fetch_progression", _boom)


# ── no-DB path: built-ins only + notice ─────────────────────────────


def test_no_db_lists_builtins_with_notice(monkeypatch, capsys, roster_mod):
    _db_down(monkeypatch)
    _progression_down(monkeypatch, roster_mod)

    assert roster_mod.main([]) == 0

    out, err = capsys.readouterr()
    for slug in cr.CHARACTERS:
        assert slug in out
    assert "vega" not in out
    assert "character_def DB unreachable" in err
    assert "built-in roster only" in err
    assert "progression DB unreachable" in err
    # Notices go to stderr, never into the table.
    assert "NOTICE" not in out


# ── DB path: vega (DB row) lists alongside the 4 built-ins ─────────


def test_db_roster_lists_vega_and_builtins_with_cards_and_skills(
    monkeypatch, capsys, roster_mod
):
    _db_returns(monkeypatch, [_VEGA_ROW, _EMBER_ROW])
    _progression_returns(monkeypatch, roster_mod, _CARDS, _SKILLS)

    assert roster_mod.main([]) == 0

    out, err = capsys.readouterr()
    assert err == ""
    lines = out.splitlines()
    by_slug = {line.split()[0]: line for line in lines[1:]}
    # The DB-recruited character AND all 4 built-ins.
    assert set(by_slug) == set(cr.CHARACTERS) | {"vega", "ember"}

    # Card join: level/xp on the rows that have a card, blank otherwise.
    assert "3" in by_slug["vega"].split() and "120" in by_slug["vega"].split()
    assert "5" in by_slug["nova"].split() and "900" in by_slug["nova"].split()

    # Skill format: "skill:LvN" pre-branch, "skill:LvN(branch)" once locked.
    assert "enterprise_web:Lv2" in by_slug["vega"]
    assert "rpg_systems:Lv3(scale_out)" in by_slug["vega"]

    # built_in flag: slug ∈ character_registry.CHARACTERS.
    assert by_slug["vega"].split()[6] == "no"
    assert by_slug["nova"].split()[6] == "yes"
    # Retired recruit is listed (include_retired=True) with active=no.
    assert by_slug["ember"].split()[5] == "no"
    assert by_slug["vega"].split()[5] == "yes"

    # Aligned table: each value starts at its header column's offset.
    header = lines[0]
    assert by_slug["vega"][header.index("BRAIN"):].startswith("subscription-claude")
    assert by_slug["vega"][header.index("GUILD"):].startswith("backend")
    assert by_slug["vega"][header.index("SKILLS"):].startswith("enterprise_web:Lv2")
    assert by_slug["nova"][header.index("BRAIN"):].startswith("subscription-claude")


# ── --json: same data as JSON ───────────────────────────────────────


def test_json_shape(monkeypatch, capsys, roster_mod):
    _db_returns(monkeypatch, [_VEGA_ROW])
    _progression_returns(monkeypatch, roster_mod, _CARDS, _SKILLS)

    assert roster_mod.main(["--json"]) == 0

    out, err = capsys.readouterr()
    assert err == ""
    payload = json.loads(out)
    assert payload["registry_db_loaded"] is True
    assert payload["progression_db_loaded"] is True

    by_slug = {row["slug"]: row for row in payload["characters"]}
    assert set(by_slug) == set(cr.CHARACTERS) | {"vega"}

    vega = by_slug["vega"]
    assert vega == {
        "slug": "vega",
        "display_name": "Vega",
        "brain": "subscription-claude",
        "guild": "backend",
        "max_tier": "M",
        "active": True,
        "built_in": False,
        "level": 3,
        "xp": 120,
        "skills": ["enterprise_web:Lv2", "rpg_systems:Lv3(scale_out)"],
    }
    # A built-in without a card: blank progression, still listed.
    rex = by_slug["rex"]
    assert rex["built_in"] is True
    assert rex["level"] is None and rex["xp"] is None and rex["skills"] == []


def test_json_no_db_is_valid_json_with_notice_on_stderr(
    monkeypatch, capsys, roster_mod
):
    _db_down(monkeypatch)
    _progression_down(monkeypatch, roster_mod)

    assert roster_mod.main(["--json"]) == 0

    out, err = capsys.readouterr()
    payload = json.loads(out)  # stdout stays machine-parseable
    assert payload["registry_db_loaded"] is False
    assert payload["progression_db_loaded"] is False
    assert {row["slug"] for row in payload["characters"]} == set(cr.CHARACTERS)
    assert "NOTICE" in err


# ── read-only: the progression fetch issues SELECTs only ───────────


class _FakeCursor:
    def __init__(self, executed: list[str]):
        self._executed = executed
        self._rows: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, *params):
        self._executed.append(sql)
        if "agent_character_card" in sql:
            self._rows = [("vega", 3, 120)]
        else:
            self._rows = [
                ("vega", "enterprise_web", 2, None),
                ("vega", "rpg_systems", 3, "scale_out"),
            ]

    def fetchall(self):
        return list(self._rows)


class _FakeConn:
    def __init__(self, executed: list[str]):
        self._executed = executed

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return _FakeCursor(self._executed)

    def close(self):
        pass


def test_fetch_progression_is_read_only_and_maps_rows(monkeypatch, roster_mod):
    executed: list[str] = []
    fake_psycopg2 = types.SimpleNamespace(
        connect=lambda dsn, connect_timeout: _FakeConn(executed)
    )
    monkeypatch.setitem(sys.modules, "psycopg2", fake_psycopg2)
    monkeypatch.setenv("OMNI_TEST_PG_URL", "postgresql://u:p@db.invalid:5432/omni")

    cards, skills = roster_mod._fetch_progression()

    assert executed and all(sql.lstrip().upper().startswith("SELECT") for sql in executed)
    assert cards == {"vega": {"level": 3, "xp": 120}}
    assert skills == {
        "vega": [
            {"skill_id": "enterprise_web", "level": 2, "branch": None},
            {"skill_id": "rpg_systems", "level": 3, "branch": "scale_out"},
        ],
    }


def test_fetch_progression_raises_without_dsn(monkeypatch, roster_mod):
    for key in ("OMNISIGHT_DATABASE_URL", "DATABASE_URL", "OMNI_TEST_PG_URL"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(Exception, match="DSN"):
        roster_mod._fetch_progression()
