"""P8 Fix regression guards — named-param translation in db_pg_compat.

Locks in the ``:name`` → ``$N`` + dict-to-positional-list conversion added
in commit that fixed production 503 cascade: a single call path using
``:name`` placeholders (``save_npi_state`` / ``insert_simulation`` / many
more in db.py) was triggering ``PostgresSyntaxError`` which aborted the
shared asyncpg transaction and took down /readyz + every /api/v1/*.

Runs without Redis or Postgres — pure string-rewrite tests.
"""

from __future__ import annotations

import pytest

from backend.db_pg_compat import (
    _named_to_dollar,
    _qmark_to_dollar,
    translate_sql,
    translate_sql_and_params,
)


# ── _qmark_to_dollar — unchanged pre-existing behaviour ───────────────


class TestQmarkToDollar:
    def test_basic_numbering(self):
        assert _qmark_to_dollar("SELECT * FROM t WHERE a=? AND b=?") == (
            "SELECT * FROM t WHERE a=$1 AND b=$2"
        )

    def test_qmark_inside_string_literal_untouched(self):
        # SQL literal 'what?' must not get rewritten.
        sql = "SELECT 'what?' AS q FROM t WHERE a=?"
        assert _qmark_to_dollar(sql) == "SELECT 'what?' AS q FROM t WHERE a=$1"

    def test_doubled_quote_escape_inside_literal(self):
        sql = "SELECT 'it''s ?' AS q, ? AS x"
        # Doubled '' is the SQL escape for a literal '. The ? inside
        # stays literal; the ? outside becomes $1.
        assert _qmark_to_dollar(sql) == "SELECT 'it''s ?' AS q, $1 AS x"


# ── _named_to_dollar — new in P8 Fix ──────────────────────────────────


class TestNamedToDollar:
    def test_each_name_becomes_dollar_and_values_ordered(self):
        sql = "INSERT INTO t (a, b) VALUES (:a, :b)"
        new_sql, values = _named_to_dollar(sql, {"a": 1, "b": 2})
        assert new_sql == "INSERT INTO t (a, b) VALUES ($1, $2)"
        assert values == [1, 2]

    def test_repeated_name_repeats_value_at_each_slot(self):
        # This is the UPSERT pattern save_npi_state uses — :data appears
        # twice (once in VALUES, once in ON CONFLICT DO UPDATE SET).
        sql = (
            "INSERT INTO npi_state (id, data) VALUES ('current', :data) "
            "ON CONFLICT(id) DO UPDATE SET data = :data"
        )
        new_sql, values = _named_to_dollar(sql, {"data": "{}"})
        assert "VALUES ('current', $1)" in new_sql
        assert "SET data = $2" in new_sql
        # Both slots resolve to the same value, repeated positionally.
        assert values == ["{}", "{}"]

    def test_colon_inside_string_literal_is_left_alone(self):
        sql = "SELECT 'hello:world' AS s, :name"
        new_sql, values = _named_to_dollar(sql, {"name": "x"})
        assert new_sql == "SELECT 'hello:world' AS s, $1"
        assert values == ["x"]

    def test_pg_double_colon_cast_passed_through(self):
        # The translator must not swallow ``::text`` / ``::int`` casts.
        sql = "SELECT :payload::jsonb AS j"
        new_sql, values = _named_to_dollar(sql, {"payload": "{}"})
        # :payload → $1, then ::jsonb must stay intact.
        assert new_sql == "SELECT $1::jsonb AS j"
        assert values == ["{}"]

    def test_insert_with_many_named_params(self):
        # Mirrors insert_simulation (~15 params) — wide enough to catch
        # off-by-one in numbering.
        sql = (
            "INSERT INTO simulations (id, task_id, agent_id, status) "
            "VALUES (:id, :task_id, :agent_id, :status)"
        )
        new_sql, values = _named_to_dollar(
            sql, {"id": "s1", "task_id": "t1", "agent_id": "a1",
                  "status": "pending"},
        )
        assert "VALUES ($1, $2, $3, $4)" in new_sql
        assert values == ["s1", "t1", "a1", "pending"]

    def test_missing_key_raises_clear_keyerror(self):
        with pytest.raises(KeyError, match=":missing referenced in SQL"):
            _named_to_dollar("SELECT :missing", {})


# ── translate_sql_and_params — integration ────────────────────────────


class TestTranslateSqlAndParams:
    def test_none_params_returns_empty_tuple(self):
        sql, args = translate_sql_and_params("SELECT 1", None)
        assert sql == "SELECT 1"
        assert args == ()

    def test_empty_tuple_passes_through(self):
        sql, args = translate_sql_and_params("SELECT 1", ())
        assert sql == "SELECT 1"
        assert args == ()

    def test_positional_tuple_uses_qmark_path(self):
        sql, args = translate_sql_and_params(
            "UPDATE t SET a=? WHERE id=?", ("val", 7),
        )
        assert sql == "UPDATE t SET a=$1 WHERE id=$2"
        assert args == ("val", 7)

    def test_dict_params_uses_named_path(self):
        # The smoking-gun case: save_npi_state-style call.
        sql, args = translate_sql_and_params(
            "INSERT INTO npi_state (id, data) VALUES ('current', :data) "
            "ON CONFLICT(id) DO UPDATE SET data=excluded.data",
            {"data": '{"k":"v"}'},
        )
        assert "VALUES ('current', $1)" in sql
        assert args == ('{"k":"v"}',)

    def test_sqlite_isms_still_rewritten_before_placeholders(self):
        # Order invariant: INSERT OR IGNORE must rewrite before the
        # placeholder pass, otherwise the rewrite might drop or shift
        # our :name tokens.
        sql, args = translate_sql_and_params(
            "INSERT OR IGNORE INTO t (id, data) VALUES (:id, :data)",
            {"id": 1, "data": "{}"},
        )
        # INSERT OR IGNORE → INSERT ... ON CONFLICT DO NOTHING
        assert "ON CONFLICT" in sql
        assert "DO NOTHING" in sql
        # Placeholders intact as $1, $2.
        assert "VALUES ($1, $2)" in sql
        assert args == (1, "{}")


# ── translate_sql — legacy SQL-only entry point ───────────────────────


class TestTranslateSqlLegacyEntryPoint:
    def test_sql_only_entry_still_works_for_qmark(self):
        # Back-compat: translate_sql(sql) with no params must still
        # rewrite qmark placeholders the same as before P8.
        assert translate_sql("SELECT ? FROM t") == "SELECT $1 FROM t"

    def test_sql_only_entry_does_not_touch_named_placeholders(self):
        # Without params, translate_sql can't flatten a dict, so
        # :name must be left alone rather than corrupted. (Callers
        # that use :name must now go through translate_sql_and_params.)
        out = translate_sql("SELECT :name FROM t")
        assert ":name" in out
        assert "$" not in out
