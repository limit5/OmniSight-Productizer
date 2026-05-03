"""R20 Phase 0 — Output filter that redacts accidentally-leaked secrets.

Runs over LLM responses BEFORE they reach the chat / SSE stream / audit
log. Detects the most common secret formats by regex match. Defense in
depth: prompt hardening tries to stop the LLM from emitting secrets in
the first place, and this filter catches the cases where it does
anyway (regurgitation from retrieved context, hallucination of
plausible-looking values, malformed-but-still-secret tokens).

Pattern table is ordered most-specific first so e.g. a GitHub PAT
matches as ``github_pat`` before falling to the generic Bearer rule.

Internal hostnames are included on purpose — leaking
``pg-primary.omnisight.local`` to an external attacker is recon data
even if no credential is attached.
"""

from __future__ import annotations

import logging
import re

# (label, pattern, replacement). Keep ordering: specific → general.
_SECRET_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    # GitHub — order matters: gho_ (OAuth) checked BEFORE the generic
    # gh[psru]_ (PAT/SAML/refresh/user) so the more specific label
    # wins. `github_pat` charset deliberately excludes ``o`` to avoid
    # matching gho_ prefixes.
    ("github_oauth", re.compile(r"\bgho_[A-Za-z0-9]{30,}"), "[REDACTED:github_oauth]"),
    ("github_pat", re.compile(r"\bgh[psru]_[A-Za-z0-9]{30,}"), "[REDACTED:github_pat]"),
    # GitLab
    ("gitlab_pat", re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}"), "[REDACTED:gitlab_pat]"),
    # AWS
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "[REDACTED:aws_access_key]"),
    (
        "aws_secret",
        re.compile(
            r"\baws_secret_access_key\s*[:=]\s*['\"]?[A-Za-z0-9/+=]{40}['\"]?",
            re.I,
        ),
        "aws_secret_access_key=[REDACTED:aws_secret]",
    ),
    # Slack
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), "[REDACTED:slack_token]"),
    (
        "slack_webhook",
        re.compile(r"https?://hooks\.slack\.com/services/[A-Z0-9]+/[A-Z0-9]+/[A-Za-z0-9]+"),
        "https://hooks.slack.com/services/[REDACTED:slack_webhook]",
    ),
    # Stripe
    ("stripe", re.compile(r"\b(?:sk|pk|rk)_(?:test|live)_[A-Za-z0-9]{20,}"), "[REDACTED:stripe_key]"),
    # Provider API keys and key-like assignments
    ("google_api_key", re.compile(r"\bAIza[A-Za-z0-9_\-]{35}"), "[REDACTED:google_api_key]"),
    (
        "api_key_assignment",
        re.compile(
            r"\b(?:api[_-]?key|x-api-key|client_secret|secret_key)\s*[:=]\s*"
            r"['\"]?[A-Za-z0-9_.\-/+=]{20,}['\"]?",
            re.I,
        ),
        "api_key=[REDACTED:api_key]",
    ),
    # Anthropic — must run BEFORE openai because both start with sk-
    ("anthropic", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}"), "[REDACTED:anthropic_key]"),
    # OpenAI
    ("openai", re.compile(r"\bsk-[A-Za-z0-9]{20,}"), "[REDACTED:openai_key]"),
    # OAuth tokens commonly logged as form fields / JSON keys
    (
        "oauth_token",
        re.compile(
            r"\b(?:access_token|refresh_token|id_token|oauth_token)\s*[:=]\s*"
            r"['\"]?[A-Za-z0-9_.\-]{20,}['\"]?",
            re.I,
        ),
        "oauth_token=[REDACTED:oauth_token]",
    ),
    # Database URLs with embedded credentials
    (
        "database_url",
        re.compile(
            r"\b(?:postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|redis)://"
            r"[^:\s/@]+:[^@\s]+@[^\s)'\"]+",
            re.I,
        ),
        "[REDACTED:database_url]",
    ),
    # Generic Bearer (any header value > 20 chars after "Bearer ")
    ("bearer", re.compile(r"\bBearer\s+[A-Za-z0-9_.\-]{20,}", re.I), "Bearer [REDACTED:token]"),
    # JWT (3 base64 segments separated by dots, starting with eyJ)
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
        "[REDACTED:jwt]",
    ),
    # Cookie / Set-Cookie headers carrying non-JWT session-like values.
    # Ordered after JWT so token-shaped cookie values keep the more
    # specific jwt label.
    (
        "cookie",
        re.compile(
            r"\b((?:Cookie|Set-Cookie):\s*)[^\r\n]*"
            r"(?:session|sid|auth|token|jwt)[^=;]*=(?!\[REDACTED:)[^;\s]{12,}[^\r\n]*",
            re.I,
        ),
        r"\1[REDACTED:cookie]",
    ),
    # Private key blocks (SSH/PGP) — redact entire block
    (
        "private_key_block",
        re.compile(
            r"-----BEGIN (?:RSA |OPENSSH |PGP |EC |DSA )?PRIVATE KEY-----"
            r"[\s\S]+?"
            r"-----END (?:RSA |OPENSSH |PGP |EC |DSA )?PRIVATE KEY-----"
        ),
        "[REDACTED:private_key_block]",
    ),
    # Internal hostnames — production stack-specific names. Update when
    # adding a new internal-only host. These leaking is a recon signal
    # even without a credential attached.
    ("pg_internal", re.compile(r"\bpg-(?:primary|standby|test)\b"), "[REDACTED:internal_host]"),
    ("ai_internal", re.compile(r"\bai_(?:cache|engine|gateway|tunnel)\b"), "[REDACTED:internal_host]"),
    # GitHub PAT generic-looking pattern (last-ditch — long high-base-N alnum
    # in a context that looks credential-shaped). Ordered last so the
    # specific GitHub patterns above win.
    (
        "high_entropy_token",
        re.compile(
            r"(?<![A-Za-z0-9])"
            r"[A-Za-z0-9]{40,80}"
            r"(?![A-Za-z0-9])"
        ),
        "[REDACTED:high_entropy_token]",
    ),
]

# Allow-list: tokens / hostnames the redactor must NOT touch even if
# they match a generic pattern. These are public docs paths, common
# variable names, etc. that share shape with secrets.
_ALLOWLIST = {
    "claude-haiku-4-5-20251001",  # public model id
    "claude-opus-4-7",
    "claude-sonnet-4-6",
}

_LOG_REDACTED_RE = re.compile(r"\[REDACTED:[^\]]+\]")
_LOGGER_FILTER_NAME = "omnisight_secret_scrubber"


def redact(text: str) -> tuple[str, list[str]]:
    """Redact known-secret patterns from ``text``.

    Returns ``(redacted_text, fired_labels)`` where ``fired_labels`` is
    the list of pattern labels that matched (deduped, in firing order).
    Audit-log writers can record this list to track what kinds of
    secrets the system caught the LLM trying to emit — useful for
    finding the upstream leak source.
    """
    if not text:
        return text, []

    # The high_entropy_token pattern is genuinely loose — apply it only
    # if no specific pattern fired, otherwise we double-redact and the
    # first specific replacement gets clobbered.
    fired: list[str] = []
    out = text
    for label, pat, replacement in _SECRET_PATTERNS:
        if label == "high_entropy_token":
            # Skip the catch-all when we already fired a specific rule
            # — if we redacted a specific token already, we don't need
            # the loose entropy fallback. Reduces false positives on
            # long base64 image-data URIs etc.
            if fired:
                continue
        if pat.search(out):
            # Allow-list check: temporarily extract allowlisted matches,
            # apply redaction, then restore. Rare path; cheap enough.
            saved: list[tuple[str, str]] = []
            for safe in _ALLOWLIST:
                if safe in out:
                    placeholder = f"\x00ALLOWED_{len(saved)}\x00"
                    saved.append((placeholder, safe))
                    out = out.replace(safe, placeholder)
            new_out = pat.sub(replacement, out)
            for placeholder, safe in saved:
                new_out = new_out.replace(placeholder, safe)
            if new_out != out:
                fired.append(label)
                out = new_out
    return out, fired


def redact_for_log(text: str) -> tuple[str, list[str]]:
    """Redact known-secret patterns for log sinks.

    Chat output keeps pattern labels for audit metrics. Logs use the
    uniform ``[REDACTED]`` token required by KS.1.7 so downstream sinks
    never receive a secret-shaped value or a provider-specific label.
    """
    out, fired = redact(text)
    if fired:
        out = _LOG_REDACTED_RE.sub("[REDACTED]", out)
    return out, fired


class SecretScrubbingFilter(logging.Filter):
    """Stdlib logging filter that redacts secret-shaped message text.

    Module-global state audit: the filter reads immutable regex tables
    only; every worker derives identical scrubbed output from each log
    record and does not share mutable process-local state.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        scrubbed, _ = redact_for_log(message)
        if scrubbed != message:
            record.msg = scrubbed
            record.args = ()
        return True


def install_logging_filter(logger: logging.Logger | None = None) -> SecretScrubbingFilter:
    """Install the KS.1.7 secret scrubber on ``logger`` once."""
    target = logger or logging.getLogger()
    filt: SecretScrubbingFilter | None = None
    for existing in target.filters:
        if getattr(existing, "name", "") == _LOGGER_FILTER_NAME:
            filt = existing  # type: ignore[assignment]
            break
    if filt is None:
        filt = SecretScrubbingFilter(_LOGGER_FILTER_NAME)
        target.addFilter(filt)
    for handler in target.handlers:
        if not any(getattr(existing, "name", "") == _LOGGER_FILTER_NAME for existing in handler.filters):
            handler.addFilter(filt)
    return filt
