"""W15.5 #XXX — Vite-config scaffold injection for the W15 self-healing loop.

W15.1 ships ``packages/omnisight-vite-plugin`` (the Vite plugin that
POSTs every compile-time and runtime build error to the OmniSight
backend).  W15.2 folds each error into ``state.error_history``.
W15.3 quotes the most recent error back to the agent on every LLM
turn via the Chinese-localised system-prompt banner.  W15.4 escalates
to the operator when the same error pattern repeats 3× in a row.

W15.5 (this row) closes the loop on the **scaffold side**: every
project the W6 SKILL-NEXTJS / W7 SKILL-NUXT / W8 SKILL-ASTRO
scaffolders generate now ships ``@omnisight/vite-plugin`` wired into
its Vite-equivalent config out-of-the-box.  When that scaffolded
project runs inside the W14.1 omnisight-web-preview sidecar — where
the env vars :data:`OMNISIGHT_WORKSPACE_ID_ENV` and
:data:`OMNISIGHT_BACKEND_URL_ENV` are populated — the plugin is
active and round-trips errors into the W15.1 endpoint.  When that
project runs *outside* the sidecar (e.g. an operator running ``pnpm
dev`` on their laptop), the bootstrap returns ``null`` and the
plugin is a no-op so the build is byte-identical to the legacy
behaviour.

Where it slots into the W15 pipeline
------------------------------------

::

    W14.1 sidecar boots scaffolded project (W6/W7/W8)
                       ↓
    process.env.OMNISIGHT_WORKSPACE_ID + OMNISIGHT_BACKEND_URL set
                       ↓
    scripts/omnisight-vite-plugin.mjs (rendered by W15.5 — this row)
                       ↓
    @omnisight/vite-plugin attached to vite/astro/nuxt config
                       ↓
    POST /web-sandbox/preview/{ws}/error                       ← W15.1
                       ↓
    ViteErrorBuffer → vite_error_relay → state.error_history    ← W15.2
                       ↓
    System-prompt banner / 3-strike escalation                  ← W15.3 / W15.4
                       ↓
    W15.6 syntax / undefined / import-typo self-fix tests       ← W15.6

Row boundary
------------

W15.5 owns:

  * The frozen npm package name + semver pin
    (:data:`OMNISIGHT_VITE_PLUGIN_PACKAGE`,
    :data:`OMNISIGHT_VITE_PLUGIN_PACKAGE_VERSION`) the scaffolders
    write into the rendered ``package.json``.
  * The frozen import name (:data:`OMNISIGHT_VITE_PLUGIN_IMPORT_NAME`)
    the rendered config files reference.
  * The frozen env-var names
    (:data:`OMNISIGHT_WORKSPACE_ID_ENV`,
    :data:`OMNISIGHT_BACKEND_URL_ENV`,
    :data:`OMNISIGHT_BACKEND_TOKEN_ENV`) the bootstrap module reads.
  * The frozen relative path
    (:data:`OMNISIGHT_VITE_PLUGIN_BOOTSTRAP_RELATIVE_PATH`) where
    each scaffolder writes the bootstrap module into the rendered
    project tree.
  * The bootstrap module template
    (:data:`OMNISIGHT_VITE_PLUGIN_BOOTSTRAP_TEMPLATE`) and the
    renderer :func:`render_omnisight_plugin_bootstrap_module`.
  * The package.json devDependency entry
    (:func:`omnisight_plugin_package_json_entry`) + the typed error
    :class:`ViteConfigInjectionError` callers raise on contract
    violations.

W15.5 explicitly does NOT own:

  * The plugin implementation itself — that lives in
    ``packages/omnisight-vite-plugin/index.js`` (W15.1).
  * The wire shape / endpoint contract — frozen by W15.1 and pinned
    in :data:`backend.web_sandbox_vite_errors.WEB_SANDBOX_VITE_ERROR_SCHEMA_VERSION`.
  * The history-entry projection — frozen in W15.2.
  * The system-prompt banner — frozen in W15.3.
  * The 3-strike escalation predicate — frozen in W15.4.
  * The Rolldown / Webpack flavoured sibling plugins (the W15.1 row
    spec calls them out as "another write" — when they land, this
    module gains parallel constants for those package names but the
    W6 vitest path still uses the Vite plugin since vitest *is* Vite).

Module-global state audit (SOP §1)
----------------------------------

Zero mutable module-level state — only frozen string constants
(:data:`OMNISIGHT_VITE_PLUGIN_PACKAGE`,
:data:`OMNISIGHT_VITE_PLUGIN_IMPORT_NAME`,
:data:`OMNISIGHT_VITE_PLUGIN_BOOTSTRAP_TEMPLATE`, …), int caps
(:data:`MAX_OMNISIGHT_BOOTSTRAP_MODULE_BYTES`), a frozen typed
error class, and pure stdlib-only helpers.

**Answer #1** of SOP §1 — every uvicorn worker reads the same
constants from the same git checkout; the bootstrap module is
rendered at scaffold-time (cold-path, idempotent) and persisted to
the operator's project tree.  No singleton, no in-memory cache, no
cross-worker coordination required.

Read-after-write timing audit (SOP §2)
--------------------------------------

N/A — pure projection from compile-time constants to text
artefacts.  No DB pool, no asyncio.gather race, no compat→pool
conversion.  The bootstrap file is written via the same
:func:`pathlib.Path.write_text` path every scaffolder uses for the
rest of its tree.

Compat fingerprint grep (SOP §3)
--------------------------------

Pure stdlib only, verified clean::

    $ grep -nE "_conn\\(\\)|await conn\\.commit\\(\\)|datetime\\('now'\\)|VALUES.*\\?[,)]" \\
        backend/web/vite_config_injection.py
    (empty)

Production Readiness Gate §158
------------------------------

(a) **No new pip dep** — only stdlib (``json`` / ``dataclasses`` /
    ``typing``).  The npm-side ``@omnisight/vite-plugin`` is a
    workspace-local package (``packages/omnisight-vite-plugin``)
    that ships no transitive deps; ``peerDependencies`` pins
    ``vite >= 4 < 7`` already shipped in W15.1's ``package.json``.
(b) **No alembic migration** — purely scaffold-time text rendering.
(c) **No new ``OMNISIGHT_*`` backend env knob** — the env-var names
    consumed by the **rendered project** (``OMNISIGHT_WORKSPACE_ID``
    / ``OMNISIGHT_BACKEND_URL`` / ``OMNISIGHT_BACKEND_TOKEN``) are
    set by the W14.1 sidecar at container-start and read inside the
    Node/Vite process — not by the OmniSight backend.  No ``.env``
    edit on the backend host required.
(d) **No Dockerfile rebuild required** — the bootstrap module is
    written to the rendered project's tree at scaffold-time;
    re-running the scaffolder on an existing project picks up new
    versions through the standard overwrite path.
(e) **Drift guards locked at literals** — see the §A test class in
    ``backend/tests/test_w15_5_vite_config_injection.py``.  Pinned
    constants:
    :data:`OMNISIGHT_VITE_PLUGIN_PACKAGE`,
    :data:`OMNISIGHT_VITE_PLUGIN_PACKAGE_VERSION`,
    :data:`OMNISIGHT_VITE_PLUGIN_IMPORT_NAME`,
    :data:`OMNISIGHT_WORKSPACE_ID_ENV`,
    :data:`OMNISIGHT_BACKEND_URL_ENV`,
    :data:`OMNISIGHT_VITE_PLUGIN_BOOTSTRAP_RELATIVE_PATH`,
    :data:`MAX_OMNISIGHT_BOOTSTRAP_MODULE_BYTES`.

Inspired by firecrawl/open-lovable's ``monitor-vite-logs`` /
``report-vite-error`` pattern (MIT — see W11.13 attribution).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Mapping


__all__ = [
    "MAX_OMNISIGHT_BOOTSTRAP_MODULE_BYTES",
    "OMNISIGHT_BACKEND_TOKEN_ENV",
    "OMNISIGHT_BACKEND_URL_ENV",
    "OMNISIGHT_VITE_PLUGIN_BOOTSTRAP_RELATIVE_PATH",
    "OMNISIGHT_VITE_PLUGIN_BOOTSTRAP_TEMPLATE",
    "OMNISIGHT_VITE_PLUGIN_IMPORT_NAME",
    "OMNISIGHT_VITE_PLUGIN_PACKAGE",
    "OMNISIGHT_VITE_PLUGIN_PACKAGE_VERSION",
    "OMNISIGHT_WORKSPACE_ID_ENV",
    "ViteConfigInjectionError",
    "ViteConfigInjectionResult",
    "omnisight_plugin_package_json_entry",
    "render_omnisight_plugin_bootstrap_module",
]


#: Frozen npm package name — matches ``packages/omnisight-vite-plugin/
#: package.json::name``.  Bumping this is a backwards-incompatible
#: change that requires lock-step edits to every rendered scaffold's
#: ``package.json`` import path.
OMNISIGHT_VITE_PLUGIN_PACKAGE: str = "@omnisight/vite-plugin"

#: Frozen semver range matching ``packages/omnisight-vite-plugin/
#: package.json::version``.  ``^0.1.0`` accepts patch + minor bumps
#: while staying inside the W15.1 wire-shape contract (any major
#: bump breaks the wire shape and must update both this pin and
#: :data:`backend.web_sandbox_vite_errors.WEB_SANDBOX_VITE_ERROR_SCHEMA_VERSION`).
OMNISIGHT_VITE_PLUGIN_PACKAGE_VERSION: str = "^0.1.0"

#: Frozen named-export the rendered configs import.  Must match the
#: ``omnisightVitePlugin`` named export from
#: ``packages/omnisight-vite-plugin/index.js``.
OMNISIGHT_VITE_PLUGIN_IMPORT_NAME: str = "omnisightVitePlugin"

#: Env var the bootstrap reads for the operator workspace id.  Matches
#: the W15.1 plugin docstring's "the W14.1 sidecar's W15.5 vite.config
#: scaffold will read this from ``process.env.OMNISIGHT_WORKSPACE_ID``"
#: contract — bumping this name requires a paired edit in
#: ``packages/omnisight-vite-plugin/index.js``.
OMNISIGHT_WORKSPACE_ID_ENV: str = "OMNISIGHT_WORKSPACE_ID"

#: Env var the bootstrap reads for the OmniSight backend base URL.
OMNISIGHT_BACKEND_URL_ENV: str = "OMNISIGHT_BACKEND_URL"

#: Optional bearer-token env var the bootstrap forwards as
#: ``Authorization: Bearer <t>`` when the backend rejects anonymous
#: error reports.  Empty / unset → no header sent (the default for
#: the W14.1 in-network deployment posture).
OMNISIGHT_BACKEND_TOKEN_ENV: str = "OMNISIGHT_BACKEND_TOKEN"

#: Relative path inside the rendered project tree where the bootstrap
#: module lives.  ``scripts/`` is the conventional location for
#: build-helper modules across all three frameworks (Next.js / Nuxt /
#: Astro all treat ``scripts/`` as outside the bundler's app root).
#: ``.mjs`` rather than ``.ts`` so the file is consumable by all three
#: frameworks without a TypeScript dep — the rendered configs
#: themselves carry typed surface, the bootstrap is plain ESM.
OMNISIGHT_VITE_PLUGIN_BOOTSTRAP_RELATIVE_PATH: str = "scripts/omnisight-vite-plugin.mjs"

#: Hard cap on the bootstrap module byte size.  Pinned so a future
#: edit that accidentally explodes the template (regex bug, copy-paste
#: of the runtime overlay, etc.) trips the contract test before it
#: ships into operator project trees.  4 KiB is generous for the
#: ~1 KiB template the row writes today.
MAX_OMNISIGHT_BOOTSTRAP_MODULE_BYTES: int = 4 * 1024


#: The frozen bootstrap module template.  Written by each scaffolder
#: into ``<project>/scripts/omnisight-vite-plugin.mjs`` so the
#: rendered ``vite.config`` (or ``vitest.config.ts`` / ``nuxt.config.ts``
#: / ``astro.config.mjs``) imports it via a stable relative path.
#:
#: The template is **byte-stable** — the W15.5 contract test pins the
#: exact bytes so a refactor that changes whitespace, comment
#: phrasing, or the env-var lookup order trips immediately.  When a
#: change is intentional, bump both the pin and the row's HANDOFF
#: entry so the W15.6 self-fix tests can be re-validated against the
#: new shape.
#:
#: Why ``return null`` instead of throwing on missing env vars: the
#: scaffolded project is expected to run in two distinct contexts:
#:   1. Inside the W14.1 sidecar — env vars set, plugin active.
#:   2. On an operator's laptop / CI runner — env vars unset, plugin
#:      a no-op so the build is byte-identical to the legacy
#:      behaviour.
#: Throwing in #2 would force every operator to ``unset`` the env
#: vars or set them to placeholders — a footgun.  Returning null lets
#: each rendered config use ``[makePlugin()].filter(Boolean)`` and
#: keep the surface area minimal.
OMNISIGHT_VITE_PLUGIN_BOOTSTRAP_TEMPLATE: str = """\
// Generated by W15.5 (omnisight-vite-plugin scaffold injection).
//
// W15.1 ships the @omnisight/vite-plugin Vite plugin.  W15.5 (this
// file) wires it into the rendered project so any compile-time or
// runtime build error round-trips to the OmniSight backend's
// LangGraph self-healing loop (W15.2 -> W15.3 -> W15.4).
//
// Behaviour contract:
//   * In the W14.1 omnisight-web-preview sidecar (env vars set):
//     plugin is active and POSTs errors to
//     /web-sandbox/preview/{workspace_id}/error.
//   * Outside the sidecar (env vars unset): returns null so the
//     consuming config can `.filter(Boolean)` it out.  Build stays
//     byte-identical to a project without the plugin.
//
// DO NOT EDIT BY HAND.  Re-running the W6/W7/W8 scaffolder
// regenerates this file from
// backend/web/vite_config_injection.py::OMNISIGHT_VITE_PLUGIN_BOOTSTRAP_TEMPLATE.

import { omnisightVitePlugin } from "@omnisight/vite-plugin"

export function makeOmnisightVitePlugin() {
  const workspaceId = process.env.OMNISIGHT_WORKSPACE_ID
  const backendUrl = process.env.OMNISIGHT_BACKEND_URL
  if (!workspaceId || !backendUrl) {
    return null
  }
  const authToken = process.env.OMNISIGHT_BACKEND_TOKEN || undefined
  return omnisightVitePlugin({
    workspaceId,
    backendUrl,
    authToken,
  })
}

export default makeOmnisightVitePlugin
"""


class ViteConfigInjectionError(Exception):
    """Raised on a W15.5 contract violation.

    Today the only call site is
    :func:`render_omnisight_plugin_bootstrap_module` which raises
    this when the frozen template exceeds
    :data:`MAX_OMNISIGHT_BOOTSTRAP_MODULE_BYTES`.  The exception is
    deliberately a single class — callers should not branch on
    sub-types; the contract test asserts on the message substring
    instead.
    """


@dataclass(frozen=True)
class ViteConfigInjectionResult:
    """Frozen record of a single bootstrap-module write.

    Returned by the per-scaffolder helper that orchestrates the W15.5
    injection so the test suite can assert what landed without
    re-reading the file from disk.
    """

    bootstrap_relative_path: str
    bootstrap_bytes: int
    package_name: str
    package_version: str


def render_omnisight_plugin_bootstrap_module() -> str:
    """Return the frozen bootstrap module text.

    Raises:
        ViteConfigInjectionError: The template exceeds
            :data:`MAX_OMNISIGHT_BOOTSTRAP_MODULE_BYTES`.  This is a
            self-check against accidental template explosion (regex
            bugs, copy-paste of the W15.1 runtime overlay) — the
            template is a compile-time constant so the check is a
            drift guard, not a runtime guard.
    """
    template = OMNISIGHT_VITE_PLUGIN_BOOTSTRAP_TEMPLATE
    encoded = template.encode("utf-8")
    if len(encoded) > MAX_OMNISIGHT_BOOTSTRAP_MODULE_BYTES:
        raise ViteConfigInjectionError(
            f"omnisight-vite-plugin bootstrap module exceeds "
            f"{MAX_OMNISIGHT_BOOTSTRAP_MODULE_BYTES} byte cap "
            f"({len(encoded)} bytes)"
        )
    return template


def omnisight_plugin_package_json_entry() -> Mapping[str, str]:
    """Return the package.json devDependency entry the scaffolders
    splice into the rendered ``package.json``.

    Returned as an immutable single-pair mapping rather than a
    constant string so callers can ``dict(**existing,
    **omnisight_plugin_package_json_entry())`` without the JSON
    comma-separator surgery the .j2 templates would otherwise need.
    The returned object is intentionally a fresh dict on every call
    (defence in depth — callers historically mutate dicts they get
    from helpers; immutability would be a contract change for them).
    """
    return {OMNISIGHT_VITE_PLUGIN_PACKAGE: OMNISIGHT_VITE_PLUGIN_PACKAGE_VERSION}


# ── Internal serialisation helper (used by the contract test §B) ────


def _bootstrap_module_signature() -> str:
    """Return a compact JSON-encoded signature of the frozen
    constants for the contract test's drift-guard pin.

    Not part of the public surface — intentionally ``_``-prefixed.
    The test uses it to assert that any change to the constants
    triggers a paired pin bump.
    """
    return json.dumps(
        {
            "package": OMNISIGHT_VITE_PLUGIN_PACKAGE,
            "package_version": OMNISIGHT_VITE_PLUGIN_PACKAGE_VERSION,
            "import_name": OMNISIGHT_VITE_PLUGIN_IMPORT_NAME,
            "workspace_id_env": OMNISIGHT_WORKSPACE_ID_ENV,
            "backend_url_env": OMNISIGHT_BACKEND_URL_ENV,
            "backend_token_env": OMNISIGHT_BACKEND_TOKEN_ENV,
            "bootstrap_path": OMNISIGHT_VITE_PLUGIN_BOOTSTRAP_RELATIVE_PATH,
            "max_bytes": MAX_OMNISIGHT_BOOTSTRAP_MODULE_BYTES,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
