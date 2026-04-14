"""Phase 62 — PII / secret scrubber for skill extraction.

Skill files derived from `workflow_runs` + agent transcripts can
inadvertently embed:

  * Absolute filesystem paths leaking host topology
  * Email addresses (operators, customers)
  * API keys, bearer tokens, AWS keys, GitHub PATs
  * Long base64 / hex strings that look like credentials
  * IP addresses pointing at internal infra

We replace each match with a class-tagged placeholder so the resulting
markdown is still useful for an LLM agent to learn from but doesn't
re-emit secrets if it gets quoted back.

`scrub(text)` returns ``(scrubbed_text, hits)`` where ``hits`` is a
``Counter[str]`` so the caller can both record telemetry and refuse to
promote a skill that scrubbed too aggressively (signal that the source
material was unsafe to extract from in the first place).
"""

from __future__ import annotations

import re
from collections import Counter

# Order matters: more specific patterns first so they don't get
# swallowed by the generic high-entropy catcher.
_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    # ── Provider-specific keys (high signal, near-zero false positive) ──
    ("aws_key", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"), "[AWS_KEY]"),
    ("github_pat", re.compile(r"\bghp_[A-Za-z0-9]{36}\b"), "[GITHUB_PAT]"),
    ("github_oauth", re.compile(r"\bgho_[A-Za-z0-9]{36}\b"), "[GITHUB_OAUTH]"),
    ("github_app", re.compile(r"\b(ghu|ghs)_[A-Za-z0-9]{36}\b"), "[GITHUB_APP]"),
    ("gitlab_pat", re.compile(r"\bglpat-[A-Za-z0-9_\-]{20}\b"), "[GITLAB_PAT]"),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), "[OPENAI_KEY]"),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b"), "[ANTHROPIC_KEY]"),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}\b"), "[SLACK_TOKEN]"),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b"), "[JWT]"),
    ("ssh_private_key",
     re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
                re.DOTALL), "[SSH_PRIVATE_KEY]"),

    # ── Generic secret-like assignments ──
    ("env_assign",
     re.compile(r"\b(api[_\-]?key|secret|token|password|passwd|pwd)\s*[=:]\s*['\"]?([^\s'\"]{8,})['\"]?",
                re.IGNORECASE),
     r"\1=[REDACTED]"),

    # ── PII ──
    ("email",
     re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
     "[EMAIL]"),

    # ── Filesystem paths ──
    # Absolute Unix paths that look real (avoid catching e.g. /usr/bin
    # in code samples — only catch /home/, /Users/, /root/, /var/lib/<host>).
    ("home_path",
     re.compile(r"/(?:home|Users|root)/[A-Za-z0-9._\-]+(?:/[A-Za-z0-9._\-]+)*"),
     "[HOME_PATH]"),

    # ── IPs ──
    # IPv4 with all four octets numeric. Skip 127.0.0.1 / 0.0.0.0 / local.
    ("ipv4",
     re.compile(r"\b(?!127\.0\.0\.1\b|0\.0\.0\.0\b)"
                r"(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b"),
     "[IPV4]"),

    # ── High-entropy long strings (catch-all, last) ──
    # 40+ char base64-ish blob. Last so specific keys above win first.
    ("high_entropy",
     re.compile(r"\b[A-Za-z0-9+/=_\-]{40,}\b"),
     "[OPAQUE_BLOB]"),
]


def scrub(text: str) -> tuple[str, Counter[str]]:
    """Return (scrubbed_text, hits_by_class)."""
    hits: Counter[str] = Counter()
    out = text
    for name, pat, repl in _PATTERNS:
        # Count first (so we can log even when repl uses backrefs).
        n = len(pat.findall(out))
        if n:
            hits[name] += n
            out = pat.sub(repl, out)
    return out, hits


# Promotion safety threshold. If a single skill triggers more than
# this many redactions, the source material is probably too sensitive
# to learn from at all — caller should refuse promotion.
SAFETY_THRESHOLD = 25


def is_safe_to_promote(hits: Counter[str]) -> bool:
    """A skill with >SAFETY_THRESHOLD scrub hits has so many secret
    fragments that even the redacted version is dangerous — the LLM
    can probably reconstruct what they were."""
    return sum(hits.values()) <= SAFETY_THRESHOLD
