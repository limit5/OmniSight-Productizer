"""X7 #303 — SKILL-RUST-CLI contract tests.

SKILL-RUST-CLI is the third software-vertical skill pack and
re-exercises the X0-X4 framework on a Rust toolchain (cargo + rustc
+ cargo-dist). These tests lock the framework invariants the same
way ``test_skill_fastapi.py`` locks X5 and ``test_skill_go_service.py``
locks X6:

* **X0** — ``target_kind=software`` profile loads cleanly for the
  ``linux-x86_64-native`` binding the scaffold defaults to.
* **X1** — ``scripts/check_cov.sh`` pins the X1 Rust threshold
  (``COVERAGE_THRESHOLDS["rust"] == 75.0`` in
  ``backend.software_simulator``).
* **X2** — rendered project honours cli-tooling role defaults:
  clap derive (not raw std::env::args), anyhow for main-level
  errors, ``--version``/``--help``/``--json``/exit-code 0/1/2/130
  contract, tracing-subscriber on stderr, is_terminal ANSI gating.
* **X3** — ``CargoDistAdapter`` accepts the rendered tree without
  hitting the network.
* **X4** — ``software_compliance.run_all`` bundle runs against the
  rendered project (ecosystem=cargo via Cargo.toml) and produces a
  structured verdict.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from backend.build_adapters import BuildSource, CargoDistAdapter
from backend.platform import get_platform_config
from backend.rust_cli_scaffolder import (
    ScaffoldOptions,
    _SCAFFOLDS_DIR,
    _SKILL_DIR,
    _slugify_bin,
    _slugify_crate,
    dry_run_build,
    pilot_report,
    render_project,
    validate_pack,
)
from backend.skill_registry import get_skill, list_skills, validate_skill
from backend.software_compliance.licenses import detect_ecosystem
from backend.software_simulator import COVERAGE_THRESHOLDS


@pytest.fixture
def project_dir():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp) / "rust-pilot"


def _default_opts(**overrides) -> ScaffoldOptions:
    kwargs = dict(
        project_name="rust-pilot",
        runtime="tokio",
        completions=True,
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
        assert "skill-rust-cli" in names

    def test_pack_validates_clean(self):
        result = validate_skill("skill-rust-cli")
        assert result.ok, (
            f"skill-rust-cli validation failed: "
            f"{[(i.level, i.message) for i in result.issues]}"
        )

    def test_all_five_artifact_kinds_declared(self):
        info = get_skill("skill-rust-cli")
        assert info is not None
        assert info.artifact_kinds == {"tasks", "scaffolds", "tests", "hil", "docs"}

    def test_manifest_declares_core_dependencies(self):
        info = get_skill("skill-rust-cli")
        assert info is not None and info.manifest is not None
        assert "CORE-05" in info.manifest.depends_on_core
        assert info.manifest.depends_on_skills == []

    def test_manifest_keywords_include_rust_markers(self):
        info = get_skill("skill-rust-cli")
        assert info and info.manifest
        kws = set(info.manifest.keywords)
        assert {"rust", "cli", "clap", "cargo-dist", "x7"}.issubset(kws)

    def test_validate_pack_helper(self):
        result = validate_pack()
        assert result["installed"] is True
        assert result["ok"] is True
        assert result["skill_name"] == "skill-rust-cli"

    def test_skill_dir_resolution(self):
        assert _SKILL_DIR.is_dir()
        assert (_SKILL_DIR / "skill.yaml").is_file()
        assert (_SKILL_DIR / "tasks.yaml").is_file()
        assert (_SKILL_DIR / "SKILL.md").is_file()
        assert _SCAFFOLDS_DIR.is_dir()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Slug helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestSlugifyHelpers:
    def test_bin_preserves_hyphens(self):
        assert _slugify_bin("my-cli-tool") == "my-cli-tool"

    def test_bin_lowercases(self):
        assert _slugify_bin("MyApp") == "myapp"

    def test_bin_strips_non_alpha(self):
        assert _slugify_bin("foo/bar@baz") == "foo-bar-baz"

    def test_bin_empty_fallback(self):
        assert _slugify_bin("") == "app"
        assert _slugify_bin("---") == "app"

    def test_crate_underscorifies(self):
        assert _slugify_crate("my-cli-tool") == "my_cli_tool"

    def test_crate_rejects_dots(self):
        assert _slugify_crate("foo.bar") == "foo_bar"

    def test_crate_empty_fallback(self):
        assert _slugify_crate("") == "app"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Scaffold option validation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestScaffoldOptions:
    def test_defaults_validate(self):
        _default_opts().validate()

    def test_bad_runtime_rejected(self):
        with pytest.raises(ValueError, match="runtime"):
            _default_opts(runtime="async-std").validate()

    def test_empty_project_name_rejected(self):
        with pytest.raises(ValueError, match="project_name"):
            _default_opts(project_name="   ").validate()

    def test_default_bin_name(self):
        opts = _default_opts(project_name="Acme-CLI")
        assert opts.resolved_bin_name() == "acme-cli"

    def test_default_crate_name_derives_from_bin(self):
        opts = _default_opts(project_name="Acme-CLI")
        assert opts.resolved_crate_name() == "acme_cli"

    def test_explicit_bin_name_wins(self):
        opts = _default_opts(bin_name="custombin")
        assert opts.resolved_bin_name() == "custombin"

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
            "Cargo.toml",
            "rust-toolchain.toml",
            "dist-workspace.toml",
            "build.rs",
            "Makefile",
            "README.md",
            "rustfmt.toml",
            "clippy.toml",
            "deny.toml",
            "spdx.allowlist.json",
            ".env.example",
            ".gitignore",
            "src/main.rs",
            "src/cli.rs",
            "src/error.rs",
            "src/logging.rs",
            "src/commands/mod.rs",
            "src/commands/init.rs",
            "src/commands/run.rs",
            "src/commands/status.rs",
            "src/commands/version.rs",
            "src/commands/completions.rs",
            "tests/cli_integration.rs",
            "scripts/check_cov.sh",
        ]
        for rel in required:
            assert (project_dir / rel).is_file(), f"missing: {rel}"

    def test_cov_script_is_executable(self, project_dir):
        render_project(project_dir, _default_opts())
        script = project_dir / "scripts" / "check_cov.sh"
        assert script.stat().st_mode & 0o111, "check_cov.sh must be executable"

    def test_completions_off_drops_subcommand_file(self, project_dir):
        render_project(project_dir, _default_opts(completions=False))
        assert not (project_dir / "src" / "commands" / "completions.rs").exists()

    def test_completions_off_drops_clap_complete_dep(self, project_dir):
        render_project(project_dir, _default_opts(completions=False))
        cargo = (project_dir / "Cargo.toml").read_text(encoding="utf-8")
        assert "clap_complete" not in cargo

    def test_completions_off_drops_module_declaration(self, project_dir):
        render_project(project_dir, _default_opts(completions=False))
        mod_rs = (project_dir / "src" / "commands" / "mod.rs").read_text()
        assert "pub mod completions" not in mod_rs

    def test_compliance_off_skips_spdx_and_deny(self, project_dir):
        render_project(project_dir, _default_opts(compliance=False))
        assert not (project_dir / "spdx.allowlist.json").exists()
        assert not (project_dir / "deny.toml").exists()

    def test_idempotent_re_render(self, project_dir):
        o1 = render_project(project_dir, _default_opts())
        o2 = render_project(project_dir, _default_opts())
        assert o1.files_written == o2.files_written
        assert o1.bytes_written == o2.bytes_written

    def test_overwrite_false_warns_on_existing(self, project_dir):
        render_project(project_dir, _default_opts())
        outcome = render_project(project_dir, _default_opts(), overwrite=False)
        assert outcome.warnings
        assert all("skipped existing" in w for w in outcome.warnings)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Runtime knob — tokio vs sync
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestRuntimeKnob:
    def test_tokio_mentions_tokio_main(self, project_dir):
        render_project(project_dir, _default_opts(runtime="tokio"))
        main = (project_dir / "src" / "main.rs").read_text()
        assert "#[tokio::main" in main
        assert "tokio::select!" in main

    def test_sync_omits_tokio_main(self, project_dir):
        render_project(project_dir, _default_opts(runtime="sync"))
        main = (project_dir / "src" / "main.rs").read_text()
        assert "#[tokio::main" not in main
        assert "run_sync" in main

    def test_tokio_in_cargo_for_tokio(self, project_dir):
        render_project(project_dir, _default_opts(runtime="tokio"))
        cargo = (project_dir / "Cargo.toml").read_text()
        assert "tokio = " in cargo

    def test_tokio_absent_from_cargo_for_sync(self, project_dir):
        render_project(project_dir, _default_opts(runtime="sync"))
        cargo = (project_dir / "Cargo.toml").read_text()
        assert "tokio = " not in cargo

    def test_bin_name_injected_into_cargo(self, project_dir):
        render_project(project_dir, _default_opts(bin_name="mytool"))
        cargo = (project_dir / "Cargo.toml").read_text()
        assert 'name = "mytool"' in cargo

    def test_env_prefix_replaces_hyphens_with_underscores(self, project_dir):
        # Bin names may carry hyphens (Cargo bin names allow them),
        # but env var identifiers cannot — the env_prefix must collapse
        # them to underscores so `{{ env_prefix }}_INPUT` is a valid
        # shell variable name.
        render_project(project_dir, _default_opts(bin_name="my-cli"))
        env_example = (project_dir / ".env.example").read_text()
        assert "MY_CLI_INPUT=" in env_example
        assert "MY-CLI_INPUT" not in env_example
        run_rs = (project_dir / "src" / "commands" / "run.rs").read_text()
        assert 'env = "MY_CLI_INPUT"' in run_rs


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

    def test_cargo_dist_has_5_targets(self, project_dir):
        render_project(project_dir, _default_opts())
        cargo = (project_dir / "Cargo.toml").read_text()
        # 3 OSes × arch variants per the X0 software profile family.
        for triple in (
            "x86_64-unknown-linux-gnu",
            "aarch64-unknown-linux-gnu",
            "x86_64-pc-windows-msvc",
            "aarch64-apple-darwin",
            "x86_64-apple-darwin",
        ):
            assert triple in cargo, f"missing cargo-dist target: {triple}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  X1 — Rust coverage floor
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestX1CoverageThreshold:
    def test_x1_rust_threshold_is_75(self):
        assert COVERAGE_THRESHOLDS["rust"] == 75.0

    def test_check_cov_defaults_to_75(self, project_dir):
        render_project(project_dir, _default_opts())
        script = (project_dir / "scripts" / "check_cov.sh").read_text()
        assert "COVERAGE_THRESHOLD:-75" in script

    def test_makefile_wires_coverage_script(self, project_dir):
        render_project(project_dir, _default_opts())
        makefile = (project_dir / "Makefile").read_text()
        assert "./scripts/check_cov.sh" in makefile


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  X2 — cli-tooling role alignment
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestX2RoleAlignment:
    def test_uses_clap_derive_not_raw_argv(self, project_dir):
        render_project(project_dir, _default_opts())
        cli = (project_dir / "src" / "cli.rs").read_text()
        assert "use clap::{Parser, Subcommand}" in cli
        # cli-tooling role anti-pattern: hand-rolled argv parsing.
        main = (project_dir / "src" / "main.rs").read_text()
        assert "std::env::args" not in main

    def test_version_contract_name_semver_sha(self, project_dir):
        render_project(project_dir, _default_opts())
        cli = (project_dir / "src" / "cli.rs").read_text()
        # cli-tooling role rule #1: `--version` prints
        # `{name} {semver} ({git_sha})`.
        assert 'concat!(env!("CARGO_PKG_VERSION")' in cli
        assert 'env!("BUILD_GIT_SHA")' in cli

    def test_build_rs_injects_git_sha(self, project_dir):
        render_project(project_dir, _default_opts())
        build = (project_dir / "build.rs").read_text()
        assert "rustc-env=BUILD_GIT_SHA" in build
        assert "git" in build and "rev-parse" in build

    def test_anyhow_in_main(self, project_dir):
        render_project(project_dir, _default_opts())
        main = (project_dir / "src" / "main.rs").read_text()
        assert "anyhow::Result" in main

    def test_json_flag_is_global(self, project_dir):
        render_project(project_dir, _default_opts())
        cli = (project_dir / "src" / "cli.rs").read_text()
        # cli-tooling role rule #3: --json is the machine-readable
        # switch — global so every subcommand honours it.
        assert "pub json: bool" in cli
        assert 'long, global = true' in cli

    def test_tracing_writes_to_stderr(self, project_dir):
        render_project(project_dir, _default_opts())
        log = (project_dir / "src" / "logging.rs").read_text()
        # cli-tooling role rule: logs → stderr, data → stdout.
        assert "std::io::stderr" in log
        # backend-rust role default: tracing, not log crate.
        assert "tracing" in log

    def test_is_terminal_gates_ansi(self, project_dir):
        render_project(project_dir, _default_opts())
        log = (project_dir / "src" / "logging.rs").read_text()
        # cli-tooling role anti-pattern: hard-coded ANSI escapes.
        assert "is_terminal" in log or "IsTerminal" in log

    def test_exit_codes_documented(self, project_dir):
        render_project(project_dir, _default_opts())
        main = (project_dir / "src" / "main.rs").read_text()
        # The exit-code comment header is the evidence operators read
        # first when auditing the CLI contract.
        assert "130" in main
        assert "ExitCode::from(130)" in main
        assert "ExitCode::from(1)" in main

    def test_cargo_toml_pins_msrv_and_edition(self, project_dir):
        render_project(project_dir, _default_opts())
        cargo = (project_dir / "Cargo.toml").read_text()
        assert 'edition = "2021"' in cargo
        assert 'rust-version = "1.76"' in cargo

    def test_release_profile_optimised_for_size(self, project_dir):
        render_project(project_dir, _default_opts())
        cargo = (project_dir / "Cargo.toml").read_text()
        # backend-rust role "CLI ≤ 8 MiB" budget requires these three.
        assert 'lto = "fat"' in cargo
        assert "strip = true" in cargo
        assert "codegen-units = 1" in cargo


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  X3 — cargo-dist adapter resolution
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestX3CargoDistAdapter:
    def test_cargo_dist_adapter_validates_rendered_tree(self, project_dir):
        render_project(project_dir, _default_opts())
        adapter = CargoDistAdapter(name="rust-pilot", version="0.1.0")
        source = BuildSource(path=project_dir)
        source.validate()
        adapter._validate_source(source)

    def test_dry_run_reports_cargo_dist(self, project_dir):
        render_project(project_dir, _default_opts())
        res = dry_run_build(project_dir, _default_opts())
        assert res["cargo-dist"]["adapter"] == "CargoDistAdapter"
        assert res["cargo-dist"]["artifact_valid"] is True

    def test_dist_workspace_toml_present(self, project_dir):
        render_project(project_dir, _default_opts())
        assert (project_dir / "dist-workspace.toml").is_file()

    def test_workspace_metadata_dist_block_present(self, project_dir):
        render_project(project_dir, _default_opts())
        cargo = (project_dir / "Cargo.toml").read_text()
        assert "[workspace.metadata.dist]" in cargo
        assert "cargo-dist-version" in cargo


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  X4 — software compliance
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestX4Compliance:
    def test_spdx_allowlist_denies_gpl_by_default(self, project_dir):
        render_project(project_dir, _default_opts())
        import json
        allow = json.loads(
            (project_dir / "spdx.allowlist.json").read_text(encoding="utf-8")
        )
        assert "GPL-3.0" in allow["denied_licenses"]
        assert "AGPL-3.0" in allow["denied_licenses"]
        assert "Apache-2.0" in allow["allowed_licenses"]
        assert "MIT" in allow["allowed_licenses"]

    def test_deny_toml_has_license_block(self, project_dir):
        render_project(project_dir, _default_opts())
        deny = (project_dir / "deny.toml").read_text()
        assert "[licenses]" in deny
        assert "Apache-2.0" in deny
        assert 'GPL-3.0' in deny

    def test_ecosystem_detected_as_cargo(self, project_dir):
        render_project(project_dir, _default_opts())
        assert detect_ecosystem(project_dir) == "cargo"

    def test_pilot_report_runs_x4_bundle(self, project_dir):
        render_project(project_dir, _default_opts())
        report = pilot_report(project_dir, _default_opts())
        bundle = report["x4_compliance"]
        assert "gates" in bundle
        gate_ids = {g["gate_id"] for g in bundle["gates"]}
        assert gate_ids == {"license", "cve", "sbom"}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pilot report — one-shot X0-X4 roll-up
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestPilotReport:
    def test_pilot_report_shape(self, project_dir):
        render_project(project_dir, _default_opts())
        report = pilot_report(project_dir, _default_opts())
        assert report["skill"] == "skill-rust-cli"
        assert report["x0_profile"] == "linux-x86_64-native"
        assert report["x1_coverage_floor"] == 75.0
        assert "x3_build" in report
        assert "x4_compliance" in report

    def test_pilot_report_is_json_safe(self, project_dir):
        import json
        render_project(project_dir, _default_opts())
        report = pilot_report(project_dir, _default_opts())
        json.dumps(report)

    def test_pilot_report_options_round_trip(self, project_dir):
        render_project(project_dir, _default_opts(bin_name="customcli"))
        report = pilot_report(project_dir, _default_opts(bin_name="customcli"))
        assert report["options"]["bin_name"] == "customcli"
        assert report["options"]["crate_name"] == "customcli"
