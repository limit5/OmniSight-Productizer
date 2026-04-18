"""WCAG 2.2 色彩對比自動計算 — tokens × text-size 笛卡兒積掃描。

用於 B16 Part C row 291 的「色彩對比自動計算」gap。
在 CI 階段讀取設計 token，計算所有 foreground × background × text-size 組合
是否滿足 WCAG AA（正文 ≥ 4.5:1，大字 ≥ 3:1，UI 元件 ≥ 3:1）。

Token 輸入格式（任一皆可）:
  1) configs/web/*.tokens.json — {"colors": {"name": "#rrggbb", ...}}
  2) Style Dictionary 扁平 json — {"color.text.primary": {"value": "#111"}}

輸出：
  - exit 0 + JSON 報告（stdout）當 violations == 0
  - exit 1 + 違規清單（stdout）當任一 pair 不過

整合 W2 simulate-track：
  `run_a11y_audit()` 已涵蓋 axe/pa11y runtime 檢查，此腳本補 build-time / token
  層級，讓違反組合在進入 CSS 之前就被攔截。
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


# ─── WCAG 2.2 AA thresholds（§ 1.4.3 / 1.4.11） ───
AA_NORMAL = 4.5
AA_LARGE = 3.0  # ≥ 18pt or ≥ 14pt bold
AA_NON_TEXT = 3.0  # UI components / meaningful graphics

TEXT_SIZES = ("normal", "large", "ui")
THRESHOLDS = {"normal": AA_NORMAL, "large": AA_LARGE, "ui": AA_NON_TEXT}


@dataclass
class Violation:
    fg: str
    bg: str
    fg_hex: str
    bg_hex: str
    text_size: str
    ratio: float
    required: float


def _hex_to_rgb(hex_color: str) -> tuple[float, float, float]:
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        raise ValueError(f"invalid hex color: {hex_color!r}")
    return tuple(int(h[i : i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore[return-value]


def _srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(hex_color: str) -> float:
    r, g, b = _hex_to_rgb(hex_color)
    lr, lg, lb = _srgb_to_linear(r), _srgb_to_linear(g), _srgb_to_linear(b)
    return 0.2126 * lr + 0.7152 * lg + 0.0722 * lb


def contrast_ratio(fg_hex: str, bg_hex: str) -> float:
    l1 = relative_luminance(fg_hex)
    l2 = relative_luminance(bg_hex)
    lighter, darker = max(l1, l2), min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


def _flatten_tokens(obj: dict, prefix: str = "") -> dict[str, str]:
    flat: dict[str, str] = {}
    for key, value in obj.items():
        full = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            if "value" in value and isinstance(value["value"], str) and value["value"].startswith("#"):
                flat[full] = value["value"]
            else:
                flat.update(_flatten_tokens(value, full))
        elif isinstance(value, str) and value.startswith("#"):
            flat[full] = value
    return flat


def load_tokens(path: Path) -> dict[str, str]:
    raw = json.loads(path.read_text())
    if isinstance(raw, dict) and "colors" in raw and isinstance(raw["colors"], dict):
        return {f"colors.{k}": v for k, v in raw["colors"].items() if isinstance(v, str)}
    return _flatten_tokens(raw)


def audit_pairs(
    fg_tokens: dict[str, str],
    bg_tokens: dict[str, str],
    sizes: Iterable[str] = TEXT_SIZES,
) -> list[Violation]:
    violations: list[Violation] = []
    for fg_name, fg_hex in fg_tokens.items():
        for bg_name, bg_hex in bg_tokens.items():
            if fg_hex.lower() == bg_hex.lower():
                continue
            ratio = contrast_ratio(fg_hex, bg_hex)
            for size in sizes:
                required = THRESHOLDS[size]
                if ratio < required:
                    violations.append(
                        Violation(
                            fg=fg_name,
                            bg=bg_name,
                            fg_hex=fg_hex,
                            bg_hex=bg_hex,
                            text_size=size,
                            ratio=round(ratio, 2),
                            required=required,
                        )
                    )
    return violations


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    p.add_argument("--tokens", type=Path, required=True, help="JSON token file")
    p.add_argument(
        "--fg-prefix",
        default="text",
        help="prefix filter for foreground tokens (default: text)",
    )
    p.add_argument(
        "--bg-prefix",
        default="bg",
        help="prefix filter for background tokens (default: bg)",
    )
    p.add_argument(
        "--sizes",
        nargs="+",
        choices=TEXT_SIZES,
        default=list(TEXT_SIZES),
        help="text sizes to audit",
    )
    p.add_argument(
        "--format",
        choices=("json", "table"),
        default="json",
        help="output format",
    )
    args = p.parse_args(argv)

    if not args.tokens.exists():
        print(f"ERROR: token file not found: {args.tokens}", file=sys.stderr)
        return 2

    tokens = load_tokens(args.tokens)
    fg = {k: v for k, v in tokens.items() if args.fg_prefix in k}
    bg = {k: v for k, v in tokens.items() if args.bg_prefix in k}
    if not fg or not bg:
        print(
            f"ERROR: no fg (prefix={args.fg_prefix!r}) or bg (prefix={args.bg_prefix!r}) tokens matched",
            file=sys.stderr,
        )
        return 2

    violations = audit_pairs(fg, bg, args.sizes)
    report = {
        "fg_count": len(fg),
        "bg_count": len(bg),
        "sizes": args.sizes,
        "total_pairs": len(fg) * len(bg) * len(args.sizes),
        "violations": [asdict(v) for v in violations],
    }

    if args.format == "json":
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        if not violations:
            print(f"OK — {report['total_pairs']} pair(s) audited, 0 violations")
        else:
            print(f"FAIL — {len(violations)}/{report['total_pairs']} violating pair(s):")
            for v in violations:
                print(
                    f"  {v.fg} ({v.fg_hex}) × {v.bg} ({v.bg_hex}) "
                    f"@ {v.text_size}: {v.ratio} < {v.required}"
                )

    return 0 if not violations else 1


if __name__ == "__main__":
    raise SystemExit(main())
