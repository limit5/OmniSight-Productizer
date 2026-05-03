"""SC.7.4 — Unit tests for OWASP CSRF token templates."""

from __future__ import annotations

import re

import pytest

from backend.security import csrf_templates as csrf


def _issue(exc: pytest.ExceptionInfo[csrf.CsrfTokenError]) -> csrf.CsrfIssue:
    return exc.value.issue


class TestGenerateCsrfToken:
    def test_generates_urlsafe_random_token(self):
        token = csrf.generate_csrf_token()
        assert len(token) >= 32
        assert re.fullmatch(r"[A-Za-z0-9_-]+", token)

    @pytest.mark.parametrize(
        "byte_length, code",
        [(True, "type"), ("32", "type"), (15, "too_small"), (65, "too_large")],
    )
    def test_rejects_weak_or_invalid_byte_lengths(self, byte_length: object, code: str):
        with pytest.raises(csrf.CsrfTokenError) as exc:
            csrf.generate_csrf_token(byte_length=byte_length)  # type: ignore[arg-type]
        assert _issue(exc).field == "byte_length"
        assert _issue(exc).code == code


class TestBuildCsrfTemplate:
    def test_builds_default_render_context(self):
        template = csrf.build_csrf_template("token-123")
        assert template.token == "token-123"
        assert template.cookie_name == csrf.DEFAULT_COOKIE_NAME
        assert template.header_value == {csrf.DEFAULT_HEADER_NAME: "token-123"}
        assert template.hidden_input_html == (
            '<input type="hidden" name="csrf_token" value="token-123">'
        )

    def test_hidden_input_escapes_custom_names_and_values(self):
        template = csrf.build_csrf_template(
            'tok"en',
            form_field_name='csrf"field',
        )
        assert template.hidden_input_html == (
            '<input type="hidden" name="csrf&quot;field" value="tok&quot;en">'
        )

    def test_generates_token_when_not_supplied(self):
        template = csrf.build_csrf_template()
        assert template.token
        assert template.header_name == csrf.DEFAULT_HEADER_NAME

    @pytest.mark.parametrize(
        "kwargs, field, code",
        [
            ({"token": ""}, "token", "empty"),
            ({"token": "ok\nbad"}, "token", "control_char"),
            ({"cookie_name": ""}, "cookie_name", "empty"),
            ({"header_name": "X-CSRF\nBad"}, "header_name", "control_char"),
            ({"form_field_name": None}, "form_field_name", "type"),
        ],
    )
    def test_rejects_invalid_template_parts(
        self,
        kwargs: dict[str, object],
        field: str,
        code: str,
    ):
        with pytest.raises(csrf.CsrfTokenError) as exc:
            csrf.build_csrf_template(**kwargs)  # type: ignore[arg-type]
        assert _issue(exc).field == field
        assert _issue(exc).code == code


class TestSubmittedToken:
    def test_reads_header_case_insensitively_before_form(self):
        token = csrf.submitted_token(
            {"x-csrf-token": "from-header"},
            {"csrf_token": "from-form"},
        )
        assert token == "from-header"

    def test_falls_back_to_form_field(self):
        assert csrf.submitted_token({}, {"csrf_token": "from-form"}) == "from-form"

    def test_returns_none_when_no_token_was_submitted(self):
        assert csrf.submitted_token({"Other": "value"}, {}) is None


class TestValidateCsrfToken:
    def test_accepts_matching_token(self):
        csrf.validate_csrf_token("expected", "expected")

    @pytest.mark.parametrize(
        "expected, candidate, field, code",
        [
            ("expected", None, "candidate_token", "missing"),
            ("expected", "other", "candidate_token", "mismatch"),
            ("", "candidate", "expected_token", "empty"),
            ("expected", "bad\ncandidate", "candidate_token", "control_char"),
            ("expected", "x" * (csrf.MAX_TOKEN_LENGTH + 1), "candidate_token", "too_long"),
        ],
    )
    def test_rejects_missing_mismatched_or_malformed_tokens(
        self,
        expected: str,
        candidate: str | None,
        field: str,
        code: str,
    ):
        with pytest.raises(csrf.CsrfTokenError) as exc:
            csrf.validate_csrf_token(expected, candidate)
        assert _issue(exc).field == field
        assert _issue(exc).code == code


class TestRequireCsrf:
    @pytest.mark.parametrize("method", ["GET", "head", "OPTIONS", "TRACE"])
    def test_safe_methods_do_not_require_token(self, method: str):
        csrf.require_csrf(method, "expected")

    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
    def test_unsafe_methods_require_matching_header_token(self, method: str):
        csrf.require_csrf(method, "expected", headers={"X-CSRF-Token": "expected"})

    def test_unsafe_method_accepts_matching_form_token(self):
        csrf.require_csrf("POST", "expected", form={"csrf_token": "expected"})

    def test_unsafe_method_rejects_missing_token(self):
        with pytest.raises(csrf.CsrfTokenError) as exc:
            csrf.require_csrf("POST", "expected")
        assert _issue(exc).field == "candidate_token"
        assert _issue(exc).code == "missing"
