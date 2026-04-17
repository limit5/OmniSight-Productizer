"""X8 #304 — SKILL-DESKTOP-TAURI contract tests.

SKILL-DESKTOP-TAURI is the fourth software-vertical skill pack and
re-exercises the X0-X4 framework on a hybrid Tauri 2.x deliverable
(Rust backend + system-webview frontend) producing platform-specific
installers (msi / dmg / deb / AppImage / rpm) for the three desktop
OSes — first dual-language consumer of the framework. These tests
lock the framework invariants the same way ``test_skill_fastapi.py``
locks X5, ``test_skill_go_service.py`` locks X6, and
``test_skill_rust_cli.py`` locks X7:

* **X0** — ``target_kind=software`` profile loads cleanly for the
  ``linux-x86_64-native`` binding the scaffold defaults to.
* **X1** — ``scripts/check_cov.sh`` pins the X1 Rust threshold (75%)
  AND ``vite.config.ts`` pins the Node threshold (80%).
* **X2** — rendered project honours desktop-tauri role mandates:
  reverse-DNS bundle.identifier, strict CSP, capability grants per
  ``#[tauri::command]`` (no `**` wildcard), no Tauri 1.x allowlist
  patterns.
* **X3** — ``CargoDistAdapter`` accepts the rendered ``src-tauri/``
  tree without hitting the network.
* **X4** — ``software_compliance.run_all`` runs against both
  ecosystems (npm at root, cargo under src-tauri/) and produces a
  structured verdict.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from backend.build_adapters import BuildSource, CargoDistAdapter
from backend.platform import get_platform_config
from backend.skill_registry import get_skill, list_skills, validate_skill
from backend.software_compliance.licenses import detect_ecosystem
from backend.software_simulator import COVERAGE_THRESHOLDS
from backend.tauri_scaffolder import (
    ScaffoldOptions,
    _SCAFFOLDS_DIR,
    _SKILL_DIR,
    _humanise,
    _slugify_bin,
    _slugify_crate,
    _slugify_npm,
    dry_run_build,
    pilot_report,
    render_project,
    validate_pack,
)


@pytest.fixture
def project_dir():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp) / "tauri-pilot"


def _default_opts(**overrides) -> ScaffoldOptions:
    kwargs = dict(
        project_name="tauri-pilot",
        frontend="react",
        updater=True,
        compliance=True,
    )
    kwargs.update(overrides)
    return ScaffoldOptions(**kwargs)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Skill pack registry invariants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestSkillPackRegistry:
    def test_pack_discoverable(self):
        names = {s.name for s in list_skills()}
        assert "skill-desktop-tauri" in names

    def test_pack_validates_clean(self):
        result = validate_skill("skill-desktop-tauri")
        assert result.ok, (
            f"skill-desktop-tauri validation failed: "
            f"{[(i.level, i.message) for i in result.issues]}"
        )

    def test_all_five_artifact_kinds_declared(self):
        info = get_skill("skill-desktop-tauri")
        assert info is not None
        assert info.artifact_kinds == {"tasks", "scaffolds", "tests", "hil", "docs"}

    def test_manifest_declares_core_dependencies(self):
        info = get_skill("skill-desktop-tauri")
        assert info is not None and info.manifest is not None
        assert "CORE-05" in info.manifest.depends_on_core
        assert info.manifest.depends_on_skills == []

    def test_manifest_keywords_include_tauri_markers(self):
        info = get_skill("skill-desktop-tauri")
        assert info and info.manifest
        kws = set(info.manifest.keywords)
        assert {"tauri", "tauri-2", "desktop", "cargo-dist", "x8"}.issubset(kws)

    def test_validate_pack_helper(self):
        result = validate_pack()
        assert result["installed"] is True
        assert result["ok"] is True
        assert result["skill_name"] == "skill-desktop-tauri"

    def test_skill_dir_resolution(self):
        assert _SKILL_DIR.is_dir()
        assert (_SKILL_DIR / "skill.yaml").is_file()
        assert (_SKILL_DIR / "tasks.yaml").is_file()
        assert (_SKILL_DIR / "SKILL.md").is_file()
        assert _SCAFFOLDS_DIR.is_dir()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Slug + name helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestSlugifyHelpers:
    def test_bin_preserves_hyphens(self):
        assert _slugify_bin("my-app") == "my-app"

    def test_bin_lowercases(self):
        assert _slugify_bin("MyApp") == "myapp"

    def test_bin_strips_non_alpha(self):
        assert _slugify_bin("foo/bar@baz") == "foo-bar-baz"

    def test_crate_underscorifies(self):
        assert _slugify_crate("my-app") == "my_app"

    def test_crate_rejects_dots(self):
        assert _slugify_crate("foo.bar") == "foo_bar"

    def test_npm_kebab(self):
        assert _slugify_npm("My_App") == "my-app"

    def test_npm_strips_dots(self):
        assert _slugify_npm("foo.bar") == "foo-bar"

    def test_humanise_capitalises_words(self):
        assert _humanise("my-tauri-app") == "My Tauri App"

    def test_humanise_handles_underscore(self):
        assert _humanise("snake_case_name") == "Snake Case Name"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Scaffold option validation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestScaffoldOptions:
    def test_defaults_validate(self):
        _default_opts().validate()

    def test_bad_frontend_rejected(self):
        with pytest.raises(ValueError, match="frontend"):
            _default_opts(frontend="svelte").validate()

    def test_empty_project_name_rejected(self):
        with pytest.raises(ValueError, match="project_name"):
            _default_opts(project_name="   ").validate()

    def test_default_app_name_humanises(self):
        opts = _default_opts(project_name="my-tauri-app")
        assert opts.resolved_app_name() == "My Tauri App"

    def test_default_bin_name(self):
        opts = _default_opts(project_name="My-Tauri")
        assert opts.resolved_bin_name() == "my-tauri"

    def test_default_crate_name_derives_from_bin(self):
        opts = _default_opts(project_name="My-Tauri")
        assert opts.resolved_crate_name() == "my_tauri"

    def test_default_identifier_is_reverse_dns(self):
        opts = _default_opts(project_name="My-Tauri")
        # Reverse-DNS labels reject `_`; the default must use the
        # kebab slug, not the underscore crate name.
        assert opts.resolved_identifier() == "com.example.my-tauri"

    def test_explicit_identifier_lowercased(self):
        opts = _default_opts(identifier="COM.Example.MyApp")
        assert opts.resolved_identifier() == "com.example.myapp"

    def test_identifier_rejects_non_reverse_dns(self):
        with pytest.raises(ValueError, match="reverse-DNS"):
            _default_opts(identifier="not-reverse-dns").validate()

    def test_identifier_rejects_underscores(self):
        # macOS code-sign rejects `_` in bundle identifiers.
        with pytest.raises(ValueError, match="reverse-DNS"):
            _default_opts(identifier="com.example.bad_id").validate()

    def test_explicit_bin_name_wins(self):
        opts = _default_opts(bin_name="customcli")
        assert opts.resolved_bin_name() == "customcli"

    def test_explicit_crate_name_wins(self):
        opts = _default_opts(crate_name="custom_crate")
        assert opts.resolved_crate_name() == "custom_crate"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Render outcome — structural invariants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestRenderOutcome:
    def test_render_creates_required_files(self, project_dir):
        render_project(project_dir, _default_opts())
        required = [
            "package.json",
            "vite.config.ts",
            "tsconfig.json",
            "tsconfig.node.json",
            "index.html",
            "Makefile",
            "README.md",
            "spdx.allowlist.json",
            ".env.example",
            ".gitignore",
            "scripts/check_cov.sh",
            ".github/workflows/release.yml",
            "src/App.css",
            "src/useTauri.ts",
            "src/main.tsx",
            "src/App.tsx",
            "src/__tests__/App.test.tsx",
            "src-tauri/Cargo.toml",
            "src-tauri/rust-toolchain.toml",
            "src-tauri/rustfmt.toml",
            "src-tauri/clippy.toml",
            "src-tauri/deny.toml",
            "src-tauri/dist-workspace.toml",
            "src-tauri/build.rs",
            "src-tauri/tauri.conf.json",
            "src-tauri/capabilities/default.json",
            "src-tauri/icons/README.md",
            "src-tauri/src/main.rs",
            "src-tauri/src/lib.rs",
            "src-tauri/src/commands.rs",
        ]
        for rel in required:
            assert (project_dir / rel).is_file(), f"missing: {rel}"

    def test_cov_script_is_executable(self, project_dir):
        render_project(project_dir, _default_opts())
        script = project_dir / "scripts" / "check_cov.sh"
        assert script.stat().st_mode & 0o111, "check_cov.sh must be executable"

    def test_react_default_drops_vue_files(self, project_dir):
        render_project(project_dir, _default_opts(frontend="react"))
        assert not (project_dir / "src" / "App.vue").exists()
        assert not (project_dir / "src" / "main.ts").exists()
        assert not (project_dir / "src" / "__tests__" / "App.test.ts").exists()

    def test_vue_variant_drops_react_files(self, project_dir):
        render_project(project_dir, _default_opts(frontend="vue"))
        assert (project_dir / "src" / "App.vue").is_file()
        assert (project_dir / "src" / "main.ts").is_file()
        assert (project_dir / "src" / "__tests__" / "App.test.ts").is_file()
        assert not (project_dir / "src" / "App.tsx").exists()
        assert not (project_dir / "src" / "main.tsx").exists()
        assert not (project_dir / "src" / "__tests__" / "App.test.tsx").exists()

    def test_vue_variant_uses_vue_index_script(self, project_dir):
        render_project(project_dir, _default_opts(frontend="vue"))
        index = (project_dir / "index.html").read_text()
        assert "/src/main.ts" in index
        assert "/src/main.tsx" not in index

    def test_react_variant_uses_react_index_script(self, project_dir):
        render_project(project_dir, _default_opts(frontend="react"))
        index = (project_dir / "index.html").read_text()
        assert "/src/main.tsx" in index

    def test_vue_variant_swaps_vite_plugin(self, project_dir):
        render_project(project_dir, _default_opts(frontend="vue"))
        cfg = (project_dir / "vite.config.ts").read_text()
        assert "@vitejs/plugin-vue" in cfg
        assert "@vitejs/plugin-react" not in cfg

    def test_react_variant_swaps_vite_plugin(self, project_dir):
        render_project(project_dir, _default_opts(frontend="react"))
        cfg = (project_dir / "vite.config.ts").read_text()
        assert "@vitejs/plugin-react" in cfg
        assert "@vitejs/plugin-vue" not in cfg

    def test_vue_variant_swaps_package_json_deps(self, project_dir):
        render_project(project_dir, _default_opts(frontend="vue"))
        pkg = json.loads((project_dir / "package.json").read_text())
        assert "vue" in pkg["dependencies"]
        assert "react" not in pkg["dependencies"]

    def test_react_variant_swaps_package_json_deps(self, project_dir):
        render_project(project_dir, _default_opts(frontend="react"))
        pkg = json.loads((project_dir / "package.json").read_text())
        assert "react" in pkg["dependencies"]
        assert "vue" not in pkg["dependencies"]

    def test_compliance_off_skips_spdx_and_deny(self, project_dir):
        render_project(project_dir, _default_opts(compliance=False))
        assert not (project_dir / "spdx.allowlist.json").exists()
        assert not (project_dir / "src-tauri" / "deny.toml").exists()

    def test_idempotent_re_render(self, project_dir):
        o1 = render_project(project_dir, _default_opts())
        o2 = render_project(project_dir, _default_opts())
        assert sorted(o1.files_written) == sorted(o2.files_written)
        assert o1.bytes_written == o2.bytes_written

    def test_overwrite_false_warns_on_existing(self, project_dir):
        render_project(project_dir, _default_opts())
        outcome = render_project(project_dir, _default_opts(), overwrite=False)
        assert outcome.warnings
        assert all("skipped existing" in w for w in outcome.warnings)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Updater knob
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestUpdaterKnob:
    def test_updater_on_adds_plugin_to_cargo(self, project_dir):
        render_project(project_dir, _default_opts(updater=True))
        cargo = (project_dir / "src-tauri" / "Cargo.toml").read_text()
        assert "tauri-plugin-updater" in cargo
        assert "tauri-plugin-process" in cargo

    def test_updater_off_strips_plugin_from_cargo(self, project_dir):
        render_project(project_dir, _default_opts(updater=False))
        cargo = (project_dir / "src-tauri" / "Cargo.toml").read_text()
        assert "tauri-plugin-updater" not in cargo
        assert "tauri-plugin-process" not in cargo

    def test_updater_on_adds_bundle_block(self, project_dir):
        render_project(project_dir, _default_opts(updater=True))
        conf = json.loads(
            (project_dir / "src-tauri" / "tauri.conf.json").read_text(),
        )
        assert conf["bundle"].get("createUpdaterArtifacts") is True
        assert "updater" in conf.get("plugins", {})
        assert "pubkey" in conf["plugins"]["updater"]
        assert conf["plugins"]["updater"]["endpoints"]

    def test_updater_off_strips_bundle_block(self, project_dir):
        render_project(project_dir, _default_opts(updater=False))
        conf = json.loads(
            (project_dir / "src-tauri" / "tauri.conf.json").read_text(),
        )
        assert "createUpdaterArtifacts" not in conf["bundle"]
        assert "updater" not in conf.get("plugins", {})

    def test_updater_on_registers_plugin_in_lib_rs(self, project_dir):
        render_project(project_dir, _default_opts(updater=True))
        lib = (project_dir / "src-tauri" / "src" / "lib.rs").read_text()
        assert "tauri_plugin_updater::Builder::default()" in lib

    def test_updater_off_omits_plugin_from_lib_rs(self, project_dir):
        render_project(project_dir, _default_opts(updater=False))
        lib = (project_dir / "src-tauri" / "src" / "lib.rs").read_text()
        assert "tauri_plugin_updater" not in lib

    def test_updater_on_adds_npm_plugin_dep(self, project_dir):
        render_project(project_dir, _default_opts(updater=True))
        pkg = json.loads((project_dir / "package.json").read_text())
        assert "@tauri-apps/plugin-updater" in pkg["dependencies"]

    def test_updater_off_omits_npm_plugin_dep(self, project_dir):
        render_project(project_dir, _default_opts(updater=False))
        pkg = json.loads((project_dir / "package.json").read_text())
        assert "@tauri-apps/plugin-updater" not in pkg["dependencies"]

    def test_updater_off_drops_capability_grants(self, project_dir):
        render_project(project_dir, _default_opts(updater=False))
        cap = json.loads(
            (project_dir / "src-tauri" / "capabilities" / "default.json").read_text(),
        )
        assert "updater:default" not in cap["permissions"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  X0 — platform profile binding
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestX0PlatformBinding:
    def test_default_profile_resolves_software(self):
        cfg = get_platform_config("linux-x86_64-native")
        assert cfg["target_kind"] == "software"

    def test_render_records_profile_binding(self, project_dir):
        outcome = render_project(project_dir, _default_opts())
        assert outcome.profile_binding == "linux-x86_64-native"

    def test_cargo_dist_lists_5_targets(self, project_dir):
        render_project(project_dir, _default_opts())
        cargo = (project_dir / "src-tauri" / "Cargo.toml").read_text()
        for triple in (
            "x86_64-unknown-linux-gnu",
            "aarch64-unknown-linux-gnu",
            "x86_64-pc-windows-msvc",
            "aarch64-apple-darwin",
            "x86_64-apple-darwin",
        ):
            assert triple in cargo, f"missing cargo-dist target: {triple}"

    def test_three_platform_ci_matrix(self, project_dir):
        render_project(project_dir, _default_opts())
        wf = (project_dir / ".github" / "workflows" / "release.yml").read_text()
        assert "macos-14" in wf
        assert "ubuntu-22.04" in wf
        assert "windows-latest" in wf


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  X1 — coverage thresholds (Rust 75% + frontend 80%)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestX1CoverageThresholds:
    def test_x1_rust_threshold_is_75(self):
        assert COVERAGE_THRESHOLDS["rust"] == 75.0

    def test_check_cov_defaults_to_75(self, project_dir):
        render_project(project_dir, _default_opts())
        script = (project_dir / "scripts" / "check_cov.sh").read_text()
        assert "COVERAGE_THRESHOLD:-75" in script

    def test_check_cov_walks_into_src_tauri(self, project_dir):
        # The Cargo.toml lives under src-tauri/, not at root, so the
        # script must `cd src-tauri` before invoking llvm-cov.
        render_project(project_dir, _default_opts())
        script = (project_dir / "scripts" / "check_cov.sh").read_text()
        assert "src-tauri" in script

    def test_makefile_wires_both_tracks(self, project_dir):
        render_project(project_dir, _default_opts())
        makefile = (project_dir / "Makefile").read_text()
        assert "test-rust" in makefile
        assert "test-frontend" in makefile
        assert "./scripts/check_cov.sh" in makefile
        assert "pnpm test" in makefile

    def test_vite_config_pins_80_percent_floor(self, project_dir):
        render_project(project_dir, _default_opts())
        cfg = (project_dir / "vite.config.ts").read_text()
        # All four Vitest threshold knobs (lines/functions/branches/
        # statements) must be at 80 to honour the X1 Node track.
        assert "lines: 80" in cfg
        assert "functions: 80" in cfg
        assert "branches: 80" in cfg
        assert "statements: 80" in cfg


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  X2 — desktop-tauri role alignment
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestX2RoleAlignment:
    def test_bundle_identifier_reverse_dns(self, project_dir):
        render_project(project_dir, _default_opts(identifier="com.example.myapp"))
        conf = json.loads(
            (project_dir / "src-tauri" / "tauri.conf.json").read_text(),
        )
        # desktop-tauri role rule: bundle.identifier must be unique
        # and reverse-DNS shaped (macOS code-sign requirement).
        assert conf["identifier"] == "com.example.myapp"

    def test_csp_is_strict_no_unsafe_eval(self, project_dir):
        render_project(project_dir, _default_opts())
        conf = json.loads(
            (project_dir / "src-tauri" / "tauri.conf.json").read_text(),
        )
        csp = conf["app"]["security"]["csp"]
        # desktop-tauri role mandatory: no `unsafe-eval`, no `*` wildcard
        # outside the well-known data:/asset:/tauri:/ipc: schemes.
        assert "unsafe-eval" not in csp
        assert "default-src 'self'" in csp

    def test_capabilities_no_wildcard(self, project_dir):
        render_project(project_dir, _default_opts())
        cap_text = (
            project_dir / "src-tauri" / "capabilities" / "default.json"
        ).read_text()
        # desktop-tauri role mandatory anti-pattern — `permissions: ["**"]`.
        assert '"**"' not in cap_text

    def test_capabilities_grant_per_command(self, project_dir):
        render_project(project_dir, _default_opts())
        cap = json.loads(
            (project_dir / "src-tauri" / "capabilities" / "default.json").read_text(),
        )
        crate = _default_opts().resolved_crate_name()
        # Every #[tauri::command] declared in commands.rs must have
        # a matching `<crate>:allow-<command>` grant.
        assert f"{crate}:allow-greet" in cap["permissions"]
        assert f"{crate}:allow-app-info" in cap["permissions"]

    def test_commands_use_tauri_command_macro(self, project_dir):
        render_project(project_dir, _default_opts())
        commands = (
            project_dir / "src-tauri" / "src" / "commands.rs"
        ).read_text()
        # IPC entry points must be #[tauri::command] — never raw fns.
        assert "#[tauri::command]" in commands

    def test_no_tauri_1x_allowlist_pattern(self, project_dir):
        render_project(project_dir, _default_opts())
        conf_text = (
            project_dir / "src-tauri" / "tauri.conf.json"
        ).read_text()
        # Tauri 1.x used `tauri.allowlist.*`; 2.x replaces it with
        # the capability system. Leaking a 1.x pattern is the role's
        # mandatory anti-pattern.
        assert '"allowlist"' not in conf_text

    def test_logs_to_stderr(self, project_dir):
        render_project(project_dir, _default_opts())
        lib = (project_dir / "src-tauri" / "src" / "lib.rs").read_text()
        # cli-tooling / desktop-tauri role rule: logs → stderr,
        # data → IPC return value.
        assert "std::io::stderr" in lib

    def test_main_uses_windows_subsystem(self, project_dir):
        render_project(project_dir, _default_opts())
        main_rs = (project_dir / "src-tauri" / "src" / "main.rs").read_text()
        # Windows release must suppress the console window — without
        # this the operator sees a flashing cmd.exe shell on launch.
        assert 'windows_subsystem = "windows"' in main_rs

    def test_release_profile_optimised_for_size(self, project_dir):
        render_project(project_dir, _default_opts())
        cargo = (project_dir / "src-tauri" / "Cargo.toml").read_text()
        # desktop-tauri role budget — installer ≤ 15 MiB MSI / 12 MiB
        # DMG / 18 MiB AppImage. These three together drive most of the
        # delta vs an Electron baseline.
        assert 'lto = "fat"' in cargo
        assert "strict = true" in cargo or "strip = true" in cargo
        assert "codegen-units = 1" in cargo

    def test_three_platform_bundle_targets(self, project_dir):
        render_project(project_dir, _default_opts())
        conf = json.loads(
            (project_dir / "src-tauri" / "tauri.conf.json").read_text(),
        )
        targets = conf["bundle"]["targets"]
        # Windows (msi) / macOS (dmg + app) / Linux (deb + appimage + rpm).
        assert "msi" in targets
        assert "dmg" in targets or "app" in targets
        assert "deb" in targets
        assert "appimage" in targets


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  X3 — cargo-dist adapter resolution against src-tauri/
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestX3CargoDistAdapter:
    def test_cargo_dist_adapter_validates_src_tauri(self, project_dir):
        render_project(project_dir, _default_opts())
        adapter = CargoDistAdapter(name="tauri_pilot", version="0.1.0")
        source = BuildSource(path=project_dir / "src-tauri")
        source.validate()
        adapter._validate_source(source)

    def test_dry_run_reports_cargo_dist(self, project_dir):
        render_project(project_dir, _default_opts())
        res = dry_run_build(project_dir, _default_opts())
        assert res["cargo-dist"]["adapter"] == "CargoDistAdapter"
        assert res["cargo-dist"]["artifact_valid"] is True
        # The adapter MUST point at src-tauri/, not the project root.
        assert res["cargo-dist"]["config"].endswith("src-tauri/Cargo.toml")
        assert res["src_tauri_dir"].endswith("src-tauri")

    def test_dist_workspace_toml_present(self, project_dir):
        render_project(project_dir, _default_opts())
        assert (project_dir / "src-tauri" / "dist-workspace.toml").is_file()

    def test_workspace_metadata_dist_block_present(self, project_dir):
        render_project(project_dir, _default_opts())
        cargo = (project_dir / "src-tauri" / "Cargo.toml").read_text()
        assert "[workspace.metadata.dist]" in cargo
        assert "cargo-dist-version" in cargo


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  X4 — two-ecosystem software compliance
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestX4Compliance:
    def test_spdx_allowlist_denies_gpl_by_default(self, project_dir):
        render_project(project_dir, _default_opts())
        allow = json.loads(
            (project_dir / "spdx.allowlist.json").read_text(),
        )
        assert "GPL-3.0" in allow["denied_licenses"]
        assert "AGPL-3.0" in allow["denied_licenses"]
        assert "Apache-2.0" in allow["allowed_licenses"]
        assert "MIT" in allow["allowed_licenses"]

    def test_deny_toml_under_src_tauri(self, project_dir):
        render_project(project_dir, _default_opts())
        deny = (project_dir / "src-tauri" / "deny.toml").read_text()
        assert "[licenses]" in deny
        assert "Apache-2.0" in deny
        assert "GPL-3.0" in deny

    def test_ecosystem_root_is_npm(self, project_dir):
        render_project(project_dir, _default_opts())
        assert detect_ecosystem(project_dir) == "npm"

    def test_ecosystem_src_tauri_is_cargo(self, project_dir):
        render_project(project_dir, _default_opts())
        assert detect_ecosystem(project_dir / "src-tauri") == "cargo"

    def test_pilot_report_runs_x4_for_both_ecosystems(self, project_dir):
        render_project(project_dir, _default_opts())
        report = pilot_report(project_dir, _default_opts())
        bundle = report["x4_compliance"]
        assert "npm" in bundle
        assert "cargo" in bundle
        for eco in ("npm", "cargo"):
            gate_ids = {g["gate_id"] for g in bundle[eco]["gates"]}
            assert gate_ids == {"license", "cve", "sbom"}, (
                f"{eco} gates: {gate_ids}"
            )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pilot report — one-shot X0-X4 roll-up
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestPilotReport:
    def test_pilot_report_shape(self, project_dir):
        render_project(project_dir, _default_opts())
        report = pilot_report(project_dir, _default_opts())
        assert report["skill"] == "skill-desktop-tauri"
        assert report["x0_profile"] == "linux-x86_64-native"
        assert report["x1_coverage_floor_rust"] == 75.0
        assert report["x1_coverage_floor_frontend"] == 80.0
        assert "x3_build" in report
        assert "x4_compliance" in report

    def test_pilot_report_is_json_safe(self, project_dir):
        render_project(project_dir, _default_opts())
        report = pilot_report(project_dir, _default_opts())
        json.dumps(report)

    def test_pilot_report_options_round_trip(self, project_dir):
        render_project(
            project_dir,
            _default_opts(bin_name="customcli", identifier="com.acme.cli"),
        )
        report = pilot_report(
            project_dir,
            _default_opts(bin_name="customcli", identifier="com.acme.cli"),
        )
        assert report["options"]["bin_name"] == "customcli"
        assert report["options"]["identifier"] == "com.acme.cli"

    def test_pilot_report_records_two_ecosystems(self, project_dir):
        render_project(project_dir, _default_opts())
        report = pilot_report(project_dir, _default_opts())
        assert report["x4_ecosystems_detected"]["root"] == "npm"
        assert report["x4_ecosystems_detected"]["src_tauri"] == "cargo"
