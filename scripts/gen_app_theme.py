#!/usr/bin/env python3
"""Compile design-tokens.json -> brand token block in app/globals.css.

Web analogue of third_party/omnisight-ui/launcher-web/scripts/gen_web_theme.py
for the OmniSight-Productizer. The vendored
third_party/omnisight-ui/design-system/design-tokens.json is the SINGLE
source for the brand look (deep-space / neural-blue / holo-glass /
Orbitron / Fira Code / shadcn semantic palette / chart series / 4K-aware
breakpoint). This generator owns ONE fenced block inside app/globals.css:

    /* @generated tokens BEGIN ... */
    :root { /* raw brand vars + shadcn semantic + chart + sidebar */ }
    @theme inline { /* Tailwind v4 mappings via var(--*) */ }
    /* @generated tokens END */

Everything outside the fence (Tailwind v4 @import, brand .holo-glass /
.fui-panel / .neural-grid classes, @keyframes, etc.) is hand-written and
preserved across runs.

Drift is loud: the vitest token-sync test invokes this with --check so a
hand-edit to the fenced region never lands.

Usage:
  python3 scripts/gen_app_theme.py \\
      --tokens third_party/omnisight-ui/design-system/design-tokens.json \\
      --out    app/globals.css
  python3 scripts/gen_app_theme.py \\
      --tokens third_party/omnisight-ui/design-system/design-tokens.json \\
      --out    app/globals.css --check
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import sys


FENCE_BEGIN = (
    "/* @generated tokens BEGIN — DO NOT EDIT. "
    "Source: third_party/omnisight-ui/design-system/design-tokens.json. */"
)
FENCE_END = "/* @generated tokens END */"
FENCE_RE = re.compile(
    r"/\* @generated tokens BEGIN[^*]*\*/.*?/\* @generated tokens END \*/",
    re.DOTALL,
)

# Productizer-side font integration. Next.js's font loader exposes the
# loaded display + mono families as CSS variables (--font-orbitron,
# --font-fira-code) on <html>. The generated Tailwind --font-sans /
# --font-mono need to reference those vars so brand text renders in the
# actual loaded font rather than a system fallback.
_FONT_VAR_MAP: dict[str, str] = {
    "Orbitron": "var(--font-orbitron), ui-sans-serif, system-ui, sans-serif",
    "Fira Code": "var(--font-fira-code), ui-monospace, monospace",
}


# ---------- value conversion ----------

def _css_color(v: str) -> str:
    """Token color value -> CSS color literal (passes through verbatim).

    Hex and rgba() are both accepted by every consumer of these vars
    (browser, Tailwind v4 @theme, our hand-written brand classes).
    """
    if not isinstance(v, str):
        raise TypeError(f"expected color string, got {type(v).__name__}: {v!r}")
    return v.strip()


def _alpha_variant(hex_rgb: str, alpha: float) -> str:
    """#rrggbb + alpha -> rgba(r,g,b,a).

    Used for the brand *-dim derivations (hardware-orange-dim, etc.)
    that productizer CSS classes reference via var() but that the
    canonical tokens spec leaves implicit (always 30% of the base hue).
    Keeping the derivation in the generator means the dim variants stay
    locked to their base color: changing #f97316 in tokens.json
    propagates to --hardware-orange-dim without a second edit.
    """
    h = hex_rgb.lstrip("#")
    if len(h) != 6:
        raise ValueError(f"expected #rrggbb hex, got {hex_rgb!r}")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r},{g},{b},{alpha:g})"


# ---------- per-block emitters ----------

def _emit_root(t) -> list[str]:
    c = t["color"]
    sem = c["semantic"]
    chart = c["chart"]
    radius = t["radius_rem"]

    out: list[str] = [":root {"]
    out += [
        "    /* Deep Space Background */",
        f"    --deep-space-start: {_css_color(c['deep_space_start'])};",
        f"    --deep-space-end: {_css_color(c['deep_space_end'])};",
        "",
        "    /* Neural Blue — panels, UI lines, energy beams */",
        f"    --neural-blue: {_css_color(c['neural_blue'])};",
        f"    --neural-blue-dim: {_css_color(c['neural_blue_dim'])};",
        f"    --neural-blue-glow: {_css_color(c['neural_blue_glow'])};",
        "",
        "    /* Hardware Orange — firmware, hardware specs */",
        f"    --hardware-orange: {_css_color(c['hardware_orange'])};",
        f"    --hardware-orange-dim: {_alpha_variant(c['hardware_orange'], 0.3)};",
        "",
        "    /* Artifact Purple — reporter, customer requirements */",
        f"    --artifact-purple: {_css_color(c['artifact_purple'])};",
        f"    --artifact-purple-dim: {_alpha_variant(c['artifact_purple'], 0.3)};",
        "",
        "    /* Validation Emerald — test passed, FPS data */",
        f"    --validation-emerald: {_css_color(c['validation_emerald'])};",
        f"    --validation-emerald-dim: {_alpha_variant(c['validation_emerald'], 0.3)};",
        "",
        "    /* Critical Red — errors, compilation failures */",
        f"    --critical-red: {_css_color(c['critical_red'])};",
        f"    --critical-red-dim: {_alpha_variant(c['critical_red'], 0.3)};",
        "",
        "    /* Holo Glass */",
        f"    --holo-glass: {_css_color(c['holo_glass'])};",
        f"    --holo-glass-border: {_css_color(c['holo_glass_border'])};",
        "",
        "    /* Semantic (shadcn-style) — from color.semantic */",
        f"    --background: {_css_color(sem['background'])};",
        f"    --foreground: {_css_color(sem['foreground'])};",
        f"    --card: {_css_color(sem['card'])};",
        f"    --card-foreground: {_css_color(sem['card_foreground'])};",
        f"    --popover: {_css_color(sem['popover'])};",
        f"    --popover-foreground: {_css_color(sem['foreground'])};",
        f"    --primary: {_css_color(sem['primary'])};",
        f"    --primary-foreground: {_css_color(sem['primary_foreground'])};",
        f"    --secondary: {_css_color(sem['secondary'])};",
        f"    --secondary-foreground: {_css_color(sem['foreground'])};",
        f"    --muted: {_css_color(sem['muted'])};",
        f"    --muted-foreground: {_css_color(sem['muted_foreground'])};",
        f"    --accent: {_css_color(sem['accent'])};",
        f"    --accent-foreground: {_css_color(sem['primary_foreground'])};",
        f"    --destructive: {_css_color(sem['destructive'])};",
        # destructive-foreground is a near-white not present in tokens;
        # kept here as the historical productizer value for high-contrast
        # legibility on red surfaces (#ef4444 -> #fef2f2 ≈ 18:1 AAA).
        "    --destructive-foreground: #fef2f2;",
        f"    --border: {_css_color(sem['border'])};",
        f"    --input: {_css_color(sem['input'])};",
        f"    --ring: {_css_color(sem['ring'])};",
        f"    --radius: {radius['base']}rem;",
        "",
        "    /* Chart palette — from color.chart */",
    ]
    for i, v in enumerate(chart, start=1):
        out.append(f"    --chart-{i}: {_css_color(v)};")
    out += [
        "",
        "    /* Sidebar — derived from neural-blue brand accent */",
        "    --sidebar: rgba(0,242,255,0.02);",
        f"    --sidebar-foreground: {_css_color(sem['foreground'])};",
        f"    --sidebar-primary: {_css_color(c['neural_blue'])};",
        f"    --sidebar-primary-foreground: {_css_color(sem['primary_foreground'])};",
        f"    --sidebar-accent: {_alpha_variant(c['neural_blue'], 0.1)};",
        f"    --sidebar-accent-foreground: {_css_color(sem['foreground'])};",
        f"    --sidebar-border: {_alpha_variant(c['neural_blue'], 0.15)};",
        f"    --sidebar-ring: {_css_color(c['neural_blue'])};",
        "}",
    ]
    return out


def _font_stack(family: str) -> str:
    head = family.split(",")[0].strip()
    return _FONT_VAR_MAP.get(head, family)


def _emit_theme_inline(t) -> list[str]:
    chart = t["color"]["chart"]
    bp = t["breakpoints_px"]
    ty = t["typography"]
    out: list[str] = ["@theme inline {"]
    out += [
        "    /* Custom 3xl breakpoint — global-status-header's right cluster */",
        "    /* (WSL2 + USB pills + ModeSelector + SSE + Arch + Help + Lang + */",
        "    /* Time + Settings + Bell + EmergencyStop) only fits comfortably */",
        "    /* at ≥2160px. Default 2xl=1536 fires too early. */",
        f"    --breakpoint-3xl: {int(bp['3xl'])}px;",
        f"    --font-sans: {_font_stack(ty['display']['family'])};",
        f"    --font-mono: {_font_stack(ty['mono']['family'])};",
        "    --color-background: var(--background);",
        "    --color-foreground: var(--foreground);",
        "    --color-card: var(--card);",
        "    --color-card-foreground: var(--card-foreground);",
        "    --color-popover: var(--popover);",
        "    --color-popover-foreground: var(--popover-foreground);",
        "    --color-primary: var(--primary);",
        "    --color-primary-foreground: var(--primary-foreground);",
        "    --color-secondary: var(--secondary);",
        "    --color-secondary-foreground: var(--secondary-foreground);",
        "    --color-muted: var(--muted);",
        "    --color-muted-foreground: var(--muted-foreground);",
        "    --color-accent: var(--accent);",
        "    --color-accent-foreground: var(--accent-foreground);",
        "    --color-destructive: var(--destructive);",
        "    --color-destructive-foreground: var(--destructive-foreground);",
        "    --color-border: var(--border);",
        "    --color-input: var(--input);",
        "    --color-ring: var(--ring);",
    ]
    for i in range(1, len(chart) + 1):
        out.append(f"    --color-chart-{i}: var(--chart-{i});")
    out += [
        "    --radius-sm: calc(var(--radius) - 4px);",
        "    --radius-md: calc(var(--radius) - 2px);",
        "    --radius-lg: var(--radius);",
        "    --radius-xl: calc(var(--radius) + 4px);",
        "    --color-sidebar: var(--sidebar);",
        "    --color-sidebar-foreground: var(--sidebar-foreground);",
        "    --color-sidebar-primary: var(--sidebar-primary);",
        "    --color-sidebar-primary-foreground: var(--sidebar-primary-foreground);",
        "    --color-sidebar-accent: var(--sidebar-accent);",
        "    --color-sidebar-accent-foreground: var(--sidebar-accent-foreground);",
        "    --color-sidebar-border: var(--sidebar-border);",
        "    --color-sidebar-ring: var(--sidebar-ring);",
        "}",
    ]
    return out


# ---------- top-level compile ----------

def compile_block(tokens_path: pathlib.Path) -> str:
    raw = tokens_path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    t = json.loads(raw)
    head = [
        FENCE_BEGIN,
        "/* AUTO-GENERATED by scripts/gen_app_theme.py. */",
        "/* Source: third_party/omnisight-ui/design-system/design-tokens.json (single source of truth). */",
        "/* Web analogue of launcher-web/scripts/gen_web_theme.py — both the */",
        "/* launchers and the productizer read from the SAME tokens file, so */",
        "/* the brand stays pixel-consistent across the fleet. */",
        "/* Regenerate via: */",
        "/*   python3 scripts/gen_app_theme.py \\ */",
        "/*     --tokens third_party/omnisight-ui/design-system/design-tokens.json \\ */",
        "/*     --out    app/globals.css */",
        "/* The vitest token-sync test invokes this with --check on every run. */",
        f"/* tokens.sha256: {sha} */",
        f"/* brand: {t.get('brand', 'omnisight')} */",
    ]
    body = _emit_root(t) + [""] + _emit_theme_inline(t)
    return "\n".join(head + body + [FENCE_END]) + "\n"


def _splice(existing: str, block: str) -> str:
    """Replace the fenced region in `existing` with `block`.

    If no fence is found, append the block at the end of the file. In
    practice the productizer ships globals.css with the fence in place
    once OP-2305 lands; the bootstrap branch only matters the very first
    time the script runs against an un-fenced file.
    """
    if FENCE_RE.search(existing):
        return FENCE_RE.sub(lambda _m: block.rstrip("\n"), existing, count=1)
    sep = "" if existing.endswith("\n") or not existing else "\n"
    return existing + sep + block


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tokens", required=True, type=pathlib.Path,
                    help="path to design-tokens.json")
    ap.add_argument("--out", required=True, type=pathlib.Path,
                    help="path to app/globals.css")
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
            print(f"x {args.out} fenced brand block is stale vs {args.tokens}")
            print("  re-run: python3 scripts/gen_app_theme.py "
                  f"--tokens {args.tokens} --out {args.out}")
            return 1
        print(f"OK token-sync: {args.out} brand block matches {args.tokens}")
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
