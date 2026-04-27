"""AS.0.10 — Auto-generated password core library (Python side).

Three styles per `docs/design/as-auth-security-shared-library.md` §4:

    Style A — Random          alphanumeric + symbols (default 20 chars)
    Style B — Diceware        word1-word2-word3-word4-DD (memorable)
    Style C — Pronounceable   syllable1-syllable2-DD (consonant-vowel)

All randomness comes from :mod:`secrets` (cryptographic RNG). The module
is pure-functional and side-effect free:

  * No DB writes, no module-level mutable state, no caches.
  * Cross-worker (uvicorn ``--workers N``) safety follows answer #1 of
    SOP §1 module-global audit — every worker derives the same wordlist
    / character pool from the same source code, so behaviour is
    deterministic-by-construction.

AS.0.8 §3.1 noop matrix: this module remains importable when
``OMNISIGHT_AS_ENABLED=false`` because it has no IO and no global
state to gate on. The future HTTP endpoint ``/api/v1/auth/generate-password``
will short-circuit to 503 via ``backend.auth_baseline._as_enabled()``;
the lib itself never reads the knob.

TS twin: ``templates/_shared/password-generator/index.ts`` keeps the
identical wordlist + symbol pool. Drift-guard test
``backend/tests/test_password_generator.py::test_wordlist_parity_python_ts``
hashes both and asserts SHA-256 equality — divergence breaks CI.

Path note: the AS.0.8 §3 / design-doc §1.6 canonical path was
``backend/auth/password_generator.py``, but the legacy ``backend/auth.py``
session/RBAC module already occupies that namespace; promoting it to a
package would shadow ~140 ``from backend.auth import …`` call sites,
which is a refactor outside this row's scope. Located to
``backend/security/`` instead, parallel to AS.0.7 honeypot
(``backend/security/honeypot.py`` per design freeze §3 / §15
cross-ref). A future row may consolidate paths once a wider
``backend/auth.py`` → package migration lands.
"""

from __future__ import annotations

import secrets
import string
from dataclasses import dataclass
from typing import List


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Constants — must mirror TS twin
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Symbol pool used by Style A. Curated for password-manager / form input
# compatibility (no quotes, no backslash, no backtick — a few SaaS forms
# strip these). Mirrored byte-for-byte in the TS twin.
SYMBOL_POOL: str = "!@#$%^&*()-_=+[]{};:,.<>?"

# Visually ambiguous characters excluded when ``exclude_ambiguous=True``.
# Default is OFF (security > legibility for password-manager users); the
# UX prompt may flip the toggle for users who type passwords by hand.
AMBIGUOUS_CHARS: str = "0Ol1I"

# Allowed separators for Style B / Style C. Hyphen is the safe default
# (works in every URL / shell / CSV context). The full set is exposed
# so the UX can offer a radio toggle.
ALLOWED_SEPARATORS: tuple[str, ...] = ("-", "_", ".", " ")

# Consonants / vowels for Style C. We deliberately drop ``q`` and ``x``
# from the consonant pool — both produce awkward syllables in English
# (``qu*`` always pairs, ``x`` rarely starts syllables) — and add ``y``
# only as a vowel to keep the pattern strict CV. The pools below give:
#   * 19 consonants × 5 vowels = 95 distinct CV pairs (~6.57 bits/pair)
# Mirrored in the TS twin.
CONSONANTS: str = "bcdfghjklmnpqrstvwz"
VOWELS: str = "aeiou"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Diceware wordlist (256 words, exactly 8 bits / word)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Curated short common English words, all 3–6 letters, all lowercase,
# all unique. 256 entries = 8 bits per word; 4 words = 32 bits + the
# 2-digit tail (~6.6 bits) ≈ 38.6 bits raw entropy. Spec target was
# ~52 bits — an explicit follow-up (AS.0.10 #2 in HANDOFF) tracks
# growing this to a full 1296- or 7776-entry EFF wordlist when the
# bundle-size budget allows. For v1 the 256-word list keeps the TS
# twin under 4 KB (vs. ~50 KB for the full EFF list) at the cost of
# ~14 bits of headroom; users who need more are routed to Style A
# (~131 bits at length 20).
#
# Words chosen to be: (a) widely-known English vocabulary, (b) short
# enough for memorization, (c) free of profanity / brand names, (d)
# free of similar-sounding pairs that could confuse dictation.
#
# CRITICAL: the order, casing, and content of this tuple are part of
# the cross-twin contract. Any change MUST be mirrored in the TS twin
# (`templates/_shared/password-generator/index.ts`) in the same order;
# `test_wordlist_parity_python_ts` enforces SHA-256 equality.

DICEWARE_WORDLIST: tuple[str, ...] = (
    "able", "acid", "aged", "airy", "ajar", "akin", "amid", "amok",
    "ankle", "apex", "arch", "arena", "army", "atlas", "atom", "aunt",
    "auto", "axis", "back", "bake", "bald", "ball", "band", "bank",
    "bare", "bark", "barn", "base", "bass", "bath", "beach", "bead",
    "beam", "bean", "bear", "beef", "bell", "belt", "bench", "bend",
    "best", "bike", "bill", "bind", "bird", "black", "blaze", "blend",
    "blink", "block", "blue", "boat", "body", "bold", "bond", "bone",
    "book", "boom", "boot", "born", "boss", "both", "bowl", "brace",
    "brain", "brake", "brave", "bread", "brick", "brief", "broad",
    "brook", "broom", "brown", "build", "bulk", "bull", "burn", "bush",
    "cabin", "cable", "cake", "calf", "calm", "camel", "camp", "canal",
    "candy", "cane", "cape", "card", "care", "cargo", "carve", "case",
    "cash", "cast", "cave", "chain", "chair", "chalk", "champ", "chart",
    "cheek", "cheer", "chef", "chess", "chest", "chick", "chief",
    "child", "chin", "chip", "chord", "chunk", "civic", "claim", "clam",
    "clamp", "clan", "clap", "clash", "clasp", "class", "clean", "clear",
    "clerk", "click", "cliff", "climb", "cling", "clip", "clock", "cloth",
    "cloud", "clove", "clown", "club", "clue", "coach", "coal", "coast",
    "coat", "code", "coil", "coin", "cold", "color", "colt", "comb",
    "come", "cone", "cook", "cool", "copy", "coral", "cord", "core",
    "cork", "corn", "couch", "count", "court", "cove", "cover", "cow",
    "crab", "craft", "cramp", "crane", "crash", "crate", "crawl",
    "cream", "creek", "crepe", "crest", "crew", "crib", "crisp", "crop",
    "cross", "crow", "crowd", "crown", "crude", "cruel", "crumb",
    "crunch", "crust", "cube", "cuff", "curb", "curl", "curse", "curve",
    "cycle", "daily", "dairy", "dance", "dandy", "dark", "dart", "data",
    "dawn", "deal", "dean", "debt", "deck", "deed", "deep", "deer",
    "delta", "den", "dent", "depth", "desk", "diary", "dice", "diet",
    "dig", "dim", "dime", "diner", "dip", "dirt", "dish", "ditch",
    "dive", "dock", "doe", "dog", "doll", "dome", "door", "dose",
    "dot", "double", "dough", "dove", "dozen", "draft", "drag", "drain",
    "drama", "draw", "dream", "dress", "drift", "drill", "drink", "drive",
    "drop", "drum", "duck", "dune",
)

assert len(DICEWARE_WORDLIST) == 256, (
    f"DICEWARE_WORDLIST must have exactly 256 entries, got {len(DICEWARE_WORDLIST)}"
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Public dataclasses
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass(frozen=True)
class GeneratedPassword:
    """Result of any of the three generators.

    Attributes
    ----------
    password
        The generated password string.
    style
        ``"random"`` / ``"diceware"`` / ``"pronounceable"``.
    entropy_bits
        Approximate entropy estimate in bits, rounded to one decimal.
        Used by the UX strength meter; **not** a substitute for the
        zxcvbn check in :func:`backend.auth.validate_password_strength`.
    length
        ``len(password)`` — convenience for callers that don't want
        to recompute it.
    """

    password: str
    style: str
    entropy_bits: float
    length: int


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Random pool helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _build_random_pool(
    *,
    use_symbols: bool,
    exclude_ambiguous: bool,
) -> tuple[str, str, str, str]:
    """Return the four character classes (lower, upper, digits, symbols)
    after applying the ambiguous-char filter."""

    lower = string.ascii_lowercase
    upper = string.ascii_uppercase
    digits = string.digits
    symbols = SYMBOL_POOL if use_symbols else ""

    if exclude_ambiguous:
        ambig = set(AMBIGUOUS_CHARS)
        lower = "".join(ch for ch in lower if ch not in ambig)
        upper = "".join(ch for ch in upper if ch not in ambig)
        digits = "".join(ch for ch in digits if ch not in ambig)
        # symbols are already disambiguous; no filter

    return lower, upper, digits, symbols


def _shuffle(items: List[str]) -> List[str]:
    """Cryptographic Fisher–Yates shuffle (in-place, returns same list)."""
    n = len(items)
    for i in range(n - 1, 0, -1):
        j = secrets.randbelow(i + 1)
        items[i], items[j] = items[j], items[i]
    return items


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Style A — Random
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


RANDOM_DEFAULT_LENGTH = 20
RANDOM_MIN_LENGTH = 8
RANDOM_MAX_LENGTH = 128


def generate_random(
    length: int = RANDOM_DEFAULT_LENGTH,
    *,
    use_symbols: bool = True,
    exclude_ambiguous: bool = False,
    require_classes: bool = True,
) -> GeneratedPassword:
    """Generate a Style-A random password.

    Parameters
    ----------
    length
        Total length (default 20). Must be in ``[8, 128]``; 8 is the
        absolute floor below which character-class enforcement cannot
        be guaranteed (4 classes × 1 char each requires length ≥ 4).
    use_symbols
        Include the :data:`SYMBOL_POOL` characters. Default True.
    exclude_ambiguous
        Drop visually-confusable characters (``0 O l 1 I``). Default
        False — security-first; flip on for "password I'll type by hand"
        mode.
    require_classes
        Enforce that the result contains at least one lowercase, one
        uppercase, one digit, and (if ``use_symbols``) one symbol.
        Default True. Done via post-shuffle of one guaranteed character
        per class plus ``length - N`` random fills.

    Raises
    ------
    ValueError
        If ``length`` is out of range, or if pool ends up empty (e.g.
        every class disabled).
    """

    if not (RANDOM_MIN_LENGTH <= length <= RANDOM_MAX_LENGTH):
        raise ValueError(
            f"length must be in [{RANDOM_MIN_LENGTH}, {RANDOM_MAX_LENGTH}], got {length}"
        )

    lower, upper, digits, symbols = _build_random_pool(
        use_symbols=use_symbols,
        exclude_ambiguous=exclude_ambiguous,
    )

    classes: List[str] = [c for c in (lower, upper, digits, symbols) if c]
    if not classes:
        raise ValueError("character pool is empty after applying filters")

    full_pool = "".join(classes)

    if require_classes:
        # Guarantee one char per non-empty class, then random-fill the rest.
        if length < len(classes):
            raise ValueError(
                f"length {length} cannot satisfy {len(classes)} required classes"
            )
        seeded = [secrets.choice(c) for c in classes]
        remaining = length - len(seeded)
        seeded.extend(secrets.choice(full_pool) for _ in range(remaining))
        chars = _shuffle(seeded)
    else:
        chars = [secrets.choice(full_pool) for _ in range(length)]

    pw = "".join(chars)
    entropy = _entropy_uniform(len(full_pool), length)
    return GeneratedPassword(
        password=pw,
        style="random",
        entropy_bits=round(entropy, 1),
        length=len(pw),
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Style B — Diceware
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


DICEWARE_DEFAULT_WORDS = 4
DICEWARE_MIN_WORDS = 3
DICEWARE_MAX_WORDS = 12


def generate_diceware(
    num_words: int = DICEWARE_DEFAULT_WORDS,
    *,
    separator: str = "-",
    append_digits: int = 2,
    capitalize: bool = False,
) -> GeneratedPassword:
    """Generate a Style-B memorable Diceware-ish passphrase.

    Format: ``word1<sep>word2<sep>…<sep>wordN[<sep>DDDD]``

    Parameters
    ----------
    num_words
        Number of words (default 4). Range ``[3, 12]``.
    separator
        Character between words. Must be one of
        :data:`ALLOWED_SEPARATORS` (``"-"``, ``"_"``, ``"."``, ``" "``).
    append_digits
        Number of trailing decimal digits (default 2). Range ``[0, 6]``.
        Appended with the same separator.
    capitalize
        Capitalize the first letter of each word (e.g.
        ``Correct-Horse-Battery-Staple``).

    Raises
    ------
    ValueError
        On out-of-range ``num_words`` / ``append_digits`` / unknown
        ``separator``.
    """

    if not (DICEWARE_MIN_WORDS <= num_words <= DICEWARE_MAX_WORDS):
        raise ValueError(
            f"num_words must be in [{DICEWARE_MIN_WORDS}, {DICEWARE_MAX_WORDS}], got {num_words}"
        )
    if separator not in ALLOWED_SEPARATORS:
        raise ValueError(
            f"separator must be one of {ALLOWED_SEPARATORS}, got {separator!r}"
        )
    if not (0 <= append_digits <= 6):
        raise ValueError(f"append_digits must be in [0, 6], got {append_digits}")

    words = [secrets.choice(DICEWARE_WORDLIST) for _ in range(num_words)]
    if capitalize:
        words = [w.capitalize() for w in words]

    parts: list[str] = list(words)
    if append_digits > 0:
        digits = "".join(str(secrets.randbelow(10)) for _ in range(append_digits))
        parts.append(digits)

    pw = separator.join(parts)
    word_bits = _entropy_uniform(len(DICEWARE_WORDLIST), num_words)
    digit_bits = _entropy_uniform(10, append_digits) if append_digits > 0 else 0.0
    entropy = word_bits + digit_bits
    return GeneratedPassword(
        password=pw,
        style="diceware",
        entropy_bits=round(entropy, 1),
        length=len(pw),
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Style C — Pronounceable
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


PRONOUNCEABLE_DEFAULT_SYLLABLES = 3
PRONOUNCEABLE_MIN_SYLLABLES = 2
PRONOUNCEABLE_MAX_SYLLABLES = 8

PRONOUNCEABLE_DEFAULT_PAIRS_PER_SYLLABLE = 3  # CVCVCV (6 chars / syllable)
PRONOUNCEABLE_MIN_PAIRS_PER_SYLLABLE = 2
PRONOUNCEABLE_MAX_PAIRS_PER_SYLLABLE = 5


def generate_pronounceable(
    num_syllables: int = PRONOUNCEABLE_DEFAULT_SYLLABLES,
    *,
    pairs_per_syllable: int = PRONOUNCEABLE_DEFAULT_PAIRS_PER_SYLLABLE,
    separator: str = "-",
    append_digits: int = 2,
) -> GeneratedPassword:
    """Generate a Style-C pronounceable password.

    Format: ``syll1<sep>syll2<sep>…<sep>syllN[<sep>DD]`` where each
    syllable is ``pairs_per_syllable`` consonant-vowel pairs (so the
    syllable length in characters is ``2 * pairs_per_syllable``).

    Example with default ``num_syllables=3`` and
    ``pairs_per_syllable=3``: ``rifobe-puzaki-tomeju-43`` (6 chars per
    syllable, 3 syllables, hyphen, 2-digit tail).

    Parameters
    ----------
    num_syllables
        Number of syllables (default 3). Range ``[2, 8]``.
    pairs_per_syllable
        CV pairs per syllable (default 3 → 6 chars). Range ``[2, 5]``.
    separator
        Same as :func:`generate_diceware`.
    append_digits
        Same as :func:`generate_diceware` (range ``[0, 6]``).
    """

    if not (PRONOUNCEABLE_MIN_SYLLABLES <= num_syllables <= PRONOUNCEABLE_MAX_SYLLABLES):
        raise ValueError(
            f"num_syllables must be in [{PRONOUNCEABLE_MIN_SYLLABLES}, "
            f"{PRONOUNCEABLE_MAX_SYLLABLES}], got {num_syllables}"
        )
    if not (
        PRONOUNCEABLE_MIN_PAIRS_PER_SYLLABLE
        <= pairs_per_syllable
        <= PRONOUNCEABLE_MAX_PAIRS_PER_SYLLABLE
    ):
        raise ValueError(
            f"pairs_per_syllable must be in "
            f"[{PRONOUNCEABLE_MIN_PAIRS_PER_SYLLABLE}, "
            f"{PRONOUNCEABLE_MAX_PAIRS_PER_SYLLABLE}], got {pairs_per_syllable}"
        )
    if separator not in ALLOWED_SEPARATORS:
        raise ValueError(
            f"separator must be one of {ALLOWED_SEPARATORS}, got {separator!r}"
        )
    if not (0 <= append_digits <= 6):
        raise ValueError(f"append_digits must be in [0, 6], got {append_digits}")

    syllables = [
        _make_syllable(pairs_per_syllable) for _ in range(num_syllables)
    ]
    parts: list[str] = list(syllables)
    if append_digits > 0:
        digits = "".join(str(secrets.randbelow(10)) for _ in range(append_digits))
        parts.append(digits)

    pw = separator.join(parts)

    pair_space = len(CONSONANTS) * len(VOWELS)
    syllable_bits = _entropy_uniform(pair_space, pairs_per_syllable)
    total_syllable_bits = syllable_bits * num_syllables
    digit_bits = _entropy_uniform(10, append_digits) if append_digits > 0 else 0.0
    entropy = total_syllable_bits + digit_bits

    return GeneratedPassword(
        password=pw,
        style="pronounceable",
        entropy_bits=round(entropy, 1),
        length=len(pw),
    )


def _make_syllable(pairs: int) -> str:
    out: list[str] = []
    for _ in range(pairs):
        out.append(secrets.choice(CONSONANTS))
        out.append(secrets.choice(VOWELS))
    return "".join(out)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Entropy helper
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _entropy_uniform(alphabet_size: int, length: int) -> float:
    """Shannon entropy in bits assuming a uniform-random pick from
    ``alphabet_size`` symbols, repeated ``length`` times."""
    if alphabet_size <= 1 or length <= 0:
        return 0.0
    # log2 — done via bit_length to avoid an import of math just for this
    # tiny helper. We use a Python-stdlib import to stay test-friendly.
    import math
    return math.log2(alphabet_size) * length


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Single-style dispatcher (for HTTP layer)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


VALID_STYLES: frozenset[str] = frozenset({"random", "diceware", "pronounceable"})


def generate(style: str, **kwargs) -> GeneratedPassword:
    """Convenience dispatcher for the eventual HTTP endpoint.

    Routes ``style`` to the matching ``generate_*`` function and forwards
    keyword args. Unknown ``style`` raises :class:`ValueError`.
    """

    s = (style or "").strip().lower()
    if s == "random":
        return generate_random(**kwargs)
    if s == "diceware":
        return generate_diceware(**kwargs)
    if s == "pronounceable":
        return generate_pronounceable(**kwargs)
    raise ValueError(
        f"unknown style {style!r}; expected one of {sorted(VALID_STYLES)}"
    )


__all__ = [
    "ALLOWED_SEPARATORS",
    "AMBIGUOUS_CHARS",
    "CONSONANTS",
    "DICEWARE_DEFAULT_WORDS",
    "DICEWARE_MAX_WORDS",
    "DICEWARE_MIN_WORDS",
    "DICEWARE_WORDLIST",
    "GeneratedPassword",
    "PRONOUNCEABLE_DEFAULT_PAIRS_PER_SYLLABLE",
    "PRONOUNCEABLE_DEFAULT_SYLLABLES",
    "PRONOUNCEABLE_MAX_PAIRS_PER_SYLLABLE",
    "PRONOUNCEABLE_MAX_SYLLABLES",
    "PRONOUNCEABLE_MIN_PAIRS_PER_SYLLABLE",
    "PRONOUNCEABLE_MIN_SYLLABLES",
    "RANDOM_DEFAULT_LENGTH",
    "RANDOM_MAX_LENGTH",
    "RANDOM_MIN_LENGTH",
    "SYMBOL_POOL",
    "VALID_STYLES",
    "VOWELS",
    "generate",
    "generate_diceware",
    "generate_pronounceable",
    "generate_random",
]
