"""SC.7.4 — OWASP CSRF token templates for generated apps.

Small framework-agnostic CSRF helpers intended for generated FastAPI /
service templates.  The helpers implement the synchronizer-token shape
used by OmniSight's own auth surface: issue a cryptographic random token,
store it with the server-side session, mirror it into a non-HttpOnly
cookie or hidden form field, and require unsafe requests to echo it via
``X-CSRF-Token`` or a form field.

Security boundary:

  * This module covers CSRF token generation, render context, and
    constant-time token validation only.
  * It does not create sessions, set cookies on a framework response,
    or decide authentication mode.  Callers own persistence and HTTP
    error mapping.
  * Input validation, output encoding, SQL parameterisation, and path /
    SSRF protection are separate SC.7 rows.

All module-level state is immutable constants.  Cross-worker safety
follows SOP Step 1 answer #1: each uvicorn worker derives identical
templates from the same source code; token values are per request /
session randomness and are never stored in module globals.
"""

from __future__ import annotations

from dataclasses import dataclass
import secrets
from typing import Mapping

from backend.security.output_encoding import encode_html_attribute


DEFAULT_TOKEN_BYTES = 32
DEFAULT_COOKIE_NAME = "csrf_token"
DEFAULT_HEADER_NAME = "X-CSRF-Token"
DEFAULT_FORM_FIELD_NAME = "csrf_token"
MAX_TOKEN_LENGTH = 256

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


@dataclass(frozen=True)
class CsrfIssue:
    """Machine-readable CSRF configuration or validation failure detail."""

    field: str
    code: str
    message: str


class CsrfTokenError(ValueError):
    """Raised when a CSRF token template or check fails."""

    def __init__(self, field: str, code: str, message: str) -> None:
        super().__init__(f"{field}: {message}")
        self.issue = CsrfIssue(field=field, code=code, message=message)


@dataclass(frozen=True)
class CsrfTokenTemplate:
    """Framework-neutral CSRF token render context."""

    token: str
    cookie_name: str = DEFAULT_COOKIE_NAME
    header_name: str = DEFAULT_HEADER_NAME
    form_field_name: str = DEFAULT_FORM_FIELD_NAME

    @property
    def header_value(self) -> dict[str, str]:
        """Header dict callers can merge into fetch / API clients."""

        return {self.header_name: self.token}

    @property
    def hidden_input_html(self) -> str:
        """HTML hidden input for server-rendered unsafe forms."""

        name = encode_html_attribute(self.form_field_name)
        value = encode_html_attribute(self.token)
        return f'<input type="hidden" name="{name}" value="{value}">'


def _fail(field: str, code: str, message: str) -> None:
    raise CsrfTokenError(field, code, message)


def _field_name(field: str) -> str:
    return (field or "value").strip() or "value"


def generate_csrf_token(*, byte_length: int = DEFAULT_TOKEN_BYTES) -> str:
    """Return a cryptographic random CSRF token for one session."""

    if isinstance(byte_length, bool) or not isinstance(byte_length, int):
        _fail("byte_length", "type", "must be an integer")
    if byte_length < 16:
        _fail("byte_length", "too_small", "must be at least 16 bytes")
    if byte_length > 64:
        _fail("byte_length", "too_large", "must be at most 64 bytes")
    return secrets.token_urlsafe(byte_length)


def build_csrf_template(
    token: str | None = None,
    *,
    cookie_name: str = DEFAULT_COOKIE_NAME,
    header_name: str = DEFAULT_HEADER_NAME,
    form_field_name: str = DEFAULT_FORM_FIELD_NAME,
) -> CsrfTokenTemplate:
    """Build the token context a generated app can render or echo."""

    return CsrfTokenTemplate(
        token=_validate_token(
            generate_csrf_token() if token is None else token,
            field="token",
        ),
        cookie_name=_validate_name(cookie_name, field="cookie_name"),
        header_name=_validate_name(header_name, field="header_name"),
        form_field_name=_validate_name(form_field_name, field="form_field_name"),
    )


def is_safe_method(method: str) -> bool:
    """Return True when an HTTP method does not require CSRF validation."""

    if not isinstance(method, str):
        return False
    return method.upper() in SAFE_METHODS


def submitted_token(
    headers: Mapping[str, str] | None = None,
    form: Mapping[str, str] | None = None,
    *,
    header_name: str = DEFAULT_HEADER_NAME,
    form_field_name: str = DEFAULT_FORM_FIELD_NAME,
) -> str | None:
    """Return a submitted token from header first, then form data."""

    header_key = _validate_name(header_name, field="header_name")
    form_key = _validate_name(form_field_name, field="form_field_name")
    header_value = _case_insensitive_get(headers or {}, header_key)
    if header_value:
        return header_value
    return (form or {}).get(form_key)


def validate_csrf_token(expected_token: str, candidate_token: str | None) -> None:
    """Validate a submitted CSRF token using constant-time comparison."""

    expected = _validate_token(expected_token, field="expected_token")
    if candidate_token is None:
        _fail("candidate_token", "missing", "CSRF token is required")
    candidate = _validate_token(candidate_token, field="candidate_token")
    if not secrets.compare_digest(expected, candidate):
        _fail("candidate_token", "mismatch", "CSRF token does not match")


def require_csrf(
    method: str,
    expected_token: str,
    *,
    headers: Mapping[str, str] | None = None,
    form: Mapping[str, str] | None = None,
    header_name: str = DEFAULT_HEADER_NAME,
    form_field_name: str = DEFAULT_FORM_FIELD_NAME,
) -> None:
    """Require a matching token for unsafe HTTP methods."""

    if is_safe_method(method):
        return
    token = submitted_token(
        headers,
        form,
        header_name=header_name,
        form_field_name=form_field_name,
    )
    validate_csrf_token(expected_token, token)


def _validate_name(value: object, *, field: str) -> str:
    name = _field_name(field)
    if not isinstance(value, str):
        _fail(name, "type", "must be a string")
    text = value.strip()
    if not text:
        _fail(name, "empty", "must not be empty")
    if any(ch in text for ch in "\r\n\x00"):
        _fail(name, "control_char", "must not contain control characters")
    return text


def _validate_token(value: object, *, field: str) -> str:
    name = _field_name(field)
    if not isinstance(value, str):
        _fail(name, "type", "must be a string")
    token = value.strip()
    if not token:
        _fail(name, "empty", "must not be empty")
    if len(token) > MAX_TOKEN_LENGTH:
        _fail(name, "too_long", f"must be at most {MAX_TOKEN_LENGTH} characters")
    if any(ch in token for ch in "\r\n\x00"):
        _fail(name, "control_char", "must not contain control characters")
    return token


def _case_insensitive_get(headers: Mapping[str, str], key: str) -> str | None:
    wanted = key.lower()
    for header, value in headers.items():
        if header.lower() == wanted:
            return value
    return None


__all__ = [
    "DEFAULT_COOKIE_NAME",
    "DEFAULT_FORM_FIELD_NAME",
    "DEFAULT_HEADER_NAME",
    "DEFAULT_TOKEN_BYTES",
    "MAX_TOKEN_LENGTH",
    "SAFE_METHODS",
    "CsrfIssue",
    "CsrfTokenError",
    "CsrfTokenTemplate",
    "build_csrf_template",
    "generate_csrf_token",
    "is_safe_method",
    "require_csrf",
    "submitted_token",
    "validate_csrf_token",
]
