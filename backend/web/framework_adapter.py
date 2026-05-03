"""W11.9 #XXX — Multi-framework adapter (Next / Nuxt / Astro render paths).

Layer 6 of the W11 *Website Cloning Capability* epic — the *productizer*
that consumes a frozen :class:`~backend.web.output_transformer.TransformedSpec`
(L3 output) plus the W11.7 :class:`~backend.web.clone_manifest.CloneManifest`
(L4 traceability) and emits a runnable project skeleton in one of three
target frameworks:

* **Next.js 14 (app router)** — `next` adapter.
* **Nuxt 3 (Vue 3 + Composition API)** — `nuxt` adapter.
* **Astro 4 (server-rendered, MPA-first)** — `astro` adapter.

Why three render paths and not one
----------------------------------
The W11 epic spec is explicit (TODO row): "**不限 React** — 生 Next/Nuxt/
Astro/Vue/Svelte 任一". Rendering the same spec into three frameworks
demonstrates the spec is a *content* contract, not a React contract — the
``TransformedSpec`` carries text + design tokens + image *placeholders*,
all of which map cleanly onto each framework's template language. Future
rows (Vue / Svelte / SolidStart …) plug in via the same
:class:`FrameworkAdapter` Protocol without changing the L1–L5 pipeline.

Where it slots into the pipeline
--------------------------------
The full router contract is::

    decision  = await check_machine_refusal_pre_capture(url)        # L1
    capture   = await source.capture(url, ...)                      # W11.2
    decision  = check_machine_refusal_post_capture(capture)         # L1
    spec      = build_clone_spec_from_capture(capture)              # W11.3
    classification = await classify_clone_spec(spec)                # L2
    assert_clone_spec_safe(spec, classification=classification)     # L2
    transformed = await transform_clone_spec(                       # L3
        spec, classification=classification,
    )
    assert_no_copied_bytes(transformed)                             # L3 invariant
    manifest  = build_clone_manifest(                               # L4
        source_url=spec.source_url, fetched_at=spec.fetched_at,
        backend=spec.backend, classification=classification,
        transformed=transformed, tenant_id=tenant_id, actor=actor,
    )
    record    = await pin_clone_artefacts(                          # L4
        project_root=project_path, manifest=manifest,
    )
    project   = render_clone_project(                               # ← this row
        transformed, framework="next", manifest=manifest,
    )
    write_rendered_project(project, project_root=project_path)
    await assert_clone_rate_limit(                                  # L5
        tenant_id=tenant_id, target_url=spec.source_url,
    )

Three-stage adapter contract
----------------------------
Every adapter implements :class:`FrameworkAdapter` with a single
:func:`render` method that returns a :class:`RenderedProject`. The render
is a **pure function** — it neither reads nor writes the filesystem. The
caller owns persistence via :func:`write_rendered_project`. This split
mirrors the W11.7 ``build_clone_manifest`` / ``write_manifest_file``
discipline so unit tests can exercise the rendering shape without
touching disk and so the manifest hash + audit row land *before* the
project files do.

Defense-in-depth invariants enforced at render entry
----------------------------------------------------
1. :func:`assert_no_copied_bytes` runs over the input
   :class:`TransformedSpec` — refuses ``data:`` URIs, base64 inline
   payloads, raw byte fields. Belt-and-braces of W11.6 L3.
2. Every text surface is sanitised via :func:`_escape_html` / framework-
   specific token replacement before going into a JSX / Vue template
   to defeat trivial template-injection attempts.
3. Every output file path is locked to a known-safe relative path
   (no traversal, no absolute paths, no symlinks). :func:`write_rendered_project`
   re-validates each file's relative path against the same allow-list.
4. The W11.7 traceability comment is **mandatory** when the caller passes
   a manifest — emitted as a literal ``<!-- omnisight:clone:begin … -->``
   block in the rendered ``index.html`` static traceability page so DMCA
   tooling can ``curl`` the deployed site and grep the comment without
   parsing a SPA. Each adapter ALSO emits framework-idiomatic
   ``<meta>`` tags carrying ``omnisight-clone-id`` /
   ``omnisight-manifest-hash`` for runtime verification.

Module-global state audit (SOP §1)
----------------------------------
Module-level state is limited to immutable constants
(``SUPPORTED_FRAMEWORKS`` frozenset, the framework name strings, the
``_FILENAME_RE`` compiled regex, the per-adapter template strings) and
the module-level :data:`logger` (the stdlib ``logging`` system owns its
own thread-safe singleton — answer #1). No per-process caches, no shared
mutable state. Cross-worker consistency is trivially answer #1: every
``uvicorn`` worker derives the same constants from source.

Read-after-write timing audit (SOP §2)
--------------------------------------
N/A. :func:`render_clone_project` is a pure function over an in-memory
:class:`TransformedSpec`. :func:`write_rendered_project` performs a
sequence of single file-system writes inside a freshly resolved
project root; no parallel-vs-serial timing dependence.

Production Readiness Gate §158
------------------------------
No new pip dependencies — the adapter emits text templates only and
relies on stdlib (``html``, ``json``, ``pathlib``, ``re``, ``dataclasses``,
``typing``). The generated project skeletons require ``npm`` /
``pnpm`` / ``yarn`` at the *operator's* deployment time but the
OmniSight backend image needs no Node / Next / Nuxt / Astro installed
to run the adapter. No image rebuild required.

Inspired by firecrawl/open-lovable (MIT). The full attribution +
license text live in ``LICENSES/open-lovable-mit.txt`` (W11.13).
"""

from __future__ import annotations

import html as _html_lib
import json
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, Sequence, Tuple, runtime_checkable

from backend.web.clone_manifest import (
    CloneManifest,
    inject_html_traceability_comment,
    render_html_traceability_comment,
)
from backend.web.output_transformer import (
    TransformedSpec,
    assert_no_copied_bytes,
)
from backend.web.site_cloner import SiteClonerError

logger = logging.getLogger(__name__)


# ── Constants ──────────────────────────────────────────────────────────

#: Stable framework identifiers operators flip via the ``framework`` arg.
#: The set is closed — adding a new framework requires both a new adapter
#: class AND a new entry here AND a new entry in the contract tests.
NEXT_FRAMEWORK_NAME: str = "next"
NUXT_FRAMEWORK_NAME: str = "nuxt"
ASTRO_FRAMEWORK_NAME: str = "astro"

#: Frozen set of every supported framework. ``make_framework_adapter`` /
#: ``render_clone_project`` reject any value outside this set with
#: :class:`UnknownFrameworkError`.
SUPPORTED_FRAMEWORKS: frozenset[str] = frozenset({
    NEXT_FRAMEWORK_NAME,
    NUXT_FRAMEWORK_NAME,
    ASTRO_FRAMEWORK_NAME,
})

#: Stable relative path of the static traceability HTML scaffold every
#: adapter emits. Pinned so DMCA / audit tooling can crawl deployed
#: sites at ``<host>/clone-traceability.html`` without parsing the SPA.
TRACEABILITY_HTML_RELATIVE_PATH: str = "public/clone-traceability.html"

#: Canonical filename of the static traceability HTML page. The
#: ``public/`` prefix is what every framework in
#: :data:`SUPPORTED_FRAMEWORKS` ships verbatim into the build output.
TRACEABILITY_HTML_FILENAME: str = "clone-traceability.html"

#: Filename regex for :func:`write_rendered_project` path validation —
#: enforces that every emitted file's relative path is restricted to
#: ASCII-safe chars (alnum / dot / underscore / hyphen / forward slash).
#: No backslash, no whitespace, no leading slash, no ``..`` segments.
_FILENAME_RE = re.compile(r"^[A-Za-z0-9_.\-/]+$")

#: Hard cap on the per-list section count we render into the framework
#: page. Mirrors :data:`MAX_RENDERED_SECTIONS` in the L3 transformer —
#: a chatty model that returned 1000 sections would blow up the rendered
#: project size; cap at the same conservative number.
MAX_RENDERED_SECTIONS: int = 50

#: Hard cap on the per-list nav item count.
MAX_RENDERED_NAV_ITEMS: int = 50

#: Hard cap on the per-list image count.
MAX_RENDERED_IMAGES: int = 50

#: Default `<meta name="generator">` value baked into every page.
GENERATOR_META: str = "OmniSight Cloner (W11) — Inspired by firecrawl/open-lovable (MIT)"


# ── Errors ─────────────────────────────────────────────────────────────


class FrameworkAdapterError(SiteClonerError):
    """Base class for everything raised by ``framework_adapter``.

    Subclass of :class:`backend.web.site_cloner.SiteClonerError` so a
    single ``except SiteClonerError`` in the calling router catches L1 /
    L2 / L3 / L4 / L5 / W11.9 errors uniformly; the W11.12 audit row
    uses ``isinstance`` to assign the finer bucket.
    """


class UnknownFrameworkError(FrameworkAdapterError):
    """:func:`make_framework_adapter` / :func:`render_clone_project` was
    asked for a framework name outside :data:`SUPPORTED_FRAMEWORKS`.
    Distinct from :class:`RenderedProjectWriteError` so the audit row
    can disambiguate "operator typed wrong framework knob" from "right
    knob, filesystem broken"."""


class RenderedProjectWriteError(FrameworkAdapterError):
    """:func:`write_rendered_project` could not persist a file (parent
    directory creation failed, write permission denied, path traversal
    detected on a synthesised path, …). The exception message names the
    failing file's relative path so operators can fix the underlying
    permission / disk issue without re-running the whole pipeline."""


# ── Data structures ────────────────────────────────────────────────────


@dataclass(frozen=True)
class RenderedFile:
    """A single text file emitted by an adapter.

    ``relative_path`` is an OS-agnostic forward-slash-separated path
    relative to the project root. The path is locked by
    :data:`_FILENAME_RE` so no traversal / absolute paths / backslashes
    survive into :func:`write_rendered_project`.
    """

    relative_path: str
    content: str


@dataclass(frozen=True)
class RenderedProject:
    """The deliverable of :func:`render_clone_project`.

    Frozen so downstream code (the filesystem writer, an audit-log
    payload renderer, a future "package the project as a zipfile"
    helper) cannot mutate after the L3/L4 invariants have run.

    Attributes:
        framework: Stable framework identifier from
            :data:`SUPPORTED_FRAMEWORKS`.
        adapter_name: Stable adapter class name (for the audit row's
            ``rendered_with`` field).
        files: Ordered tuple of :class:`RenderedFile` records — every
            file the adapter wants the writer to persist. Order is
            stable across calls so manifest-hash audits don't shuffle.
        traceability_html_relative_path: ``None`` when no manifest was
            provided to :func:`render`, otherwise the relative path of
            the file that received the W11.7 traceability comment.
            Pinned to :data:`TRACEABILITY_HTML_RELATIVE_PATH` for every
            adapter so post-build verification tooling has a single
            URL to ``curl``.
        manifest_clone_id: ``None`` when no manifest was provided,
            otherwise the manifest's ``clone_id`` baked into the
            framework's ``<meta>`` tags. Surfaces in the W11.12 audit
            row's ``rendered_with`` payload.
        manifest_hash: ``None`` when no manifest was provided,
            otherwise the manifest's ``manifest_hash`` baked into the
            framework's ``<meta>`` tags.
    """

    framework: str
    adapter_name: str
    files: Tuple[RenderedFile, ...]
    traceability_html_relative_path: Optional[str]
    manifest_clone_id: Optional[str]
    manifest_hash: Optional[str]


# ── Backend protocol ───────────────────────────────────────────────────


@runtime_checkable
class FrameworkAdapter(Protocol):
    """Pluggable framework backend.

    Default implementations: :class:`NextFrameworkAdapter`,
    :class:`NuxtFrameworkAdapter`, :class:`AstroFrameworkAdapter`. Tests
    / future Vue / Svelte rows substitute their own adapter that
    satisfies this protocol.
    """

    framework: str
    """Stable identifier from :data:`SUPPORTED_FRAMEWORKS`."""

    name: str
    """Stable class name surfaced into the audit row's
    ``rendered_with.adapter`` field."""

    def render(
        self,
        transformed: TransformedSpec,
        *,
        manifest: Optional[CloneManifest] = None,
    ) -> RenderedProject: ...


# ── Helpers ────────────────────────────────────────────────────────────


def _escape_html(s: object) -> str:
    """HTML-escape arbitrary text. Returns empty string for non-string /
    falsy input so adapter templates can rely on ``str`` output without
    a None check at every interpolation site."""
    if not isinstance(s, str):
        return ""
    return _html_lib.escape(s, quote=True)


def _escape_jsx_text(s: object) -> str:
    """Escape text for inclusion as a JSX text child.

    JSX strips ``{`` / ``}`` as expression delimiters; escape them via
    ``&#123;`` / ``&#125;`` so spec text containing curly braces (e.g.
    ``{tenant}`` placeholders, mustache-template fragments) renders as
    literal text instead of triggering a JSX parse error.
    """
    return _escape_html(s).replace("{", "&#123;").replace("}", "&#125;")


def _json_safe(s: object) -> str:
    """JSON-encode a string for use inside a JSON file (e.g.
    package.json). Returns ``"\"\""`` for non-string input. Output is
    JSON-quoted (i.e. includes the surrounding double quotes) so callers
    can interpolate directly into a JSON template."""
    return json.dumps("" if not isinstance(s, str) else s)


def _slug_or_default(text: object, *, default: str) -> str:
    """Reduce ``text`` to a JS-package-name-safe slug.

    Used by the package.json template to derive a default ``name`` field
    from the rewritten title. Lowercase ASCII alnum + hyphen only; falls
    back to ``default`` when the input is empty / non-string / fully
    stripped."""
    if not isinstance(text, str):
        text = ""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or default


def _sanitised_meta(transformed: TransformedSpec) -> Mapping[str, str]:
    """Return only the safe meta keys baked into the rendered ``<head>``.

    Mirrors :class:`TransformedSpec`'s `meta` allow-list — the L3
    transformer already drops ``og:url`` / ``canonical`` / source-
    identity meta, but we re-filter here in case a future
    ``TransformedSpec`` factory widens the surface.
    """
    safe_keys = (
        "description",
        "og:description",
        "og:title",
        "twitter:description",
    )
    meta = transformed.meta or {}
    out: dict[str, str] = {}
    for key in safe_keys:
        val = meta.get(key)
        if isinstance(val, str) and val:
            out[key] = val
    return out


def _hero_text(transformed: TransformedSpec) -> Mapping[str, str]:
    """Return ``{heading, tagline, cta_label}`` strings (always present,
    fall back to empty string when the source had no hero)."""
    hero = transformed.hero or {}
    return {
        "heading": str(hero.get("heading", "") or "") if isinstance(hero, dict) else "",
        "tagline": str(hero.get("tagline", "") or "") if isinstance(hero, dict) else "",
        "cta_label": str(hero.get("cta_label", "") or "") if isinstance(hero, dict) else "",
    }


def _nav_labels(transformed: TransformedSpec) -> Sequence[str]:
    out: list[str] = []
    for item in (transformed.nav or ())[:MAX_RENDERED_NAV_ITEMS]:
        if isinstance(item, dict):
            label = item.get("label")
            if isinstance(label, str) and label:
                out.append(label)
    return tuple(out)


def _sections(transformed: TransformedSpec) -> Sequence[Mapping[str, str]]:
    out: list[dict[str, str]] = []
    for section in (transformed.sections or ())[:MAX_RENDERED_SECTIONS]:
        if not isinstance(section, dict):
            continue
        heading = section.get("heading", "")
        summary = section.get("summary", "")
        if not isinstance(heading, str):
            heading = ""
        if not isinstance(summary, str):
            summary = ""
        if heading or summary:
            out.append({"heading": heading, "summary": summary})
    return tuple(out)


def _images(transformed: TransformedSpec) -> Sequence[Mapping[str, str]]:
    out: list[dict[str, str]] = []
    for image in (transformed.images or ())[:MAX_RENDERED_IMAGES]:
        if not isinstance(image, dict):
            continue
        url = image.get("url")
        if not isinstance(url, str) or not url:
            continue
        alt = image.get("alt", "") or ""
        if not isinstance(alt, str):
            alt = ""
        out.append({"url": url, "alt": alt})
    return tuple(out)


def _footer_text(transformed: TransformedSpec) -> str:
    footer = transformed.footer or {}
    if isinstance(footer, dict):
        text = footer.get("text", "")
        if isinstance(text, str):
            return text
    return ""


def _design_tokens_css(transformed: TransformedSpec) -> str:
    """Emit ``:root { --omnisight-... }`` CSS custom properties from the
    pass-through design tokens. Adapters embed this into their global
    stylesheet so the rewritten copy gets paired with the source's
    visual rhythm without copying any layout."""
    lines: list[str] = [":root {"]
    for idx, color in enumerate((transformed.colors or ())[:24]):
        if isinstance(color, str) and color:
            lines.append(f"  --omnisight-color-{idx + 1}: {color};")
    for idx, font in enumerate((transformed.fonts or ())[:8]):
        if isinstance(font, str) and font:
            lines.append(f"  --omnisight-font-{idx + 1}: {font};")
    spacing = transformed.spacing or {}
    if isinstance(spacing, dict):
        max_width = spacing.get("max_width")
        if isinstance(max_width, str) and max_width:
            lines.append(f"  --omnisight-max-width: {max_width};")
    lines.append("}")
    return "\n".join(lines)


def _validate_relative_path(rel_path: str) -> None:
    """Reject anything that could escape the project root or rely on
    OS-specific separators. Called by :func:`write_rendered_project`
    before any filesystem op."""
    if not isinstance(rel_path, str) or not rel_path:
        raise RenderedProjectWriteError(
            f"file relative_path must be a non-empty string, got {rel_path!r}"
        )
    if rel_path.startswith("/") or rel_path.startswith("\\"):
        raise RenderedProjectWriteError(
            f"file relative_path must not be absolute, got {rel_path!r}"
        )
    if "\\" in rel_path:
        raise RenderedProjectWriteError(
            f"file relative_path must not contain backslashes, got {rel_path!r}"
        )
    if not _FILENAME_RE.match(rel_path):
        raise RenderedProjectWriteError(
            f"file relative_path contains disallowed characters, got {rel_path!r}"
        )
    parts = rel_path.split("/")
    if any(p in {"", ".", ".."} for p in parts):
        raise RenderedProjectWriteError(
            f"file relative_path must not contain '.' / '..' / empty segments, got {rel_path!r}"
        )


def _build_traceability_html_scaffold(
    transformed: TransformedSpec,
    manifest: Optional[CloneManifest],
) -> str:
    """Produce the static ``public/clone-traceability.html`` body.

    This file is the canonical static traceability artefact every
    adapter ships. It is **not** the framework's main rendered page —
    it is a parallel static file that DMCA / audit tooling can crawl at
    a stable URL. The W11.7 traceability comment is injected into its
    ``<head>``.
    """
    title_text = _escape_html(transformed.title or "Cloned site")
    meta_lines: list[str] = []
    for key, value in _sanitised_meta(transformed).items():
        meta_lines.append(
            f'    <meta name="{_escape_html(key)}" content="{_escape_html(value)}" />'
        )
    if manifest is not None:
        meta_lines.append(
            f'    <meta name="omnisight-clone-id" content="{_escape_html(manifest.clone_id)}" />'
        )
        meta_lines.append(
            f'    <meta name="omnisight-manifest-hash" content="{_escape_html(manifest.manifest_hash)}" />'
        )
        meta_lines.append(
            f'    <meta name="omnisight-source-url" content="{_escape_html((manifest.source or {}).get("url", ""))}" />'
        )
    meta_lines.append(f'    <meta name="generator" content="{_escape_html(GENERATOR_META)}" />')
    meta_block = "\n".join(meta_lines)

    hero = _hero_text(transformed)
    sections = _sections(transformed)
    section_blocks: list[str] = []
    for section in sections:
        section_blocks.append(
            f"      <section>\n"
            f"        <h2>{_escape_html(section['heading'])}</h2>\n"
            f"        <p>{_escape_html(section['summary'])}</p>\n"
            f"      </section>"
        )
    sections_html = "\n".join(section_blocks)
    footer_text = _escape_html(_footer_text(transformed))

    scaffold = (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "  <head>\n"
        '    <meta charset="utf-8" />\n'
        '    <meta name="viewport" content="width=device-width, initial-scale=1" />\n'
        f"    <title>{title_text}</title>\n"
        f"{meta_block}\n"
        "  </head>\n"
        "  <body>\n"
        "    <main>\n"
        f"      <h1>{_escape_html(hero['heading'])}</h1>\n"
        f"      <p>{_escape_html(hero['tagline'])}</p>\n"
        f"{sections_html}\n"
        f"      <footer>{footer_text}</footer>\n"
        "    </main>\n"
        "  </body>\n"
        "</html>\n"
    )
    if manifest is not None:
        scaffold = inject_html_traceability_comment(scaffold, manifest, position="head")
    return scaffold


# ── Adapter base ───────────────────────────────────────────────────────


class _AdapterBase(ABC):
    """Shared rendering scaffolding the three concrete adapters reuse.

    Each subclass implements ``_render_files`` and pins ``framework`` /
    ``name`` class attributes; :meth:`render` runs the pre-render
    invariant gates, calls the subclass renderer, and assembles the
    :class:`RenderedProject`.
    """

    framework: str = ""
    name: str = ""

    def render(
        self,
        transformed: TransformedSpec,
        *,
        manifest: Optional[CloneManifest] = None,
    ) -> RenderedProject:
        if not isinstance(transformed, TransformedSpec):
            raise FrameworkAdapterError(
                f"transformed must be TransformedSpec, got {type(transformed).__name__}"
            )
        if manifest is not None and not isinstance(manifest, CloneManifest):
            raise FrameworkAdapterError(
                f"manifest must be CloneManifest or None, got {type(manifest).__name__}"
            )
        # W11.6 invariant — defensively re-check in case a future caller
        # forgets to wrap the L3 output and the upstream gate didn't
        # fire (e.g. tests that hand-roll a TransformedSpec).
        assert_no_copied_bytes(transformed)

        files = list(self._render_files(transformed, manifest=manifest))
        # Validate every emitted relative path so we fail fast at render
        # time rather than at write time.
        for rendered_file in files:
            _validate_relative_path(rendered_file.relative_path)

        return RenderedProject(
            framework=self.framework,
            adapter_name=self.name,
            files=tuple(files),
            traceability_html_relative_path=(
                TRACEABILITY_HTML_RELATIVE_PATH if manifest is not None else None
            ),
            manifest_clone_id=manifest.clone_id if manifest is not None else None,
            manifest_hash=manifest.manifest_hash if manifest is not None else None,
        )

    @abstractmethod
    def _render_files(
        self,
        transformed: TransformedSpec,
        *,
        manifest: Optional[CloneManifest],
    ) -> Sequence[RenderedFile]:
        raise NotImplementedError


# ── Next.js 14 (app router) adapter ───────────────────────────────────


class NextFrameworkAdapter(_AdapterBase):
    """Emit a Next.js 14 app-router project skeleton.

    Files emitted:

    * ``package.json`` — Next 14 + React 18 + TypeScript stack.
    * ``next.config.mjs`` — minimal config; ``reactStrictMode: true``.
    * ``tsconfig.json`` — Next.js defaults.
    * ``app/layout.tsx`` — root layout with the rewritten ``<title>`` /
      meta tags + the W11.7 traceability ``<meta>`` tags.
    * ``app/page.tsx`` — main page rendering hero / nav / sections /
      images-as-placeholders / footer.
    * ``app/globals.css`` — design-token CSS variables.
    * ``public/clone-traceability.html`` — static traceability scaffold
      with the W11.7 ``<!-- omnisight:clone:begin … -->`` comment.
    * ``.gitignore`` — Next.js defaults + ``.omnisight/``.
    * ``README.md`` — operator instructions + attribution.
    """

    framework = NEXT_FRAMEWORK_NAME
    name = "NextFrameworkAdapter"

    def _render_files(
        self,
        transformed: TransformedSpec,
        *,
        manifest: Optional[CloneManifest],
    ) -> Sequence[RenderedFile]:
        title = _escape_jsx_text(transformed.title or "Cloned site")
        hero = _hero_text(transformed)
        nav_labels = _nav_labels(transformed)
        sections = _sections(transformed)
        images = _images(transformed)
        footer = _footer_text(transformed)
        sanitised_meta = _sanitised_meta(transformed)

        slug = _slug_or_default(transformed.title, default="omnisight-clone")
        package_json = (
            "{\n"
            f"  \"name\": {_json_safe(slug)},\n"
            "  \"version\": \"0.1.0\",\n"
            "  \"private\": true,\n"
            "  \"scripts\": {\n"
            "    \"dev\": \"next dev\",\n"
            "    \"build\": \"next build\",\n"
            "    \"start\": \"next start\"\n"
            "  },\n"
            "  \"dependencies\": {\n"
            "    \"next\": \"14.2.3\",\n"
            "    \"react\": \"18.3.1\",\n"
            "    \"react-dom\": \"18.3.1\"\n"
            "  },\n"
            "  \"devDependencies\": {\n"
            "    \"@types/react\": \"18.3.3\",\n"
            "    \"@types/react-dom\": \"18.3.0\",\n"
            "    \"@types/node\": \"20.14.2\",\n"
            "    \"typescript\": \"5.4.5\"\n"
            "  }\n"
            "}\n"
        )

        next_config = (
            "/** @type {import('next').NextConfig} */\n"
            "const nextConfig = {\n"
            "  reactStrictMode: true,\n"
            "};\n"
            "export default nextConfig;\n"
        )

        tsconfig = (
            "{\n"
            "  \"compilerOptions\": {\n"
            "    \"target\": \"ES2022\",\n"
            "    \"lib\": [\"dom\", \"dom.iterable\", \"esnext\"],\n"
            "    \"allowJs\": false,\n"
            "    \"skipLibCheck\": true,\n"
            "    \"strict\": true,\n"
            "    \"noEmit\": true,\n"
            "    \"esModuleInterop\": true,\n"
            "    \"module\": \"esnext\",\n"
            "    \"moduleResolution\": \"bundler\",\n"
            "    \"resolveJsonModule\": true,\n"
            "    \"isolatedModules\": true,\n"
            "    \"jsx\": \"preserve\",\n"
            "    \"incremental\": true\n"
            "  },\n"
            "  \"include\": [\"next-env.d.ts\", \"**/*.ts\", \"**/*.tsx\"],\n"
            "  \"exclude\": [\"node_modules\"]\n"
            "}\n"
        )

        # Build metadata fields for app/layout.tsx; Next 14 prefers a
        # ``metadata`` export over a hand-rolled <head>.
        description = sanitised_meta.get("description") or sanitised_meta.get("og:description") or ""
        meta_clone_lines: list[str] = []
        if manifest is not None:
            meta_clone_lines.extend([
                f"        \"omnisight-clone-id\": {_json_safe(manifest.clone_id)},",
                f"        \"omnisight-manifest-hash\": {_json_safe(manifest.manifest_hash)},",
                f"        \"omnisight-source-url\": {_json_safe((manifest.source or {}).get('url', ''))}",
            ])
        else:
            meta_clone_lines.append("        \"omnisight-clone-id\": \"\"")
        meta_clone_block = "\n".join(meta_clone_lines)

        layout_tsx = (
            "import type { Metadata } from 'next';\n"
            "import './globals.css';\n"
            "\n"
            "export const metadata: Metadata = {\n"
            f"  title: {_json_safe(transformed.title or 'Cloned site')},\n"
            f"  description: {_json_safe(description)},\n"
            "  generator: " + _json_safe(GENERATOR_META) + ",\n"
            "  other: {\n"
            f"{meta_clone_block}\n"
            "  },\n"
            "};\n"
            "\n"
            "export default function RootLayout({\n"
            "  children,\n"
            "}: {\n"
            "  children: React.ReactNode;\n"
            "}) {\n"
            "  return (\n"
            "    <html lang=\"en\">\n"
            "      <body>{children}</body>\n"
            "    </html>\n"
            "  );\n"
            "}\n"
        )

        nav_jsx_items = (
            "\n".join(
                f"        <li><a href=\"#\">{_escape_jsx_text(label)}</a></li>"
                for label in nav_labels
            )
            or "        <li><a href=\"#\">Home</a></li>"
        )

        section_jsx_items = "\n".join(
            f"      <section>\n"
            f"        <h2>{_escape_jsx_text(section['heading'])}</h2>\n"
            f"        <p>{_escape_jsx_text(section['summary'])}</p>\n"
            f"      </section>"
            for section in sections
        )

        image_jsx_items = "\n".join(
            f"        <img src={_json_safe(image['url'])} alt={_json_safe(image['alt'])} />"
            for image in images
        )

        page_tsx = (
            "export default function Page() {\n"
            "  return (\n"
            "    <main>\n"
            "      <nav>\n"
            "        <ul>\n"
            f"{nav_jsx_items}\n"
            "        </ul>\n"
            "      </nav>\n"
            "      <header>\n"
            f"        <h1>{_escape_jsx_text(hero['heading'])}</h1>\n"
            f"        <p>{_escape_jsx_text(hero['tagline'])}</p>\n"
            f"        <button type=\"button\">{_escape_jsx_text(hero['cta_label']) or 'Learn more'}</button>\n"
            "      </header>\n"
            f"{section_jsx_items}\n"
            "      <div className=\"omnisight-images\">\n"
            f"{image_jsx_items}\n"
            "      </div>\n"
            f"      <footer>{_escape_jsx_text(footer)}</footer>\n"
            "    </main>\n"
            "  );\n"
            "}\n"
        )

        globals_css = (
            "/* Auto-generated by OmniSight Cloner — design tokens harvested from source. */\n"
            f"{_design_tokens_css(transformed)}\n"
            "body { font-family: var(--omnisight-font-1, system-ui, sans-serif); margin: 0; }\n"
            "main { max-width: var(--omnisight-max-width, 1200px); margin: 0 auto; padding: 1rem; }\n"
            "img { max-width: 100%; height: auto; }\n"
        )

        traceability_html = _build_traceability_html_scaffold(transformed, manifest)

        gitignore = (
            "# Next.js\n"
            "node_modules/\n"
            ".next/\n"
            "out/\n"
            "build/\n"
            "dist/\n"
            "\n"
            "# OmniSight clone artefacts\n"
            ".omnisight/\n"
            "\n"
            "# Env\n"
            ".env*.local\n"
        )

        readme = self._readme(manifest)

        return [
            RenderedFile("package.json", package_json),
            RenderedFile("next.config.mjs", next_config),
            RenderedFile("tsconfig.json", tsconfig),
            RenderedFile("app/layout.tsx", layout_tsx),
            RenderedFile("app/page.tsx", page_tsx),
            RenderedFile("app/globals.css", globals_css),
            RenderedFile(TRACEABILITY_HTML_RELATIVE_PATH, traceability_html),
            RenderedFile(".gitignore", gitignore),
            RenderedFile("README.md", readme),
        ]

    @staticmethod
    def _readme(manifest: Optional[CloneManifest]) -> str:
        body = (
            "# OmniSight Cloned Site (Next.js)\n"
            "\n"
            "Generated by the OmniSight W11 cloner. The source page was\n"
            "captured under the W11 5-layer defense-in-depth pipeline\n"
            "(L1 robots/noai → L2 LLM classifier → L3 transformer →\n"
            "L4 traceability manifest → L5 rate limit + PEP HOLD).\n"
            "\n"
            "## Run\n"
            "\n"
            "```bash\n"
            "npm install\n"
            "npm run dev\n"
            "```\n"
            "\n"
            "## Traceability\n"
            "\n"
            f"The static traceability scaffold lives at `{TRACEABILITY_HTML_RELATIVE_PATH}`.\n"
            "It carries the W11.7 manifest hash + source URL in an HTML\n"
            "comment block plus matching `<meta>` tags so any DMCA /\n"
            "compliance crawler can verify provenance with one curl.\n"
            "\n"
            "## Attribution\n"
            "\n"
            "Inspired by firecrawl/open-lovable (MIT). See "
            "`LICENSES/open-lovable-mit.txt` in the OmniSight repo for the "
            "full attribution + license text.\n"
        )
        if manifest is not None:
            body += (
                "\n## Manifest\n"
                f"\n- clone_id: `{manifest.clone_id}`\n"
                f"- manifest_hash: `{manifest.manifest_hash}`\n"
            )
        return body


# ── Nuxt 3 adapter ─────────────────────────────────────────────────────


class NuxtFrameworkAdapter(_AdapterBase):
    """Emit a Nuxt 3 (Vue 3 + Composition API) project skeleton.

    Files emitted:

    * ``package.json`` — Nuxt 3 + Vue 3 + TypeScript stack.
    * ``nuxt.config.ts`` — minimal config, registers
      ``app.head`` with the rewritten title + meta + W11.7 manifest tags.
    * ``tsconfig.json`` — extends the Nuxt-generated tsconfig.
    * ``app.vue`` — root component delegating to ``<NuxtPage />``.
    * ``pages/index.vue`` — main page using ``<script setup lang="ts">``.
    * ``assets/css/main.css`` — design-token CSS variables.
    * ``public/clone-traceability.html`` — same static traceability
      scaffold every adapter ships.
    * ``.gitignore`` — Nuxt defaults + ``.omnisight/``.
    * ``README.md`` — operator instructions + attribution.
    """

    framework = NUXT_FRAMEWORK_NAME
    name = "NuxtFrameworkAdapter"

    def _render_files(
        self,
        transformed: TransformedSpec,
        *,
        manifest: Optional[CloneManifest],
    ) -> Sequence[RenderedFile]:
        hero = _hero_text(transformed)
        nav_labels = _nav_labels(transformed)
        sections = _sections(transformed)
        images = _images(transformed)
        footer = _footer_text(transformed)
        sanitised_meta = _sanitised_meta(transformed)
        description = sanitised_meta.get("description") or sanitised_meta.get("og:description") or ""

        slug = _slug_or_default(transformed.title, default="omnisight-clone")
        package_json = (
            "{\n"
            f"  \"name\": {_json_safe(slug)},\n"
            "  \"version\": \"0.1.0\",\n"
            "  \"private\": true,\n"
            "  \"scripts\": {\n"
            "    \"dev\": \"nuxt dev\",\n"
            "    \"build\": \"nuxt build\",\n"
            "    \"generate\": \"nuxt generate\",\n"
            "    \"preview\": \"nuxt preview\"\n"
            "  },\n"
            "  \"devDependencies\": {\n"
            "    \"nuxt\": \"3.12.4\",\n"
            "    \"vue\": \"3.4.34\",\n"
            "    \"typescript\": \"5.4.5\"\n"
            "  }\n"
            "}\n"
        )

        head_meta_lines = [
            "      { name: 'generator', content: " + _json_safe(GENERATOR_META) + " },",
        ]
        for key, value in sanitised_meta.items():
            head_meta_lines.append(
                "      { name: " + _json_safe(key) + ", content: " + _json_safe(value) + " },"
            )
        if manifest is not None:
            head_meta_lines.extend([
                "      { name: 'omnisight-clone-id', content: " + _json_safe(manifest.clone_id) + " },",
                "      { name: 'omnisight-manifest-hash', content: " + _json_safe(manifest.manifest_hash) + " },",
                "      { name: 'omnisight-source-url', content: " + _json_safe((manifest.source or {}).get('url', '')) + " },",
            ])
        head_meta_block = "\n".join(head_meta_lines)

        nuxt_config = (
            "// https://nuxt.com/docs/api/configuration/nuxt-config\n"
            "export default defineNuxtConfig({\n"
            "  ssr: true,\n"
            "  app: {\n"
            "    head: {\n"
            f"      title: {_json_safe(transformed.title or 'Cloned site')},\n"
            "      meta: [\n"
            f"{head_meta_block}\n"
            "      ],\n"
            "    },\n"
            "  },\n"
            "  css: ['~/assets/css/main.css'],\n"
            "});\n"
        )

        tsconfig = (
            "{\n"
            "  \"extends\": \"./.nuxt/tsconfig.json\"\n"
            "}\n"
        )

        app_vue = (
            "<template>\n"
            "  <NuxtPage />\n"
            "</template>\n"
        )

        nav_html = (
            "\n".join(
                f"        <li><a href=\"#\">{_escape_html(label)}</a></li>"
                for label in nav_labels
            )
            or "        <li><a href=\"#\">Home</a></li>"
        )
        sections_html = "\n".join(
            f"      <section>\n"
            f"        <h2>{_escape_html(section['heading'])}</h2>\n"
            f"        <p>{_escape_html(section['summary'])}</p>\n"
            f"      </section>"
            for section in sections
        )
        image_html = "\n".join(
            f"        <img :src={_json_safe(image['url'])} :alt={_json_safe(image['alt'])} />"
            for image in images
        )

        index_vue = (
            "<script setup lang=\"ts\">\n"
            "// Auto-generated page rendered by the OmniSight cloner.\n"
            f"const heroHeading = {_json_safe(hero['heading'])};\n"
            f"const heroTagline = {_json_safe(hero['tagline'])};\n"
            f"const ctaLabel = {_json_safe(hero['cta_label'] or 'Learn more')};\n"
            "</script>\n"
            "\n"
            "<template>\n"
            "  <main>\n"
            "    <nav>\n"
            "      <ul>\n"
            f"{nav_html}\n"
            "      </ul>\n"
            "    </nav>\n"
            "    <header>\n"
            "      <h1>{{ heroHeading }}</h1>\n"
            "      <p>{{ heroTagline }}</p>\n"
            "      <button type=\"button\">{{ ctaLabel }}</button>\n"
            "    </header>\n"
            f"{sections_html}\n"
            "    <div class=\"omnisight-images\">\n"
            f"{image_html}\n"
            "    </div>\n"
            f"    <footer>{_escape_html(footer)}</footer>\n"
            "  </main>\n"
            "</template>\n"
        )

        main_css = (
            "/* Auto-generated by OmniSight Cloner — design tokens harvested from source. */\n"
            f"{_design_tokens_css(transformed)}\n"
            "body { font-family: var(--omnisight-font-1, system-ui, sans-serif); margin: 0; }\n"
            "main { max-width: var(--omnisight-max-width, 1200px); margin: 0 auto; padding: 1rem; }\n"
            "img { max-width: 100%; height: auto; }\n"
        )

        traceability_html = _build_traceability_html_scaffold(transformed, manifest)

        gitignore = (
            "# Nuxt\n"
            "node_modules/\n"
            ".nuxt/\n"
            ".output/\n"
            "dist/\n"
            "\n"
            "# OmniSight clone artefacts\n"
            ".omnisight/\n"
            "\n"
            "# Env\n"
            ".env*.local\n"
        )

        readme = self._readme(manifest)

        return [
            RenderedFile("package.json", package_json),
            RenderedFile("nuxt.config.ts", nuxt_config),
            RenderedFile("tsconfig.json", tsconfig),
            RenderedFile("app.vue", app_vue),
            RenderedFile("pages/index.vue", index_vue),
            RenderedFile("assets/css/main.css", main_css),
            RenderedFile(TRACEABILITY_HTML_RELATIVE_PATH, traceability_html),
            RenderedFile(".gitignore", gitignore),
            RenderedFile("README.md", readme),
        ]

    @staticmethod
    def _readme(manifest: Optional[CloneManifest]) -> str:
        body = (
            "# OmniSight Cloned Site (Nuxt 3)\n"
            "\n"
            "Generated by the OmniSight W11 cloner under the same\n"
            "5-layer defense-in-depth pipeline as the Next.js / Astro\n"
            "render paths.\n"
            "\n"
            "## Run\n"
            "\n"
            "```bash\n"
            "npm install\n"
            "npm run dev\n"
            "```\n"
            "\n"
            "## Traceability\n"
            "\n"
            f"The static traceability scaffold lives at `{TRACEABILITY_HTML_RELATIVE_PATH}`.\n"
            "It carries the W11.7 manifest hash + source URL in an HTML\n"
            "comment block plus matching `<meta>` tags.\n"
            "\n"
            "## Attribution\n"
            "\n"
            "Inspired by firecrawl/open-lovable (MIT). See "
            "`LICENSES/open-lovable-mit.txt` in the OmniSight repo for the "
            "full attribution + license text.\n"
        )
        if manifest is not None:
            body += (
                "\n## Manifest\n"
                f"\n- clone_id: `{manifest.clone_id}`\n"
                f"- manifest_hash: `{manifest.manifest_hash}`\n"
            )
        return body


# ── Astro 4 adapter ────────────────────────────────────────────────────


class AstroFrameworkAdapter(_AdapterBase):
    """Emit an Astro 4 project skeleton.

    Astro is server-rendered MPA-first so the rendered ``index.astro``
    page contains literal HTML — the W11.7 traceability comment can be
    baked directly into the page head without a JSX detour. Files:

    * ``package.json`` — Astro 4 + TypeScript stack.
    * ``astro.config.mjs`` — minimal config.
    * ``tsconfig.json`` — Astro defaults.
    * ``src/layouts/Layout.astro`` — layout component with the
      rewritten title + meta + W11.7 ``<meta>`` tags + the W11.7
      ``<!-- omnisight:clone:begin … -->`` comment baked into ``<head>``.
    * ``src/pages/index.astro`` — main page rendering hero / nav /
      sections / images-as-placeholders / footer.
    * ``src/styles/global.css`` — design-token CSS variables.
    * ``public/clone-traceability.html`` — same static traceability
      scaffold every adapter ships.
    * ``.gitignore`` — Astro defaults + ``.omnisight/``.
    * ``README.md`` — operator instructions + attribution.
    """

    framework = ASTRO_FRAMEWORK_NAME
    name = "AstroFrameworkAdapter"

    def _render_files(
        self,
        transformed: TransformedSpec,
        *,
        manifest: Optional[CloneManifest],
    ) -> Sequence[RenderedFile]:
        hero = _hero_text(transformed)
        nav_labels = _nav_labels(transformed)
        sections = _sections(transformed)
        images = _images(transformed)
        footer = _footer_text(transformed)
        sanitised_meta = _sanitised_meta(transformed)

        slug = _slug_or_default(transformed.title, default="omnisight-clone")
        package_json = (
            "{\n"
            f"  \"name\": {_json_safe(slug)},\n"
            "  \"version\": \"0.1.0\",\n"
            "  \"private\": true,\n"
            "  \"scripts\": {\n"
            "    \"dev\": \"astro dev\",\n"
            "    \"build\": \"astro build\",\n"
            "    \"preview\": \"astro preview\"\n"
            "  },\n"
            "  \"dependencies\": {\n"
            "    \"astro\": \"4.13.1\"\n"
            "  },\n"
            "  \"devDependencies\": {\n"
            "    \"typescript\": \"5.4.5\"\n"
            "  }\n"
            "}\n"
        )

        astro_config = (
            "import { defineConfig } from 'astro/config';\n"
            "\n"
            "// https://astro.build/config\n"
            "export default defineConfig({});\n"
        )

        tsconfig = (
            "{\n"
            "  \"extends\": \"astro/tsconfigs/strict\"\n"
            "}\n"
        )

        # Layout — Astro frontmatter (TypeScript) + HTML body.
        meta_lines: list[str] = [
            f'    <meta name="generator" content="{_escape_html(GENERATOR_META)}" />',
        ]
        for key, value in sanitised_meta.items():
            meta_lines.append(
                f'    <meta name="{_escape_html(key)}" content="{_escape_html(value)}" />'
            )
        if manifest is not None:
            meta_lines.extend([
                f'    <meta name="omnisight-clone-id" content="{_escape_html(manifest.clone_id)}" />',
                f'    <meta name="omnisight-manifest-hash" content="{_escape_html(manifest.manifest_hash)}" />',
                f'    <meta name="omnisight-source-url" content="{_escape_html((manifest.source or {}).get("url", ""))}" />',
            ])
        meta_block = "\n".join(meta_lines)

        # Bake the W11.7 comment into the layout's <head> using the same
        # render path as ``inject_html_traceability_comment`` so the
        # static-build output is byte-identical to the traceability
        # HTML scaffold's <head> insertion.
        comment_block = ""
        if manifest is not None:
            comment_block = "    " + render_html_traceability_comment(manifest).replace("\n", "\n    ") + "\n"

        layout_astro = (
            "---\n"
            "interface Props {\n"
            "  title: string;\n"
            "}\n"
            "\n"
            "const { title } = Astro.props;\n"
            "---\n"
            "<!doctype html>\n"
            "<html lang=\"en\">\n"
            "  <head>\n"
            "    <meta charset=\"utf-8\" />\n"
            "    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />\n"
            "    <title>{title}</title>\n"
            f"{meta_block}\n"
            f"{comment_block}"
            "    <link rel=\"stylesheet\" href=\"/src/styles/global.css\" />\n"
            "  </head>\n"
            "  <body>\n"
            "    <slot />\n"
            "  </body>\n"
            "</html>\n"
        )

        nav_html = (
            "\n".join(
                f"        <li><a href=\"#\">{_escape_html(label)}</a></li>"
                for label in nav_labels
            )
            or "        <li><a href=\"#\">Home</a></li>"
        )
        sections_html = "\n".join(
            f"      <section>\n"
            f"        <h2>{_escape_html(section['heading'])}</h2>\n"
            f"        <p>{_escape_html(section['summary'])}</p>\n"
            f"      </section>"
            for section in sections
        )
        image_html = "\n".join(
            f"        <img src={_json_safe(image['url'])} alt={_json_safe(image['alt'])} />"
            for image in images
        )

        index_astro = (
            "---\n"
            "import Layout from '../layouts/Layout.astro';\n"
            "---\n"
            f"<Layout title={_json_safe(transformed.title or 'Cloned site')}>\n"
            "  <main>\n"
            "    <nav>\n"
            "      <ul>\n"
            f"{nav_html}\n"
            "      </ul>\n"
            "    </nav>\n"
            "    <header>\n"
            f"      <h1>{_escape_html(hero['heading'])}</h1>\n"
            f"      <p>{_escape_html(hero['tagline'])}</p>\n"
            f"      <button type=\"button\">{_escape_html(hero['cta_label']) or 'Learn more'}</button>\n"
            "    </header>\n"
            f"{sections_html}\n"
            "    <div class=\"omnisight-images\">\n"
            f"{image_html}\n"
            "    </div>\n"
            f"    <footer>{_escape_html(footer)}</footer>\n"
            "  </main>\n"
            "</Layout>\n"
        )

        global_css = (
            "/* Auto-generated by OmniSight Cloner — design tokens harvested from source. */\n"
            f"{_design_tokens_css(transformed)}\n"
            "body { font-family: var(--omnisight-font-1, system-ui, sans-serif); margin: 0; }\n"
            "main { max-width: var(--omnisight-max-width, 1200px); margin: 0 auto; padding: 1rem; }\n"
            "img { max-width: 100%; height: auto; }\n"
        )

        traceability_html = _build_traceability_html_scaffold(transformed, manifest)

        gitignore = (
            "# Astro\n"
            "node_modules/\n"
            "dist/\n"
            ".astro/\n"
            "\n"
            "# OmniSight clone artefacts\n"
            ".omnisight/\n"
            "\n"
            "# Env\n"
            ".env*.local\n"
        )

        readme = self._readme(manifest)

        return [
            RenderedFile("package.json", package_json),
            RenderedFile("astro.config.mjs", astro_config),
            RenderedFile("tsconfig.json", tsconfig),
            RenderedFile("src/layouts/Layout.astro", layout_astro),
            RenderedFile("src/pages/index.astro", index_astro),
            RenderedFile("src/styles/global.css", global_css),
            RenderedFile(TRACEABILITY_HTML_RELATIVE_PATH, traceability_html),
            RenderedFile(".gitignore", gitignore),
            RenderedFile("README.md", readme),
        ]

    @staticmethod
    def _readme(manifest: Optional[CloneManifest]) -> str:
        body = (
            "# OmniSight Cloned Site (Astro 4)\n"
            "\n"
            "Generated by the OmniSight W11 cloner under the same\n"
            "5-layer defense-in-depth pipeline as the Next.js / Nuxt\n"
            "render paths. Astro is server-rendered MPA-first so the\n"
            "W11.7 traceability comment lives in the layout `<head>`\n"
            "directly — no JSX detour.\n"
            "\n"
            "## Run\n"
            "\n"
            "```bash\n"
            "npm install\n"
            "npm run dev\n"
            "```\n"
            "\n"
            "## Traceability\n"
            "\n"
            f"The static traceability scaffold lives at `{TRACEABILITY_HTML_RELATIVE_PATH}`.\n"
            "Every page rendered by `Layout.astro` ALSO carries the\n"
            "W11.7 comment in its `<head>` plus matching `<meta>` tags.\n"
            "\n"
            "## Attribution\n"
            "\n"
            "Inspired by firecrawl/open-lovable (MIT). See "
            "`LICENSES/open-lovable-mit.txt` in the OmniSight repo for the "
            "full attribution + license text.\n"
        )
        if manifest is not None:
            body += (
                "\n## Manifest\n"
                f"\n- clone_id: `{manifest.clone_id}`\n"
                f"- manifest_hash: `{manifest.manifest_hash}`\n"
            )
        return body


# ── Public entry points ────────────────────────────────────────────────


_ADAPTER_REGISTRY: Mapping[str, type[_AdapterBase]] = {
    NEXT_FRAMEWORK_NAME: NextFrameworkAdapter,
    NUXT_FRAMEWORK_NAME: NuxtFrameworkAdapter,
    ASTRO_FRAMEWORK_NAME: AstroFrameworkAdapter,
}


def make_framework_adapter(framework: str) -> FrameworkAdapter:
    """Construct the adapter for ``framework``.

    Args:
        framework: One of :data:`SUPPORTED_FRAMEWORKS`.

    Returns:
        A new :class:`FrameworkAdapter` instance. Adapters are
        stateless so callers can construct one per call without
        worrying about cache invalidation.

    Raises:
        UnknownFrameworkError: ``framework`` is outside
            :data:`SUPPORTED_FRAMEWORKS`.
    """
    if not isinstance(framework, str) or not framework:
        raise UnknownFrameworkError(
            f"framework must be a non-empty string, got {framework!r}"
        )
    key = framework.strip().lower()
    if key not in _ADAPTER_REGISTRY:
        raise UnknownFrameworkError(
            f"unknown framework {framework!r}; expected one of "
            f"{sorted(SUPPORTED_FRAMEWORKS)}"
        )
    return _ADAPTER_REGISTRY[key]()


def render_clone_project(
    transformed: TransformedSpec,
    framework: str,
    *,
    manifest: Optional[CloneManifest] = None,
    adapter: Optional[FrameworkAdapter] = None,
) -> RenderedProject:
    """Render the cloned project for ``transformed`` in ``framework``.

    Wraps :func:`make_framework_adapter` + :meth:`FrameworkAdapter.render`
    so callers can express "I want a Next.js project for this spec" in
    one line. ``adapter`` overrides the registry lookup — useful for
    tests injecting a fake adapter or for future Vue / Svelte rows that
    plug their own adapter in without amending :data:`SUPPORTED_FRAMEWORKS`.

    Args:
        transformed: The L3-transformed spec produced by
            :func:`backend.web.output_transformer.transform_clone_spec`.
        framework: One of :data:`SUPPORTED_FRAMEWORKS` (ignored when
            ``adapter`` is provided).
        manifest: The W11.7 :class:`CloneManifest` to bake into the
            rendered project's traceability surfaces. ``None`` is
            allowed for dev / smoke flows but production callers should
            always supply one — the L4 row's TODO entry is explicit
            that the manifest must be pinned before the framework
            adapter runs.
        adapter: Override the registry lookup. Must satisfy
            :class:`FrameworkAdapter`.

    Returns:
        A frozen :class:`RenderedProject` ready for
        :func:`write_rendered_project`.

    Raises:
        UnknownFrameworkError: bad ``framework`` arg.
        FrameworkAdapterError: ``transformed`` / ``manifest`` shape is
            wrong (e.g. someone passed a plain ``CloneSpec`` instead of
            a ``TransformedSpec``).
        BytesLeakError: an upstream regression smuggled bytes into the
            transformed spec (re-raised from
            :func:`assert_no_copied_bytes`).
    """
    if adapter is None:
        adapter = make_framework_adapter(framework)
    elif not isinstance(adapter, FrameworkAdapter):
        raise FrameworkAdapterError(
            f"adapter must satisfy FrameworkAdapter protocol, "
            f"got {type(adapter).__name__}"
        )
    return adapter.render(transformed, manifest=manifest)


def write_rendered_project(
    project: RenderedProject,
    *,
    project_root: Path | str,
    overwrite: bool = True,
) -> Tuple[Path, ...]:
    """Persist ``project`` under ``project_root``.

    Creates parent directories on demand. Validates every file's
    relative path against :data:`_FILENAME_RE` again before any write
    so a tampered :class:`RenderedProject` (e.g. one mutated post-
    construction in spite of the frozen dataclass) cannot escape the
    project root. Returns the absolute paths of every written file in
    the same order as :attr:`RenderedProject.files`.

    Args:
        project: The :class:`RenderedProject` to persist.
        project_root: The directory the project should land in. Created
            if missing.
        overwrite: When ``True`` (default), existing files are
            overwritten. When ``False``, raises
            :class:`RenderedProjectWriteError` if any target file
            already exists — used by callers that want a strict "no
            stomping" guarantee.

    Raises:
        RenderedProjectWriteError: any file write failed, any path
            validation tripped, or ``overwrite=False`` and a target
            file already exists.
    """
    if not isinstance(project, RenderedProject):
        raise RenderedProjectWriteError(
            f"project must be RenderedProject, got {type(project).__name__}"
        )
    root = Path(project_root).resolve()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RenderedProjectWriteError(
            f"could not create project root {root}: {exc}"
        ) from exc

    written: list[Path] = []
    for rendered_file in project.files:
        _validate_relative_path(rendered_file.relative_path)
        target = root.joinpath(*rendered_file.relative_path.split("/")).resolve()
        # Belt-and-braces: even after path validation, ensure the
        # resolved target really lives under the project root (defends
        # against e.g. a future symlink injection in ``project_root``).
        try:
            target.relative_to(root)
        except ValueError:
            raise RenderedProjectWriteError(
                f"resolved file path {target} escapes project root {root}"
            )
        if not overwrite and target.exists():
            raise RenderedProjectWriteError(
                f"refusing to overwrite existing file {target} "
                f"(pass overwrite=True to allow)"
            )
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(rendered_file.content, encoding="utf-8")
        except OSError as exc:
            raise RenderedProjectWriteError(
                f"failed to write {rendered_file.relative_path}: {exc}"
            ) from exc
        written.append(target)
    return tuple(written)


def project_to_audit_payload(project: RenderedProject) -> Mapping[str, Any]:
    """Project a :class:`RenderedProject` onto the audit-row ``after``
    payload shape the W11.12 row will consume.

    Deliberately omits the file *contents* — the audit row stays small
    by recording only enumerated relative paths plus framework /
    manifest identifiers. Operators that need to inspect a specific
    file's bytes go to the deployed project on disk via the manifest's
    ``source.url``.
    """
    if not isinstance(project, RenderedProject):
        raise FrameworkAdapterError(
            f"project must be RenderedProject, got {type(project).__name__}"
        )
    return {
        "framework": project.framework,
        "adapter": project.adapter_name,
        "files": tuple(rf.relative_path for rf in project.files),
        "traceability_html_path": project.traceability_html_relative_path,
        "manifest_clone_id": project.manifest_clone_id,
        "manifest_hash": project.manifest_hash,
    }


__all__ = [
    "ASTRO_FRAMEWORK_NAME",
    "AstroFrameworkAdapter",
    "FrameworkAdapter",
    "FrameworkAdapterError",
    "GENERATOR_META",
    "MAX_RENDERED_IMAGES",
    "MAX_RENDERED_NAV_ITEMS",
    "MAX_RENDERED_SECTIONS",
    "NEXT_FRAMEWORK_NAME",
    "NUXT_FRAMEWORK_NAME",
    "NextFrameworkAdapter",
    "NuxtFrameworkAdapter",
    "RenderedFile",
    "RenderedProject",
    "RenderedProjectWriteError",
    "SUPPORTED_FRAMEWORKS",
    "TRACEABILITY_HTML_FILENAME",
    "TRACEABILITY_HTML_RELATIVE_PATH",
    "UnknownFrameworkError",
    "make_framework_adapter",
    "project_to_audit_payload",
    "render_clone_project",
    "write_rendered_project",
]
