#!/usr/bin/env python3
"""Compile design-system/design-tokens.json -> launcher-web/app/globals.css.

The generator owns ONE fenced block inside globals.css:

    /* @generated tokens BEGIN ... */
    @theme {
        --color-...
        --font-...
        --radius-...
    }
    /* @generated tokens END */

Everything outside the fence (Tailwind v4 @import, brand classes that
consume the CSS variables) is hand-written and preserved across runs.

Web analogue of launcher-qt/scripts/gen_theme.py: same design-tokens.json,
same brand. Drift is loud — the vitest token-sync test invokes this script
with --check so a hand-edited Theme.qml-equivalent never lands.

Usage:
  gen_web_theme.py --tokens design-system/design-tokens.json \\
                   --out    launcher-web/app/globals.css
  gen_web_theme.py --tokens design-system/design-tokens.json \\
                   --out    launcher-web/app/globals.css --check
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import sys


FENCE_BEGIN = "/* @generated tokens BEGIN — DO NOT EDIT. " \
              "Source: design-system/design-tokens.json. */"
FENCE_END = "/* @generated tokens END */"
FENCE_RE = re.compile(
    r"/\* @generated tokens BEGIN[^*]*\*/.*?/\* @generated tokens END \*/",
    re.DOTALL,
)


# ---------- value conversion ----------

def _css_color(v: str) -> str:
    """Token color value -> CSS color literal.

    Hex and rgba() pass through; rgba() is preserved verbatim because
    Tailwind v4 @theme blocks accept it as a color value (the generated
    utilities pick up alpha correctly).
    """
    if not isinstance(v, str):
        raise TypeError(f"expected color string, got {type(v).__name__}: {v!r}")
    return v.strip()


def _kebab(s: str) -> str:
    return s.replace("_", "-")


# ---------- per-section emitters ----------

def _emit_brand_colors(t) -> list[str]:
    c = t["color"]
    flat_keys = (
        "deep_space_start", "deep_space_end",
        "neural_blue", "neural_blue_dim", "neural_blue_glow",
        "hardware_orange", "artifact_purple",
        "validation_emerald", "critical_red",
        "holo_glass", "holo_glass_border",
    )
    out = ["    /* color (flat brand palette) */"]
    for k in flat_keys:
        out.append(f"    --color-{_kebab(k)}: {_css_color(c[k])};")
    return out


def _emit_semantic_colors(t) -> list[str]:
    c = t["color"]["semantic"]
    out = ["    /* color.semantic — shadcn-style role tokens */"]
    for k, v in c.items():
        out.append(f"    --color-{_kebab(k)}: {_css_color(v)};")
    return out


def _emit_chart_colors(t) -> list[str]:
    chart = t["color"]["chart"]
    out = ["    /* color.chart — sequential dataviz palette */"]
    for i, v in enumerate(chart, start=1):
        out.append(f"    --color-chart-{i}: {_css_color(v)};")
    return out


def _emit_category_accents(t) -> list[str]:
    out = ["    /* category accents — each launcher category reads at a glance */"]
    for k, v in t["category_accent"].items():
        if k.startswith("_"):
            continue
        out.append(f"    --color-cat-{_kebab(k)}: {_css_color(v)};")
    return out


def _emit_surface_colors(t) -> list[str]:
    s = t["surface"]
    out = ["    /* surface (tile / holo-glass) */"]
    out.append(f"    --color-tile-bg: {_css_color(s['tile_bg'])};")
    out.append(f"    --color-tile-border: {_css_color(s['tile_border'])};")
    out.append(f"    --color-tile-focus-ring: {_css_color(s['tile_focus_ring'])};")
    out.append(f"    --blur-tile: {int(s['tile_blur_px'])}px;")
    return out


def _emit_fonts(t) -> list[str]:
    ty = t["typography"]
    # Tailwind v4 --font-* maps to font-family utilities. Quote families
    # with spaces ("Fira Code") so they parse cleanly even before the
    # tailwind tokenizer normalizes them.
    def _stack(family: str, fallback: str) -> str:
        # First family quoted if it contains whitespace; subsequent
        # fallback families left as-is.
        head = family.split(",")[0].strip()
        rest = ",".join(family.split(",")[1:]).strip()
        head_q = f'"{head}"' if " " in head else head
        if rest:
            return f"{head_q}, {rest}"
        return f"{head_q}, {fallback}"

    out = ["    /* typography families */"]
    out.append(f"    --font-display: {_stack(ty['display']['family'], 'sans-serif')};")
    out.append(f"    --font-mono: {_stack(ty['mono']['family'], 'monospace')};")
    out.append(f"    --font-body: {_stack(ty['body']['family'], 'sans-serif')};")
    # Font weights — exposed as CSS vars so the brand classes can pick the
    # right wordmark weight without hardcoding the number in two places.
    out.append("    /* typography weights */")
    for role in ("display", "mono", "body"):
        for w in ty[role].get("weights", []):
            out.append(f"    --font-weight-{role}-{w}: {w};")
    # Type scale — Tailwind v4 --text-* keys drive text-* utilities.
    out.append("    /* typography scale (rem) */")
    for k, v in ty["scale_rem"].items():
        out.append(f"    --text-{k}: {v}rem;")
    return out


def _emit_radius(t) -> list[str]:
    out = ["    /* radius (rem; --radius-pill uses px so it's unambiguously huge) */"]
    for k, v in t["radius_rem"].items():
        if k == "pill":
            out.append(f"    --radius-{k}: {int(v)}px;")
        else:
            out.append(f"    --radius-{k}: {v}rem;")
    return out


def _emit_spacing(t) -> list[str]:
    out = ["    /* spacing scale (rem) — explicit stops, not derived */"]
    for i, v in enumerate(t["space_rem"]):
        out.append(f"    --space-{i}: {v}rem;")
    return out


def _emit_breakpoints(t) -> list[str]:
    bp = t["breakpoints_px"]
    out = ["    /* responsive breakpoints (px) — 3xl=2160 keeps the 4K awareness */"]
    for k, v in bp.items():
        out.append(f"    --breakpoint-{k}: {int(v)}px;")
    return out


def _emit_motion(t) -> list[str]:
    m = t["motion"]
    out = ["    /* motion — durations + easing */"]
    out.append(f"    --duration-tile-press: {int(m['tile_press_ms'])}ms;")
    out.append(f"    --duration-page-swipe: {int(m['page_swipe_ms'])}ms;")
    out.append(f"    --ease-omnisight: {m['easing']};")
    return out


# ---------- top-level compile ----------

def compile_block(tokens_path: pathlib.Path) -> str:
    raw = tokens_path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    t = json.loads(raw)

    head = [
        FENCE_BEGIN,
        "/* AUTO-GENERATED by launcher-web/scripts/gen_web_theme.py. */",
        "/* Source: design-system/design-tokens.json (single source of truth). */",
        "/* Both renderers (launcher-qt, launcher-web) read from there, so the */",
        "/* brand stays pixel-consistent. Regenerate via: */",
        "/*   python3 launcher-web/scripts/gen_web_theme.py \\ */",
        "/*     --tokens design-system/design-tokens.json \\ */",
        "/*     --out    launcher-web/app/globals.css */",
        "/* The vitest token-sync test invokes this with --check on every run. */",
        f"/* tokens.sha256: {sha} */",
        f"/* brand: {t.get('brand', 'omnisight')} */",
        "@theme {",
    ]
    body: list[str] = []
    for emit in (
        _emit_brand_colors,
        _emit_semantic_colors,
        _emit_chart_colors,
        _emit_category_accents,
        _emit_surface_colors,
        _emit_fonts,
        _emit_radius,
        _emit_spacing,
        _emit_breakpoints,
        _emit_motion,
    ):
        body += emit(t)
        body.append("")
    while body and body[-1] == "":
        body.pop()
    tail = ["}", FENCE_END]
    return "\n".join(head + body + tail) + "\n"


def _splice(existing: str, block: str) -> str:
    """Replace the fenced region inside `existing` with `block`.

    If no fence is found, append the block at the end of the file
    (followed by a newline). This lets the script bootstrap a fresh
    globals.css the first time.
    """
    if FENCE_RE.search(existing):
        # The compiled block ends with a trailing newline; strip it for
        # the substitution so we don't insert a blank line before whatever
        # followed the fence on disk.
        return FENCE_RE.sub(block.rstrip("\n"), existing, count=1)
    sep = "" if existing.endswith("\n") or not existing else "\n"
    return existing + sep + block


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tokens", required=True, type=pathlib.Path,
                    help="path to design-tokens.json")
    ap.add_argument("--out", required=True, type=pathlib.Path,
                    help="path to launcher-web/app/globals.css")
    ap.add_argument("--check", action="store_true",
                    help="exit nonzero if the on-disk fenced block "
                         "differs from a fresh render of the tokens")
    args = ap.parse_args(argv)

    block = compile_block(args.tokens)

    if args.check:
        if not args.out.exists():
            print(f"x {args.out} does not exist; cannot --check")
            return 1
        existing = args.out.read_text(encoding="utf-8")
        spliced = _splice(existing, block)
        if spliced != existing:
            print(f"x {args.out} fenced @theme block is stale vs "
                  f"{args.tokens}")
            print("  re-run: python3 launcher-web/scripts/gen_web_theme.py "
                  f"--tokens {args.tokens} --out {args.out}")
            return 1
        print(f"OK token-sync: {args.out} @theme matches {args.tokens}")
        return 0

    existing = args.out.read_text(encoding="utf-8") if args.out.exists() else ""
    new = _splice(existing, block)
    if new == existing:
        return 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(new, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
