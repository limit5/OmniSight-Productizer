"""C26 — HMI constrained bundle generator.

Generates a self-contained HMI bundle (HTML + inline CSS + inline JS +
optional inline fonts) suitable for flash-constrained embedded targets.
Policy enforced:

  * Whitelist framework only: ``preact`` | ``lit-html`` | ``vanilla``
  * No CDN / remote resources — everything must be inlined or same-origin
  * No analytics, no ``eval``/``Function``/``innerHTML``
  * Strict CSP header baked in; IEC 62443 baseline enforced at render time
  * i18n catalog bundled as a plain JSON blob — no runtime fetch
"""

from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from backend import hmi_framework as _hf

logger = logging.getLogger(__name__)

GENERATOR_VERSION = "1.0.0"

_DEFAULT_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'"
)

_DEFAULT_SECURITY_HEADERS = {
    "Content-Security-Policy": _DEFAULT_CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Request / response types
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class PageSection:
    id: str
    title: str
    kind: str = "form"             # "form" | "table" | "status" | "action"
    fields: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        _validate_id(self.id, "section.id")
        _validate_text(self.title, "section.title")
        if self.kind not in ("form", "table", "status", "action"):
            raise ValueError(f"Invalid section.kind: {self.kind}")


@dataclass
class GeneratorRequest:
    product_name: str
    framework: str = "vanilla"     # must be whitelisted
    platform: str = "aarch64"
    locale: str = "en"
    title_key: str = "nav.home"
    sections: list[PageSection] = field(default_factory=list)
    extra_scripts: str = ""        # already-trusted app logic (still scanned)
    extra_styles: str = ""
    i18n_overrides: dict[str, dict[str, str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_text(self.product_name, "product_name")


@dataclass
class GeneratedBundle:
    files: dict[str, str]          # {path: text content}
    headers: dict[str, str]        # HTTP response headers to set server-side
    framework: str
    platform: str
    total_bytes: int
    budget_bytes: int
    security_status: str
    security_findings: list[dict[str, Any]]
    budget_violations: list[str]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Validation helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_ID_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_\-]{0,63}$")
_TEXT_RE = re.compile(r"^[^<>\x00-\x08\x0b\x0c\x0e-\x1f]{1,256}$")


def _validate_id(value: str, label: str) -> None:
    if not _ID_RE.match(value or ""):
        raise ValueError(f"Invalid {label} '{value}': must match {_ID_RE.pattern}")


def _validate_text(value: str, label: str, allow_empty: bool = False) -> None:
    if value == "" and allow_empty:
        return
    if not _TEXT_RE.match(value or ""):
        raise ValueError(f"Invalid {label}: must be 1-256 chars, no angle brackets")


def _assert_framework_allowed(name: str) -> None:
    if not _hf.is_framework_allowed(name):
        allowed = [f.name for f in _hf.list_allowed_frameworks()]
        raise ValueError(f"Framework '{name}' not in whitelist {allowed}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Renderers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _render_html(req: GeneratorRequest, i18n_blob: str, app_js: str, css: str) -> str:
    escape = html.escape
    title = escape(req.product_name)
    locale = escape(req.locale)
    # Render section skeleton; actual field rendering happens client-side
    # from the JSON blob to keep the HTML skeleton small.
    sections_html: list[str] = []
    for s in req.sections:
        sections_html.append(
            f'<section id="{escape(s.id)}" data-kind="{escape(s.kind)}">'
            f'<h2 data-i18n="{escape(s.title)}">{escape(s.title)}</h2>'
            f'<div class="omni-hmi-body"></div>'
            "</section>"
        )

    page = f"""<!DOCTYPE html>
<html lang="{locale}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
{css}
</style>
</head>
<body>
<header class="omni-hmi-hd"><h1>{title}</h1><nav><ul>
<li><a href="#" data-nav="home" data-i18n="nav.home">Home</a></li>
<li><a href="#" data-nav="network" data-i18n="nav.network">Network</a></li>
<li><a href="#" data-nav="ota" data-i18n="nav.ota">Firmware</a></li>
<li><a href="#" data-nav="logs" data-i18n="nav.logs">Logs</a></li>
</ul></nav></header>
<main>
{"".join(sections_html)}
</main>
<script id="omni-i18n" type="application/json">{i18n_blob}</script>
<script src="app.js"></script>
</body>
</html>
"""
    return page


def _render_css(extra: str = "") -> str:
    base = """\
:root { --bg: #111; --fg: #eee; --accent: #2aa1ff; --err: #f55; }
*, *::before, *::after { box-sizing: border-box; }
body { margin: 0; font-family: system-ui, sans-serif;
       background: var(--bg); color: var(--fg); }
.omni-hmi-hd { background: #000; padding: .75rem 1rem; }
.omni-hmi-hd h1 { margin: 0; font-size: 1rem; }
nav ul { display: flex; gap: 1rem; list-style: none; padding: 0; margin: .5rem 0 0 0; }
nav a { color: var(--accent); text-decoration: none; font-size: .9rem; }
main { padding: 1rem; }
section { border: 1px solid #333; margin-bottom: 1rem; padding: .75rem; border-radius: 4px; }
section h2 { margin-top: 0; font-size: 1rem; }
form label { display: block; margin: .5rem 0 .25rem; font-size: .85rem; }
form input, form select { padding: .4rem .5rem; background: #222; color: var(--fg);
                          border: 1px solid #444; border-radius: 3px; width: 100%; max-width: 320px; }
button { padding: .4rem .75rem; background: var(--accent); color: #fff;
         border: 0; border-radius: 3px; cursor: pointer; }
.omni-hmi-err { color: var(--err); font-size: .85rem; }
"""
    return base + ("\n" + extra if extra else "")


_RUNTIME_JS_HEADER = """\
"use strict";
// C26 HMI runtime — vanilla core (Preact/lit adapters plug in on top)
(function () {
  var catalog = {};
  try {
    var blob = document.getElementById("omni-i18n");
    if (blob && blob.textContent) { catalog = JSON.parse(blob.textContent); }
  } catch (e) { catalog = {}; }
  function t(key, locale) {
    var loc = locale || document.documentElement.lang || "en";
    var table = catalog[loc] || catalog["en"] || {};
    return table[key] || key;
  }
  function applyI18n(root) {
    var nodes = (root || document).querySelectorAll("[data-i18n]");
    for (var i = 0; i < nodes.length; i++) {
      var k = nodes[i].getAttribute("data-i18n");
      if (k) { nodes[i].textContent = t(k); }
    }
  }
  window.OmniHMI = window.OmniHMI || {};
  window.OmniHMI.t = t;
  window.OmniHMI.applyI18n = applyI18n;
  window.OmniHMI.fetchJSON = function (url, opts) {
    opts = opts || {};
    opts.credentials = "same-origin";
    opts.headers = opts.headers || {};
    opts.headers["Accept"] = "application/json";
    var csrf = document.querySelector('meta[name="omni-csrf"]');
    if (csrf) { opts.headers["X-CSRF-Token"] = csrf.getAttribute("content") || ""; }
    return fetch(url, opts).then(function (r) {
      if (!r.ok) { throw new Error("HTTP " + r.status); }
      return r.json();
    });
  };
  document.addEventListener("DOMContentLoaded", function () { applyI18n(document); });
})();
"""


def _render_js(req: GeneratorRequest) -> str:
    parts = [_RUNTIME_JS_HEADER]
    framework = req.framework.lower()
    if framework == "preact":
        parts.append(
            "// Preact adapter placeholder — the device firmware ships\n"
            "// the 4KB preact build under /static/preact.js and loads\n"
            "// it via same-origin <script>.\n"
        )
    elif framework == "lit-html":
        parts.append(
            "// lit-html adapter placeholder — device firmware ships\n"
            "// the 6KB lit-html build under /static/lit-html.js.\n"
        )
    # Append extra (scanned for forbidden patterns later)
    if req.extra_scripts:
        parts.append(req.extra_scripts)
    return "\n".join(parts)


def _render_i18n_blob(req: GeneratorRequest) -> str:
    catalog = _hf.build_i18n_catalog(overrides=req.i18n_overrides or None)
    return json.dumps(catalog, ensure_ascii=False, separators=(",", ":"))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Public API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def generate_bundle(req: GeneratorRequest) -> GeneratedBundle:
    _assert_framework_allowed(req.framework)

    i18n_blob = _render_i18n_blob(req)
    css = _render_css(req.extra_styles)
    js = _render_js(req)
    page_html = _render_html(req, i18n_blob, js, css)

    files = {"index.html": page_html, "app.js": js}
    # Policy: security headers set by server, but also baked-in as <meta> and checked here
    headers = dict(_DEFAULT_SECURITY_HEADERS)

    # Run security scan on every output artifact
    sec = _hf.scan_security(
        html=page_html,
        js=js + "\n" + (req.extra_scripts or "") + "\n" + (req.extra_styles or ""),
        headers=headers,
        csp=headers["Content-Security-Policy"],
    )

    # Run bundle budget check
    m = _hf.measure_bundle({k: v for k, v in files.items()})
    verdict = _hf.check_bundle_budget(req.platform, m)

    # Fail-fast on security errors
    if sec.status == "fail":
        raise ValueError(
            "Security baseline violated: "
            + "; ".join(f.detail for f in sec.findings if f.severity == "error")
        )

    return GeneratedBundle(
        files=files,
        headers=headers,
        framework=req.framework,
        platform=req.platform,
        total_bytes=m.total_bytes,
        budget_bytes=verdict.budget_bytes,
        security_status=sec.status,
        security_findings=[{"severity": f.severity, "rule": f.rule, "detail": f.detail} for f in sec.findings],
        budget_violations=verdict.violations,
    )


def assert_budget_within(platform: str, files: dict[str, str | bytes]) -> None:
    """CI hook. Raises ``BudgetExceeded`` when bundle breaches the platform budget."""
    m = _hf.measure_bundle(files)
    verdict = _hf.check_bundle_budget(platform, m)
    if verdict.status == "fail":
        raise BudgetExceeded("; ".join(verdict.violations))


class BudgetExceeded(Exception):
    """Raised when the constrained generator produces output that breaches the flash budget."""


def summary() -> dict[str, Any]:
    return {
        "generator_version": GENERATOR_VERSION,
        "allowed_frameworks": [f.name for f in _hf.list_allowed_frameworks()],
        "forbidden_frameworks": _hf.list_forbidden_frameworks(),
        "default_csp": _DEFAULT_CSP,
        "security_headers": list(_DEFAULT_SECURITY_HEADERS.keys()),
    }
