"""W5 #279 — Unit tests for the web compliance gates.

Covers:
    * WCAG: axe-core JSON parsing, manual-checklist override merging,
      pass/fail aggregation, CLI-absent fallback.
    * GDPR: cookie-banner / retention / DPA / RTBF posture detection on
      purpose-built tmp trees.
    * SPDX: license normalisation, deny-list matching, allowlist
      override, arborist/walk fallback verdict equivalence.
    * Bundle: orchestrator composes all three, CLI exit code reflects
      the bundle verdict, and the C8 compliance-harness bridge produces
      a valid ComplianceReport.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from backend.web_compliance import (
    DEFAULT_DENY_LICENSES,
    GDPRReport,
    SPDXReport,
    WCAGReport,
    WCAG_AA_MANUAL_CHECKLIST,
    bundle_to_compliance_report,
    run_all,
    scan_gdpr,
    scan_licenses,
    run_wcag_scan,
)
from backend.web_compliance.bundle import GateVerdict
from backend.web_compliance.gdpr import (
    COOKIE_BANNER_SIGNATURES,
    _scan_cookie_banner,
    _scan_dpa_template,
    _scan_retention_policy,
    _scan_rtbf_endpoint,
)
from backend.web_compliance.spdx import (
    PackageLicense,
    _expand_atoms,
    _license_matches,
    _normalise_license,
    _walk_node_modules,
)
from backend.web_compliance.wcag import (
    WCAGAutoIssue,
    WCAGManualItem,
    _parse_axe_output,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fixtures
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.fixture()
def compliant_site(tmp_path: Path) -> Path:
    """A minimal directory tree satisfying every GDPR posture check."""
    (tmp_path / "docs" / "privacy").mkdir(parents=True)
    (tmp_path / "docs" / "privacy" / "retention.md").write_text(
        "# Retention\nWe keep logs 30 days then purge.\n"
    )
    (tmp_path / "docs" / "privacy" / "dpa.md").write_text(
        "# DPA Template\nFixture template.\n"
    )
    (tmp_path / "index.html").write_text(
        '<html><body><div class="cookie-banner">consent</div></body></html>'
    )
    (tmp_path / "server.py").write_text(
        '# gdpr:rtbf\n@app.delete("/gdpr/delete")\n'
        'def erase_user_data(): pass\n'
    )
    return tmp_path


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  WCAG
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestWCAGManualChecklist:
    def test_checklist_pinned_to_12_items(self):
        # If this size changes, update the a11y.skill.md doc + this
        # pin so evidence bundles stay traceable.
        assert len(WCAG_AA_MANUAL_CHECKLIST) == 12

    def test_every_item_has_sc_name_description(self):
        for entry in WCAG_AA_MANUAL_CHECKLIST:
            assert entry["sc"]
            assert entry["name"]
            assert entry["description"]

    def test_new_22_criteria_present(self):
        # The WCAG 2.2-new AA items the a11y role emphasises.
        scs = {e["sc"] for e in WCAG_AA_MANUAL_CHECKLIST}
        assert {"2.4.11", "2.5.7", "2.5.8", "3.3.8"}.issubset(scs)


class TestAxeOutputParsing:
    def test_parses_single_entry_with_violations(self):
        payload = json.dumps([{
            "violations": [
                {"id": "color-contrast", "impact": "serious",
                 "description": "bad contrast",
                 "helpUrl": "https://dequeuniversity.com/rules/axe/4.x/color-contrast",
                 "nodes": [{}, {}, {}]},
                {"id": "button-name", "impact": "critical",
                 "description": "unlabeled button",
                 "helpUrl": "", "nodes": [{}]},
            ],
        }])
        issues = _parse_axe_output(payload)
        assert len(issues) == 2
        assert issues[0].nodes == 3
        assert issues[0].impact == "serious"
        assert issues[1].impact == "critical"

    def test_empty_list_when_non_json(self):
        assert _parse_axe_output("not json") == []

    def test_handles_dict_root(self):
        payload = json.dumps({"violations": [{"id": "x", "impact": "minor",
                                              "description": "", "nodes": []}]})
        issues = _parse_axe_output(payload)
        assert len(issues) == 1
        assert issues[0].id == "x"


class TestWCAGScan:
    def test_mock_when_no_cli(self):
        with patch("shutil.which", return_value=None):
            report = run_wcag_scan("https://example.com")
        assert report.source == "mock"
        assert report.violations == []
        # Manual checklist is always attached.
        assert len(report.manual_checklist) == len(WCAG_AA_MANUAL_CHECKLIST)
        # Mock + no failing manual item => passes.
        assert report.passed is True

    def test_checklist_overrides_propagate(self):
        overrides = {"2.5.8": {"status": "fail",
                               "notes": "button too small"}}
        with patch("shutil.which", return_value=None):
            report = run_wcag_scan("", checklist_overrides=overrides)
        hit = [i for i in report.manual_checklist if i.sc == "2.5.8"][0]
        assert hit.status == "fail"
        assert hit.notes == "button too small"
        # A failing manual item blocks the gate even under mock scan.
        assert report.passed is False

    def test_critical_violation_fails_gate(self):
        fake = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps([{"violations": [
                {"id": "aria-hidden-focus", "impact": "critical",
                 "description": "focus trapped in aria-hidden",
                 "helpUrl": "", "nodes": [{}]},
            ]}]),
            stderr="",
        )
        with patch("shutil.which", return_value="/usr/bin/axe"), \
             patch("subprocess.run", return_value=fake):
            report = run_wcag_scan("https://example.com")
        assert report.source == "axe"
        assert report.critical_violations == 1
        assert report.passed is False

    def test_moderate_only_violation_still_passes(self):
        fake = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps([{"violations": [
                {"id": "heading-order", "impact": "moderate",
                 "description": "skipped level", "helpUrl": "", "nodes": []},
            ]}]),
            stderr="",
        )
        with patch("shutil.which", return_value="/usr/bin/axe"), \
             patch("subprocess.run", return_value=fake):
            report = run_wcag_scan("https://example.com")
        # Moderate / minor don't block the gate — only critical/serious do.
        assert report.passed is True
        assert report.critical_violations == 0
        assert report.serious_violations == 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  GDPR
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestGDPRCookieBanner:
    @pytest.mark.parametrize("sig", COOKIE_BANNER_SIGNATURES)
    def test_detects_each_signature(self, tmp_path: Path, sig: str):
        (tmp_path / "app.js").write_text(f"window.x = '{sig}'")
        check = _scan_cookie_banner(tmp_path)
        assert check.passed is True
        assert sig in check.evidence.lower()

    def test_missing_when_no_signature(self, tmp_path: Path):
        (tmp_path / "app.js").write_text("console.log('hello');")
        check = _scan_cookie_banner(tmp_path)
        assert check.passed is False
        assert "No recognised consent-manager" in check.details[0]

    def test_node_modules_excluded_from_scan(self, tmp_path: Path):
        nm = tmp_path / "node_modules" / "foo"
        nm.mkdir(parents=True)
        (nm / "iubenda-dep.js").write_text("// iubenda embedded in vendor")
        check = _scan_cookie_banner(tmp_path)
        assert check.passed is False


class TestGDPRRetention:
    def test_file_with_horizon_passes(self, tmp_path: Path):
        p = tmp_path / "docs" / "privacy"
        p.mkdir(parents=True)
        (p / "retention.md").write_text("Keep logs 30 days max.")
        check = _scan_retention_policy(tmp_path)
        assert check.passed is True
        assert "30 days" in check.evidence

    def test_file_without_horizon_fails(self, tmp_path: Path):
        (tmp_path / "PRIVACY.md").write_text("we care about privacy")
        check = _scan_retention_policy(tmp_path)
        assert check.passed is False

    def test_no_file_fails(self, tmp_path: Path):
        check = _scan_retention_policy(tmp_path)
        assert check.passed is False


class TestGDPRDPA:
    def test_dpa_md_passes(self, tmp_path: Path):
        p = tmp_path / "docs" / "legal"
        p.mkdir(parents=True)
        (p / "dpa.md").write_text("DPA")
        check = _scan_dpa_template(tmp_path)
        assert check.passed is True

    def test_no_dpa_fails(self, tmp_path: Path):
        check = _scan_dpa_template(tmp_path)
        assert check.passed is False


class TestGDPRRtbf:
    def test_sentinel_comment_passes(self, tmp_path: Path):
        (tmp_path / "handler.py").write_text("# gdpr:rtbf\ndef delete(): pass")
        check = _scan_rtbf_endpoint(tmp_path)
        assert check.passed is True

    def test_route_pattern_passes(self, tmp_path: Path):
        (tmp_path / "routes.ts").write_text('router.delete("/gdpr/forget", h)')
        check = _scan_rtbf_endpoint(tmp_path)
        assert check.passed is True

    def test_absent_fails(self, tmp_path: Path):
        (tmp_path / "unrelated.py").write_text("print('hi')")
        check = _scan_rtbf_endpoint(tmp_path)
        assert check.passed is False


class TestGDPREndToEnd:
    def test_all_four_pass(self, compliant_site: Path):
        report = scan_gdpr(compliant_site)
        assert isinstance(report, GDPRReport)
        assert report.passed is True
        assert report.passed_count == 4

    def test_missing_dir_returns_all_failing(self, tmp_path: Path):
        ghost = tmp_path / "does-not-exist"
        report = scan_gdpr(ghost)
        assert report.passed is False
        for c in report.checks:
            assert any("not a directory" in d for d in c.details)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SPDX
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestLicenseNormalisation:
    def test_string_passthrough(self):
        assert _normalise_license("MIT") == "MIT"

    def test_dict_type_field(self):
        assert _normalise_license({"type": "Apache-2.0"}) == "Apache-2.0"

    def test_list_becomes_or_expression(self):
        out = _normalise_license([{"type": "MIT"}, {"type": "Apache-2.0"}])
        assert "MIT" in out and "Apache-2.0" in out and "OR" in out

    def test_none_becomes_unknown(self):
        assert _normalise_license(None) == "UNKNOWN"

    def test_empty_dict_is_unknown(self):
        assert _normalise_license({}) == "UNKNOWN"


class TestLicenseMatching:
    def test_atoms_split_suffixes(self):
        assert _expand_atoms("GPL-3.0-or-later") == {"GPL-3.0"}
        assert _expand_atoms("LGPL-2.1-only") == {"LGPL-2.1"}

    def test_or_expression_matches_any_deny_atom(self):
        assert _license_matches("(MIT OR GPL-3.0-or-later)",
                                {"GPL-3.0"}) is True

    def test_unknown_does_not_match(self):
        assert _license_matches("UNKNOWN", {"GPL-3.0"}) is False

    def test_mit_is_clean(self):
        assert _license_matches("MIT", DEFAULT_DENY_LICENSES) is False

    def test_agpl_matches_default_deny(self):
        assert _license_matches("AGPL-3.0-or-later",
                                DEFAULT_DENY_LICENSES) is True


class TestSPDXWalkFallback:
    def _write_pkg(self, root: Path, name: str, version: str, license: object):
        d = root / "node_modules" / name
        d.mkdir(parents=True)
        (d / "package.json").write_text(json.dumps({
            "name": name, "version": version, "license": license,
        }))

    def test_mit_only_tree_passes(self, tmp_path: Path):
        self._write_pkg(tmp_path, "left-pad", "1.0.0", "MIT")
        self._write_pkg(tmp_path, "lodash", "4.17.21", "MIT")
        report = scan_licenses(tmp_path)
        assert report.passed is True
        assert report.total_packages == 2
        assert report.source == "walk"

    def test_gpl_package_fails(self, tmp_path: Path):
        self._write_pkg(tmp_path, "clean", "1.0.0", "MIT")
        self._write_pkg(tmp_path, "copyleft-dep", "2.0.0", "GPL-3.0-or-later")
        report = scan_licenses(tmp_path)
        assert report.passed is False
        assert len(report.denied) == 1
        assert report.denied[0].name == "copyleft-dep"

    def test_allowlist_overrides_deny(self, tmp_path: Path):
        self._write_pkg(tmp_path, "readline", "1.0.0", "GPL-3.0")
        report = scan_licenses(tmp_path, allowlist=["readline"])
        assert report.passed is True
        assert not report.denied

    def test_unknown_license_surfaced_but_not_failed(self, tmp_path: Path):
        self._write_pkg(tmp_path, "mystery", "0.0.1", None)
        report = scan_licenses(tmp_path)
        assert report.passed is True
        assert len(report.unknown) == 1

    def test_walk_skips_nested_tests(self, tmp_path: Path):
        self._write_pkg(tmp_path, "good", "1.0.0", "MIT")
        # A transitive package.json under a test/ dir should be ignored.
        nested = tmp_path / "node_modules" / "good" / "test" / "fixtures"
        nested.mkdir(parents=True)
        (nested / "package.json").write_text(
            json.dumps({"name": "bad-gpl-fixture", "version": "0.0.0",
                        "license": "GPL-3.0"})
        )
        report = scan_licenses(tmp_path)
        assert report.passed is True
        assert report.total_packages == 1

    def test_missing_app_path_records_error(self, tmp_path: Path):
        ghost = tmp_path / "nope"
        report = scan_licenses(ghost)
        assert report.passed is False
        assert "not a directory" in report.error


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Bundle / orchestrator
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestBundleAggregation:
    def test_all_gates_ids_present(self, compliant_site: Path):
        bundle = run_all(compliant_site)
        ids = {g.gate_id for g in bundle.gates}
        assert ids == {"wcag", "gdpr", "spdx"}

    def test_skipped_gates_do_not_block(self, compliant_site: Path):
        # No URL → WCAG skipped; no node_modules → SPDX skipped; GDPR pass.
        bundle = run_all(compliant_site)
        assert bundle.passed is True
        assert bundle.skipped_count == 2
        assert bundle.failed_count == 0

    def test_failing_gdpr_blocks_bundle(self, tmp_path: Path):
        # Empty tree — nothing satisfies the GDPR posture checks.
        bundle = run_all(tmp_path)
        assert bundle.passed is False
        assert bundle.get("gdpr").verdict == GateVerdict.fail

    def test_get_returns_none_for_unknown(self, compliant_site: Path):
        bundle = run_all(compliant_site)
        assert bundle.get("does-not-exist") is None

    def test_to_dict_round_trip(self, compliant_site: Path):
        d = run_all(compliant_site).to_dict()
        assert set(d.keys()) >= {
            "app_path", "passed", "failed_count", "skipped_count",
            "total_gates", "gates",
        }
        assert len(d["gates"]) == 3


class TestC8ComplianceBridge:
    def test_bundle_translates_to_compliance_report(self, compliant_site: Path):
        bundle = run_all(compliant_site)
        report = bundle_to_compliance_report(bundle)
        assert report.tool_name == "w5_web_compliance"
        assert report.total == 3
        test_ids = {r.test_id for r in report.results}
        assert test_ids == {"W5-WCAG", "W5-GDPR", "W5-SPDX"}

    def test_bundle_metadata_carries_bundle_dict(self, compliant_site: Path):
        bundle = run_all(compliant_site)
        report = bundle_to_compliance_report(bundle)
        assert report.metadata["origin"] == "web_compliance"
        assert "bundle" in report.metadata


class TestCLIExit:
    def test_cli_exits_zero_when_bundle_passes(self, compliant_site: Path,
                                               tmp_path: Path, capsys):
        from backend.web_compliance.__main__ import main
        out = tmp_path / "out.json"
        rc = main(["--app-path", str(compliant_site),
                   "--json-out", str(out)])
        assert rc == 0
        payload = json.loads(out.read_text())
        assert payload["passed"] is True

    def test_cli_exits_nonzero_when_bundle_fails(self, tmp_path: Path):
        from backend.web_compliance.__main__ import main
        rc = main(["--app-path", str(tmp_path),
                   "--json-out", str(tmp_path / "o.json")])
        assert rc == 1
