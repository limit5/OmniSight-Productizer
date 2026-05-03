"""SC.7.1 — OWASP input-validation helpers for generated apps.

Small allowlist-first validators intended for generated FastAPI /
service templates.  The helpers are deliberately pure and framework
agnostic: callers decide whether a validation failure becomes HTTP 400,
422, a form error, or a background-job rejection.

Security boundary:

  * This module covers scalar input shape checks only.
  * Path traversal / SSRF helpers are explicitly out of scope for this
    row and land in SC.7.5.
  * Output encoding, SQL parameterisation, and CSRF templates are
    separate SC.7 rows.

All module-level state is immutable constants / compiled regexes.
Cross-worker safety follows SOP Step 1 answer #1: each uvicorn worker
derives identical validators from the same source code; there is no
shared cache, singleton, or runtime mutation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Pattern


DEFAULT_MAX_TEXT_LENGTH = 1024
MAX_EMAIL_LENGTH = 254
MAX_EMAIL_LOCAL_LENGTH = 64
MAX_SLUG_LENGTH = 64
MAX_IDENTIFIER_LENGTH = 64

SLUG_RE: Pattern[str] = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$")
IDENTIFIER_RE: Pattern[str] = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
DOMAIN_LABEL_RE: Pattern[str] = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
CONTROL_CHARS_RE: Pattern[str] = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class InputValidationIssue:
    """Machine-readable validation failure detail."""

    field: str
    code: str
    message: str


class InputValidationError(ValueError):
    """Raised when an input value does not satisfy the requested rule."""

    def __init__(self, field: str, code: str, message: str) -> None:
        super().__init__(f"{field}: {message}")
        self.issue = InputValidationIssue(field=field, code=code, message=message)


def _fail(field: str, code: str, message: str) -> None:
    raise InputValidationError(field, code, message)


def _field_name(field: str) -> str:
    return (field or "value").strip() or "value"


def validate_text(
    value: object,
    *,
    field: str = "value",
    min_length: int = 0,
    max_length: int = DEFAULT_MAX_TEXT_LENGTH,
    pattern: Pattern[str] | None = None,
    allow_control_chars: bool = False,
) -> str:
    """Validate a bounded text field and return the stripped string.

    ``pattern`` is an allowlist regex that must match the whole
    stripped value.  Control characters are rejected by default because
    they frequently become log-injection / header-smuggling primitives
    when later layers concatenate strings for diagnostics.
    """

    name = _field_name(field)
    if not isinstance(value, str):
        _fail(name, "type", "must be a string")
    text = value.strip()
    if len(text) < min_length:
        _fail(name, "too_short", f"must be at least {min_length} characters")
    if len(text) > max_length:
        _fail(name, "too_long", f"must be at most {max_length} characters")
    if not allow_control_chars and CONTROL_CHARS_RE.search(text):
        _fail(name, "control_char", "must not contain control characters")
    if pattern is not None and pattern.fullmatch(text) is None:
        _fail(name, "pattern", "contains characters outside the allowlist")
    return text


def validate_slug(
    value: object,
    *,
    field: str = "slug",
    max_length: int = MAX_SLUG_LENGTH,
) -> str:
    """Validate a stable URL/catalog slug.

    Accepted shape: lower-case ASCII letters, digits, ``_`` and ``-``;
    must start and end with an alphanumeric character.  The helper does
    not interpret path separators or filesystem semantics; SC.7.5 owns
    path traversal protection.
    """

    text = validate_text(
        value,
        field=field,
        min_length=1,
        max_length=max_length,
    ).lower()
    if SLUG_RE.fullmatch(text) is None:
        _fail(
            _field_name(field),
            "slug",
            "must use lowercase letters, digits, '-' or '_' and start/end alnum",
        )
    return text


def validate_identifier(
    value: object,
    *,
    field: str = "identifier",
    max_length: int = MAX_IDENTIFIER_LENGTH,
) -> str:
    """Validate a Python/SQL-style symbolic identifier.

    This only validates shape.  It is not a permission slip for string
    interpolation into SQL; SC.7.3 owns parameterized query templates.
    """

    text = validate_text(
        value,
        field=field,
        min_length=1,
        max_length=max_length,
    )
    if IDENTIFIER_RE.fullmatch(text) is None:
        _fail(
            _field_name(field),
            "identifier",
            "must start with a letter or '_' and contain only letters, digits, '_'",
        )
    return text


def normalize_email(value: object, *, field: str = "email") -> str:
    """Validate and normalise an email address for account lookup.

    This intentionally implements a conservative SaaS-login shape, not
    the full RFC 5322 grammar.  Quoted local parts, comments, IP-literal
    domains, and whitespace folding are rejected so generated apps do
    not inherit surprising parser differences between frontend, backend,
    and identity-provider layers.
    """

    email = validate_text(
        value,
        field=field,
        min_length=3,
        max_length=MAX_EMAIL_LENGTH,
    ).lower()
    name = _field_name(field)
    if email.count("@") != 1:
        _fail(name, "email", "must contain exactly one '@'")
    local, domain = email.split("@", 1)
    if not local or not domain:
        _fail(name, "email", "must include local part and domain")
    if len(local) > MAX_EMAIL_LOCAL_LENGTH:
        _fail(name, "email_local_too_long", "local part is too long")
    if local.startswith(".") or local.endswith(".") or ".." in local:
        _fail(name, "email_local", "local part has invalid dot placement")
    if not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+", local):
        _fail(name, "email_local", "local part contains unsupported characters")
    labels = domain.split(".")
    if len(labels) < 2:
        _fail(name, "email_domain", "domain must include a public suffix")
    if any(label == "" for label in labels):
        _fail(name, "email_domain", "domain labels must not be empty")
    if any(DOMAIN_LABEL_RE.fullmatch(label) is None for label in labels):
        _fail(name, "email_domain", "domain label contains unsupported characters")
    return email


def validate_enum(
    value: object,
    allowed: Iterable[str],
    *,
    field: str = "value",
    case_sensitive: bool = False,
) -> str:
    """Validate that ``value`` is one of ``allowed`` and return it.

    ``allowed`` is consumed into a local tuple so callers may pass any
    iterable without creating module-level mutable state.
    """

    name = _field_name(field)
    raw = validate_text(value, field=name, min_length=1, max_length=128)
    choices = tuple(allowed)
    if not choices:
        _fail(name, "enum_config", "allowed choices must not be empty")
    if case_sensitive:
        if raw in choices:
            return raw
    else:
        lowered = raw.lower()
        for choice in choices:
            if lowered == choice.lower():
                return choice
    _fail(name, "enum", f"must be one of: {', '.join(choices)}")


def validate_int_range(
    value: object,
    *,
    field: str = "value",
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    """Validate an integer with optional inclusive bounds."""

    name = _field_name(field)
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(name, "type", "must be an integer")
    if minimum is not None and value < minimum:
        _fail(name, "too_small", f"must be at least {minimum}")
    if maximum is not None and value > maximum:
        _fail(name, "too_large", f"must be at most {maximum}")
    return value


__all__ = [
    "CONTROL_CHARS_RE",
    "DEFAULT_MAX_TEXT_LENGTH",
    "DOMAIN_LABEL_RE",
    "IDENTIFIER_RE",
    "InputValidationError",
    "InputValidationIssue",
    "MAX_EMAIL_LENGTH",
    "MAX_EMAIL_LOCAL_LENGTH",
    "MAX_IDENTIFIER_LENGTH",
    "MAX_SLUG_LENGTH",
    "SLUG_RE",
    "normalize_email",
    "validate_enum",
    "validate_identifier",
    "validate_int_range",
    "validate_slug",
    "validate_text",
]
