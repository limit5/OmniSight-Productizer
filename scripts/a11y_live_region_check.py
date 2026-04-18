"""動態內容 ARIA live region 靜態檢查 — W5 a11y skill build-time gate。

掃描 TSX/JSX 源碼，找出「render 會隨狀態變更」但**未宣告 aria-live / role=status / role=alert**
的片段。對四種典型 dynamic-surface 場景高敏感：

  1) toast / snackbar / notification
  2) loading / spinner / progress / skeleton
  3) error / validation / flash
  4) route-change / async-fetch / polling

依據 WCAG 2.2 AA § 4.1.3 Status Messages 與 W2 `run_a11y_audit()` 的 axe `aria-allowed-role`
規則互補：axe 驗 runtime DOM、此腳本驗 source code（catch pre-runtime）。

Exit codes:
  0 — no suspected surfaces without live-region announcement
  1 — suspects found（需人工 review）
  2 — invocation error
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path


# 關鍵字辨識 — 同檔案 / 同節點命中即視為 dynamic-surface 嫌疑
DYNAMIC_PATTERNS = {
    "toast": re.compile(r"\b(toast|snackbar|notification|alert-banner)\b", re.I),
    "loading": re.compile(r"\b(loading|spinner|skeleton|progress(bar)?)\b", re.I),
    "error": re.compile(r"\b(error|validation|flash|form[- ]error)\b", re.I),
    "async": re.compile(r"\b(fetch(ing)?|polling|async[- ]update|revalidate)\b", re.I),
}

# live-region 宣告證據 — 任一存在即視為 compliant
LIVE_SIGNALS = re.compile(
    r'(aria-live\s*=|role\s*=\s*["\'](?:status|alert|log|timer)["\']'
    r'|useAnnouncer|ToastProvider|LiveRegion|aria-atomic|aria-relevant)',
    re.I,
)


@dataclass
class Suspect:
    file: str
    line: int
    category: str
    snippet: str


def _walk(root: Path, exts: tuple[str, ...]) -> list[Path]:
    out: list[Path] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() not in exts:
            continue
        parts = set(p.parts)
        if parts & {"node_modules", "dist", "build", ".next", "__pycache__", ".git"}:
            continue
        out.append(p)
    return out


def scan_file(path: Path) -> list[Suspect]:
    suspects: list[Suspect] = []
    try:
        text = path.read_text(errors="ignore")
    except OSError:
        return suspects

    has_live = bool(LIVE_SIGNALS.search(text))
    if has_live:
        return suspects

    for idx, line in enumerate(text.splitlines(), start=1):
        for category, pat in DYNAMIC_PATTERNS.items():
            if pat.search(line):
                suspects.append(
                    Suspect(
                        file=str(path),
                        line=idx,
                        category=category,
                        snippet=line.strip()[:200],
                    )
                )
                break
    return suspects


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    p.add_argument(
        "--root",
        type=Path,
        default=Path("components"),
        help="scan root (default: components)",
    )
    p.add_argument(
        "--ext",
        nargs="+",
        default=[".tsx", ".jsx", ".ts", ".js", ".svelte", ".vue"],
        help="file extensions to scan",
    )
    p.add_argument(
        "--format",
        choices=("json", "table"),
        default="table",
    )
    p.add_argument(
        "--ignore",
        nargs="*",
        default=[],
        help="substrings — skip files whose path contains any of these",
    )
    args = p.parse_args(argv)

    if not args.root.exists():
        print(f"ERROR: scan root not found: {args.root}", file=sys.stderr)
        return 2

    files = _walk(args.root, tuple(args.ext))
    if args.ignore:
        files = [
            f for f in files if not any(tok in str(f) for tok in args.ignore)
        ]

    all_suspects: list[Suspect] = []
    for f in files:
        all_suspects.extend(scan_file(f))

    if args.format == "json":
        print(
            json.dumps(
                {
                    "scanned_files": len(files),
                    "suspect_count": len(all_suspects),
                    "suspects": [asdict(s) for s in all_suspects],
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        if not all_suspects:
            print(f"OK — {len(files)} file(s) scanned, 0 live-region suspects")
        else:
            print(
                f"SUSPECTS — {len(all_suspects)} potential dynamic-surface(s) missing "
                f"aria-live / role=status|alert declaration:"
            )
            for s in all_suspects:
                print(f"  [{s.category}] {s.file}:{s.line}  {s.snippet}")

    return 0 if not all_suspects else 1


if __name__ == "__main__":
    raise SystemExit(main())
