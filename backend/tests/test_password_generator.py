"""AS.0.10 — `backend.auth.password_generator` core lib contract tests.

Exercises the three styles (Random / Diceware / Pronounceable) of the
password generator, plus the cross-twin drift guard that pins
behaviour parity with `templates/_shared/password-generator/index.ts`.

Test families:

    1. Style A — Random           length, character classes,
                                  ambiguous-char filter, error paths,
                                  uniqueness over many calls.
    2. Style B — Diceware         word count, separator, digit suffix,
                                  capitalize, words drawn from list,
                                  error paths.
    3. Style C — Pronounceable    CV alternating pattern, syllable
                                  count, separator, digit suffix,
                                  error paths.
    4. Wordlist sanity            count == 256, all unique,
                                  all-lowercase, all 3–6 chars.
    5. Cross-twin drift guard     SHA-256 of Python wordlist == SHA-256
                                  of TS wordlist; symbol pool, ambiguous
                                  chars, consonants, vowels, separator
                                  set all match byte-for-byte. Drives
                                  AS.0.10 §4 "Python and TS twin must
                                  produce same distribution" contract.
    6. Dispatcher                 `generate(style, ...)` routes correctly,
                                  rejects unknown style.
    7. Module-global state audit  Importing the module twice yields the
                                  same constants (no module-init
                                  randomness), per SOP §1 audit.

The lib is pure — no DB, no network, no asyncio — so all tests are
synchronous and dependency-free (no `pg_test_pool`, no `client`).
"""

from __future__ import annotations

import hashlib
import importlib
import pathlib
import re

import pytest

from backend.security import password_generator as pg


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Style A — Random
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_random_default_length():
    out = pg.generate_random()
    assert out.style == "random"
    assert out.length == pg.RANDOM_DEFAULT_LENGTH == 20
    assert len(out.password) == 20
    assert out.entropy_bits > 100  # ~131 bits at length 20 with all classes


@pytest.mark.parametrize("length", [8, 12, 16, 20, 32, 64, 128])
def test_random_length_honored(length: int):
    out = pg.generate_random(length=length)
    assert out.length == length
    assert len(out.password) == length


@pytest.mark.parametrize("bad", [-1, 0, 7, 129, 1024])
def test_random_length_out_of_range_raises(bad: int):
    with pytest.raises(ValueError, match="length must be in"):
        pg.generate_random(length=bad)


def test_random_require_classes_includes_all_four_by_default():
    """With default `use_symbols=True` + `require_classes=True` every
    output must contain at least one of each: lower, upper, digit, symbol."""
    for _ in range(50):  # statistical: 50 samples must all pass
        out = pg.generate_random(length=12)
        assert any(c.islower() for c in out.password), out.password
        assert any(c.isupper() for c in out.password), out.password
        assert any(c.isdigit() for c in out.password), out.password
        assert any(c in pg.SYMBOL_POOL for c in out.password), out.password


def test_random_no_symbols_omits_symbol_class():
    for _ in range(20):
        out = pg.generate_random(length=16, use_symbols=False)
        assert all(c not in pg.SYMBOL_POOL for c in out.password)


def test_random_exclude_ambiguous_filter():
    for _ in range(50):
        out = pg.generate_random(length=20, exclude_ambiguous=True)
        for ch in pg.AMBIGUOUS_CHARS:
            assert ch not in out.password, (
                f"ambiguous char {ch!r} leaked into {out.password!r}"
            )


def test_random_uniqueness_across_many_calls():
    """50 calls at default length must yield 50 distinct passwords —
    a soft proof the RNG isn't a constant. Birthday-bound: collision
    probability for 50 picks from 2^131 space is effectively zero."""
    seen = {pg.generate_random().password for _ in range(50)}
    assert len(seen) == 50


def test_random_require_classes_false_skips_class_seeding():
    # With require_classes=False the generator may (rarely) miss a class.
    # We can't assert a single output misses one (might pass by luck),
    # but we can assert the function still returns a string of the
    # right length — i.e. the path doesn't error out.
    out = pg.generate_random(length=20, require_classes=False)
    assert len(out.password) == 20


def test_random_length_below_required_classes_raises():
    # With 4 classes (lower/upper/digit/symbol) length must be ≥ 4. The
    # public floor is 8 anyway, so this is a defensive belt-and-braces
    # check that the higher floor wins first.
    with pytest.raises(ValueError, match="length must be in"):
        pg.generate_random(length=3)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Style B — Diceware
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_diceware_default_shape():
    out = pg.generate_diceware()
    assert out.style == "diceware"
    parts = out.password.split("-")
    # 4 words + 2-digit tail = 5 parts
    assert len(parts) == 5
    # last part is 2 digits
    assert re.fullmatch(r"\d{2}", parts[-1])
    # first 4 are wordlist entries
    for w in parts[:4]:
        assert w in pg.DICEWARE_WORDLIST


@pytest.mark.parametrize("n", [3, 4, 5, 6, 8, 12])
def test_diceware_word_count(n: int):
    out = pg.generate_diceware(num_words=n)
    parts = out.password.split("-")
    # n words + (1 digit tail since append_digits default = 2)
    assert len(parts) == n + 1


@pytest.mark.parametrize("bad", [0, 1, 2, 13, 100])
def test_diceware_word_count_out_of_range_raises(bad: int):
    with pytest.raises(ValueError, match="num_words must be in"):
        pg.generate_diceware(num_words=bad)


@pytest.mark.parametrize("sep", ["-", "_", ".", " "])
def test_diceware_separator_honored(sep: str):
    out = pg.generate_diceware(separator=sep)
    parts = out.password.split(sep)
    assert len(parts) == 5
    for w in parts[:4]:
        assert w in pg.DICEWARE_WORDLIST


@pytest.mark.parametrize("bad_sep", ["/", "+", "", "abc", "::"])
def test_diceware_unknown_separator_raises(bad_sep: str):
    with pytest.raises(ValueError, match="separator must be one of"):
        pg.generate_diceware(separator=bad_sep)


@pytest.mark.parametrize("digits", [0, 1, 2, 3, 6])
def test_diceware_digit_suffix(digits: int):
    out = pg.generate_diceware(append_digits=digits)
    parts = out.password.split("-")
    if digits == 0:
        assert len(parts) == 4  # no digit tail
        for w in parts:
            assert w in pg.DICEWARE_WORDLIST
    else:
        assert len(parts) == 5
        assert re.fullmatch(r"\d{" + str(digits) + r"}", parts[-1])


@pytest.mark.parametrize("bad_digits", [-1, 7, 100])
def test_diceware_digit_count_out_of_range_raises(bad_digits: int):
    with pytest.raises(ValueError, match="append_digits must be in"):
        pg.generate_diceware(append_digits=bad_digits)


def test_diceware_capitalize_flag():
    out = pg.generate_diceware(capitalize=True)
    parts = out.password.split("-")
    for w in parts[:-1]:
        # Every word starts with an uppercase letter
        assert w[0].isupper(), f"capitalize flag not applied to {w!r}"
        # Rest of the word is lowercase
        assert w[1:].islower() or len(w) == 1


def test_diceware_uniqueness_across_many_calls():
    seen = {pg.generate_diceware().password for _ in range(50)}
    assert len(seen) >= 48  # tiny chance of birthday collision in 256^4


def test_diceware_entropy_bits_within_expected_band():
    out = pg.generate_diceware(num_words=4, append_digits=2)
    # 256 words = 8 bits/word × 4 = 32 bits + 10^2 ≈ 6.6 bits = ~38.6
    assert 38.0 <= out.entropy_bits <= 39.5


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Style C — Pronounceable
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_pronounceable_default_shape():
    out = pg.generate_pronounceable()
    assert out.style == "pronounceable"
    parts = out.password.split("-")
    # 3 syllables + 2-digit tail = 4 parts
    assert len(parts) == 4
    assert re.fullmatch(r"\d{2}", parts[-1])
    # each syllable: 3 CV pairs = 6 chars
    for s in parts[:3]:
        assert len(s) == 6
        # Strict CV alternation
        for i, ch in enumerate(s):
            if i % 2 == 0:
                assert ch in pg.CONSONANTS, f"{s!r}[{i}]={ch!r} not consonant"
            else:
                assert ch in pg.VOWELS, f"{s!r}[{i}]={ch!r} not vowel"


@pytest.mark.parametrize("syllables", [2, 3, 4, 5, 8])
def test_pronounceable_syllable_count(syllables: int):
    out = pg.generate_pronounceable(num_syllables=syllables)
    parts = out.password.split("-")
    assert len(parts) == syllables + 1  # + digit tail


@pytest.mark.parametrize("pairs", [2, 3, 4, 5])
def test_pronounceable_pairs_per_syllable(pairs: int):
    out = pg.generate_pronounceable(pairs_per_syllable=pairs)
    parts = out.password.split("-")
    for s in parts[:-1]:
        assert len(s) == 2 * pairs


@pytest.mark.parametrize("bad_syll", [0, 1, 9, 100])
def test_pronounceable_syllables_out_of_range_raises(bad_syll: int):
    with pytest.raises(ValueError, match="num_syllables must be in"):
        pg.generate_pronounceable(num_syllables=bad_syll)


@pytest.mark.parametrize("bad_pairs", [0, 1, 6, 99])
def test_pronounceable_pairs_out_of_range_raises(bad_pairs: int):
    with pytest.raises(ValueError, match="pairs_per_syllable must be in"):
        pg.generate_pronounceable(pairs_per_syllable=bad_pairs)


@pytest.mark.parametrize("bad_sep", ["/", "+", "", "::"])
def test_pronounceable_unknown_separator_raises(bad_sep: str):
    with pytest.raises(ValueError, match="separator must be one of"):
        pg.generate_pronounceable(separator=bad_sep)


@pytest.mark.parametrize("bad_digits", [-1, 7, 100])
def test_pronounceable_digits_out_of_range_raises(bad_digits: int):
    with pytest.raises(ValueError, match="append_digits must be in"):
        pg.generate_pronounceable(append_digits=bad_digits)


def test_pronounceable_no_digits_omits_tail():
    out = pg.generate_pronounceable(append_digits=0)
    parts = out.password.split("-")
    assert len(parts) == pg.PRONOUNCEABLE_DEFAULT_SYLLABLES
    for s in parts:
        # syllable only, no digit tail to worry about
        assert all(c.isalpha() for c in s)


def test_pronounceable_uniqueness_across_many_calls():
    seen = {pg.generate_pronounceable().password for _ in range(50)}
    assert len(seen) == 50


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Wordlist sanity
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_wordlist_count_is_256():
    assert len(pg.DICEWARE_WORDLIST) == 256


def test_wordlist_all_unique():
    assert len(set(pg.DICEWARE_WORDLIST)) == 256


def test_wordlist_all_lowercase_alpha():
    for w in pg.DICEWARE_WORDLIST:
        assert w.islower(), f"{w!r} not lowercase"
        assert w.isalpha(), f"{w!r} contains non-alpha"


def test_wordlist_word_length_band():
    for w in pg.DICEWARE_WORDLIST:
        assert 3 <= len(w) <= 6, f"{w!r} outside 3-6 char band"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Cross-twin drift guard — Python ↔ TS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Path of the TS twin, computed relative to repo root. Skipping the test
# (rather than failing) when the file is absent keeps this test friendly
# during partial-tree checkouts. CI has the full tree.
_TS_TWIN_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "templates"
    / "_shared"
    / "password-generator"
    / "index.ts"
)


def _read_ts_twin() -> str:
    if not _TS_TWIN_PATH.exists():
        pytest.skip(f"TS twin not present at {_TS_TWIN_PATH}")
    return _TS_TWIN_PATH.read_text(encoding="utf-8")


def _extract_ts_string(src: str, name: str) -> str:
    """Pull `export const NAME = "..."` literal value out of TS source."""
    m = re.search(
        rf'export\s+const\s+{name}\s*=\s*"((?:[^"\\]|\\.)*)"',
        src,
    )
    assert m, f"could not find `export const {name} = \"...\"` in TS twin"
    # Decode minimal JS escapes we care about (none in our constants
    # today, but be defensive). Backslash-escape characters in our pool
    # are: backslash itself (none of our pools include it), so safe.
    return m.group(1)


def _extract_ts_string_array(src: str, name: str) -> list[str]:
    """Pull `export const NAME[: type] = [ "a", "b", ... ]` array body
    out of TS source. Tolerates intervening type annotations like
    `: readonly string[]` that themselves contain `[]`."""
    m = re.search(
        rf'export\s+const\s+{name}[^=]*=\s*\[\s*(.+?)\s*\]',
        src,
        re.DOTALL,
    )
    assert m, f"could not find `export const {name} = [...]` in TS twin"
    body = m.group(1)
    return re.findall(r'"([^"]+)"', body)


def test_wordlist_parity_python_ts():
    """SHA-256 of the diceware wordlist must match between sides.

    Canonical form: newline-joined entries in declaration order.
    Any reorder, addition, removal, or rename on either side breaks
    this oracle.
    """
    ts_src = _read_ts_twin()
    ts_words = _extract_ts_string_array(ts_src, "DICEWARE_WORDLIST")

    py_join = "\n".join(pg.DICEWARE_WORDLIST)
    ts_join = "\n".join(ts_words)

    py_hash = hashlib.sha256(py_join.encode("utf-8")).hexdigest()
    ts_hash = hashlib.sha256(ts_join.encode("utf-8")).hexdigest()

    assert py_hash == ts_hash, (
        f"DICEWARE_WORDLIST drift between Python and TS twin\n"
        f"  Python SHA-256: {py_hash}\n"
        f"  TS     SHA-256: {ts_hash}\n"
        f"  Python count : {len(pg.DICEWARE_WORDLIST)}\n"
        f"  TS     count : {len(ts_words)}"
    )


def test_symbol_pool_parity_python_ts():
    ts_src = _read_ts_twin()
    assert _extract_ts_string(ts_src, "SYMBOL_POOL") == pg.SYMBOL_POOL


def test_ambiguous_chars_parity_python_ts():
    ts_src = _read_ts_twin()
    assert _extract_ts_string(ts_src, "AMBIGUOUS_CHARS") == pg.AMBIGUOUS_CHARS


def test_consonants_parity_python_ts():
    ts_src = _read_ts_twin()
    assert _extract_ts_string(ts_src, "CONSONANTS") == pg.CONSONANTS


def test_vowels_parity_python_ts():
    ts_src = _read_ts_twin()
    assert _extract_ts_string(ts_src, "VOWELS") == pg.VOWELS


def test_separators_parity_python_ts():
    ts_src = _read_ts_twin()
    ts_seps = _extract_ts_string_array(ts_src, "ALLOWED_SEPARATORS")
    assert tuple(ts_seps) == pg.ALLOWED_SEPARATORS


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Dispatcher — `generate(style, ...)`
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.parametrize("style", ["random", "diceware", "pronounceable"])
def test_dispatcher_routes_known_style(style: str):
    out = pg.generate(style)
    assert out.style == style


def test_dispatcher_lowercases_style():
    out = pg.generate("RANDOM")
    assert out.style == "random"


def test_dispatcher_passes_kwargs_through():
    out = pg.generate("random", length=12)
    assert out.length == 12


@pytest.mark.parametrize("bad", ["", "passphrase", "totally-bogus"])
def test_dispatcher_unknown_style_raises(bad: str):
    with pytest.raises(ValueError, match="unknown style"):
        pg.generate(bad)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Module-global state audit (SOP §1)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_module_constants_stable_across_reimport():
    """Reimporting the module must yield byte-identical constants —
    no module-init randomness that would diverge between uvicorn workers."""
    snapshot = {
        "wordlist": tuple(pg.DICEWARE_WORDLIST),
        "symbol_pool": pg.SYMBOL_POOL,
        "ambiguous": pg.AMBIGUOUS_CHARS,
        "consonants": pg.CONSONANTS,
        "vowels": pg.VOWELS,
        "separators": pg.ALLOWED_SEPARATORS,
    }
    importlib.reload(pg)
    assert tuple(pg.DICEWARE_WORDLIST) == snapshot["wordlist"]
    assert pg.SYMBOL_POOL == snapshot["symbol_pool"]
    assert pg.AMBIGUOUS_CHARS == snapshot["ambiguous"]
    assert pg.CONSONANTS == snapshot["consonants"]
    assert pg.VOWELS == snapshot["vowels"]
    assert pg.ALLOWED_SEPARATORS == snapshot["separators"]


def test_module_uses_secrets_not_random():
    """Pin the cryptographic-RNG provenance: the source must reference
    `secrets.choice` / `secrets.randbelow` and must NOT import the
    seedable `random` module. This catches a class of regressions where
    a well-meaning refactor swaps in `random.choice` for "speed"."""
    src = pathlib.Path(pg.__file__).read_text(encoding="utf-8")
    assert "import secrets" in src
    assert "secrets.choice" in src
    assert "secrets.randbelow" in src
    # `import random` (top-level) would be a regression. Allow the word
    # "random" to appear in docstrings / function names.
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith('"""'):
            continue
        assert not re.match(r"^\s*import\s+random\b", line), (
            f"`import random` regression — use secrets module: {line!r}"
        )
        assert not re.match(r"^\s*from\s+random\s+import\b", line), (
            f"`from random import …` regression — use secrets: {line!r}"
        )
