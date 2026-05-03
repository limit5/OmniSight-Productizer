"""SC.7.1 — Unit tests for OWASP input-validation helpers."""

from __future__ import annotations

import re

import pytest

from backend.security import input_validation as iv


def _issue(exc: pytest.ExceptionInfo[iv.InputValidationError]) -> iv.InputValidationIssue:
    return exc.value.issue


class TestValidateText:
    def test_strips_and_returns_bounded_text(self):
        assert iv.validate_text("  hello  ", min_length=1, max_length=8) == "hello"

    def test_rejects_non_string(self):
        with pytest.raises(iv.InputValidationError) as exc:
            iv.validate_text(123, field="name")
        assert _issue(exc).field == "name"
        assert _issue(exc).code == "type"

    def test_rejects_control_characters_by_default(self):
        with pytest.raises(iv.InputValidationError) as exc:
            iv.validate_text("ok\nbad", field="comment")
        assert _issue(exc).code == "control_char"

    def test_can_allow_control_characters_for_textarea_callers(self):
        assert iv.validate_text("line 1\nline 2", allow_control_chars=True) == "line 1\nline 2"

    def test_pattern_is_allowlist(self):
        assert iv.validate_text("abc-123", pattern=re.compile(r"^[a-z0-9-]+$")) == "abc-123"
        with pytest.raises(iv.InputValidationError) as exc:
            iv.validate_text("abc;<script>", field="slug", pattern=re.compile(r"^[a-z0-9-]+$"))
        assert _issue(exc).code == "pattern"


class TestValidateSlug:
    def test_accepts_and_lowercases_catalog_slug(self):
        assert iv.validate_slug("  Demo_App-1  ") == "demo_app-1"

    @pytest.mark.parametrize(
        "value",
        ["-bad", "bad-", "bad space", "bad.dot", "", "../etc/passwd"],
    )
    def test_rejects_non_slug_shapes(self, value: str):
        with pytest.raises(iv.InputValidationError) as exc:
            iv.validate_slug(value)
        assert _issue(exc).code in {"too_short", "slug"}


class TestValidateIdentifier:
    def test_accepts_symbolic_identifier(self):
        assert iv.validate_identifier("_tenant42") == "_tenant42"

    @pytest.mark.parametrize("value", ["42tenant", "tenant-name", "tenant name", "tenant;drop"])
    def test_rejects_identifier_injection_shapes(self, value: str):
        with pytest.raises(iv.InputValidationError) as exc:
            iv.validate_identifier(value, field="column")
        assert _issue(exc).field == "column"
        assert _issue(exc).code == "identifier"


class TestNormalizeEmail:
    def test_normalizes_common_login_email(self):
        assert iv.normalize_email("  Alice.Example+Ops@Example.COM  ") == "alice.example+ops@example.com"

    @pytest.mark.parametrize(
        "value, code",
        [
            ("missing-at.example.com", "email"),
            ("a@@example.com", "email"),
            (".alice@example.com", "email_local"),
            ("alice..ops@example.com", "email_local"),
            ("alice@example", "email_domain"),
            ("alice@bad_domain.com", "email_domain"),
            ("alice@example..com", "email_domain"),
        ],
    )
    def test_rejects_ambiguous_or_non_saas_email_shapes(self, value: str, code: str):
        with pytest.raises(iv.InputValidationError) as exc:
            iv.normalize_email(value)
        assert _issue(exc).code == code


class TestValidateEnum:
    def test_returns_canonical_choice_case_insensitive_by_default(self):
        assert iv.validate_enum("Admin", ("viewer", "admin", "owner"), field="role") == "admin"

    def test_case_sensitive_mode_returns_exact_choice(self):
        assert iv.validate_enum("Admin", ("Admin", "Owner"), case_sensitive=True) == "Admin"
        with pytest.raises(iv.InputValidationError) as exc:
            iv.validate_enum("admin", ("Admin", "Owner"), case_sensitive=True)
        assert _issue(exc).code == "enum"

    def test_empty_allowed_set_is_configuration_error(self):
        with pytest.raises(iv.InputValidationError) as exc:
            iv.validate_enum("admin", (), field="role")
        assert _issue(exc).code == "enum_config"


class TestValidateIntRange:
    def test_accepts_integer_inside_bounds(self):
        assert iv.validate_int_range(3, field="page", minimum=1, maximum=10) == 3

    @pytest.mark.parametrize(
        "value, code",
        [(True, "type"), ("3", "type"), (0, "too_small"), (11, "too_large")],
    )
    def test_rejects_non_int_or_out_of_range(self, value: object, code: str):
        with pytest.raises(iv.InputValidationError) as exc:
            iv.validate_int_range(value, field="page", minimum=1, maximum=10)
        assert _issue(exc).code == code
