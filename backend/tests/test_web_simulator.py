"""W2 #276 — unit tests for `backend.web_simulator`.

Exercises the Python library independently of the shell layer. The
integration with `scripts/simulate.sh` is covered separately by
`test_web_simulate.py`. Everything here runs under pure pytest with
temp dirs; no network, no external binaries required.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend import web_simulator as ws


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  parse_budget
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestParseBudget:
    @pytest.mark.parametrize("spec,expected", [
        ("500KiB", 512000),
        ("5MiB", 5 * 1024 * 1024),
        ("1MiB", 1 * 1024 * 1024),
        ("50MiB", 50 * 1024 * 1024),
        ("10KB", 10_000),
        ("2 GB", 2 * 1000 ** 3),
        ("1024", 1024),          # plain bytes
        (42, 42),                # plain int
        (1.5, 1),
    ])
    def test_unit_matrix(self, spec, expected):
        assert ws.parse_budget(spec) == expected

    def test_empty_returns_fallback(self):
        assert ws.parse_budget("") == 0
        assert ws.parse_budget("", fallback=999) == 999
        assert ws.parse_budget(None, fallback=42) == 42

    def test_bogus_unit_returns_fallback(self):
        assert ws.parse_budget("10QQ", fallback=77) == 77
        assert ws.parse_budget("not a number", fallback=88) == 88


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fixture builder
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_GOOD_HTML = (
    b"<html><head>"
    b"<title>Hi</title>"
    b'<meta name="description" content="w2 fixture">'
    b'<meta name="viewport" content="width=device-width">'
    b'<link rel="canonical" href="https://x/">'
    b'<meta property="og:title" content="Hi">'
    b"</head><body>ok</body></html>"
)


def _build_good_fixture(tmp_path: Path, *, sub: str = "dist") -> Path:
    """Create a minimal but SEO-clean static site under `tmp_path/{sub}`."""
    out = tmp_path / sub
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_bytes(_GOOD_HTML)
    (out / "app.js").write_text("console.log(1)")
    return tmp_path


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Individual gate runners
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestBundleGate:
    def test_sums_dist_dir(self, tmp_path):
        root = _build_good_fixture(tmp_path)
        report = ws.run_bundle_gate(root, budget_bytes=500_000)
        assert report.total_bytes > 0
        assert report.file_count == 2
        assert report.violations == []
        assert report.budget_bytes == 500_000

    def test_flags_over_budget(self, tmp_path):
        root = _build_good_fixture(tmp_path)
        report = ws.run_bundle_gate(root, budget_bytes=50)  # too small
        assert report.violations  # total over budget
        assert any("exceeds budget" in v for v in report.violations)

    def test_zero_budget_reports_no_violations(self, tmp_path):
        root = _build_good_fixture(tmp_path)
        report = ws.run_bundle_gate(root, budget_bytes=0)
        # 0 budget is interpreted as "no gate" — no violations even on big sites
        assert report.budget_bytes == 0
        assert report.violations == []

    def test_fallback_to_flat_dir(self, tmp_path):
        # No dist/build/.next etc. — simulator walks tmp_path directly.
        (tmp_path / "foo.js").write_text("x")
        report = ws.run_bundle_gate(tmp_path, budget_bytes=100)
        assert report.file_count == 1


class TestSEOLint:
    def test_clean_page_no_issues(self, tmp_path):
        root = _build_good_fixture(tmp_path)
        assert ws.run_seo_lint(root).issues == 0

    def test_missing_title(self, tmp_path):
        root = tmp_path
        (root / "dist").mkdir()
        # Strip <title>
        (root / "dist" / "index.html").write_bytes(_GOOD_HTML.replace(b"<title>Hi</title>", b""))
        report = ws.run_seo_lint(root)
        assert report.issues >= 1
        assert any("title" in d for d in report.details)

    def test_missing_index_html(self, tmp_path):
        report = ws.run_seo_lint(tmp_path)
        assert report.issues >= 1
        assert any("index.html" in d for d in report.details)


class TestE2ESmoke:
    def test_degrades_to_mock_when_no_specs(self, tmp_path):
        # No e2e/ directory — mock pass
        report = ws.run_e2e_smoke(tmp_path, url="http://localhost")
        assert report.status == "mock"
        assert report.passed >= 1
        assert report.failed == 0

    def test_no_url_is_mock(self, tmp_path):
        (tmp_path / "e2e").mkdir()
        (tmp_path / "e2e" / "smoke.spec.ts").write_text("// stub")
        assert ws.run_e2e_smoke(tmp_path, url=None).status == "mock"


class TestVisualRegression:
    def test_skip_when_no_baseline(self, tmp_path):
        assert ws.run_visual_regression(tmp_path).status == "skip"

    def test_pass_when_baseline_matches(self, tmp_path):
        baseline = tmp_path / "visual" / "baseline"
        current = tmp_path / "visual" / "current"
        baseline.mkdir(parents=True)
        current.mkdir(parents=True)
        (baseline / "home.png").write_bytes(b"pixels")
        (current / "home.png").write_bytes(b"pixels")
        r = ws.run_visual_regression(tmp_path)
        assert r.status == "pass"
        assert r.diffs == 0

    def test_fail_on_diff(self, tmp_path):
        baseline = tmp_path / "visual" / "baseline"
        current = tmp_path / "visual" / "current"
        baseline.mkdir(parents=True)
        current.mkdir(parents=True)
        (baseline / "home.png").write_bytes(b"original")
        (current / "home.png").write_bytes(b"different")
        r = ws.run_visual_regression(tmp_path)
        assert r.status == "fail"
        assert r.diffs == 1


class TestLighthouseMock:
    def test_mock_returns_baseline_minimums(self, tmp_path):
        # No --url → synthetic mock that passes all three thresholds
        lh = ws.run_lighthouse(tmp_path, url=None)
        assert lh.source == "mock"
        assert lh.performance >= ws.LIGHTHOUSE_MIN_PERF
        assert lh.accessibility >= ws.LIGHTHOUSE_MIN_A11Y
        assert lh.seo >= ws.LIGHTHOUSE_MIN_SEO


class TestA11yMock:
    def test_mock_reports_zero_violations(self, tmp_path):
        a = ws.run_a11y_audit(tmp_path, url=None)
        assert a.violations == 0
        assert a.source == "mock"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  simulate_web orchestrator
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestSimulateWeb:
    def test_happy_path_web_static(self, tmp_path):
        root = _build_good_fixture(tmp_path)
        result = ws.simulate_web(profile="web-static", app_path=root)
        assert result.overall_pass()
        assert result.bundle.budget_bytes == 500 * 1024  # web-static budget
        assert result.seo.issues == 0
        assert all(result.gates.values())

    def test_budget_override_wins(self, tmp_path):
        root = _build_good_fixture(tmp_path)
        result = ws.simulate_web(
            profile="web-static", app_path=root, budget_override=100,
        )
        # Override wins → bundle is now over 100B → gate fails
        assert result.bundle.budget_bytes == 100
        assert not result.gates["bundle_budget"]
        assert not result.overall_pass()

    def test_ssr_node_profile_budget(self, tmp_path):
        root = _build_good_fixture(tmp_path)
        result = ws.simulate_web(profile="web-ssr-node", app_path=root)
        assert result.bundle.budget_bytes == 5 * 1024 * 1024

    def test_cloudflare_profile_budget(self, tmp_path):
        root = _build_good_fixture(tmp_path)
        result = ws.simulate_web(profile="web-edge-cloudflare", app_path=root)
        assert result.bundle.budget_bytes == 1 * 1024 * 1024

    def test_vercel_profile_budget(self, tmp_path):
        root = _build_good_fixture(tmp_path)
        result = ws.simulate_web(profile="web-vercel", app_path=root)
        assert result.bundle.budget_bytes == 50 * 1024 * 1024

    def test_bad_profile_falls_back_and_records_error(self, tmp_path):
        root = _build_good_fixture(tmp_path)
        result = ws.simulate_web(profile="does-not-exist", app_path=root)
        # Profile load fails → fallback budget applied + error captured
        assert result.errors
        assert result.bundle.budget_bytes > 0

    def test_result_to_json_shape(self, tmp_path):
        root = _build_good_fixture(tmp_path)
        result = ws.simulate_web(profile="web-static", app_path=root)
        blob = ws.result_to_json(result)
        # Every field referenced by simulate.sh must exist in the dict
        for key in (
            "lighthouse_perf", "lighthouse_a11y", "lighthouse_seo",
            "lighthouse_best_practices", "lighthouse_source",
            "bundle_total_bytes", "bundle_budget_bytes",
            "bundle_violations", "a11y_violations", "a11y_source",
            "seo_issues", "e2e_status", "visual_status",
            "overall_pass", "gates",
        ):
            assert key in blob, f"missing key {key}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CLI contract with simulate.sh
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestCLIContract:
    """Guards the argparse contract simulate.sh relies on. If anyone
    renames --app-path or --budget-override the shell will silently
    pass empty flags and these tests catch it immediately."""

    def test_cli_emits_single_json_line(self, tmp_path, capsys, monkeypatch):
        root = _build_good_fixture(tmp_path)
        monkeypatch.setattr(
            "sys.argv",
            [
                "web_simulator",
                "--profile", "web-static",
                "--app-path", str(root),
                "--budget-override", "600000",
            ],
        )
        rc = ws._cli_main()
        assert rc == 0
        captured = capsys.readouterr().out.strip()
        # Must be exactly one JSON line — simulate.sh reads it wholesale
        data = json.loads(captured)
        assert data["profile"] == "web-static"
        assert data["bundle_budget_bytes"] == 600000
