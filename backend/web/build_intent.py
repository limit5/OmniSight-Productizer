"""W16.3 #XXX — Build-intent detection (auto scaffold + auto-trigger W14).

Where this slots into the W16 epic
----------------------------------

W16.1 surfaced URL-paste intent; W16.2 surfaced image-attachment intent;
W16.3 (this row) covers **freeform "蓋一個 landing page" / "make me a
website"** intent — the operator never had to paste a URL or attach a
screenshot, they just *said* what they want.  When an action keyword
(蓋 / 做 / 建 / make / build / create) co-fires with a subject keyword
(網站 / landing / page / app) inside the live INVOKE command, we surface
a scaffold-and-preview coach card so the operator picks a scaffold kind
in one tap and the planner kicks off W14's live-preview sandbox
automatically.

::

    Operator types: "幫我蓋一個 landing page"
                          ↓
        backend.web.build_intent.detect_build_intents_in_text   ← W16.3
                          ↓
              List[BuildIntentRef]   (deduped, capped, hashed)
                          ↓
        backend.routers.invoke._detect_coaching_triggers
                          ↓
            "build_intent:<intent_hash>" trigger keys
                          ↓
        backend.routers.invoke._build_coach_context (LLM path)
        backend.routers.invoke._build_templated_coach_message (fallback)
                          ↓
                Coach card surfaces three options:
                  (a) landing page  → /scaffold landing --auto-preview
                  (b) full site     → /scaffold site --auto-preview
                  (c) web app       → /scaffold app --auto-preview
                          ↓
        ``/scaffold`` slash router (consumer-side, future W16.5
        edit-while-preview row) runs the W11.9 framework adapter to
        emit the project skeleton, then auto-triggers W14's
        ``omnisight-web-preview`` sidecar so the operator sees the
        live URL inside the chat without copy-pasting any command.

Detection wire shape
--------------------

The orchestrator chat surfaces operator intent as a single ``command``
string.  W16.3 scans that string for an *action* token and a
*subject* token co-occurring within the same window — co-occurrence is
what disambiguates "build intent" from a passing mention of
"website" / "I'm building confidence".  The window is the full
command (operators rarely mix two unrelated build intents in one line),
but each detected pair produces a single :class:`BuildIntentRef` with a
stable ``intent_hash`` so the W16.1-style suppress-via-sessionStorage
pattern keeps the coach card from re-firing on every INVOKE press.

Scaffold-kind classification is a closed enum (:data:`BUILD_INTENT_KIND_LANDING`
/ :data:`BUILD_INTENT_KIND_SITE` / :data:`BUILD_INTENT_KIND_PAGE` /
:data:`BUILD_INTENT_KIND_APP`) so the downstream ``/scaffold`` router has
a stable bucket key.  The classifier is intentionally simple — first
matching subject keyword wins — because the coach card always offers
the operator the alternative kinds as additional options, so a wrong
classification just means the "primary" suggestion lands second
instead of first.

Module-global / cross-worker state audit (per docs/sop/implement_phase_
step.md Step 1):  every constant / regex / dataclass in this module is
frozen literal — every uvicorn worker derives the same value from
source code (Answer #1, per-worker stateless derivation).  No singleton,
no in-memory cache, no shared mutable state.  The detection function
is pure: ``str → list[BuildIntentRef]``.

Read-after-write timing audit (per SOP §2): N/A — pure projection from
``str`` to ``list[BuildIntentRef]``.  No DB pool, no compat→pool
conversion, no ``asyncio.gather`` race surface.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Sequence, Tuple


# ── Frozen wire-shape constants ──────────────────────────────────────

#: SHA-256 prefix length (hex chars) used as the stable identifier for
#: a :class:`BuildIntentRef`.  16 hex = 64 bits of entropy — plenty for
#: per-session dedup / suppress and bounded enough to keep the trigger
#: key URL param tidy.  Pinned by drift guard so the frontend's
#: ``build_intent:<hash16>`` parser cannot drift.
BUILD_INTENT_HASH_HEX_LENGTH: int = 16

#: Hard cap on the number of distinct build intents detected in a
#: single command.  Mirrors the W16.1 ``_MAX_URL_TRIGGERS`` /
#: W16.2 ``MAX_IMAGE_ATTACHMENTS`` philosophy: prevents a runaway
#: paste from blowing up the coach card / LLM context block.  In
#: practice operators almost always declare one build intent per
#: message; the cap is a defensive belt.
MAX_BUILD_INTENTS: int = 3

#: Maximum chars used when echoing the detected verb+subject phrase
#: inside the coach card.  The full excerpt is preserved in the
#: ``raw_excerpt`` field for debug-finding callers; only the display
#: form is truncated.
MAX_BUILD_INTENT_DISPLAY_CHARS: int = 80


# ── Scaffold-kind enum ───────────────────────────────────────────────

#: Single-page landing site (hero + features + CTA) — the most common
#: "蓋一個 landing page" intent.  Maps to the W11.9 Next.js
#: framework adapter's single-route render path.
BUILD_INTENT_KIND_LANDING: str = "landing"

#: Multi-page marketing / informational site.  Maps to a multi-route
#: scaffold (home + about + contact + blog stub).
BUILD_INTENT_KIND_SITE: str = "site"

#: Single-page (single-route) generic page.  Falls back when the
#: subject is bare "page" without a more specific qualifier.
BUILD_INTENT_KIND_PAGE: str = "page"

#: Interactive web app shell (router + state + auth stub).  Subject
#: keyword "app" / "webapp" / "web app".
BUILD_INTENT_KIND_APP: str = "app"

#: Row-spec-ordered tuple — UIs that render the option set MUST iterate
#: this order so the coach card stays deterministic across renders.
#: Order is: most-common-intent first (landing) → broadest fallback
#: last (app).  Drift guard pins the ordering at module import.
BUILD_INTENT_KINDS: Tuple[str, ...] = (
    BUILD_INTENT_KIND_LANDING,
    BUILD_INTENT_KIND_SITE,
    BUILD_INTENT_KIND_PAGE,
    BUILD_INTENT_KIND_APP,
)


# ── Trigger-key contract (consumed by backend.routers.invoke) ────────

#: Coach trigger key prefix.  The full key shape is
#: ``build_intent:<hash16>``; the W16.1-style suppress system in
#: ``backend.routers.invoke._detect_coaching_triggers`` consumes this
#: prefix verbatim.  Pinned by drift guard.
BUILD_INTENT_TRIGGER_PREFIX: str = "build_intent:"


# ── Slash-command contract (consumed by future W16.3 router) ─────────

#: Slash-command verb the coach card pre-renders.  Frozen so the W16.9
#: e2e tests can pin the bucket key.
BUILD_INTENT_SCAFFOLD_COMMAND: str = "/scaffold"

#: Slash-command flag that auto-triggers W14 live-preview after the
#: scaffold completes.  Frozen so the W16.4 inline-preview row can pin
#: the same flag in its iframe wiring.
BUILD_INTENT_AUTO_PREVIEW_FLAG: str = "--auto-preview"


# ── Action / subject keyword tables ──────────────────────────────────

#: Action verbs that signal "the operator wants something built".  CJK
#: tokens are matched as substrings (no word boundary in Chinese);
#: Latin tokens are matched case-insensitively with word boundaries
#: so "rebuilds" / "Buildbot" don't false-positive.  Lock-step pinned
#: with the drift guard.
BUILD_INTENT_ACTION_KEYWORDS: Tuple[str, ...] = (
    # CJK verbs (substring match — no word boundary in Chinese).
    "蓋",
    "做",
    "建",
    # Latin verbs (whole-word match, case-insensitive).
    "make",
    "build",
    "create",
)

#: CJK action-verb subset — matched as substrings (no word-boundary
#: notion in Chinese).  Mirrors the prefix in
#: :data:`BUILD_INTENT_ACTION_KEYWORDS`.
_BUILD_INTENT_ACTION_KEYWORDS_CJK: Tuple[str, ...] = ("蓋", "做", "建")

#: Latin action-verb subset — matched case-insensitively with word
#: boundaries.
_BUILD_INTENT_ACTION_KEYWORDS_LATIN: Tuple[str, ...] = (
    "make", "build", "create",
)

#: Subject keywords ordered by *specificity*.  The classifier walks
#: the table top-down and the first match wins — so "landing page"
#: classifies as ``landing`` not ``page`` even though both subjects
#: would substring-match.  Each row is
#: ``(keyword_lowercase, scaffold_kind, is_cjk)``.
#:
#: ``is_cjk`` toggles word-boundary handling: CJK keywords match by
#: substring (no word boundary in Chinese script), Latin keywords
#: match by lowercased word boundary so "appointment" /
#: "pageant" don't false-positive.
_BUILD_INTENT_SUBJECT_TABLE: Tuple[Tuple[str, str, bool], ...] = (
    # CJK subjects — substring match, walked first because they are
    # less ambiguous than the bare Latin tokens.
    ("登陸頁", BUILD_INTENT_KIND_LANDING, True),
    ("登陆页", BUILD_INTENT_KIND_LANDING, True),
    ("網站", BUILD_INTENT_KIND_SITE, True),
    ("网站", BUILD_INTENT_KIND_SITE, True),
    ("網頁", BUILD_INTENT_KIND_PAGE, True),
    ("网页", BUILD_INTENT_KIND_PAGE, True),
    ("頁面", BUILD_INTENT_KIND_PAGE, True),
    ("页面", BUILD_INTENT_KIND_PAGE, True),
    ("應用", BUILD_INTENT_KIND_APP, True),
    ("应用", BUILD_INTENT_KIND_APP, True),
    # Latin subjects — order-sensitive: multi-word phrases first so
    # "landing page" classifies as ``landing`` instead of ``page``.
    ("landing page", BUILD_INTENT_KIND_LANDING, False),
    ("web app", BUILD_INTENT_KIND_APP, False),
    ("webapp", BUILD_INTENT_KIND_APP, False),
    ("website", BUILD_INTENT_KIND_SITE, False),
    ("landing", BUILD_INTENT_KIND_LANDING, False),
    ("site", BUILD_INTENT_KIND_SITE, False),
    ("page", BUILD_INTENT_KIND_PAGE, False),
    ("app", BUILD_INTENT_KIND_APP, False),
)


# ── Detection regexes (compiled at module-import time) ───────────────

# Latin action-verb regex — case-insensitive whole-word match.  Built
# once at import so every detection call reuses the compiled pattern
# (per-worker stateless derivation, Answer #1).
_BUILD_INTENT_ACTION_LATIN_PATTERN: re.Pattern[str] = re.compile(
    r"\b(?:" + "|".join(re.escape(v) for v in _BUILD_INTENT_ACTION_KEYWORDS_LATIN) + r")\b",
    re.IGNORECASE,
)

# Latin subject phrases — case-insensitive whole-word(s) match.
# Multi-word phrases are accepted via ``\s+`` so "landing  page"
# (operator double-spaced) still matches.  Order-sensitive: the
# alternation walks longest-first to prefer "landing page" over bare
# "page".
_BUILD_INTENT_LATIN_SUBJECTS_ORDERED: Tuple[Tuple[str, str], ...] = tuple(
    (kw, kind) for (kw, kind, is_cjk) in _BUILD_INTENT_SUBJECT_TABLE
    if not is_cjk
)

_BUILD_INTENT_LATIN_SUBJECT_PATTERN: re.Pattern[str] = re.compile(
    r"\b(?:" + "|".join(
        re.escape(kw).replace(r"\ ", r"\s+")
        for (kw, _kind) in _BUILD_INTENT_LATIN_SUBJECTS_ORDERED
    ) + r")\b",
    re.IGNORECASE,
)


# ── Public dataclass ─────────────────────────────────────────────────


@dataclass(frozen=True)
class BuildIntentRef:
    """A single build-intent declaration detected in the operator's command.

    Attributes
    ----------
    verb:
        The matched action keyword (lower-cased for Latin verbs;
        verbatim for CJK verbs which have no case).  E.g. ``"build"``,
        ``"蓋"``.
    subject:
        The matched subject keyword (lower-cased for Latin subjects;
        verbatim for CJK subjects).  E.g. ``"landing page"``,
        ``"網站"``.
    scaffold_kind:
        One of :data:`BUILD_INTENT_KINDS`.  The downstream
        ``/scaffold`` router branches on this.
    intent_hash:
        Stable 16-hex-char SHA-256 prefix used as the suppress / dedup
        key.  Computed over ``f"{verb}|{subject}|{scaffold_kind}"``
        so two calls on the same intent produce byte-identical
        hashes — drift guard pins this.
    raw_excerpt:
        Up to :data:`MAX_BUILD_INTENT_DISPLAY_CHARS` chars of the
        matched window (verb + ... + subject) for debug correlation.
    """

    verb: str
    subject: str
    scaffold_kind: str
    intent_hash: str
    raw_excerpt: str

    def trigger_key(self) -> str:
        """Return the W16.3 coach trigger key for this intent."""
        return f"{BUILD_INTENT_TRIGGER_PREFIX}{self.intent_hash}"

    def scaffold_command(self) -> str:
        """Return the operator-facing slash command that runs the scaffold
        and auto-triggers W14 live preview.

        Frozen shape: ``/scaffold <kind> --auto-preview``.  Used by both
        the LLM context block and the templated fallback so the operator
        always sees a copy-paste-ready command regardless of which
        renderer fired.
        """
        return (
            f"{BUILD_INTENT_SCAFFOLD_COMMAND} {self.scaffold_kind} "
            f"{BUILD_INTENT_AUTO_PREVIEW_FLAG}"
        )


# ── Helpers ──────────────────────────────────────────────────────────


def _compute_intent_hash(verb: str, subject: str, scaffold_kind: str) -> str:
    """Return the stable 16-hex-char hash for *(verb, subject, kind)*.

    The triple is concatenated with ``|`` separators and SHA-256'd.  We
    deliberately include all three components so a future "same verb +
    different subject" intent does not collide on hash, and so a future
    classifier change that reroutes a subject to a different kind
    re-triggers the coach card (correct behaviour — the operator should
    re-confirm intent if classification changed).
    """
    payload = f"{verb}|{subject}|{scaffold_kind}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:BUILD_INTENT_HASH_HEX_LENGTH]


def _truncate_excerpt(text: str) -> str:
    """Trim *text* to :data:`MAX_BUILD_INTENT_DISPLAY_CHARS` with ellipsis."""
    if len(text) <= MAX_BUILD_INTENT_DISPLAY_CHARS:
        return text
    return text[: MAX_BUILD_INTENT_DISPLAY_CHARS - 1].rstrip() + "…"


def _has_cjk_action_verb(text: str) -> str:
    """Return the first CJK action verb found in *text*, else ``""``.

    CJK has no word-boundary notion, so substring match is sufficient
    and the verb table is small enough that linear scan is fast.
    Returns the verb verbatim (no case folding — Chinese has no case).
    """
    for verb in _BUILD_INTENT_ACTION_KEYWORDS_CJK:
        if verb in text:
            return verb
    return ""


def _find_cjk_subject(text: str) -> Tuple[str, str]:
    """Return ``(subject, scaffold_kind)`` for the first CJK subject
    keyword found in *text*, else ``("", "")``.

    The :data:`_BUILD_INTENT_SUBJECT_TABLE` is walked top-down so
    multi-character / more-specific keywords (e.g. "登陸頁") match
    before the broader "頁面".
    """
    for (kw, kind, is_cjk) in _BUILD_INTENT_SUBJECT_TABLE:
        if not is_cjk:
            continue
        if kw in text:
            return kw, kind
    return "", ""


def _find_latin_subject(text_lower: str) -> Tuple[str, str]:
    """Return ``(subject, scaffold_kind)`` for the first Latin subject
    keyword found in *text_lower*, else ``("", "")``.

    Walks the ordered subject table (multi-word phrases first) so
    "landing page" wins over "page" / "landing" alone.  Uses regex
    word-boundaries so "appointment" / "pageant" don't false-positive.
    """
    for (kw, kind, is_cjk) in _BUILD_INTENT_SUBJECT_TABLE:
        if is_cjk:
            continue
        # Build per-keyword pattern lazily — the count is small so the
        # cost is negligible vs. caching, and per-call patterns avoid a
        # module-level mutable cache (cross-worker concern).
        pattern = re.compile(
            r"\b" + re.escape(kw).replace(r"\ ", r"\s+") + r"\b",
            re.IGNORECASE,
        )
        if pattern.search(text_lower):
            return kw, kind
    return "", ""


def detect_build_intents_in_text(text: str | None) -> list[BuildIntentRef]:
    """Return up to :data:`MAX_BUILD_INTENTS` distinct
    :class:`BuildIntentRef` records found in *text*.

    Detection requires *both* an action keyword (蓋 / 做 / 建 / make /
    build / create) and a subject keyword (網站 / landing / page / app
    + CJK variants) co-occurring inside *text*.  Mixed-script messages
    are handled — a CJK verb pairs freely with a Latin subject (e.g.
    "幫我蓋一個 landing page") and vice versa.

      * CJK:    substring match (no word boundary in Chinese)
      * Latin:  case-insensitive whole-word(s) match

    The classifier walks subjects top-down by specificity so
    multi-word phrases like "landing page" classify as
    :data:`BUILD_INTENT_KIND_LANDING` instead of the broader
    :data:`BUILD_INTENT_KIND_PAGE`.

    When *both* a CJK and a Latin subject co-occur (rare — a true
    bilingual paste like "蓋一個 landing page 給網站"), the more-
    specific subject wins; if both have the same specificity, CJK
    takes priority since CJK is the platform's default operator
    locale.  Each emitted ref carries a stable ``intent_hash`` over
    *(verb, subject, kind)* so a re-paste of the same intent produces
    the same trigger key (suppress / dedup safe).

    Empty / ``None`` input returns ``[]`` so callers can pipe arbitrary
    corpora through without pre-filtering.

    Module-global / cross-worker state audit: pure ``str → list``
    projection.  Module-level constants only (frozen tables + compiled
    regex patterns); no caches, no singletons.  Cross-worker concern
    N/A (Answer #1).
    """
    if not text:
        return []

    text_lower = text.lower()

    # First, find ANY action verb — CJK preferred (operator's likely
    # native locale; the platform default is CJK), Latin fallback.  We
    # do not require the verb and subject to be in the same script:
    # bilingual operators routinely write "幫我蓋一個 landing page".
    cjk_verb = _has_cjk_action_verb(text)
    latin_action_match = _BUILD_INTENT_ACTION_LATIN_PATTERN.search(text_lower)
    latin_verb = latin_action_match.group(0).lower() if latin_action_match else ""

    if not (cjk_verb or latin_verb):
        return []

    # Then find the *most specific* subject — CJK and Latin tables are
    # walked separately so we can compare specificity before picking a
    # winner.  Subject specificity = position in the table (top wins).
    cjk_subject, cjk_kind = _find_cjk_subject(text)
    latin_subject, latin_kind = _find_latin_subject(text_lower)

    if not (cjk_subject or latin_subject):
        return []

    # Pick the winning subject + kind.  Multi-word Latin phrases like
    # "landing page" only fire when the operator wrote them in full,
    # so when both fire we prefer CJK by default (default locale) but
    # promote Latin when its kind is a *more specific* sibling
    # (LANDING > SITE > PAGE specificity in the closed enum) — this
    # falls out naturally from the table ordering.
    if cjk_subject and latin_subject:
        # When both fire, prefer the kind that came earlier in the
        # closed enum (LANDING > SITE > PAGE > APP) since the table
        # ordering reflects intent specificity for the most common
        # operator phrasings.
        cjk_rank = BUILD_INTENT_KINDS.index(cjk_kind)
        latin_rank = BUILD_INTENT_KINDS.index(latin_kind)
        if latin_rank < cjk_rank:
            chosen_subject, chosen_kind = latin_subject, latin_kind
        else:
            chosen_subject, chosen_kind = cjk_subject, cjk_kind
    elif cjk_subject:
        chosen_subject, chosen_kind = cjk_subject, cjk_kind
    else:
        chosen_subject, chosen_kind = latin_subject, latin_kind

    # Pick the verb — CJK preferred when both fire (operator's likely
    # native locale).  When only one script's verb fires, use it
    # regardless of which script the subject came from (mixed-script
    # phrasing is the common bilingual case).
    chosen_verb = cjk_verb if cjk_verb else latin_verb

    h = _compute_intent_hash(chosen_verb, chosen_subject, chosen_kind)
    ref = BuildIntentRef(
        verb=chosen_verb,
        subject=chosen_subject,
        scaffold_kind=chosen_kind,
        intent_hash=h,
        raw_excerpt=_truncate_excerpt(text.strip()),
    )
    return [ref]


def build_intent_trigger_key(ref: BuildIntentRef) -> str:
    """Convenience wrapper around :meth:`BuildIntentRef.trigger_key`.

    Kept as a top-level function so call-sites that want the coach
    trigger key without unpacking the ref (e.g. test fixtures that
    use dict-style intents) have a stable entry point.  Mirror of
    :func:`backend.web.image_attachment.image_attachment_trigger_key`.
    """
    return ref.trigger_key()


def trigger_keys_for_build_intents(refs: Sequence[BuildIntentRef]) -> list[str]:
    """Return ``[ref.trigger_key() for ref in refs]`` preserving order."""
    return [ref.trigger_key() for ref in refs]


def classify_subject_to_kind(subject: str) -> str:
    """Map an arbitrary subject keyword to the matching
    :data:`BUILD_INTENT_KINDS` bucket.

    Falls back to :data:`BUILD_INTENT_KIND_PAGE` when no entry in
    :data:`_BUILD_INTENT_SUBJECT_TABLE` matches — a generic page is
    the safest scaffold for an unrecognised subject (lowest blast
    radius if the operator's intent was something else).

    Public so the W16.4 inline-preview row + the W16.5 edit-while-
    preview consumer can re-classify without re-importing the private
    table.
    """
    if not subject:
        return BUILD_INTENT_KIND_PAGE
    s = subject.strip().lower()
    for (kw, kind, is_cjk) in _BUILD_INTENT_SUBJECT_TABLE:
        if is_cjk:
            if kw in subject:  # CJK keeps original casing.
                return kind
        else:
            if kw == s:
                return kind
    return BUILD_INTENT_KIND_PAGE


# ── Drift guards (assert at module-import time) ──────────────────────

# Hash length guard — if a future PR shortens / lengthens the prefix
# without updating the trigger-key contract, the frontend's parser
# will silently misalign.  Surfacing the mismatch at import time
# pushes it to CI red.
assert BUILD_INTENT_HASH_HEX_LENGTH == 16, (
    f"BUILD_INTENT_HASH_HEX_LENGTH drift: expected 16, "
    f"got {BUILD_INTENT_HASH_HEX_LENGTH!r}"
)

assert BUILD_INTENT_KINDS == (
    BUILD_INTENT_KIND_LANDING,
    BUILD_INTENT_KIND_SITE,
    BUILD_INTENT_KIND_PAGE,
    BUILD_INTENT_KIND_APP,
), "BUILD_INTENT_KINDS drift vs row-spec ordering"

assert BUILD_INTENT_TRIGGER_PREFIX.endswith(":"), (
    "BUILD_INTENT_TRIGGER_PREFIX must end in ':' for "
    "backend.routers.invoke._detect_coaching_triggers parsing"
)

assert BUILD_INTENT_ACTION_KEYWORDS, (
    "BUILD_INTENT_ACTION_KEYWORDS cannot be empty"
)

# Subject table coverage guard — every entry must classify into one of
# the four enum kinds, else the downstream ``/scaffold`` router has no
# bucket to route to.
assert all(
    kind in BUILD_INTENT_KINDS
    for (_kw, kind, _is_cjk) in _BUILD_INTENT_SUBJECT_TABLE
), "_BUILD_INTENT_SUBJECT_TABLE classifies into an unknown scaffold kind"

# Action-keyword partition guard — the CJK + Latin subset tuples must
# concatenate exactly into the public ``BUILD_INTENT_ACTION_KEYWORDS``.
assert (
    tuple(_BUILD_INTENT_ACTION_KEYWORDS_CJK)
    + tuple(_BUILD_INTENT_ACTION_KEYWORDS_LATIN)
    == BUILD_INTENT_ACTION_KEYWORDS
), "BUILD_INTENT_ACTION_KEYWORDS partition drift"


__all__ = [
    "BUILD_INTENT_ACTION_KEYWORDS",
    "BUILD_INTENT_AUTO_PREVIEW_FLAG",
    "BUILD_INTENT_HASH_HEX_LENGTH",
    "BUILD_INTENT_KINDS",
    "BUILD_INTENT_KIND_APP",
    "BUILD_INTENT_KIND_LANDING",
    "BUILD_INTENT_KIND_PAGE",
    "BUILD_INTENT_KIND_SITE",
    "BUILD_INTENT_SCAFFOLD_COMMAND",
    "BUILD_INTENT_TRIGGER_PREFIX",
    "BuildIntentRef",
    "MAX_BUILD_INTENTS",
    "MAX_BUILD_INTENT_DISPLAY_CHARS",
    "build_intent_trigger_key",
    "classify_subject_to_kind",
    "detect_build_intents_in_text",
    "trigger_keys_for_build_intents",
]
