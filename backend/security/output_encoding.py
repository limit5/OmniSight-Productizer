"""SC.7.2 — OWASP output-encoding helpers for generated apps.

Small context-specific encoders intended for generated FastAPI /
service templates.  The helpers are deliberately pure and framework
agnostic: callers choose the correct encoder for the sink they are
about to write to.

Security boundary:

  * This module covers output encoding only.
  * Input validation, SQL parameterisation, CSRF templates, and path /
    SSRF protection are separate SC.7 rows.
  * These helpers do not mark output as trusted HTML.  Template engines
    should keep autoescape enabled and use these functions only at
    explicit string-construction boundaries.

All module-level state is immutable constants.  Cross-worker safety
follows SOP Step 1 answer #1: each uvicorn worker derives identical
encoders from the same source code; there is no shared cache,
singleton, or runtime mutation.
"""

from __future__ import annotations

import html
import json
from urllib.parse import quote


URL_COMPONENT_SAFE_CHARS = ""


def _text(value: object) -> str:
    return "" if value is None else str(value)


def encode_html_text(value: object) -> str:
    """Encode untrusted text for an HTML text-node context."""

    return html.escape(_text(value), quote=False)


def encode_html_attribute(value: object) -> str:
    """Encode untrusted text for a quoted HTML attribute value."""

    return html.escape(_text(value), quote=True)


def encode_javascript_string(value: object) -> str:
    """Encode untrusted text as a JavaScript string literal.

    The returned value includes the surrounding quotes.  ``<``, ``>``,
    ``&`` and the two JavaScript line separators are escaped after JSON
    encoding so an inline ``<script>`` block cannot be terminated by a
    user-controlled ``</script>`` substring.
    """

    return _escape_script_json(json.dumps(_text(value), ensure_ascii=False))


def encode_json_script(value: object) -> str:
    """Encode JSON data for an inline ``<script type="application/json">`` body."""

    return _escape_script_json(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def encode_url_component(value: object) -> str:
    """Percent-encode untrusted text for one URL path or query component."""

    return quote(_text(value), safe=URL_COMPONENT_SAFE_CHARS)


def _escape_script_json(encoded: str) -> str:
    return (
        encoded.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


__all__ = [
    "URL_COMPONENT_SAFE_CHARS",
    "encode_html_attribute",
    "encode_html_text",
    "encode_javascript_string",
    "encode_json_script",
    "encode_url_component",
]
