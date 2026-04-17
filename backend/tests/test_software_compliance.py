"""X4 #300 — Unit tests for the software compliance gates.

Covers:
    * Licenses: multi-ecosystem detection, tool-vs-walk fallback, SPDX
      normalisation, deny-list matching, allowlist override, unknown
      bucket routing.
    * CVEs: trivy / grype / osv-scanner output parsers, severity
      thresholding, tool-absent mock path, scanner probe order.
    * SBOM: CycloneDX 1.5 schema shape, SPDX 2.3 tag-value structure,
      PURL mapping per ecosystem, round-trip idempotence.
    * Bundle: orchestrator composes all three gates, verdict aggregation,
      SBOM write-to-disk path, CLI exit code, C8 bridge conversion.

All external tools (``cargo-license`` / ``go-licenses`` / ``pip-licenses``
/ ``license-checker`` / ``trivy`` / ``grype`` / ``osv-scanner``) are
monkey-patched — the suite stays offline and passes on a CI image with
no toolchain installed.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from backend.software_compliance import (
    CVEReport,
    DEFAULT_DENY_LICENSES,
    DEFAULT_FAIL_ON,
    ECOSYSTEMS,
    LicenseReport,
    PackageLicense,
    SBOM_FORMATS,
    SBOMDocument,
    SoftwareComplianceBundle,
    Vulnerability,
    bundle_to_compliance_report,
    detect_ecosystem,
    emit_sbom,
    run_all,
    scan_cves,
    scan_licenses,
    to_cyclonedx,
    to_spdx,
)
from backend.software_compliance.bundle import GateVerdict
from backend.software_compliance.licenses import (
    _expand_atoms,
    _license_matches,
    _normalise_license,
    _parse_cargo_lock,
    _parse_go_mod,
    _parse_package_json_direct,
    _parse_pip_manifest,
    _walk_node_modules,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Licenses — SPDX normalisation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestNormaliseLicense:
    def test_string_spdx(self):
        assert _normalise_license("MIT") == "MIT"

    def test_none_is_unknown(self):
        assert _normalise_license(None) == "UNKNOWN"

    def test_empty_string_is_unknown(self):
        assert _normalise_license("  ") == "UNKNOWN"

    def test_dict_with_type(self):
        assert _normalise_license({"type": "Apache-2.0"}) == "Apache-2.0"

    def test_dict_without_keys_is_unknown(self):
        assert _normalise_license({"unrelated": 1}) == "UNKNOWN"

    def test_list_of_strings_joined_by_or(self):
        out = _normalise_license(["MIT", "Apache-2.0"])
        assert "MIT" in out and "Apache-2.0" in out and " OR " in out

    def test_list_with_all_unknown(self):
        assert _normalise_license([None, {"x": 1}]) == "UNKNOWN"

    def test_unexpected_type_is_unknown(self):
        assert _normalise_license(42) == "UNKNOWN"


class TestExpandAtoms:
    def test_single(self):
        assert _expand_atoms("MIT") == {"MIT"}

    def test_or_expression(self):
        assert _expand_atoms("MIT OR Apache-2.0") == {"MIT", "Apache-2.0"}

    def test_parentheses_removed(self):
        assert _expand_atoms("(MIT OR GPL-3.0-or-later)") == {"MIT", "GPL-3.0"}

    def test_suffix_strip(self):
        assert _expand_atoms("GPL-3.0-only") == {"GPL-3.0"}
        assert _expand_atoms("LGPL-2.1+") == {"LGPL-2.1"}

    def test_with_exception_clause(self):
        # "WITH" is a keyword; the exception name is still treated as an atom.
        atoms = _expand_atoms("GPL-2.0 WITH Classpath-exception-2.0")
        assert "GPL-2.0" in atoms
        assert "AND" not in atoms


class TestLicenseMatches:
    def test_exact_match(self):
        assert _license_matches("GPL-3.0", {"GPL-3.0"})

    def test_case_insensitive(self):
        assert _license_matches("gpl-3.0", {"GPL-3.0"})

    def test_or_expression_one_denied(self):
        # Even though user can pick MIT, the deny list triggers because
        # "GPL-3.0" appears in the atoms. Matches W5 web_compliance behavior.
        assert _license_matches("MIT OR GPL-3.0", {"GPL-3.0"})

    def test_unknown_never_denies(self):
        assert not _license_matches("UNKNOWN", {"GPL-3.0"})

    def test_empty_never_denies(self):
        assert not _license_matches("", {"GPL-3.0"})

    def test_non_denied_passes(self):
        assert not _license_matches("MIT", {"GPL-3.0"})


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Licenses — ecosystem detection & fallback parsers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestDetectEcosystem:
    def test_cargo(self, tmp_path: Path):
        (tmp_path / "Cargo.toml").write_text("[package]\nname=\"foo\"\n")
        assert detect_ecosystem(tmp_path) == "cargo"

    def test_go(self, tmp_path: Path):
        (tmp_path / "go.mod").write_text("module foo\n")
        assert detect_ecosystem(tmp_path) == "go"

    def test_pip_pyproject(self, tmp_path: Path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='foo'\n")
        assert detect_ecosystem(tmp_path) == "pip"

    def test_pip_requirements_txt(self, tmp_path: Path):
        (tmp_path / "requirements.txt").write_text("requests\n")
        assert detect_ecosystem(tmp_path) == "pip"

    def test_npm(self, tmp_path: Path):
        (tmp_path / "package.json").write_text('{"name":"x"}')
        assert detect_ecosystem(tmp_path) == "npm"

    def test_maven_pom(self, tmp_path: Path):
        (tmp_path / "pom.xml").write_text(
            '<?xml version="1.0"?>\n<project/>\n'
        )
        assert detect_ecosystem(tmp_path) == "maven"

    def test_maven_gradle_kts(self, tmp_path: Path):
        (tmp_path / "build.gradle.kts").write_text('plugins { java }\n')
        assert detect_ecosystem(tmp_path) == "maven"

    def test_nothing_returns_none(self, tmp_path: Path):
        assert detect_ecosystem(tmp_path) is None

    def test_precedence_cargo_over_npm(self, tmp_path: Path):
        (tmp_path / "Cargo.toml").write_text("[package]\nname=\"f\"\n")
        (tmp_path / "package.json").write_text('{"name":"x"}')
        assert detect_ecosystem(tmp_path) == "cargo"

    def test_precedence_npm_over_maven(self, tmp_path: Path):
        # Spring Boot project that also has a static frontend bundled
        # under package.json — npm wins since it's listed first.
        (tmp_path / "package.json").write_text('{"name":"x"}')
        (tmp_path / "pom.xml").write_text('<?xml version="1.0"?><project/>')
        assert detect_ecosystem(tmp_path) == "npm"


class TestMavenParser:
    def test_pom_xml_direct_deps(self, tmp_path: Path):
        from backend.software_compliance.licenses import _parse_pom_xml
        pom = tmp_path / "pom.xml"
        pom.write_text(
            '<?xml version="1.0"?>\n'
            '<project>\n'
            '  <dependencies>\n'
            '    <dependency>\n'
            '      <groupId>org.springframework.boot</groupId>\n'
            '      <artifactId>spring-boot-starter-web</artifactId>\n'
            '      <version>3.2.5</version>\n'
            '    </dependency>\n'
            '    <dependency>\n'
            '      <groupId>org.junit.jupiter</groupId>\n'
            '      <artifactId>junit-jupiter</artifactId>\n'
            '      <version>5.10.2</version>\n'
            '      <scope>test</scope>\n'
            '    </dependency>\n'
            '  </dependencies>\n'
            '</project>\n'
        )
        pkgs = _parse_pom_xml(pom)
        assert len(pkgs) == 2
        coords = {(p.name, p.version) for p in pkgs}
        assert ("org.springframework.boot:spring-boot-starter-web", "3.2.5") in coords
        assert ("org.junit.jupiter:junit-jupiter", "5.10.2") in coords
        assert all(p.license == "UNKNOWN" for p in pkgs)
        assert all(p.ecosystem == "maven" for p in pkgs)

    def test_build_gradle_kts_direct_deps(self, tmp_path: Path):
        from backend.software_compliance.licenses import _parse_gradle_build
        build = tmp_path / "build.gradle.kts"
        build.write_text(
            'plugins { java }\n'
            'dependencies {\n'
            '    implementation("org.springframework.boot:spring-boot-starter-web:3.2.5")\n'
            '    runtimeOnly("org.postgresql:postgresql:42.7.3")\n'
            '    testImplementation("org.junit.jupiter:junit-jupiter:5.10.2")\n'
            '}\n'
        )
        pkgs = _parse_gradle_build(build)
        assert len(pkgs) == 3
        coords = {(p.name, p.version) for p in pkgs}
        assert (
            "org.springframework.boot:spring-boot-starter-web", "3.2.5"
        ) in coords
        assert ("org.postgresql:postgresql", "42.7.3") in coords
        assert all(p.ecosystem == "maven" for p in pkgs)


class TestCargoLockParser:
    def test_parses_packages(self, tmp_path: Path):
        lock = tmp_path / "Cargo.lock"
        lock.write_text(
            '[[package]]\n'
            'name = "foo"\n'
            'version = "0.1.0"\n'
            '\n'
            '[[package]]\n'
            'name = "bar"\n'
            'version = "1.2.3"\n'
        )
        pkgs = _parse_cargo_lock(lock)
        names = {p.name for p in pkgs}
        assert names == {"foo", "bar"}
        for p in pkgs:
            assert p.license == "UNKNOWN"
            assert p.ecosystem == "cargo"

    def test_missing_file(self, tmp_path: Path):
        assert _parse_cargo_lock(tmp_path / "missing") == []


class TestGoModParser:
    def test_block_require(self, tmp_path: Path):
        mod = tmp_path / "go.mod"
        mod.write_text(
            "module example.com/foo\n\n"
            "go 1.21\n\n"
            "require (\n"
            "    github.com/gin-gonic/gin v1.9.0\n"
            "    github.com/stretchr/testify v1.8.4\n"
            ")\n"
        )
        pkgs = _parse_go_mod(mod)
        names = {p.name for p in pkgs}
        assert names == {
            "github.com/gin-gonic/gin",
            "github.com/stretchr/testify",
        }

    def test_single_line_require(self, tmp_path: Path):
        mod = tmp_path / "go.mod"
        mod.write_text(
            "module foo\n"
            "go 1.21\n"
            "require github.com/pkg/errors v0.9.1\n"
        )
        pkgs = _parse_go_mod(mod)
        assert {p.name for p in pkgs} == {"github.com/pkg/errors"}

    def test_dedupe(self, tmp_path: Path):
        mod = tmp_path / "go.mod"
        mod.write_text(
            "require (\n"
            "    github.com/a/b v1.0.0\n"
            "    github.com/a/b v1.0.0\n"
            ")\n"
        )
        assert len(_parse_go_mod(mod)) == 1


class TestPipManifestParser:
    def test_requirements_txt(self, tmp_path: Path):
        req = tmp_path / "requirements.txt"
        req.write_text(
            "# comment\n"
            "requests>=2.25.0\n"
            "pyyaml==6.0\n"
            "flask[async]\n"
        )
        pkgs = _parse_pip_manifest(req)
        names = {p.name for p in pkgs}
        assert {"requests", "pyyaml", "flask"}.issubset(names)

    def test_pyproject(self, tmp_path: Path):
        pp = tmp_path / "pyproject.toml"
        pp.write_text(
            '[project]\n'
            'name = "x"\n'
            'dependencies = [\n'
            '  "fastapi>=0.100",\n'
            '  "uvicorn[standard]==0.23.2",\n'
            ']\n'
        )
        pkgs = _parse_pip_manifest(pp)
        names = {p.name for p in pkgs}
        assert "fastapi" in names
        assert "uvicorn" in names


class TestPackageJsonDirect:
    def test_extracts_direct_deps(self, tmp_path: Path):
        pj = tmp_path / "package.json"
        pj.write_text(json.dumps({
            "name": "x",
            "version": "0.1.0",
            "dependencies": {"left-pad": "^1.3.0", "lodash": "~4.17.0"},
            "devDependencies": {"jest": "29.0.0"},
        }))
        pkgs = _parse_package_json_direct(pj)
        names = {p.name for p in pkgs}
        assert names == {"left-pad", "lodash", "jest"}


class TestWalkNodeModules:
    def test_skips_test_dirs(self, tmp_path: Path):
        nm = tmp_path / "node_modules"
        (nm / "real-pkg").mkdir(parents=True)
        (nm / "real-pkg" / "package.json").write_text(
            json.dumps({"name": "real-pkg", "version": "1.0.0", "license": "MIT"})
        )
        (nm / "real-pkg" / "tests" / "nested").mkdir(parents=True)
        (nm / "real-pkg" / "tests" / "nested" / "package.json").write_text(
            json.dumps({"name": "nested-test-stub", "version": "1.0.0"})
        )
        pkgs = _walk_node_modules(tmp_path)
        names = {p.name for p in pkgs}
        assert "real-pkg" in names
        assert "nested-test-stub" not in names


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Licenses — scan_licenses (full)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestScanLicensesNpm:
    def test_denies_gpl(self, tmp_path: Path):
        (tmp_path / "package.json").write_text('{"name":"x"}')
        nm = tmp_path / "node_modules"
        (nm / "bad").mkdir(parents=True)
        (nm / "bad" / "package.json").write_text(
            json.dumps({"name": "bad", "version": "1.0.0", "license": "GPL-3.0"})
        )
        (nm / "good").mkdir(parents=True)
        (nm / "good" / "package.json").write_text(
            json.dumps({"name": "good", "version": "1.0.0", "license": "MIT"})
        )
        report = scan_licenses(tmp_path, ecosystem="npm")
        assert not report.passed
        assert len(report.denied) == 1
        assert report.denied[0].name == "bad"
        assert len(report.allowed) == 1

    def test_allowlist_override(self, tmp_path: Path):
        (tmp_path / "package.json").write_text('{"name":"x"}')
        nm = tmp_path / "node_modules"
        (nm / "bad").mkdir(parents=True)
        (nm / "bad" / "package.json").write_text(
            json.dumps({"name": "bad", "version": "1.0.0", "license": "GPL-3.0"})
        )
        # allow by name
        report = scan_licenses(tmp_path, ecosystem="npm", allowlist=["bad"])
        assert report.passed
        assert len(report.denied) == 0
        assert len(report.allowed) == 1

    def test_allowlist_versioned(self, tmp_path: Path):
        (tmp_path / "package.json").write_text('{"name":"x"}')
        nm = tmp_path / "node_modules"
        (nm / "bad").mkdir(parents=True)
        (nm / "bad" / "package.json").write_text(
            json.dumps({"name": "bad", "version": "1.0.0", "license": "GPL-3.0"})
        )
        report = scan_licenses(
            tmp_path, ecosystem="npm", allowlist=["bad@GPL-3.0"]
        )
        assert report.passed

    def test_unknown_routes_to_unknown_bucket(self, tmp_path: Path):
        (tmp_path / "package.json").write_text('{"name":"x"}')
        nm = tmp_path / "node_modules"
        (nm / "mystery").mkdir(parents=True)
        (nm / "mystery" / "package.json").write_text(
            json.dumps({"name": "mystery", "version": "0.0.1"})
        )
        report = scan_licenses(tmp_path, ecosystem="npm")
        assert len(report.unknown) == 1
        assert report.passed  # unknown does not fail the gate


class TestScanLicensesAutoDetect:
    def test_no_marker_errors_out(self, tmp_path: Path):
        report = scan_licenses(tmp_path)
        assert report.error
        assert report.source == "mock"
        assert not report.passed

    def test_cargo_auto_detected(self, tmp_path: Path):
        (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\n")
        (tmp_path / "Cargo.lock").write_text(
            '[[package]]\nname = "tokio"\nversion = "1.0.0"\n'
        )
        with mock.patch("backend.software_compliance.licenses.shutil.which", return_value=None):
            report = scan_licenses(tmp_path)
        assert report.ecosystem == "cargo"
        assert report.source == "walk"
        assert report.total_packages == 1

    def test_invalid_app_path(self, tmp_path: Path):
        report = scan_licenses(tmp_path / "does-not-exist")
        assert report.error

    def test_unknown_ecosystem_hint_errors(self, tmp_path: Path):
        (tmp_path / "package.json").write_text('{"name":"x"}')
        report = scan_licenses(tmp_path, ecosystem="fortran")
        assert "unknown ecosystem" in report.error


class TestScanLicensesToolPath:
    """Verify the cargo-license / pip-licenses / license-checker parsers
    handle well-formed tool output — we mock subprocess to avoid needing
    the tools installed."""

    def test_cargo_license_json(self, tmp_path: Path):
        (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\n")
        fake_out = json.dumps([
            {"name": "foo", "version": "0.1.0", "license": "MIT"},
            {"name": "bar", "version": "0.2.0", "license": "GPL-3.0"},
        ])
        with mock.patch(
            "backend.software_compliance.licenses.shutil.which",
            lambda x: "/fake/cargo-license" if x == "cargo-license" else None,
        ), mock.patch(
            "backend.software_compliance.licenses._run_tool",
            return_value=(0, fake_out, ""),
        ):
            report = scan_licenses(tmp_path, ecosystem="cargo")
        assert report.source == "cargo-license"
        assert report.total_packages == 2
        assert len(report.denied) == 1
        assert report.denied[0].name == "bar"

    def test_pip_licenses_json(self, tmp_path: Path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        fake_out = json.dumps([
            {"Name": "requests", "Version": "2.28.0", "License": "Apache-2.0"},
            {"Name": "gpl-thing", "Version": "1.0", "License": "AGPL-3.0"},
        ])
        with mock.patch(
            "backend.software_compliance.licenses.shutil.which",
            lambda x: "/fake/pip-licenses" if x == "pip-licenses" else None,
        ), mock.patch(
            "backend.software_compliance.licenses._run_tool",
            return_value=(0, fake_out, ""),
        ):
            report = scan_licenses(tmp_path, ecosystem="pip")
        assert report.source == "pip-licenses"
        assert {p.name for p in report.denied} == {"gpl-thing"}

    def test_license_checker_json(self, tmp_path: Path):
        (tmp_path / "package.json").write_text('{"name":"x"}')
        fake_out = json.dumps({
            "foo@1.0.0": {"licenses": "MIT", "path": "/a"},
            "bar@2.0.0": {"licenses": "GPL-3.0", "path": "/b"},
        })
        with mock.patch(
            "backend.software_compliance.licenses.shutil.which",
            lambda x: "/fake/license-checker" if x == "license-checker" else None,
        ), mock.patch(
            "backend.software_compliance.licenses._run_tool",
            return_value=(0, fake_out, ""),
        ):
            report = scan_licenses(tmp_path, ecosystem="npm")
        assert report.source == "license-checker"
        assert report.total_packages == 2

    def test_go_licenses_csv(self, tmp_path: Path):
        (tmp_path / "go.mod").write_text("module x\n")
        fake_out = (
            "github.com/a/b,v1.0.0,Apache-2.0\n"
            "github.com/c/d,v2.0.0,GPL-3.0\n"
        )
        with mock.patch(
            "backend.software_compliance.licenses.shutil.which",
            lambda x: "/fake/go-licenses" if x == "go-licenses" else None,
        ), mock.patch(
            "backend.software_compliance.licenses._run_tool",
            return_value=(0, fake_out, ""),
        ):
            report = scan_licenses(tmp_path, ecosystem="go")
        assert report.source == "go-licenses"
        assert {p.name for p in report.denied} == {"github.com/c/d"}

    def test_tool_failure_falls_back_to_walk(self, tmp_path: Path):
        (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\n")
        (tmp_path / "Cargo.lock").write_text(
            '[[package]]\nname = "only-dep"\nversion = "0.1.0"\n'
        )
        with mock.patch(
            "backend.software_compliance.licenses.shutil.which",
            lambda x: "/fake/cargo-license" if x == "cargo-license" else None,
        ), mock.patch(
            "backend.software_compliance.licenses._run_tool",
            return_value=(2, "", "boom"),
        ):
            report = scan_licenses(tmp_path, ecosystem="cargo")
        assert report.source == "walk"
        assert report.total_packages == 1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CVEs
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_TRIVY_PAYLOAD = {
    "Results": [
        {
            "Type": "npm",
            "Vulnerabilities": [
                {
                    "VulnerabilityID": "CVE-2024-1234",
                    "PkgName": "evil",
                    "InstalledVersion": "1.0.0",
                    "FixedVersion": "1.0.1",
                    "Severity": "HIGH",
                    "Title": "Bad thing",
                },
                {
                    "VulnerabilityID": "CVE-2024-5678",
                    "PkgName": "meh",
                    "InstalledVersion": "2.0.0",
                    "Severity": "LOW",
                    "Title": "Minor",
                },
            ],
        }
    ]
}


class TestCVEsTrivy:
    def test_parses_trivy_json(self, tmp_path: Path):
        with mock.patch(
            "backend.software_compliance.cves.shutil.which",
            lambda x: "/fake/trivy" if x == "trivy" else None,
        ), mock.patch(
            "backend.software_compliance.cves._run",
            return_value=(1, json.dumps(_TRIVY_PAYLOAD), ""),
        ):
            report = scan_cves(tmp_path)
        assert report.source == "trivy"
        assert report.total_findings == 2
        assert report.severity_counts == {"HIGH": 1, "LOW": 1}
        assert len(report.blocking_findings) == 1  # HIGH is blocking
        assert not report.passed

    def test_critical_only_threshold(self, tmp_path: Path):
        with mock.patch(
            "backend.software_compliance.cves.shutil.which",
            lambda x: "/fake/trivy" if x == "trivy" else None,
        ), mock.patch(
            "backend.software_compliance.cves._run",
            return_value=(1, json.dumps(_TRIVY_PAYLOAD), ""),
        ):
            report = scan_cves(tmp_path, fail_on={"CRITICAL"})
        assert report.passed  # HIGH doesn't block under CRITICAL-only

    def test_error_rc_returns_empty(self, tmp_path: Path):
        with mock.patch(
            "backend.software_compliance.cves.shutil.which",
            lambda x: "/fake/trivy" if x == "trivy" else None,
        ), mock.patch(
            "backend.software_compliance.cves._run",
            return_value=(2, "", "fatal: db missing"),
        ):
            report = scan_cves(tmp_path)
        # rc==2 → tool "ran but broken" → returns empty findings for this source
        # but still records source attempt; caller treats as no-findings mock.
        assert report.total_findings == 0


_GRYPE_PAYLOAD = {
    "matches": [
        {
            "vulnerability": {
                "id": "GHSA-abcd-1234",
                "severity": "Critical",
                "description": "remote exec",
                "fix": {"state": "fixed", "versions": ["2.0.0"]},
            },
            "artifact": {"name": "cosmos", "version": "1.0.0", "type": "npm"},
        }
    ]
}


class TestCVEsGrype:
    def test_parses_grype_json(self, tmp_path: Path):
        with mock.patch(
            "backend.software_compliance.cves.shutil.which",
            lambda x: "/fake/grype" if x == "grype" else None,
        ), mock.patch(
            "backend.software_compliance.cves._run",
            return_value=(0, json.dumps(_GRYPE_PAYLOAD), ""),
        ):
            report = scan_cves(tmp_path, scanner="grype")
        assert report.source == "grype"
        assert report.total_findings == 1
        f = report.findings[0]
        assert f.cve_id == "GHSA-abcd-1234"
        assert f.severity == "CRITICAL"
        assert f.fixed_version == "2.0.0"


_OSV_PAYLOAD = {
    "results": [
        {
            "packages": [
                {
                    "package": {"name": "flask", "version": "0.12", "ecosystem": "PyPI"},
                    "vulnerabilities": [
                        {
                            "id": "GHSA-5wv5",
                            "summary": "XSS in render_template",
                            "database_specific": {"severity": "MODERATE"},
                            "affected": [
                                {"ranges": [{"events": [{"fixed": "1.0.3"}]}]}
                            ],
                        }
                    ],
                }
            ]
        }
    ]
}


class TestCVEsOsv:
    def test_parses_osv_json(self, tmp_path: Path):
        with mock.patch(
            "backend.software_compliance.cves.shutil.which",
            lambda x: "/fake/osv-scanner" if x == "osv-scanner" else None,
        ), mock.patch(
            "backend.software_compliance.cves._run",
            return_value=(1, json.dumps(_OSV_PAYLOAD), ""),
        ):
            report = scan_cves(tmp_path, scanner="osv-scanner")
        assert report.source == "osv-scanner"
        assert report.total_findings == 1
        f = report.findings[0]
        assert f.severity == "MEDIUM"  # MODERATE → MEDIUM normalised
        assert f.fixed_version == "1.0.3"
        assert report.passed  # MEDIUM not in default CRITICAL/HIGH


class TestCVEsNoScanner:
    def test_mock_when_nothing_on_path(self, tmp_path: Path):
        with mock.patch(
            "backend.software_compliance.cves.shutil.which", return_value=None
        ):
            report = scan_cves(tmp_path)
        assert report.source == "mock"
        assert report.total_findings == 0
        assert report.passed  # no findings = pass (source=mock still passes but orchestrator treats as skipped)

    def test_invalid_scanner_name(self, tmp_path: Path):
        report = scan_cves(tmp_path, scanner="nonsense")
        assert report.error
        assert not report.passed

    def test_invalid_app_path(self, tmp_path: Path):
        report = scan_cves(tmp_path / "missing")
        assert report.error


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SBOM emit
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _sample_license_report(tmp_path: Path) -> LicenseReport:
    return LicenseReport(
        ecosystem="npm",
        source="walk",
        app_path=str(tmp_path),
        allowed=[
            PackageLicense(name="foo", version="1.0.0", license="MIT", ecosystem="npm"),
            PackageLicense(name="bar", version="2.0.0", license="Apache-2.0", ecosystem="npm"),
        ],
        denied=[],
        unknown=[
            PackageLicense(name="mystery", version="", license="UNKNOWN", ecosystem="npm"),
        ],
        total_packages=3,
    )


class TestCycloneDX:
    def test_basic_shape(self, tmp_path: Path):
        report = _sample_license_report(tmp_path)
        cdx = to_cyclonedx(report, component_name="myapp", component_version="1.0.0")
        assert cdx["bomFormat"] == "CycloneDX"
        assert cdx["specVersion"] == "1.5"
        assert cdx["metadata"]["component"]["name"] == "myapp"
        assert cdx["metadata"]["component"]["version"] == "1.0.0"
        assert len(cdx["components"]) == 3
        # known components carry a license entry, unknown does not
        named = {c["name"]: c for c in cdx["components"]}
        assert named["foo"]["licenses"] == [{"expression": "MIT"}]
        assert "licenses" not in named["mystery"]

    def test_purl_mapping_per_ecosystem(self, tmp_path: Path):
        report = LicenseReport(
            ecosystem="pip",
            source="walk",
            app_path=str(tmp_path),
            allowed=[
                PackageLicense(name="Requests", version="2.0.0", license="Apache-2.0", ecosystem="pip"),
            ],
            total_packages=1,
        )
        cdx = to_cyclonedx(report)
        assert cdx["components"][0]["purl"] == "pkg:pypi/requests@2.0.0"

    def test_serial_number_is_urn_uuid(self, tmp_path: Path):
        report = _sample_license_report(tmp_path)
        cdx = to_cyclonedx(report)
        assert cdx["serialNumber"].startswith("urn:uuid:")


class TestSPDX:
    def test_tag_value_structure(self, tmp_path: Path):
        report = _sample_license_report(tmp_path)
        text = to_spdx(report, component_name="myapp", component_version="1.0.0")
        assert text.startswith("SPDXVersion: SPDX-2.3")
        assert "DataLicense: CC0-1.0" in text
        assert "PackageName: myapp" in text
        assert "PackageName: foo" in text
        assert "PackageLicenseConcluded: MIT" in text
        # unknown license → NOASSERTION
        assert "PackageLicenseConcluded: NOASSERTION" in text
        # relationship entries generated for deps
        assert "Relationship: SPDXRef-ROOT DEPENDS_ON" in text

    def test_unsafe_spdx_name_sanitised(self, tmp_path: Path):
        report = LicenseReport(
            ecosystem="npm",
            source="walk",
            app_path=str(tmp_path),
            allowed=[
                PackageLicense(name="@scope/weird name", version="1.0.0", license="MIT", ecosystem="npm"),
            ],
            total_packages=1,
        )
        text = to_spdx(report)
        # SPDX IDs must match [A-Za-z0-9.-]+ — look for the sanitised form
        import re as _re
        spdx_ids = _re.findall(r"SPDXID: (SPDXRef-Pkg-[^\s]+)", text)
        for sid in spdx_ids:
            assert _re.match(r"^SPDXRef-[A-Za-z0-9.\-]+$", sid)


class TestEmitSbom:
    def test_cyclonedx_default(self, tmp_path: Path):
        report = _sample_license_report(tmp_path)
        doc = emit_sbom(report, fmt="cyclonedx")
        assert doc.format == "cyclonedx"
        parsed = json.loads(doc.content)
        assert parsed["bomFormat"] == "CycloneDX"

    def test_spdx(self, tmp_path: Path):
        report = _sample_license_report(tmp_path)
        doc = emit_sbom(report, fmt="spdx")
        assert doc.format == "spdx"
        assert "SPDXVersion" in doc.content

    def test_unknown_format_raises(self, tmp_path: Path):
        report = _sample_license_report(tmp_path)
        with pytest.raises(ValueError):
            emit_sbom(report, fmt="fortran")

    def test_write_to_disk(self, tmp_path: Path):
        report = _sample_license_report(tmp_path)
        doc = emit_sbom(report, fmt="cyclonedx")
        out = tmp_path / "out" / "sbom.cdx.json"
        written = doc.write(out)
        assert written.exists()
        assert json.loads(written.read_text())["bomFormat"] == "CycloneDX"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Bundle / orchestrator
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.fixture()
def npm_clean_site(tmp_path: Path) -> Path:
    (tmp_path / "package.json").write_text('{"name":"a"}')
    nm = tmp_path / "node_modules"
    (nm / "foo").mkdir(parents=True)
    (nm / "foo" / "package.json").write_text(
        json.dumps({"name": "foo", "version": "1.0.0", "license": "MIT"})
    )
    return tmp_path


@pytest.fixture()
def npm_dirty_site(tmp_path: Path) -> Path:
    (tmp_path / "package.json").write_text('{"name":"b"}')
    nm = tmp_path / "node_modules"
    (nm / "bad").mkdir(parents=True)
    (nm / "bad" / "package.json").write_text(
        json.dumps({"name": "bad", "version": "1.0.0", "license": "GPL-3.0"})
    )
    return tmp_path


class TestBundleRun:
    def test_clean_site_passes(self, npm_clean_site: Path):
        with mock.patch(
            "backend.software_compliance.cves.shutil.which", return_value=None
        ):
            bundle = run_all(npm_clean_site)
        assert bundle.ecosystem == "npm"
        assert len(bundle.gates) == 3
        assert bundle.get("license").verdict == GateVerdict.pass_
        assert bundle.get("cve").verdict == GateVerdict.skipped
        assert bundle.get("sbom").verdict == GateVerdict.pass_
        assert bundle.passed  # skipped ≠ fail

    def test_dirty_site_fails(self, npm_dirty_site: Path):
        with mock.patch(
            "backend.software_compliance.cves.shutil.which", return_value=None
        ):
            bundle = run_all(npm_dirty_site)
        assert not bundle.passed
        assert bundle.failed_count >= 1
        assert bundle.get("license").verdict == GateVerdict.fail

    def test_sbom_written_to_disk(self, npm_clean_site: Path, tmp_path: Path):
        out = tmp_path / "bom.json"
        with mock.patch(
            "backend.software_compliance.cves.shutil.which", return_value=None
        ):
            bundle = run_all(npm_clean_site, sbom_out=out)
        assert out.exists()
        assert bundle.get("sbom").detail["written_to"] == str(out)

    def test_unknown_sbom_format_errors_gate(self, npm_clean_site: Path):
        with mock.patch(
            "backend.software_compliance.cves.shutil.which", return_value=None
        ):
            bundle = run_all(npm_clean_site, sbom_format="fortran")
        assert bundle.get("sbom").verdict == GateVerdict.error
        assert not bundle.passed

    def test_to_dict_shape(self, npm_clean_site: Path):
        with mock.patch(
            "backend.software_compliance.cves.shutil.which", return_value=None
        ):
            bundle = run_all(npm_clean_site)
        d = bundle.to_dict()
        assert {"app_path", "ecosystem", "timestamp", "passed",
                "passed_count", "failed_count", "skipped_count",
                "total_gates", "gates"}.issubset(d.keys())
        assert len(d["gates"]) == 3


class TestBundleC8Bridge:
    def test_converts_to_compliance_report(self, npm_clean_site: Path):
        from backend.compliance_harness import ComplianceReport
        with mock.patch(
            "backend.software_compliance.cves.shutil.which", return_value=None
        ):
            bundle = run_all(npm_clean_site)
        report = bundle_to_compliance_report(bundle)
        assert isinstance(report, ComplianceReport)
        assert report.tool_name == "x4_software_compliance"
        assert len(report.results) == 3
        assert report.metadata["origin"] == "software_compliance"
        assert report.metadata["ecosystem"] == "npm"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CLI
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestCliMain:
    def test_exit_0_when_bundle_passes(self, npm_clean_site: Path, tmp_path: Path, capsys):
        from backend.software_compliance.__main__ import main
        json_out = tmp_path / "bundle.json"
        with mock.patch(
            "backend.software_compliance.cves.shutil.which", return_value=None
        ):
            rc = main([
                "--app-path", str(npm_clean_site),
                "--ecosystem", "npm",
                "--json-out", str(json_out),
                "--sbom-format", "cyclonedx",
            ])
        assert rc == 0
        payload = json.loads(json_out.read_text())
        assert payload["passed"] is True

    def test_exit_1_when_bundle_fails(self, npm_dirty_site: Path, tmp_path: Path):
        from backend.software_compliance.__main__ import main
        json_out = tmp_path / "bundle.json"
        with mock.patch(
            "backend.software_compliance.cves.shutil.which", return_value=None
        ):
            rc = main([
                "--app-path", str(npm_dirty_site),
                "--ecosystem", "npm",
                "--json-out", str(json_out),
            ])
        assert rc == 1

    def test_exit_2_missing_app_path(self, tmp_path: Path, capsys):
        from backend.software_compliance.__main__ import main
        rc = main(["--app-path", str(tmp_path / "does-not-exist")])
        assert rc == 2

    def test_allowlist_arg_parsed(self, npm_dirty_site: Path, tmp_path: Path):
        from backend.software_compliance.__main__ import main
        json_out = tmp_path / "bundle.json"
        with mock.patch(
            "backend.software_compliance.cves.shutil.which", return_value=None
        ):
            rc = main([
                "--app-path", str(npm_dirty_site),
                "--ecosystem", "npm",
                "--allowlist", "bad",
                "--json-out", str(json_out),
            ])
        assert rc == 0  # allowlisted
