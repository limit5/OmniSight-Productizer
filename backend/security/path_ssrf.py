"""SC.7.5 — OWASP path-traversal and SSRF helpers for generated apps.

Small framework-agnostic helpers intended for generated FastAPI /
service templates.  Path helpers validate relative filesystem names
before callers join them to a trusted base directory.  URL helpers
canonicalise public HTTP(S) targets and apply a static SSRF blocklist
before any server-side fetcher sees the URL.

Security boundary:

  * This module covers path traversal checks and static SSRF gates only.
  * The SSRF gate does not perform DNS resolution.  Callers that fetch
    remote URLs still need HTTP-client controls for redirects, final IP
    auditing, fetch-once semantics, and DNS rebinding protection.
  * Input validation, output encoding, SQL parameterisation, and CSRF
    templates are separate SC.7 rows.

All module-level state is immutable constants / compiled regexes.
Cross-worker safety follows SOP Step 1 answer #1: each uvicorn worker
derives identical guards from the same source code; there is no shared
cache, singleton, or runtime mutation.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Pattern
from urllib.parse import unquote, urlsplit, urlunsplit


DEFAULT_MAX_PATH_LENGTH = 512
DEFAULT_ALLOWED_URL_SCHEMES = frozenset({"http", "https"})
CLOUD_METADATA_IP = "169.254.169.254"

BLOCKED_HOSTNAME_LITERALS = frozenset({
    "localhost",
    "ip6-localhost",
    "ip6-loopback",
})

BLOCKED_HOSTNAME_SUFFIXES = (
    ".local",
    ".localhost",
    ".internal",
    ".lan",
    ".home",
    ".home.arpa",
    ".onion",
)

CONTROL_CHARS_RE: Pattern[str] = re.compile(r"[\x00-\x1f\x7f]")
DRIVE_LETTER_RE: Pattern[str] = re.compile(r"^[A-Za-z]:[\\/]")
HOSTNAME_CHAR_RE: Pattern[str] = re.compile(r"^[A-Za-z0-9._-]+$")


@dataclass(frozen=True)
class PathSsrIssue:
    """Machine-readable path / SSRF guard failure detail."""

    field: str
    code: str
    message: str


class PathSsrError(ValueError):
    """Raised when a path or server-side URL target is unsafe."""

    def __init__(self, field: str, code: str, message: str) -> None:
        super().__init__(f"{field}: {message}")
        self.issue = PathSsrIssue(field=field, code=code, message=message)


def _fail(field: str, code: str, message: str) -> None:
    raise PathSsrError(field, code, message)


def _field_name(field: str) -> str:
    return (field or "value").strip() or "value"


def validate_relative_path(
    value: object,
    *,
    field: str = "path",
    max_length: int = DEFAULT_MAX_PATH_LENGTH,
) -> str:
    """Validate a relative path and return a normalised slash path.

    The helper rejects absolute paths, Windows drive-letter paths,
    control characters, empty values, and ``..`` segments.  Percent
    escapes are decoded once before traversal checks so URL-derived
    inputs such as ``%2e%2e/secrets`` cannot bypass callers that receive
    raw path strings from a framework layer.
    """

    name = _field_name(field)
    if not isinstance(value, str):
        _fail(name, "type", "must be a string")
    if isinstance(max_length, bool) or not isinstance(max_length, int):
        _fail("max_length", "type", "must be an integer")
    if max_length < 1:
        _fail("max_length", "too_small", "must be at least 1")

    raw = value.strip()
    if not raw:
        _fail(name, "empty", "must not be empty")
    if len(raw) > max_length:
        _fail(name, "too_long", f"must be at most {max_length} characters")
    if CONTROL_CHARS_RE.search(raw):
        _fail(name, "control_char", "must not contain control characters")

    decoded = unquote(raw)
    if decoded != raw and CONTROL_CHARS_RE.search(decoded):
        _fail(name, "control_char", "must not contain control characters")

    candidate = decoded.replace("\\", "/")
    if candidate.startswith("/"):
        _fail(name, "absolute", "must be a relative path")
    if DRIVE_LETTER_RE.match(decoded):
        _fail(name, "drive_letter", "must not include a drive letter")

    path = PurePosixPath(candidate)
    parts = path.parts
    if not parts or str(path) == ".":
        _fail(name, "empty", "must not be empty")
    if any(part == ".." for part in parts):
        _fail(name, "traversal", "must not contain '..' segments")
    if path.is_absolute():
        _fail(name, "absolute", "must be a relative path")
    return str(path)


def resolve_path_within_base(
    base_dir: Path | str,
    relative_path: object,
    *,
    field: str = "path",
) -> Path:
    """Resolve ``relative_path`` under ``base_dir`` or raise on escape."""

    safe_path = validate_relative_path(relative_path, field=field)
    try:
        root = Path(base_dir).resolve(strict=False)
    except TypeError:
        _fail("base_dir", "type", "must be path-like")
    target = (root / safe_path).resolve(strict=False)
    try:
        target.relative_to(root)
    except ValueError:
        _fail(field, "traversal", "resolved path escapes base directory")
    return target


def normalize_public_url(
    value: object,
    *,
    field: str = "url",
    allowed_schemes: Iterable[str] = DEFAULT_ALLOWED_URL_SCHEMES,
) -> str:
    """Validate URL syntax and return a canonical HTTP(S) URL.

    This does not apply the SSRF destination blocklist.  Use
    ``validate_public_url`` when the URL will be fetched server-side.
    """

    name = _field_name(field)
    if not isinstance(value, str):
        _fail(name, "type", "must be a string")
    raw = value.strip()
    if not raw:
        _fail(name, "empty", "must not be empty")
    if CONTROL_CHARS_RE.search(raw):
        _fail(name, "control_char", "must not contain control characters")

    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        _fail(name, "parse", f"failed to parse URL: {exc}")

    scheme = (parts.scheme or "").lower()
    schemes = frozenset(item.lower() for item in allowed_schemes)
    if scheme not in schemes:
        _fail(name, "scheme", f"scheme must be one of: {', '.join(sorted(schemes))}")
    if not parts.hostname:
        _fail(name, "host", "must include a hostname")
    if "@" in (parts.netloc or ""):
        _fail(name, "userinfo", "must not include userinfo")

    host = _normalise_hostname(parts.hostname, field=name)
    try:
        port = parts.port
    except ValueError as exc:
        _fail(name, "port", f"invalid port: {exc}")

    netloc = _canonical_netloc(scheme, host, port)
    path = "" if parts.path == "/" else parts.path
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def extract_hostname(value: object, *, field: str = "url") -> str:
    """Return the normalised hostname from a URL."""

    canonical = normalize_public_url(value, field=field)
    host = urlsplit(canonical).hostname
    if not host:
        _fail(_field_name(field), "host", "must include a hostname")
    return host.lower()


def is_public_destination(host: object) -> bool:
    """Return True when ``host`` is not a static SSRF blocklist target."""

    if not isinstance(host, str):
        return False
    h = host.strip().lower()
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    h = h.rstrip(".")
    if not h:
        return False
    if h in BLOCKED_HOSTNAME_LITERALS:
        return False
    if any(h.endswith(suffix) for suffix in BLOCKED_HOSTNAME_SUFFIXES):
        return False

    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return _is_domain_host(h)

    return not (
        ip.is_loopback
        or ip.is_link_local
        or ip.is_private
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def validate_public_url(
    value: object,
    *,
    field: str = "url",
    allowed_schemes: Iterable[str] = DEFAULT_ALLOWED_URL_SCHEMES,
) -> str:
    """Return a canonical public URL or raise for SSRF-prone targets."""

    canonical = normalize_public_url(
        value,
        field=field,
        allowed_schemes=allowed_schemes,
    )
    host = extract_hostname(canonical, field=field)
    if not is_public_destination(host):
        _fail(
            _field_name(field),
            "blocked_destination",
            "destination matches the loopback / private / reserved blocklist",
        )
    return canonical


def _normalise_hostname(hostname: str, *, field: str) -> str:
    host = hostname.strip().lower()
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass

    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError:
        _fail(field, "host", "hostname must be IDNA-encodable")
    if not HOSTNAME_CHAR_RE.fullmatch(ascii_host):
        _fail(field, "host", "hostname contains unsupported characters")
    return ascii_host


def _canonical_netloc(scheme: str, host: str, port: int | None) -> str:
    if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        port = None
    rendered_host = f"[{host}]" if ":" in host else host
    if port is None:
        return rendered_host
    return f"{rendered_host}:{port}"


def _is_domain_host(host: str) -> bool:
    if not HOSTNAME_CHAR_RE.fullmatch(host):
        return False
    if all(ch.isdigit() or ch == "." for ch in host):
        return False
    labels = host.rstrip(".").split(".")
    if any(label == "" for label in labels):
        return False
    return True


__all__ = [
    "BLOCKED_HOSTNAME_LITERALS",
    "BLOCKED_HOSTNAME_SUFFIXES",
    "CLOUD_METADATA_IP",
    "CONTROL_CHARS_RE",
    "DEFAULT_ALLOWED_URL_SCHEMES",
    "DEFAULT_MAX_PATH_LENGTH",
    "DRIVE_LETTER_RE",
    "HOSTNAME_CHAR_RE",
    "PathSsrError",
    "PathSsrIssue",
    "extract_hostname",
    "is_public_destination",
    "normalize_public_url",
    "resolve_path_within_base",
    "validate_public_url",
    "validate_relative_path",
]
