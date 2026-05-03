"""W16.5 #XXX — Edit-while-preview intent detection.

Where this slots into the W16 epic
----------------------------------

W16.1 surfaced URL-paste intent; W16.2 surfaced image-attachment intent;
W16.3 surfaced "蓋一個 landing page" build intent; W16.4 inlined the
``preview.ready`` SSE event so the chat surface mounts a sandboxed
``<iframe>`` the moment the dev server boots.  W16.5 (this row) closes
the **edit-while-preview live cycle**: when the operator already has a
live preview running and types something like 「header 大一點」 / "make
the button bigger" / "change the hero font", we surface a coach card
that routes the request through the agent edit pipeline so vite HMR
auto-reloads the iframe without the operator pasting any slash command.

::

    Operator types: "header 大一點"
                          ↓
        backend.web.edit_intent.detect_edit_intents_in_text   ← W16.5
                          ↓
              List[EditIntentRef]   (deduped, capped, hashed)
                          ↓
        backend.routers.invoke._detect_coaching_triggers
                          ↓
            "edit_while_preview:<edit_hash>" trigger keys
                          ↓
        backend.routers.invoke._build_coach_context (LLM path)
        backend.routers.invoke._build_templated_coach_message (fallback)
                          ↓
                Coach card surfaces three options:
                  (a) 直接套用 / Apply now → /edit-preview <ws> "<instr>"
                  (b) 預覽影響範圍 / Dry-run → /edit-preview <ws> --dry
                  (c) 改用對話 / Send to chat → keep typing
                          ↓
        ``/edit-preview`` slash router (consumer-side, future row)
        runs the agent edit pipeline against the workspace's source
        files; vite (the W14.1 sidecar's dev server) detects the file
        change via inotify and triggers HMR; the frontend SSE consumer
        receives a ``preview.hmr_reload`` event and refreshes the
        existing iframe in-place rather than appending a new chat row.

Detection wire shape
--------------------

The orchestrator chat surfaces operator intent as a single ``command``
string.  W16.5 scans that string for an *edit verb* OR *modifier hint*
(蓋/做 sit in W16.3; W16.5 keeps to "改/換/修/加/move/resize/bigger/
smaller/colour/font/...") co-occurring with a *target noun* (UI element
like header / footer / button / nav / 標題列 / 按鈕 / 導覽列).  Co-
occurrence is what disambiguates edit intent from a passing mention of
"button" / "I'll fix this later".

Each detected pair produces a single :class:`EditIntentRef` with a
stable ``edit_hash`` so the W16.1-style suppress-via-sessionStorage
pattern keeps the coach card from re-firing on every INVOKE press.

Module-global / cross-worker state audit (per docs/sop/implement_phase_
step.md Step 1):  every constant / regex / dataclass in this module is
a frozen literal — every uvicorn worker derives the same value from
source code (Answer #1, per-worker stateless derivation).  No
singleton, no in-memory cache, no shared mutable state.  The detection
function is pure: ``str → list[EditIntentRef]``.

Read-after-write timing audit (per SOP §2): N/A — pure projection from
``str`` to ``list[EditIntentRef]``.  No DB pool, no compat→pool
conversion, no ``asyncio.gather`` race surface.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Sequence, Tuple


# ── Frozen wire-shape constants ──────────────────────────────────────

#: SHA-256 prefix length (hex chars) used as the stable identifier for
#: an :class:`EditIntentRef`.  16 hex = 64 bits of entropy — plenty for
#: per-session dedup / suppress and bounded enough to keep the trigger
#: key URL param tidy.  Mirrors :data:`backend.web.build_intent.
#: BUILD_INTENT_HASH_HEX_LENGTH` so the W16.1/W16.2/W16.3/W16.5 trigger
#: families share the same hex width across the suppress cookie shape.
EDIT_INTENT_HASH_HEX_LENGTH: int = 16

#: Hard cap on the number of distinct edit intents detected in a single
#: command.  Mirrors :data:`backend.web.build_intent.MAX_BUILD_INTENTS`
#: / :data:`backend.web.image_attachment.MAX_IMAGE_ATTACHMENTS`: prevents
#: a runaway paste from blowing up the coach card / LLM context block.
#: In practice operators almost always declare one edit per turn; the
#: cap is a defensive belt.
MAX_EDIT_INTENTS: int = 3

#: Maximum chars used when echoing the detected verb+target phrase
#: inside the coach card.  The full excerpt is preserved in the
#: ``raw_excerpt`` field for debug-finding callers; only the display
#: form is truncated.
MAX_EDIT_INTENT_DISPLAY_CHARS: int = 80


# ── Trigger-key contract (consumed by backend.routers.invoke) ────────

#: Coach trigger key prefix.  The full key shape is
#: ``edit_while_preview:<hash16>``; the W16.1-style suppress system in
#: ``backend.routers.invoke._detect_coaching_triggers`` consumes this
#: prefix verbatim.  Pinned by drift guard.
EDIT_INTENT_TRIGGER_PREFIX: str = "edit_while_preview:"


# ── Slash-command contract (consumed by future W16.5 router) ─────────

#: Slash-command verb the coach card pre-renders.  Frozen so the W16.9
#: e2e tests can pin the bucket key.  The downstream ``/edit-preview``
#: router runs the agent edit pipeline against the named workspace's
#: source tree; vite picks up the change via inotify and triggers HMR.
EDIT_INTENT_SLASH_COMMAND: str = "/edit-preview"

#: Slash-command flag for dry-run mode (preview affected files without
#: touching disk).  Operators can chain ``--dry`` to see the file list
#: before committing to an edit.  Frozen so the consumer-side router
#: can grep for the literal.
EDIT_INTENT_DRY_RUN_FLAG: str = "--dry"


# ── Verb / modifier / target keyword tables ──────────────────────────

#: Direct edit verbs that signal "the operator wants something changed".
#: CJK tokens are matched as substrings (no word boundary in Chinese);
#: Latin tokens are matched case-insensitively with word boundaries so
#: "fixate" / "movement" don't false-positive.  Lock-step pinned with
#: the drift guard.  Excludes 蓋/做/建/make/build/create — those live
#: in :mod:`backend.web.build_intent` (W16.3) and create new things
#: rather than edit existing ones.
EDIT_INTENT_VERB_KEYWORDS: Tuple[str, ...] = (
    # CJK verbs (substring match — no word boundary in Chinese).
    "改",
    "換",
    "修",
    "加",
    "移",
    "調",
    "變",
    "縮",
    # Latin verbs (whole-word match, case-insensitive).
    "change",
    "edit",
    "fix",
    "update",
    "move",
    "resize",
    "rename",
    "replace",
    "tweak",
    "adjust",
    "shrink",
    "enlarge",
)

#: CJK verb subset — matched as substrings (no word-boundary notion in
#: Chinese).  Mirrors the prefix in :data:`EDIT_INTENT_VERB_KEYWORDS`.
_EDIT_INTENT_VERB_KEYWORDS_CJK: Tuple[str, ...] = (
    "改", "換", "修", "加", "移", "調", "變", "縮",
)

#: Latin verb subset — matched case-insensitively with word boundaries.
_EDIT_INTENT_VERB_KEYWORDS_LATIN: Tuple[str, ...] = (
    "change", "edit", "fix", "update", "move", "resize",
    "rename", "replace", "tweak", "adjust", "shrink", "enlarge",
)

#: Modifier hints that signal an edit intent even without a direct verb.
#: 「大一點」/ "bigger" alone ("header 大一點") implies "make X bigger" —
#: the implicit verb is recovered from the modifier.  CJK substring
#: match; Latin word-boundary match.
EDIT_INTENT_MODIFIER_KEYWORDS: Tuple[str, ...] = (
    # CJK modifiers — "header 大一點" / "字體小一點" / "顏色換成藍色".
    "大一點",
    "大一些",
    "小一點",
    "小一些",
    "多一點",
    "少一點",
    "高一點",
    "矮一點",
    "寬一點",
    "窄一點",
    "顏色",
    "字體",
    "字型",
    "位置",
    "大小",
    # Latin modifiers — "make header bigger" / "change button colour".
    "bigger",
    "smaller",
    "larger",
    "taller",
    "shorter",
    "wider",
    "narrower",
    "colour",
    "color",
    "font",
    "padding",
    "margin",
)

_EDIT_INTENT_MODIFIER_KEYWORDS_CJK: Tuple[str, ...] = (
    "大一點", "大一些", "小一點", "小一些",
    "多一點", "少一點", "高一點", "矮一點",
    "寬一點", "窄一點",
    "顏色", "字體", "字型", "位置", "大小",
)

_EDIT_INTENT_MODIFIER_KEYWORDS_LATIN: Tuple[str, ...] = (
    "bigger", "smaller", "larger", "taller", "shorter",
    "wider", "narrower", "colour", "color", "font",
    "padding", "margin",
)

#: Target UI-element nouns the operator might reference when edit
#: intent fires.  CJK substring + Latin word-boundary match.  Each
#: target maps to a normalised lower-case display name used in the
#: coach card and the ``edit_hash`` so the same target name surfaces
#: the same suppress key regardless of casing / script.
EDIT_INTENT_TARGET_KEYWORDS: Tuple[Tuple[str, str, bool], ...] = (
    # CJK targets — substring match (no word boundary in Chinese).
    ("標題列", "header", True),
    ("標題", "header", True),
    ("標頭", "header", True),
    ("頁首", "header", True),
    ("頁尾", "footer", True),
    ("導覽列", "nav", True),
    ("導航", "nav", True),
    ("選單", "menu", True),
    ("按鈕", "button", True),
    ("區塊", "section", True),
    ("卡片", "card", True),
    ("連結", "link", True),
    ("圖片", "image", True),
    ("表單", "form", True),
    ("側欄", "sidebar", True),
    ("橫幅", "banner", True),
    # Latin targets — case-insensitive whole-word match.  Multi-word
    # phrases first so "navbar" classifies before bare "nav".
    ("navbar", "nav", False),
    ("sidebar", "sidebar", False),
    ("header", "header", False),
    ("footer", "footer", False),
    ("nav", "nav", False),
    ("menu", "menu", False),
    ("hero", "hero", False),
    ("button", "button", False),
    ("section", "section", False),
    ("card", "card", False),
    ("link", "link", False),
    ("image", "image", False),
    ("form", "form", False),
    ("logo", "logo", False),
    ("banner", "banner", False),
    ("cta", "cta", False),
)


# ── Detection regexes (compiled at module-import time) ───────────────

# Latin verb regex — case-insensitive whole-word match.  Built once at
# import so every detection call reuses the compiled pattern (per-worker
# stateless derivation, Answer #1).
_EDIT_INTENT_VERB_LATIN_PATTERN: re.Pattern[str] = re.compile(
    r"\b(?:" + "|".join(
        re.escape(v) for v in _EDIT_INTENT_VERB_KEYWORDS_LATIN
    ) + r")\b",
    re.IGNORECASE,
)

# Latin modifier regex — case-insensitive whole-word match.
_EDIT_INTENT_MODIFIER_LATIN_PATTERN: re.Pattern[str] = re.compile(
    r"\b(?:" + "|".join(
        re.escape(m) for m in _EDIT_INTENT_MODIFIER_KEYWORDS_LATIN
    ) + r")\b",
    re.IGNORECASE,
)


# ── Public dataclass ─────────────────────────────────────────────────


@dataclass(frozen=True)
class EditIntentRef:
    """A single edit-while-preview declaration detected in the operator's
    command.

    Attributes
    ----------
    trigger:
        The matched verb or modifier keyword (lower-cased for Latin;
        verbatim for CJK which has no case).  E.g. ``"改"``, ``"bigger"``.
    target:
        The normalised UI-element target name (always lower-case
        Latin) — e.g. ``"header"``, ``"button"``.  Normalisation
        collapses CJK / Latin synonyms (標題列 / header / standard
        header) to a single bucket so suppress / dedup is locale-
        agnostic.
    edit_hash:
        Stable 16-hex-char SHA-256 prefix used as the suppress / dedup
        key.  Computed over ``f"{trigger}|{target}"`` so a re-paste of
        the same intent produces a byte-identical hash — drift guard
        pins this.
    raw_excerpt:
        Up to :data:`MAX_EDIT_INTENT_DISPLAY_CHARS` chars of the
        operator's original command (verbatim) for debug correlation
        and coach-card phrasing.
    """

    trigger: str
    target: str
    edit_hash: str
    raw_excerpt: str

    def trigger_key(self) -> str:
        """Return the W16.5 coach trigger key for this intent."""
        return f"{EDIT_INTENT_TRIGGER_PREFIX}{self.edit_hash}"

    def slash_command(self, workspace_id: str, *, dry_run: bool = False) -> str:
        """Return the operator-facing slash command that runs the edit
        against *workspace_id*.

        Frozen shape: ``/edit-preview <ws> "<instruction>"`` with an
        optional ``--dry`` suffix.  Used by both the LLM context block
        and the templated fallback so the operator always sees a copy-
        paste-ready command regardless of which renderer fired.

        The instruction body is the ``raw_excerpt`` quoted with double
        quotes; downstream the consumer-side router is expected to shlex-
        unescape.  Operators that paste an excerpt containing a literal
        ``"`` will see a malformed command — acceptable for the v1
        coach since the column-cap on raw_excerpt makes that edge
        cosmetic.
        """
        instruction = self.raw_excerpt.replace('"', '\\"')
        suffix = f" {EDIT_INTENT_DRY_RUN_FLAG}" if dry_run else ""
        return (
            f'{EDIT_INTENT_SLASH_COMMAND} {workspace_id} '
            f'"{instruction}"{suffix}'
        )


# ── Helpers ──────────────────────────────────────────────────────────


def _compute_edit_hash(trigger: str, target: str) -> str:
    """Return the stable 16-hex-char hash for *(trigger, target)*.

    Hashes only the (trigger, target) pair so re-typing the same
    "header bigger" intent twice in the same session produces the same
    suppress key.  A different target ("footer bigger") or a different
    trigger ("header smaller") re-coaches.
    """
    payload = f"{trigger}|{target}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:EDIT_INTENT_HASH_HEX_LENGTH]


def _truncate_excerpt(text: str) -> str:
    """Trim *text* to :data:`MAX_EDIT_INTENT_DISPLAY_CHARS` with ellipsis."""
    if len(text) <= MAX_EDIT_INTENT_DISPLAY_CHARS:
        return text
    return text[: MAX_EDIT_INTENT_DISPLAY_CHARS - 1].rstrip() + "…"


def _has_cjk_verb(text: str) -> str:
    """Return the first CJK verb found in *text*, else ``""``."""
    for verb in _EDIT_INTENT_VERB_KEYWORDS_CJK:
        if verb in text:
            return verb
    return ""


def _has_cjk_modifier(text: str) -> str:
    """Return the first CJK modifier found in *text*, else ``""``."""
    for mod in _EDIT_INTENT_MODIFIER_KEYWORDS_CJK:
        if mod in text:
            return mod
    return ""


def _find_target(text: str, text_lower: str) -> Tuple[str, str]:
    """Return ``(matched_keyword, normalised_target)`` for the first
    target keyword found in *text*, else ``("", "")``.

    The :data:`EDIT_INTENT_TARGET_KEYWORDS` table is walked top-down so
    multi-character / more-specific keywords (e.g. "標題列" before bare
    "標題", "navbar" before bare "nav") match first.
    """
    for (kw, normalised, is_cjk) in EDIT_INTENT_TARGET_KEYWORDS:
        if is_cjk:
            if kw in text:
                return kw, normalised
        else:
            pattern = re.compile(
                r"\b" + re.escape(kw) + r"\b",
                re.IGNORECASE,
            )
            if pattern.search(text_lower):
                return kw, normalised
    return "", ""


def detect_edit_intents_in_text(text: str | None) -> list[EditIntentRef]:
    """Return up to :data:`MAX_EDIT_INTENTS` distinct
    :class:`EditIntentRef` records found in *text*.

    Detection requires *both* a trigger (verb keyword OR modifier
    keyword) and a target keyword (header / button / 標題 / 按鈕 / ...)
    co-occurring inside *text*.  Mixed-script messages are handled — a
    CJK modifier pairs freely with a Latin target ("header 大一點")
    and vice versa.

      * CJK:    substring match (no word boundary in Chinese)
      * Latin:  case-insensitive whole-word match

    A *modifier-only* message ("header 大一點" — no explicit verb) is
    treated as edit intent because the implicit verb ("make X bigger")
    is recovered from the modifier.

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

    # Find a verb OR modifier — modifier-only is enough because
    # phrases like "header 大一點" carry the implicit verb in the
    # modifier ("大一點" → "make bigger").  CJK preferred (operator's
    # likely native locale).
    cjk_verb = _has_cjk_verb(text)
    cjk_modifier = _has_cjk_modifier(text)
    latin_verb_match = _EDIT_INTENT_VERB_LATIN_PATTERN.search(text_lower)
    latin_verb = (
        latin_verb_match.group(0).lower() if latin_verb_match else ""
    )
    latin_modifier_match = _EDIT_INTENT_MODIFIER_LATIN_PATTERN.search(
        text_lower,
    )
    latin_modifier = (
        latin_modifier_match.group(0).lower()
        if latin_modifier_match else ""
    )

    chosen_trigger = (
        cjk_verb or cjk_modifier or latin_verb or latin_modifier
    )
    if not chosen_trigger:
        return []

    matched_target_kw, normalised_target = _find_target(text, text_lower)
    if not matched_target_kw:
        return []

    h = _compute_edit_hash(chosen_trigger, normalised_target)
    ref = EditIntentRef(
        trigger=chosen_trigger,
        target=normalised_target,
        edit_hash=h,
        raw_excerpt=_truncate_excerpt(text.strip()),
    )
    return [ref]


def edit_intent_trigger_key(ref: EditIntentRef) -> str:
    """Convenience wrapper around :meth:`EditIntentRef.trigger_key`.

    Mirror of :func:`backend.web.build_intent.build_intent_trigger_key`.
    Kept as a top-level function so call-sites that want the coach
    trigger key without unpacking the ref (e.g. test fixtures that use
    dict-style intents) have a stable entry point.
    """
    return ref.trigger_key()


def trigger_keys_for_edit_intents(refs: Sequence[EditIntentRef]) -> list[str]:
    """Return ``[ref.trigger_key() for ref in refs]`` preserving order."""
    return [ref.trigger_key() for ref in refs]


# ── Drift guards (assert at module-import time) ──────────────────────

# Hash length guard — if a future PR shortens / lengthens the prefix
# without updating the trigger-key contract, the frontend's parser will
# silently misalign.  Surfacing the mismatch at import time pushes it
# to CI red.
assert EDIT_INTENT_HASH_HEX_LENGTH == 16, (
    f"EDIT_INTENT_HASH_HEX_LENGTH drift: expected 16, "
    f"got {EDIT_INTENT_HASH_HEX_LENGTH!r}"
)

assert EDIT_INTENT_TRIGGER_PREFIX.endswith(":"), (
    "EDIT_INTENT_TRIGGER_PREFIX must end in ':' for "
    "backend.routers.invoke._detect_coaching_triggers parsing"
)

assert EDIT_INTENT_VERB_KEYWORDS, "EDIT_INTENT_VERB_KEYWORDS cannot be empty"

assert EDIT_INTENT_MODIFIER_KEYWORDS, (
    "EDIT_INTENT_MODIFIER_KEYWORDS cannot be empty"
)

assert EDIT_INTENT_TARGET_KEYWORDS, (
    "EDIT_INTENT_TARGET_KEYWORDS cannot be empty"
)

# Verb partition guard — CJK + Latin subsets must concatenate exactly
# into the public ``EDIT_INTENT_VERB_KEYWORDS``.
assert (
    tuple(_EDIT_INTENT_VERB_KEYWORDS_CJK)
    + tuple(_EDIT_INTENT_VERB_KEYWORDS_LATIN)
    == EDIT_INTENT_VERB_KEYWORDS
), "EDIT_INTENT_VERB_KEYWORDS partition drift"

# Modifier partition guard — CJK + Latin subsets must concatenate
# exactly into the public ``EDIT_INTENT_MODIFIER_KEYWORDS``.
assert (
    tuple(_EDIT_INTENT_MODIFIER_KEYWORDS_CJK)
    + tuple(_EDIT_INTENT_MODIFIER_KEYWORDS_LATIN)
    == EDIT_INTENT_MODIFIER_KEYWORDS
), "EDIT_INTENT_MODIFIER_KEYWORDS partition drift"

# Target table sanity — every entry's normalised value must be a non-
# empty lower-case Latin token (the bucket key downstream consumers
# branch on).
assert all(
    isinstance(normalised, str)
    and normalised
    and normalised == normalised.lower()
    for (_kw, normalised, _is_cjk) in EDIT_INTENT_TARGET_KEYWORDS
), "EDIT_INTENT_TARGET_KEYWORDS normalised value drift"

# Slash command must start with "/" so the consumer-side router's
# dispatcher can trust the lead character.
assert EDIT_INTENT_SLASH_COMMAND.startswith("/"), (
    "EDIT_INTENT_SLASH_COMMAND must start with '/' for router dispatch"
)

# Dry-run flag must start with "--" (POSIX long-flag convention) so
# the consumer-side argparse / shlex parser handles it predictably.
assert EDIT_INTENT_DRY_RUN_FLAG.startswith("--"), (
    "EDIT_INTENT_DRY_RUN_FLAG must start with '--' (POSIX long-flag)"
)


__all__ = [
    "EDIT_INTENT_DRY_RUN_FLAG",
    "EDIT_INTENT_HASH_HEX_LENGTH",
    "EDIT_INTENT_MODIFIER_KEYWORDS",
    "EDIT_INTENT_SLASH_COMMAND",
    "EDIT_INTENT_TARGET_KEYWORDS",
    "EDIT_INTENT_TRIGGER_PREFIX",
    "EDIT_INTENT_VERB_KEYWORDS",
    "EditIntentRef",
    "MAX_EDIT_INTENTS",
    "MAX_EDIT_INTENT_DISPLAY_CHARS",
    "detect_edit_intents_in_text",
    "edit_intent_trigger_key",
    "trigger_keys_for_edit_intents",
]
