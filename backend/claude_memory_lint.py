"""γ-1 leg-3 — Claude-memory lint profile (DETECT-ONLY, blocking/advisory).

The claude lane LINTS, it never de-imperativizes (design tension 1: the
corpus's value IS directive text; authority lives in the kernel, not in
text filtering) and never rewrites (verbatim-mirror invariant — a secret
hit fails lint; the HUMAN fixes the file and re-ingests).

BLOCKING (publish-refusing): secret-scan — reuses the shipped
``security/secret_filter._SECRET_PATTERNS`` in detect-only mode, with a
localhost/test-DSN allowlist (integrity-audit MINOR-5: the corpus carries
`postgresql://u6test:u6test@localhost:5455`-style TEST DSNs that would
false-positive every handoff doc) plus the Atlassian ``ATATT`` token
pattern (this project's hottest secret class, absent from the shared
table — added here locally; promoting it to the shared filter is a
separate reviewed change).

ADVISORY (shown at publish, never blocking): oversize body (>48KB),
dead ``[[links]]`` (target not in the known slug set), stale-marker
prose (SUPERSEDED/STALE annotations).
"""

from __future__ import annotations

import re

_ATATT_RE = re.compile(r"\bATATT[0-9A-Za-z_\-=]{20,}")
_LOCALHOST_RE = re.compile(r"localhost|127\.0\.0\.1")
# Corpus-tuned allowlists (verified against the LIVE store at build time):
#  * pg_internal/ai_internal are LOG-SCRUB topology labels (docker-internal
#    hostnames like pg-primary/ai_gateway) — the memory lane documents
#    architecture BY DESIGN; they are not credentials.
#  * pure-hex ≥40-char strings are HASHES the corpus records (image digests,
#    suite sha256s, Change-Ids) — not bearer tokens (tokens are base64ish
#    mixed-alphabet). Everything else stays blocking.
_TOPOLOGY_LABELS = frozenset({"pg_internal", "ai_internal"})
_PURE_HEX_RE = re.compile(r"\A[0-9a-f]{40,}\Z")
#  * Gerrit Change-Ids (I+40hex) are public identifiers.
#  * strings whose surrounding CONTEXT says "fingerprint"/"SHA256" are
#    public-key fingerprints (identity material, not credentials).
_CHANGE_ID_RE = re.compile(r"\AI[0-9a-f]{40}\Z")
_FINGERPRINT_CTX_RE = re.compile(r"fingerprint|SHA256", re.IGNORECASE)
_STALE_RE = re.compile(r"\bSUPERSEDED\b|\bSTALE\b")
_LINK_RE = re.compile(r"\[\[([^\]|#]+)")
_OVERSIZE_B = 48 * 1024


def lint_body(
    body: str, *, known_slugs: "set[str] | None" = None,
) -> dict:
    """Return ``{"blocking": [...], "advisory": [...]}`` for one body."""
    blocking: list[str] = []
    advisory: list[str] = []

    # Secret scan — detect-only walk over the shared pattern table.
    from backend.security.secret_filter import _SECRET_PATTERNS

    for label, pat, _replacement in _SECRET_PATTERNS:
        if label in _TOPOLOGY_LABELS:
            continue  # topology masks, not credentials
        for m in pat.finditer(body):
            span = body[max(0, m.start() - 40):m.end() + 40]
            if _LOCALHOST_RE.search(span):
                continue  # test-DSN allowlist (never a prod secret shape)
            if _PURE_HEX_RE.match(m.group(0)):
                continue  # documented hash, not a token
            if _CHANGE_ID_RE.match(m.group(0)):
                continue  # Gerrit Change-Id — public identifier
            ctx = body[max(0, m.start() - 200):m.start()]
            if _FINGERPRINT_CTX_RE.search(ctx):
                continue  # labeled public-key fingerprint
            blocking.append(f"secret:{label}")
            break  # one flag per pattern class
    if _ATATT_RE.search(body):
        blocking.append("secret:atlassian_token")

    if len(body.encode("utf-8", "ignore")) > _OVERSIZE_B:
        advisory.append("oversize_body")
    if _STALE_RE.search(body):
        advisory.append("stale_marker")
    if known_slugs is not None:
        dead = sorted({
            t.strip() for t in (m.group(1) for m in _LINK_RE.finditer(body))
            if t.strip() and t.strip() not in known_slugs
        })
        if dead:
            advisory.append(f"dead_links:{len(dead)}")

    return {"blocking": sorted(set(blocking)), "advisory": advisory}
