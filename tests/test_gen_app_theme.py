"""OP-2305 — gen_app_theme.py token-sync contract.

The productizer's brand tokens in app/globals.css are compiled from the
vendored shared design-tokens source:

    third_party/omnisight-ui/design-system/design-tokens.json

scripts/gen_app_theme.py owns the fenced region inside app/globals.css.
This test enforces the U4.2 acceptance criteria:

* AC-Integration: ``gen_app_theme.py --check`` is green against the
  on-disk app/globals.css (the generated block is in sync with the
  vendored tokens — drift would mean the productizer + launchers
  rendered different brand colors).
* AC-Exercised: a hand-edit to the fenced block is detected by --check
  (the script is actually load-bearing, not a no-op).
* Generator is idempotent: a fresh render twice in a row produces the
  identical block (no nondeterminism from dict iteration order or
  whitespace drift).

The script runs in-process via runpy / importlib — no subprocess hop
keeps the test sub-100ms and avoids depending on a Python path the CI
container may not surface (PYTHONPATH / venv binding).
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "gen_app_theme.py"
TOKENS = REPO_ROOT / "third_party" / "omnisight-ui" / "design-system" / "design-tokens.json"
GLOBALS_CSS = REPO_ROOT / "app" / "globals.css"


def _load_script():
    spec = importlib.util.spec_from_file_location("gen_app_theme", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def gen():
    return _load_script()


def test_script_and_inputs_exist():
    """The vendored tokens + script + globals.css all sit where the
    generator expects. If one moves, every other AC implicitly fails —
    fail fast here with a clear message."""
    assert SCRIPT.is_file(), f"missing generator: {SCRIPT}"
    assert TOKENS.is_file(), f"missing vendored tokens: {TOKENS}"
    assert GLOBALS_CSS.is_file(), f"missing productizer globals.css: {GLOBALS_CSS}"


def test_tokens_json_is_well_formed():
    """Catches a corrupted vendor pull before the generator does — the
    error here ("expected a JSON object with color.semantic.background")
    is more actionable than the generator's KeyError stack."""
    data = json.loads(TOKENS.read_bytes())
    assert data.get("brand") == "omnisight"
    for k in ("deep_space_start", "deep_space_end", "neural_blue",
              "hardware_orange", "artifact_purple", "validation_emerald",
              "critical_red", "holo_glass", "holo_glass_border"):
        assert k in data["color"], f"tokens.color missing brand key {k!r}"
    for k in ("background", "foreground", "primary", "primary_foreground",
              "secondary", "muted", "muted_foreground", "accent",
              "destructive", "border", "input", "ring"):
        assert k in data["color"]["semantic"], (
            f"tokens.color.semantic missing shadcn key {k!r}"
        )
    assert "3xl" in data["breakpoints_px"], "tokens missing 3xl breakpoint"


def test_check_passes_against_repo_state(gen):
    """The headline AC-Integration: on a fresh checkout of develop, the
    fenced block in app/globals.css matches a fresh render of the
    vendored tokens. Any future hand-edit to the fence will turn this
    test red, and any tokens.json change without a regen will too."""
    rc = gen.main([
        "--tokens", str(TOKENS),
        "--out", str(GLOBALS_CSS),
        "--check",
    ])
    assert rc == 0, "gen_app_theme.py --check failed — regenerate via " \
                    f"python3 {SCRIPT.relative_to(REPO_ROOT)} " \
                    f"--tokens {TOKENS.relative_to(REPO_ROOT)} " \
                    f"--out {GLOBALS_CSS.relative_to(REPO_ROOT)}"


def test_compile_block_is_deterministic(gen):
    """Same input -> byte-identical output. Without this the --check
    contract is meaningless (a flaky generator would either always pass
    spuriously or always fail spuriously). Covers dict-order regressions
    in a future refactor."""
    a = gen.compile_block(TOKENS)
    b = gen.compile_block(TOKENS)
    assert a == b


def test_generated_block_carries_source_sha(gen):
    """The fenced block embeds the SHA-256 of the tokens it was rendered
    from, so reviewers can spot stale generated blocks at a glance in a
    diff (the SHA flips even on whitespace-only token edits)."""
    import hashlib
    block = gen.compile_block(TOKENS)
    expected = hashlib.sha256(TOKENS.read_bytes()).hexdigest()
    assert f"tokens.sha256: {expected}" in block


def test_brand_keys_inside_fence_only(gen):
    """AC-Code: there is no hand-maintained duplicate brand-hex
    definition outside the generated fence. We check by extracting the
    fenced region from app/globals.css and confirming the canonical brand
    custom-property names are defined inside it (and only inside it).

    This is a definition check, not a usage check: hand-written brand
    classes (.holo-glass, @keyframes pulse-blue, etc.) legitimately
    reference rgba(56,189,248,...) literals; what we forbid is a second
    `--neural-blue: ...;` declaration outside the fence that could drift
    from the token source.
    """
    text = GLOBALS_CSS.read_text(encoding="utf-8")
    match = gen.FENCE_RE.search(text)
    assert match, "fence markers missing from globals.css"
    inside = match.group(0)
    outside = text[:match.start()] + text[match.end():]

    canonical_brand_vars = (
        "--deep-space-start", "--deep-space-end",
        "--neural-blue", "--neural-blue-dim", "--neural-blue-glow",
        "--hardware-orange", "--artifact-purple",
        "--validation-emerald", "--critical-red",
        "--holo-glass", "--holo-glass-border",
        "--background", "--foreground", "--primary", "--accent",
        "--destructive", "--border", "--ring",
    )
    for var in canonical_brand_vars:
        assert f"{var}:" in inside, (
            f"{var} is not defined inside the generated fence — "
            "the brand region is incomplete"
        )
        # A re-definition outside the fence would re-introduce the
        # hand-maintained duplicate the AC explicitly forbids. var(--*)
        # *usages* outside the fence are fine and expected — that's the
        # whole point of the indirection — so we look for the colon
        # immediately after the name (a custom-property declaration)
        # rather than the bare name.
        assert f"{var}:" not in outside, (
            f"{var} re-declared OUTSIDE the generated fence — that "
            "duplicate must be removed; the fenced block is the only "
            "source of truth"
        )


def test_check_detects_drift(gen, tmp_path):
    """--check must return nonzero when the fence content drifts from a
    fresh render of the tokens. Without this guarantee the AC has no
    teeth: a CI green light could mean "nobody touched it" rather than
    "it's in sync"."""
    block = gen.compile_block(TOKENS)
    # Mutate one byte inside the fenced block to simulate a hand edit.
    tampered = block.replace("#38bdf8", "#000000", 1)
    assert tampered != block, "test fixture failed to mutate the block"

    fake_css = tmp_path / "globals.css"
    fake_css.write_text(tampered, encoding="utf-8")

    rc = gen.main([
        "--tokens", str(TOKENS),
        "--out", str(fake_css),
        "--check",
    ])
    assert rc == 1, "--check did not catch a hand-edit to the fence"


def test_write_then_check_is_clean(gen, tmp_path):
    """Round-trip: render to a fresh file then --check it. Catches a
    splice bug where the on-write splice and the --check splice would
    use different fence-detection paths and disagree."""
    fake_css = tmp_path / "globals.css"
    # Bootstrap: no fence yet, generator appends.
    rc = gen.main([
        "--tokens", str(TOKENS),
        "--out", str(fake_css),
    ])
    assert rc == 0
    assert fake_css.is_file()

    rc = gen.main([
        "--tokens", str(TOKENS),
        "--out", str(fake_css),
        "--check",
    ])
    assert rc == 0, "fresh-render fence failed --check on the same input"
