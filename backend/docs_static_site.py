"""OP-792 static docs-site builder.

Builds a small static HTML artifact from the repo's markdown docs without
touching the Next.js application. CI publishes the generated directory to
GitHub Pages only after this module exits successfully, so a broken docs
build leaves the previous green site live.
"""
from __future__ import annotations

import argparse
import html
import re
from dataclasses import dataclass
from pathlib import Path

from backend import docs_site_adr, docs_site_lessons

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = REPO_ROOT / "docs-site-dist"
OPERATOR_DIR = REPO_ROOT / "docs" / "operator"
LOCALES = ("en", "zh-TW", "zh-CN", "ja")
OPERATOR_DOCS = (
    ("tutorial/first-invoke", "tutorial/first-invoke.md"),
    ("tutorial/handling-a-decision", "tutorial/handling-a-decision.md"),
    ("reference/operation-modes", "reference/operation-modes.md"),
    ("reference/decision-severity", "reference/decision-severity.md"),
    ("reference/panels-overview", "reference/panels-overview.md"),
    ("reference/budget-strategies", "reference/budget-strategies.md"),
    ("reference/glossary", "reference/glossary.md"),
    ("troubleshooting", "troubleshooting.md"),
)


@dataclass(frozen=True)
class Page:
    path: str
    title: str
    markdown: str


def _slugify(value: str) -> str:
    slug = re.sub(r"[^\w\s-]", "", value.lower(), flags=re.UNICODE)
    slug = re.sub(r"\s+", "-", slug.strip())
    return slug or "section"


def _inline(text: str) -> str:
    escaped = html.escape(text, quote=True)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", _link, escaped)
    return escaped


def _link(match: re.Match[str]) -> str:
    label = match.group(1)
    href = match.group(2).replace('"', "")
    if not re.match(r"^(https?:|/|\.{0,2}/)", href):
        href = "#"
    href = href.removesuffix(".md")
    return f'<a href="{href}">{label}</a>'


def markdown_to_html(markdown: str) -> str:
    lines = markdown.splitlines()
    out: list[str] = []
    in_code = False
    in_list = False
    in_table = False

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    def close_table() -> None:
        nonlocal in_table
        if in_table:
            out.append("</tbody></table>")
            in_table = False

    for line in lines:
        if line.startswith("```"):
            close_list()
            close_table()
            out.append("</code></pre>" if in_code else "<pre><code>")
            in_code = not in_code
            continue
        if in_code:
            out.append(html.escape(line))
            continue

        heading = re.match(r"^(#{1,4})\s+(.*)$", line)
        if heading:
            close_list()
            close_table()
            level = len(heading.group(1))
            text = heading.group(2).strip()
            ident = _slugify(text)
            out.append(f'<h{level} id="{ident}">{_inline(text)}</h{level}>')
            continue

        if line.startswith("|"):
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if all(re.match(r"^:?-{2,}:?$", cell) for cell in cells):
                continue
            close_list()
            tag = "th" if not in_table else "td"
            if not in_table:
                out.append("<table><thead>")
            out.append("<tr>" + "".join(f"<{tag}>{_inline(cell)}</{tag}>" for cell in cells) + "</tr>")
            if not in_table:
                out.append("</thead><tbody>")
                in_table = True
            continue
        close_table()

        item = re.match(r"^[-*]\s+(.*)$", line)
        if item:
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{_inline(item.group(1))}</li>")
            continue
        close_list()

        if not line.strip():
            out.append("")
        else:
            out.append(f"<p>{_inline(line)}</p>")

    close_list()
    close_table()
    if in_code:
        out.append("</code></pre>")
    return "\n".join(out)


def _title(markdown: str, fallback: str) -> str:
    for line in markdown.splitlines():
        match = re.match(r"^#\s+(.*)$", line)
        if match:
            return match.group(1).strip()
    return fallback


def _write_if_changed(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return
    path.write_text(content, encoding="utf-8")


def _render_page(title: str, body_html: str, nav_html: str = "") -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)} - OmniSight Docs</title>
  <style>
    :root {{ color-scheme: dark; font-family: ui-sans-serif, system-ui, sans-serif; background: #0b1220; color: #dbeafe; }}
    body {{ margin: 0; }}
    main {{ max-width: 960px; margin: 0 auto; padding: 32px 20px 56px; }}
    nav {{ margin-bottom: 28px; display: flex; flex-wrap: wrap; gap: 12px; font-size: 14px; }}
    a {{ color: #67e8f9; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    article {{ line-height: 1.68; }}
    h1, h2, h3, h4 {{ color: #f8fafc; line-height: 1.2; }}
    code, pre {{ background: #111827; border: 1px solid #334155; border-radius: 6px; }}
    code {{ padding: 1px 4px; }}
    pre {{ padding: 14px; overflow-x: auto; }}
    table {{ width: 100%; border-collapse: collapse; margin: 18px 0; }}
    th, td {{ border: 1px solid #334155; padding: 8px; text-align: left; vertical-align: top; }}
  </style>
</head>
<body>
  <main>
    {nav_html}
    <article>
{body_html}
    </article>
  </main>
</body>
</html>
"""


def _operator_pages() -> list[Page]:
    pages: list[Page] = []
    for locale in LOCALES:
        links: list[str] = []
        for route, filename in OPERATOR_DOCS:
            path = OPERATOR_DIR / locale / filename
            if not path.exists():
                continue
            markdown = path.read_text(encoding="utf-8")
            title = _title(markdown, route.rsplit("/", 1)[-1])
            pages.append(Page(f"docs/operator/{locale}/{route}/index.html", title, markdown))
            links.append(f'- [{title}](/docs/operator/{locale}/{route}/)')
        index_md = "\n".join([f"# Operator docs ({locale})", "", *links, ""])
        pages.append(Page(f"docs/operator/{locale}/index.html", f"Operator docs ({locale})", index_md))
    return pages


def build(output_dir: Path = DEFAULT_OUTPUT_DIR) -> tuple[int, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    pages = [
        Page("docs/adr/index.html", "Architecture Decision Records", docs_site_adr.build_adr_index()),
        Page("docs/sop/lessons/index.html", "Lessons Learned", docs_site_lessons.build_lessons_index()),
        *_operator_pages(),
    ]

    home_links = [
        "- [Architecture Decision Records](/docs/adr/)",
        "- [Lessons Learned](/docs/sop/lessons/)",
        *[f"- [Operator docs ({locale})](/docs/operator/{locale}/)" for locale in LOCALES],
    ]
    pages.append(Page("index.html", "OmniSight Docs", "\n".join(["# OmniSight Docs", "", *home_links, ""])))

    nav = '<nav><a href="/">Docs home</a><a href="/docs/adr/">ADR</a><a href="/docs/sop/lessons/">Lessons</a></nav>'
    for page in pages:
        body = markdown_to_html(page.markdown)
        _write_if_changed(output_dir / page.path, _render_page(page.title, body, nav))

    _write_if_changed(output_dir / ".nojekyll", "")
    return len(pages), output_dir


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the static docs-site artifact.")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    count, out = build(args.out)
    print(f"built {count} docs pages in {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
