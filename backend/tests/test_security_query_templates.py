"""SC.7.3 — Unit tests for OWASP parameterized-query templates."""

from __future__ import annotations

import pytest

from backend.security import query_templates as qt


def _issue(exc: pytest.ExceptionInfo[qt.ParameterizedQueryError]) -> qt.ParameterizedQueryIssue:
    return exc.value.issue


class TestIdentifierHelpers:
    def test_quotes_reserved_word_identifier(self):
        assert qt.quote_identifier("user") == '"user"'

    @pytest.mark.parametrize("value", ["name;drop", "bad-name", "42bad", "bad space"])
    def test_rejects_identifier_injection_shapes(self, value: str):
        with pytest.raises(qt.ParameterizedQueryError) as exc:
            qt.quote_identifier(value, field="column")
        assert _issue(exc).field == "column"
        assert _issue(exc).code == "identifier"

    def test_placeholders_are_postgres_positional(self):
        assert qt.placeholder(3) == "$3"
        assert qt.placeholders(3, start=2) == ("$2", "$3", "$4")

    def test_placeholder_index_uses_query_template_error(self):
        with pytest.raises(qt.ParameterizedQueryError) as exc:
            qt.placeholder(0)
        assert _issue(exc).field == "index"
        assert _issue(exc).code == "too_small"


class TestSelectRows:
    def test_builds_select_with_where_order_and_limit_params(self):
        query = qt.select_rows(
            "users",
            ("id", "email"),
            where={"tenant_id": "t-1", "role": "admin"},
            order_by=(("created_at", "desc"),),
            limit=25,
        )
        assert query.sql == (
            'SELECT "id", "email" FROM "users" '
            'WHERE "tenant_id" = $1 AND "role" = $2 '
            'ORDER BY "created_at" DESC LIMIT $3'
        )
        assert query.params == ("t-1", "admin", 25)

    def test_value_injection_stays_out_of_sql_text(self):
        payload = "admin'; DROP TABLE users; --"
        query = qt.select_rows("users", ("id",), where={"role": payload})
        assert payload not in query.sql
        assert query.params == (payload,)

    def test_rejects_empty_projection(self):
        with pytest.raises(qt.ParameterizedQueryError) as exc:
            qt.select_rows("users", ())
        assert _issue(exc).field == "columns"
        assert _issue(exc).code == "empty"

    def test_rejects_unknown_order_direction(self):
        with pytest.raises(qt.ParameterizedQueryError) as exc:
            qt.select_rows("users", ("id",), order_by=(("created_at", "sideways"),))
        assert _issue(exc).field == "order_by"
        assert _issue(exc).code == "direction"


class TestInsertRow:
    def test_builds_insert_with_returning_clause(self):
        query = qt.insert_row(
            "users",
            {"id": "u-1", "email": "a@example.com", "role": "admin"},
            returning=("id", "created_at"),
        )
        assert query.sql == (
            'INSERT INTO "users" ("id", "email", "role") '
            'VALUES ($1, $2, $3) RETURNING "id", "created_at"'
        )
        assert query.params == ("u-1", "a@example.com", "admin")

    def test_rejects_empty_insert_values(self):
        with pytest.raises(qt.ParameterizedQueryError) as exc:
            qt.insert_row("users", {})
        assert _issue(exc).field == "values"
        assert _issue(exc).code == "empty"


class TestUpdateRows:
    def test_builds_update_with_set_and_where_params(self):
        query = qt.update_rows(
            "users",
            {"email": "new@example.com", "role": "owner"},
            {"id": "u-1", "tenant_id": "t-1"},
            returning=("id",),
        )
        assert query.sql == (
            'UPDATE "users" SET "email" = $1, "role" = $2 '
            'WHERE "id" = $3 AND "tenant_id" = $4 RETURNING "id"'
        )
        assert query.params == ("new@example.com", "owner", "u-1", "t-1")

    def test_rejects_update_without_where(self):
        with pytest.raises(qt.ParameterizedQueryError) as exc:
            qt.update_rows("users", {"role": "owner"}, {})
        assert _issue(exc).field == "where"
        assert _issue(exc).code == "empty"


class TestDeleteRows:
    def test_builds_delete_with_required_where(self):
        query = qt.delete_rows("sessions", {"token": "s-1"}, returning=("token",))
        assert query.sql == 'DELETE FROM "sessions" WHERE "token" = $1 RETURNING "token"'
        assert query.params == ("s-1",)

    def test_rejects_delete_without_where(self):
        with pytest.raises(qt.ParameterizedQueryError) as exc:
            qt.delete_rows("sessions", {})
        assert _issue(exc).field == "where"
        assert _issue(exc).code == "empty"
