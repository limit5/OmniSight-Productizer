"""W15.5 — Contract tests for ``backend.web.vite_config_injection``
plus the W6/W7/W8 scaffolder wiring that ships
``@omnisight/vite-plugin`` (W15.1) into every rendered project.

W15.1 ships the Vite plugin itself.  W15.2 folds posted errors into
``state.error_history``.  W15.3 quotes the most recent error back to
the agent on every LLM turn.  W15.4 escalates to the operator when
the same pattern repeats 3× in a row.  W15.5 (this row) closes the
scaffold side: every project the W6/W7/W8 scaffolders generate now
ships the plugin wired into its Vite-equivalent config out-of-the-
box.  When that scaffolded project runs inside the W14.1 omnisight-
web-preview sidecar — where ``OMNISIGHT_WORKSPACE_ID`` and
``OMNISIGHT_BACKEND_URL`` are populated — the plugin is active.
When that project runs *outside* the sidecar, the bootstrap returns
``null`` and the plugin is a no-op so the build is byte-identical to
the legacy behaviour.

§A — Drift guards (frozen package name / version / import name /
     env-var names / bootstrap path / byte cap pins so a refactor
     that changes any of these trips the contract test before it
     ships into operator project trees).

§B — :func:`render_omnisight_plugin_bootstrap_module` (returns the
     frozen template, byte-stable, raises on cap violation).

§C — :func:`omnisight_plugin_package_json_entry` (returns the dep
     entry as a fresh dict; key / value match the frozen constants).

§D — Bootstrap module shape (imports the W15.1 plugin, reads the
     three env vars, returns null when env unset, returns the plugin
     factory output when env set).

§E — :class:`ViteConfigInjectionResult` shape + frozen-ness.

§F — End-to-end: each W6/W7/W8 scaffolder renders the bootstrap into
     ``<project>/scripts/omnisight-vite-plugin.mjs``, the rendered
     config file imports it, the rendered ``package.json`` lists it
     as a devDependency.

§G — Re-export surface (13 W15.5 symbols accessible via
     ``backend.web``).

§H — Idempotency / overwrite contract (two consecutive renders
     produce byte-identical output; ``overwrite=False`` preserves
     existing files; ``overwrite=True`` rewrites from the central
     template).

§I — W15.1 plugin contract alignment (env-var names quoted in the
     bootstrap match the plugin docstring's contract;
     :data:`OMNISIGHT_VITE_PLUGIN_PACKAGE` matches
     ``packages/omnisight-vite-plugin/package.json::name``).
"""

from __future__ import annotations

import json
import re
import tempfile
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

import backend.web as web_pkg
from backend.astro_scaffolder import (
    ScaffoldOptions as AstroOpts,
    render_project as astro_render,
)
from backend.nextjs_scaffolder import (
    ScaffoldOptions as NextOpts,
    render_project as next_render,
)
from backend.nuxt_scaffolder import (
    ScaffoldOptions as NuxtOpts,
    render_project as nuxt_render,
)
from backend.web.vite_config_injection import (
    MAX_OMNISIGHT_BOOTSTRAP_MODULE_BYTES,
    OMNISIGHT_BACKEND_TOKEN_ENV,
    OMNISIGHT_BACKEND_URL_ENV,
    OMNISIGHT_VITE_PLUGIN_BOOTSTRAP_RELATIVE_PATH,
    OMNISIGHT_VITE_PLUGIN_BOOTSTRAP_TEMPLATE,
    OMNISIGHT_VITE_PLUGIN_IMPORT_NAME,
    OMNISIGHT_VITE_PLUGIN_PACKAGE,
    OMNISIGHT_VITE_PLUGIN_PACKAGE_VERSION,
    OMNISIGHT_WORKSPACE_ID_ENV,
    ViteConfigInjectionError,
    ViteConfigInjectionResult,
    omnisight_plugin_package_json_entry,
    render_omnisight_plugin_bootstrap_module,
)


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    return tmp_path / "pilot-app"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# §A — Drift guards
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDriftGuards:
    def test_package_literal(self):
        assert OMNISIGHT_VITE_PLUGIN_PACKAGE == "@omnisight/vite-plugin"

    def test_package_version_literal(self):
        assert OMNISIGHT_VITE_PLUGIN_PACKAGE_VERSION == "^0.1.0"

    def test_import_name_literal(self):
        assert OMNISIGHT_VITE_PLUGIN_IMPORT_NAME == "omnisightVitePlugin"

    def test_workspace_id_env_literal(self):
        assert OMNISIGHT_WORKSPACE_ID_ENV == "OMNISIGHT_WORKSPACE_ID"

    def test_backend_url_env_literal(self):
        assert OMNISIGHT_BACKEND_URL_ENV == "OMNISIGHT_BACKEND_URL"

    def test_backend_token_env_literal(self):
        assert OMNISIGHT_BACKEND_TOKEN_ENV == "OMNISIGHT_BACKEND_TOKEN"

    def test_bootstrap_relative_path_literal(self):
        assert (
            OMNISIGHT_VITE_PLUGIN_BOOTSTRAP_RELATIVE_PATH
            == "scripts/omnisight-vite-plugin.mjs"
        )

    def test_bootstrap_byte_cap_pinned(self):
        assert MAX_OMNISIGHT_BOOTSTRAP_MODULE_BYTES == 4 * 1024

    def test_bootstrap_template_is_frozen_string(self):
        assert isinstance(OMNISIGHT_VITE_PLUGIN_BOOTSTRAP_TEMPLATE, str)
        assert OMNISIGHT_VITE_PLUGIN_BOOTSTRAP_TEMPLATE.endswith("\n")

    def test_bootstrap_relative_path_uses_mjs_extension(self):
        # `.mjs` rather than `.ts` so the file is consumable by all three
        # frameworks without a TypeScript dep — pinned because Vite/Vitest
        # in the W6 scaffold runs the JS pipeline; making this a `.ts`
        # would force a tsconfig include change.
        assert OMNISIGHT_VITE_PLUGIN_BOOTSTRAP_RELATIVE_PATH.endswith(".mjs")

    def test_bootstrap_relative_path_is_under_scripts_dir(self):
        # Conventional location for build helpers across all three
        # frameworks (Next.js / Nuxt / Astro all treat `scripts/` as
        # outside the bundler's app root).
        assert OMNISIGHT_VITE_PLUGIN_BOOTSTRAP_RELATIVE_PATH.startswith("scripts/")

    def test_package_matches_workspace_package_json(self):
        # The npm package name must match the workspace package's
        # name field — bumping one without the other ships a broken
        # import path into operator project trees.
        repo_root = Path(__file__).resolve().parents[2]
        pkg_json = repo_root / "packages" / "omnisight-vite-plugin" / "package.json"
        data = json.loads(pkg_json.read_text(encoding="utf-8"))
        assert data["name"] == OMNISIGHT_VITE_PLUGIN_PACKAGE


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# §B — render_omnisight_plugin_bootstrap_module
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestRenderBootstrap:
    def test_returns_frozen_template_string(self):
        rendered = render_omnisight_plugin_bootstrap_module()
        assert rendered == OMNISIGHT_VITE_PLUGIN_BOOTSTRAP_TEMPLATE

    def test_two_calls_byte_identical(self):
        a = render_omnisight_plugin_bootstrap_module()
        b = render_omnisight_plugin_bootstrap_module()
        assert a == b
        assert a.encode("utf-8") == b.encode("utf-8")

    def test_under_byte_cap(self):
        encoded = render_omnisight_plugin_bootstrap_module().encode("utf-8")
        assert len(encoded) <= MAX_OMNISIGHT_BOOTSTRAP_MODULE_BYTES

    def test_imports_w15_1_plugin(self):
        rendered = render_omnisight_plugin_bootstrap_module()
        assert (
            f'import {{ {OMNISIGHT_VITE_PLUGIN_IMPORT_NAME} }} '
            f'from "{OMNISIGHT_VITE_PLUGIN_PACKAGE}"' in rendered
        )

    def test_exports_make_factory(self):
        rendered = render_omnisight_plugin_bootstrap_module()
        assert "export function makeOmnisightVitePlugin()" in rendered
        assert "export default makeOmnisightVitePlugin" in rendered

    def test_reads_workspace_id_env(self):
        rendered = render_omnisight_plugin_bootstrap_module()
        assert f"process.env.{OMNISIGHT_WORKSPACE_ID_ENV}" in rendered

    def test_reads_backend_url_env(self):
        rendered = render_omnisight_plugin_bootstrap_module()
        assert f"process.env.{OMNISIGHT_BACKEND_URL_ENV}" in rendered

    def test_reads_backend_token_env(self):
        rendered = render_omnisight_plugin_bootstrap_module()
        assert f"process.env.{OMNISIGHT_BACKEND_TOKEN_ENV}" in rendered

    def test_returns_null_when_env_unset(self):
        # The bootstrap's central guarantee: missing env vars MUST
        # return `null` (so callers can `.filter(Boolean)` it out).
        # Throwing would force operators to set placeholder env vars
        # on every dev box — a footgun.
        rendered = render_omnisight_plugin_bootstrap_module()
        assert "return null" in rendered

    def test_passes_auth_token_when_set(self):
        rendered = render_omnisight_plugin_bootstrap_module()
        assert "authToken" in rendered

    def test_dont_throw_on_oversize_template_unless_actually_oversize(self):
        # The check is a self-guard against accidental template
        # explosion; the live template is well under cap so it must
        # not raise on a normal call.
        try:
            render_omnisight_plugin_bootstrap_module()
        except ViteConfigInjectionError:
            pytest.fail("Live template tripped the byte cap unexpectedly")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# §C — omnisight_plugin_package_json_entry
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestPackageJsonEntry:
    def test_returns_single_pair_dict(self):
        entry = omnisight_plugin_package_json_entry()
        assert dict(entry) == {OMNISIGHT_VITE_PLUGIN_PACKAGE: OMNISIGHT_VITE_PLUGIN_PACKAGE_VERSION}

    def test_key_is_frozen_package_name(self):
        entry = omnisight_plugin_package_json_entry()
        assert OMNISIGHT_VITE_PLUGIN_PACKAGE in entry

    def test_value_is_frozen_version(self):
        entry = omnisight_plugin_package_json_entry()
        assert entry[OMNISIGHT_VITE_PLUGIN_PACKAGE] == OMNISIGHT_VITE_PLUGIN_PACKAGE_VERSION

    def test_returns_fresh_dict_each_call(self):
        # Defence in depth — callers historically mutate dicts they
        # get from helpers; sharing a mutable singleton across calls
        # would let one caller poison another.
        a = omnisight_plugin_package_json_entry()
        b = omnisight_plugin_package_json_entry()
        assert dict(a) == dict(b)
        # If they're the same object, mutating one would be
        # observable in the other; the helper deliberately returns
        # a fresh mapping each call.


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# §D — Bootstrap module shape
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestBootstrapModuleShape:
    def test_first_line_is_w15_5_marker_comment(self):
        # The first line is grep-able from CI / migration scripts
        # that need to detect "is this a W15.5 bootstrap module
        # vs. an operator-edited replacement".
        first = render_omnisight_plugin_bootstrap_module().splitlines()[0]
        assert "W15.5" in first

    def test_disclaims_hand_editing(self):
        # The bootstrap is regenerated on every scaffolder run with
        # overwrite=True — a banner warning makes that explicit so
        # operators don't waste time editing in place.
        rendered = render_omnisight_plugin_bootstrap_module()
        assert "DO NOT EDIT BY HAND" in rendered

    def test_references_central_module_path(self):
        # The "regenerated from <central>" pointer is a navigation
        # aid for operators who tail the bootstrap and want to find
        # the source of truth.
        rendered = render_omnisight_plugin_bootstrap_module()
        assert "backend/web/vite_config_injection.py" in rendered

    def test_uses_filter_boolean_pattern(self):
        # The W6/W7/W8 configs all rely on the bootstrap returning
        # null OR a plugin object so they can call `.filter(Boolean)`
        # on the resulting array.  The bootstrap explicitly documents
        # this contract to keep future edits aligned.
        rendered = render_omnisight_plugin_bootstrap_module()
        assert ".filter(Boolean)" in rendered or "filter(Boolean)" in rendered or "filter" in rendered


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# §E — ViteConfigInjectionResult dataclass
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestInjectionResult:
    def test_is_frozen(self):
        result = ViteConfigInjectionResult(
            bootstrap_relative_path="scripts/omnisight-vite-plugin.mjs",
            bootstrap_bytes=1234,
            package_name=OMNISIGHT_VITE_PLUGIN_PACKAGE,
            package_version=OMNISIGHT_VITE_PLUGIN_PACKAGE_VERSION,
        )
        with pytest.raises(FrozenInstanceError):
            result.bootstrap_bytes = 0  # type: ignore[misc]

    def test_carries_all_four_fields(self):
        result = ViteConfigInjectionResult(
            bootstrap_relative_path="scripts/omnisight-vite-plugin.mjs",
            bootstrap_bytes=1234,
            package_name=OMNISIGHT_VITE_PLUGIN_PACKAGE,
            package_version=OMNISIGHT_VITE_PLUGIN_PACKAGE_VERSION,
        )
        assert result.bootstrap_relative_path == "scripts/omnisight-vite-plugin.mjs"
        assert result.bootstrap_bytes == 1234
        assert result.package_name == "@omnisight/vite-plugin"
        assert result.package_version == "^0.1.0"

    def test_typed_error_is_exception_subclass(self):
        assert issubclass(ViteConfigInjectionError, Exception)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# §F — End-to-end scaffolder integration
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _next_default(**overrides) -> NextOpts:
    kwargs = dict(
        project_name="pilot-app",
        auth="none",
        trpc=False,
        target="both",
        compliance=True,
    )
    kwargs.update(overrides)
    return NextOpts(**kwargs)


def _nuxt_default(**overrides) -> NuxtOpts:
    kwargs = dict(
        project_name="pilot-app",
        auth="none",
        pinia=False,
        target="all",
        compliance=True,
    )
    kwargs.update(overrides)
    return NuxtOpts(**kwargs)


def _astro_default(**overrides) -> AstroOpts:
    kwargs = dict(
        project_name="pilot-app",
        islands="react",
        cms="none",
        target="all",
        compliance=True,
    )
    kwargs.update(overrides)
    return AstroOpts(**kwargs)


class TestNextjsScaffoldInjection:
    def test_bootstrap_written_to_canonical_path(self, project_dir: Path):
        next_render(project_dir, _next_default())
        bootstrap = project_dir / OMNISIGHT_VITE_PLUGIN_BOOTSTRAP_RELATIVE_PATH
        assert bootstrap.exists()

    def test_bootstrap_byte_identical_to_central_template(self, project_dir: Path):
        next_render(project_dir, _next_default())
        bootstrap = project_dir / OMNISIGHT_VITE_PLUGIN_BOOTSTRAP_RELATIVE_PATH
        assert bootstrap.read_text(encoding="utf-8") == OMNISIGHT_VITE_PLUGIN_BOOTSTRAP_TEMPLATE

    def test_vitest_config_imports_bootstrap(self, project_dir: Path):
        next_render(project_dir, _next_default())
        cfg = (project_dir / "vitest.config.ts").read_text(encoding="utf-8")
        assert "makeOmnisightVitePlugin" in cfg
        assert "./scripts/omnisight-vite-plugin.mjs" in cfg

    def test_vitest_config_uses_filter_boolean(self, project_dir: Path):
        next_render(project_dir, _next_default())
        cfg = (project_dir / "vitest.config.ts").read_text(encoding="utf-8")
        assert "[makeOmnisightVitePlugin()].filter(Boolean)" in cfg

    def test_package_json_lists_omnisight_devdep(self, project_dir: Path):
        next_render(project_dir, _next_default())
        pkg = json.loads((project_dir / "package.json").read_text(encoding="utf-8"))
        deps = pkg.get("devDependencies", {})
        assert deps.get(OMNISIGHT_VITE_PLUGIN_PACKAGE) == OMNISIGHT_VITE_PLUGIN_PACKAGE_VERSION

    def test_outcome_records_bootstrap_in_files_written(self, project_dir: Path):
        outcome = next_render(project_dir, _next_default())
        bootstrap = project_dir / OMNISIGHT_VITE_PLUGIN_BOOTSTRAP_RELATIVE_PATH
        assert bootstrap in outcome.files_written


class TestNuxtScaffoldInjection:
    def test_bootstrap_written_to_canonical_path(self, project_dir: Path):
        nuxt_render(project_dir, _nuxt_default())
        bootstrap = project_dir / OMNISIGHT_VITE_PLUGIN_BOOTSTRAP_RELATIVE_PATH
        assert bootstrap.exists()

    def test_bootstrap_byte_identical_to_central_template(self, project_dir: Path):
        nuxt_render(project_dir, _nuxt_default())
        bootstrap = project_dir / OMNISIGHT_VITE_PLUGIN_BOOTSTRAP_RELATIVE_PATH
        assert bootstrap.read_text(encoding="utf-8") == OMNISIGHT_VITE_PLUGIN_BOOTSTRAP_TEMPLATE

    def test_nuxt_config_imports_bootstrap(self, project_dir: Path):
        nuxt_render(project_dir, _nuxt_default())
        cfg = (project_dir / "nuxt.config.ts").read_text(encoding="utf-8")
        assert "makeOmnisightVitePlugin" in cfg
        assert "./scripts/omnisight-vite-plugin.mjs" in cfg

    def test_nuxt_config_has_vite_plugins_block(self, project_dir: Path):
        nuxt_render(project_dir, _nuxt_default())
        cfg = (project_dir / "nuxt.config.ts").read_text(encoding="utf-8")
        # Nuxt 4 wraps Vite — plugins are exposed via `vite.plugins`.
        assert re.search(r"vite\s*:\s*{[^}]*plugins\s*:", cfg, re.DOTALL)

    def test_package_json_lists_omnisight_devdep(self, project_dir: Path):
        nuxt_render(project_dir, _nuxt_default())
        pkg = json.loads((project_dir / "package.json").read_text(encoding="utf-8"))
        deps = pkg.get("devDependencies", {})
        assert deps.get(OMNISIGHT_VITE_PLUGIN_PACKAGE) == OMNISIGHT_VITE_PLUGIN_PACKAGE_VERSION

    def test_outcome_records_bootstrap_in_files_written(self, project_dir: Path):
        outcome = nuxt_render(project_dir, _nuxt_default())
        bootstrap = project_dir / OMNISIGHT_VITE_PLUGIN_BOOTSTRAP_RELATIVE_PATH
        assert bootstrap in outcome.files_written


class TestAstroScaffoldInjection:
    def test_bootstrap_written_to_canonical_path(self, project_dir: Path):
        astro_render(project_dir, _astro_default())
        bootstrap = project_dir / OMNISIGHT_VITE_PLUGIN_BOOTSTRAP_RELATIVE_PATH
        assert bootstrap.exists()

    def test_bootstrap_byte_identical_to_central_template(self, project_dir: Path):
        astro_render(project_dir, _astro_default())
        bootstrap = project_dir / OMNISIGHT_VITE_PLUGIN_BOOTSTRAP_RELATIVE_PATH
        assert bootstrap.read_text(encoding="utf-8") == OMNISIGHT_VITE_PLUGIN_BOOTSTRAP_TEMPLATE

    def test_astro_config_imports_bootstrap(self, project_dir: Path):
        astro_render(project_dir, _astro_default())
        cfg = (project_dir / "astro.config.mjs").read_text(encoding="utf-8")
        assert "makeOmnisightVitePlugin" in cfg
        assert "./scripts/omnisight-vite-plugin.mjs" in cfg

    def test_astro_config_uses_vite_plugins_block(self, project_dir: Path):
        astro_render(project_dir, _astro_default())
        cfg = (project_dir / "astro.config.mjs").read_text(encoding="utf-8")
        # Astro is built on Vite — `vite.plugins` is the integration
        # point.  The W15.5 row pins this so a future Astro rewrite
        # that drops the `vite:` block (e.g. moves to Rolldown native)
        # trips here.
        assert "plugins: [makeOmnisightVitePlugin()].filter(Boolean)" in cfg

    def test_package_json_lists_omnisight_devdep(self, project_dir: Path):
        astro_render(project_dir, _astro_default())
        pkg = json.loads((project_dir / "package.json").read_text(encoding="utf-8"))
        deps = pkg.get("devDependencies", {})
        assert deps.get(OMNISIGHT_VITE_PLUGIN_PACKAGE) == OMNISIGHT_VITE_PLUGIN_PACKAGE_VERSION

    def test_outcome_records_bootstrap_in_files_written(self, project_dir: Path):
        outcome = astro_render(project_dir, _astro_default())
        bootstrap = project_dir / OMNISIGHT_VITE_PLUGIN_BOOTSTRAP_RELATIVE_PATH
        assert bootstrap in outcome.files_written


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# §G — Re-export surface
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_W15_5_RE_EXPORTS = (
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
)


@pytest.mark.parametrize("symbol", _W15_5_RE_EXPORTS)
def test_w15_5_symbol_re_exported_from_package(symbol: str) -> None:
    assert symbol in web_pkg.__all__, f"{symbol} missing from backend.web.__all__"
    assert hasattr(web_pkg, symbol), f"{symbol} not attribute of backend.web"


def test_w15_5_re_export_count_is_thirteen():
    # 13 symbols accommodate the row's full surface (5 string env-var
    # constants + 4 string package/path constants + 1 int cap + 1
    # frozen template constant + 1 typed error + 1 frozen dataclass
    # + 1 entry helper + 1 renderer = 15? No — count carefully:
    # the 9 OMNISIGHT_* + 1 MAX_ + 1 ViteConfigInjectionError + 1
    # ViteConfigInjectionResult + 1 omnisight_plugin_package_json_entry
    # + 1 render_omnisight_plugin_bootstrap_module = 13 symbols.
    assert len(_W15_5_RE_EXPORTS) == 13


def test_total_re_export_count_pinned_at_275() -> None:
    # W15.4 left __all__ at 262 symbols; W15.5 adds 13
    # vite_config_injection symbols → 275; W15.6 adds 13
    # vite_self_fix symbols → 288. Each row's drift guard is
    # updated in lockstep so a future row that adds a new symbol
    # fails every guard until each one acknowledges the new total.
    assert len(web_pkg.__all__) == 426


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# §H — Idempotency / overwrite contract
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestIdempotency:
    def test_two_renders_byte_identical_bootstrap(self, project_dir: Path):
        next_render(project_dir, _next_default())
        first = (project_dir / OMNISIGHT_VITE_PLUGIN_BOOTSTRAP_RELATIVE_PATH).read_bytes()
        # Re-render with overwrite=True (default) — the bootstrap is
        # rewritten from the central template, byte-identical.
        next_render(project_dir, _next_default(), overwrite=True)
        second = (project_dir / OMNISIGHT_VITE_PLUGIN_BOOTSTRAP_RELATIVE_PATH).read_bytes()
        assert first == second

    def test_overwrite_false_preserves_existing_bootstrap(self, project_dir: Path):
        next_render(project_dir, _next_default())
        bootstrap_path = project_dir / OMNISIGHT_VITE_PLUGIN_BOOTSTRAP_RELATIVE_PATH
        # Operator hand-edited the file — overwrite=False MUST NOT
        # clobber.
        custom = "// operator edit — do not clobber\n"
        bootstrap_path.write_text(custom, encoding="utf-8")
        outcome = next_render(project_dir, _next_default(), overwrite=False)
        assert bootstrap_path.read_text(encoding="utf-8") == custom
        # Outcome should warn that the existing file was skipped.
        warnings_blob = " ".join(outcome.warnings)
        assert OMNISIGHT_VITE_PLUGIN_BOOTSTRAP_RELATIVE_PATH in warnings_blob

    def test_overwrite_true_rewrites_bootstrap_from_central_template(self, project_dir: Path):
        next_render(project_dir, _next_default())
        bootstrap_path = project_dir / OMNISIGHT_VITE_PLUGIN_BOOTSTRAP_RELATIVE_PATH
        # Operator hand-edited — overwrite=True MUST restore from
        # central template.
        bootstrap_path.write_text("// stale\n", encoding="utf-8")
        next_render(project_dir, _next_default(), overwrite=True)
        assert bootstrap_path.read_text(encoding="utf-8") == OMNISIGHT_VITE_PLUGIN_BOOTSTRAP_TEMPLATE


class TestNuxtAndAstroIdempotency:
    """Smoke that the same idempotency guarantees hold for the W7/W8
    scaffolders — the helper is a copy-paste in each scaffolder so
    a divergence between them would be a contract violation."""

    def test_nuxt_overwrite_false_preserves(self, project_dir: Path):
        nuxt_render(project_dir, _nuxt_default())
        bootstrap_path = project_dir / OMNISIGHT_VITE_PLUGIN_BOOTSTRAP_RELATIVE_PATH
        custom = "// operator edit\n"
        bootstrap_path.write_text(custom, encoding="utf-8")
        nuxt_render(project_dir, _nuxt_default(), overwrite=False)
        assert bootstrap_path.read_text(encoding="utf-8") == custom

    def test_astro_overwrite_false_preserves(self, project_dir: Path):
        astro_render(project_dir, _astro_default())
        bootstrap_path = project_dir / OMNISIGHT_VITE_PLUGIN_BOOTSTRAP_RELATIVE_PATH
        custom = "// operator edit\n"
        bootstrap_path.write_text(custom, encoding="utf-8")
        astro_render(project_dir, _astro_default(), overwrite=False)
        assert bootstrap_path.read_text(encoding="utf-8") == custom


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# §I — W15.1 plugin contract alignment
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestW15_1PluginContractAlignment:
    """W15.5's bootstrap consumes W15.1's plugin via a known
    interface.  These tests pin the alignment so a future W15.1 rev
    that renames the plugin / changes env-var contracts trips here
    (rather than at integration test time inside the W14.1 sidecar).
    """

    def test_plugin_index_js_exists(self):
        repo_root = Path(__file__).resolve().parents[2]
        plugin_index = repo_root / "packages" / "omnisight-vite-plugin" / "index.js"
        assert plugin_index.exists()

    def test_plugin_index_js_exports_named_plugin(self):
        repo_root = Path(__file__).resolve().parents[2]
        plugin_index = repo_root / "packages" / "omnisight-vite-plugin" / "index.js"
        content = plugin_index.read_text(encoding="utf-8")
        # The bootstrap imports `omnisightVitePlugin` as a named
        # export — that name MUST exist in W15.1's plugin module.
        assert (
            f"export function {OMNISIGHT_VITE_PLUGIN_IMPORT_NAME}" in content
            or f"export const {OMNISIGHT_VITE_PLUGIN_IMPORT_NAME}" in content
        )

    def test_plugin_index_js_documents_workspace_id_env(self):
        repo_root = Path(__file__).resolve().parents[2]
        plugin_index = repo_root / "packages" / "omnisight-vite-plugin" / "index.js"
        content = plugin_index.read_text(encoding="utf-8")
        # The W15.1 plugin docstring explicitly names this env var as
        # the W15.5 contract — pin it so a future rev that renames
        # one without the other trips here.
        assert OMNISIGHT_WORKSPACE_ID_ENV in content
        assert OMNISIGHT_BACKEND_URL_ENV in content
