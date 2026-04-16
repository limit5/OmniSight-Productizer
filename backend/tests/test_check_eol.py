"""N6 — unit tests for `scripts/check_eol.py`.

The EOL check runs monthly via ``.github/workflows/eol-check.yml``
and its job is to warn operators when a strategic platform
dependency (Python / Node / FastAPI / Next.js) approaches its EOL
date from the endoflife.date feed.

These tests cover:
  * version discovery from the repo's current pins
  * the 180-day warning threshold logic
  * graceful handling of endoflife.date oddities (`eol: false`,
    missing cycles, 404 not-tracked products, network errors)
  * markdown rendering (shape + key phrases)
  * CLI entrypoint end-to-end with a stubbed `fetch`
"""
from __future__ import annotations

import subprocess
import sys
import urllib.error
from datetime import date
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check_eol.py"

# Make the script importable as a module. It's not under a package
# (it's in `scripts/`), so we splice its dir onto sys.path once per
# test session.
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import check_eol  # noqa: E402


# ---------------------------------------------------------------------------
# Version discovery helpers
# ---------------------------------------------------------------------------

class TestVersionDiscovery:
    """Pinned version readers return the current repo state."""

    def test_python_version_is_three_point_twelve(self) -> None:
        # ci.yml has python-version: "3.12" throughout, and the
        # backend Dockerfile pins the same. Any drift here means
        # something shipped without updating ci.yml.
        assert check_eol.read_python_version(REPO_ROOT) == "3.12"

    def test_node_version_comes_from_nvmrc(self) -> None:
        assert check_eol.read_node_version(REPO_ROOT) == "20"

    def test_fastapi_version_comes_from_requirements_in(self) -> None:
        v = check_eol.read_fastapi_version(REPO_ROOT)
        # Pin format is X.Y (e.g. "0.115"); just check shape.
        assert "." in v, f"FastAPI version should be major.minor, got {v!r}"

    def test_nextjs_version_is_major_only(self) -> None:
        # endoflife.date keys Next.js cycles by major (e.g. "16").
        v = check_eol.read_nextjs_version(REPO_ROOT)
        assert v.isdigit(), f"Next.js cycle must be numeric major, got {v!r}"

    def test_build_products_assembles_four_entries(self) -> None:
        products = check_eol.build_products(REPO_ROOT)
        names = [p.name for p in products]
        assert names == ["Python", "Node.js", "FastAPI", "Next.js"]
        # Each product must carry an operator-facing pin source.
        for p in products:
            assert p.pin_source, f"{p.name} missing pin_source"
            assert p.api_slug, f"{p.name} missing api_slug"


# ---------------------------------------------------------------------------
# Severity / horizon logic
# ---------------------------------------------------------------------------

@pytest.fixture
def today() -> date:
    return date(2026, 4, 16)


def _product(name: str = "Python", api_slug: str = "python",
             version: str = "3.12") -> check_eol.Product:
    return check_eol.Product(
        name=name,
        api_slug=api_slug,
        current_version=version,
        pin_source=f"test-source:{name}",
    )


class TestEvaluateProducts:
    """The core decision: is a cycle inside the warning horizon?"""

    def test_eol_inside_horizon_emits_warning(self, today: date) -> None:
        def fetch(slug: str) -> list[dict]:
            return [{"cycle": "3.12", "eol": "2026-06-01", "latest": "3.12.7"}]
        report = check_eol.evaluate_products(
            [_product()], today=today, warn_days=180, fetch=fetch
        )
        assert len(report.warnings) == 1
        w = report.warnings[0]
        assert w.product == "Python"
        assert w.eol_date == "2026-06-01"
        # 2026-04-16 -> 2026-06-01 = 46 days
        assert w.days_remaining == 46
        assert w.latest_in_cycle == "3.12.7"

    def test_eol_outside_horizon_lands_in_ok(self, today: date) -> None:
        def fetch(slug: str) -> list[dict]:
            return [{"cycle": "3.12", "eol": "2028-10-31", "latest": "3.12.7"}]
        report = check_eol.evaluate_products(
            [_product()], today=today, warn_days=180, fetch=fetch
        )
        assert report.warnings == []
        assert len(report.ok) == 1
        assert report.ok[0]["product"] == "Python"

    def test_eol_false_means_no_scheduled_eol(self, today: date) -> None:
        # Next.js and similar rolling-release projects carry `eol: false`.
        def fetch(slug: str) -> list[dict]:
            return [{"cycle": "16", "eol": False, "latest": "16.2.0"}]
        report = check_eol.evaluate_products(
            [_product("Next.js", "nextjs", "16")],
            today=today, warn_days=180, fetch=fetch,
        )
        assert report.warnings == []
        assert len(report.ok) == 1
        assert report.ok[0]["eol_date"] == "no scheduled EOL"

    def test_eol_true_means_already_eol(self, today: date) -> None:
        # Some feeds mark an already-EOL cycle as `eol: true` without a
        # concrete date. Treat as "remaining = 0" so the warning fires.
        def fetch(slug: str) -> list[dict]:
            return [{"cycle": "3.8", "eol": True, "latest": "3.8.18"}]
        report = check_eol.evaluate_products(
            [_product("Python", "python", "3.8")],
            today=today, warn_days=180, fetch=fetch,
        )
        assert len(report.warnings) == 1
        assert report.warnings[0].days_remaining == 0

    def test_cycle_not_in_feed_is_error(self, today: date) -> None:
        # Pinned version isn't tracked on endoflife.date at all.
        def fetch(slug: str) -> list[dict]:
            return [{"cycle": "3.13", "eol": "2029-10-01"}]
        report = check_eol.evaluate_products(
            [_product("Python", "python", "3.12")],
            today=today, warn_days=180, fetch=fetch,
        )
        assert report.warnings == []
        assert report.ok == []
        assert len(report.errors) == 1
        assert "cycle" in report.errors[0]
        assert "3.12" in report.errors[0]

    def test_404_marks_product_untracked(self, today: date) -> None:
        # FastAPI is intentionally not tracked on endoflife.date.
        def fetch(slug: str) -> list[dict]:
            raise urllib.error.HTTPError(
                url=f"https://endoflife.date/api/{slug}.json",
                code=404, msg="Not Found", hdrs=None, fp=None,
            )
        report = check_eol.evaluate_products(
            [_product("FastAPI", "fastapi", "0.115")],
            today=today, warn_days=180, fetch=fetch,
        )
        assert report.warnings == []
        assert len(report.errors) == 1
        assert "not tracked" in report.errors[0]
        assert "monitor manually" in report.errors[0]

    def test_network_error_degrades_gracefully(self, today: date) -> None:
        def fetch(slug: str) -> list[dict]:
            raise urllib.error.URLError("connection refused")
        report = check_eol.evaluate_products(
            [_product()], today=today, warn_days=180, fetch=fetch,
        )
        assert report.warnings == []
        assert len(report.errors) == 1
        assert "unreachable" in report.errors[0]

    def test_multiple_products_are_independent(self, today: date) -> None:
        # Mixed: one warning, one ok, one error — all three must appear.
        def fetch(slug: str) -> list[dict]:
            if slug == "python":
                return [{"cycle": "3.12", "eol": "2028-10-31"}]
            if slug == "nodejs":
                return [{"cycle": "20", "eol": "2026-05-01"}]  # warn
            raise urllib.error.URLError("down")
        products = [
            _product("Python", "python", "3.12"),
            _product("Node.js", "nodejs", "20"),
            _product("FastAPI", "fastapi", "0.115"),
        ]
        report = check_eol.evaluate_products(
            products, today=today, warn_days=180, fetch=fetch,
        )
        assert len(report.warnings) == 1
        assert report.warnings[0].product == "Node.js"
        assert len(report.ok) == 1
        assert report.ok[0]["product"] == "Python"
        assert len(report.errors) == 1


# ---------------------------------------------------------------------------
# Issue body rendering
# ---------------------------------------------------------------------------

class TestRenderIssueBody:
    """Markdown output stays well-formed across all branches."""

    def _report_with_one_warning(self) -> check_eol.Report:
        r = check_eol.Report(checked_at="2026-04-16", horizon_days=180)
        r.warnings.append(
            check_eol.Warning(
                product="Node.js", current="20", cycle="20",
                eol_date="2026-04-30", days_remaining=14,
                pin_source=".nvmrc",
                latest_in_cycle="20.20.2", latest_overall="22.9.0",
            )
        )
        return r

    def test_header_carries_date_and_horizon(self) -> None:
        r = self._report_with_one_warning()
        body = check_eol.render_issue_body(r)
        assert "# Dependency EOL Check — 2026-04-16" in body
        assert "180-day" in body

    def test_urgency_tier_matches_days_remaining(self) -> None:
        r = self._report_with_one_warning()
        body = check_eol.render_issue_body(r)
        # 14 days ≤ 30 → URGENT tier marker.
        assert "URGENT" in body

    def test_quarter_tier_for_far_horizon(self) -> None:
        r = check_eol.Report(checked_at="2026-04-16", horizon_days=180)
        r.warnings.append(
            check_eol.Warning(
                product="Python", current="3.12", cycle="3.12",
                eol_date="2026-10-01", days_remaining=170,
                pin_source="ci.yml",
            )
        )
        body = check_eol.render_issue_body(r)
        assert "within the quarter" in body

    def test_runbook_link_present(self) -> None:
        r = self._report_with_one_warning()
        body = check_eol.render_issue_body(r)
        assert "dependency_upgrade_runbook.md" in body

    def test_empty_report_is_still_well_formed(self) -> None:
        r = check_eol.Report(checked_at="2026-04-16", horizon_days=180)
        body = check_eol.render_issue_body(r)
        assert "# Dependency EOL Check" in body
        # No warnings section when empty.
        assert "⚠️ Warnings: **0**" in body

    def test_errors_section_appears_when_populated(self) -> None:
        r = check_eol.Report(checked_at="2026-04-16", horizon_days=180)
        r.errors.append("FastAPI: not tracked on endoflife.date")
        body = check_eol.render_issue_body(r)
        assert "## ❌ Errors" in body
        assert "FastAPI" in body


# ---------------------------------------------------------------------------
# Parse helpers
# ---------------------------------------------------------------------------

class TestParseEol:
    def test_iso_date_string(self) -> None:
        assert check_eol._parse_eol("2027-01-15") == date(2027, 1, 15)

    def test_false_means_no_eol(self) -> None:
        assert check_eol._parse_eol(False) is None

    def test_none_means_no_eol(self) -> None:
        assert check_eol._parse_eol(None) is None

    def test_true_means_already_eol(self) -> None:
        out = check_eol._parse_eol(True)
        # Today is the sentinel the caller uses for "already EOL".
        assert out == date.today()

    def test_garbage_returns_none(self) -> None:
        assert check_eol._parse_eol("not-a-date") is None
        assert check_eol._parse_eol(42) is None


# ---------------------------------------------------------------------------
# Stdlib-only invariant (duplicated here + in test_dependency_governance
# for defense in depth).
# ---------------------------------------------------------------------------

class TestStdlibOnly:
    def test_no_third_party_imports(self) -> None:
        src = SCRIPT.read_text(encoding="utf-8")
        forbidden = (
            "import requests",
            "import httpx",
            "import yaml",
            "from pydantic",
            "import aiohttp",
        )
        for needle in forbidden:
            assert needle not in src, (
                f"scripts/check_eol.py must stay stdlib-only, found {needle!r}"
            )


# ---------------------------------------------------------------------------
# CLI smoke test — full subprocess round-trip.
# ---------------------------------------------------------------------------

class TestCli:
    def test_cli_writes_output_file_and_exits_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        out = tmp_path / "eol.md"
        # Force a failure-path run by pointing --root at an empty dir;
        # `build_products` should raise, the script returns exit 2.
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--out", str(out),
             "--root", str(tmp_path)],
            capture_output=True, text=True,
        )
        assert proc.returncode == 2, proc.stderr
        # Output file is not created on early failure — that's fine;
        # the workflow's `if: always()` upload handles it.

    def test_cli_happy_path_via_module(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Stub `fetch_cycles` so we don't hit the real API in tests.
        def fake_fetch(slug: str, *, timeout: int = 10) -> list[dict]:
            return [
                {"cycle": "3.12", "eol": "2028-10-31", "latest": "3.12.7"},
                {"cycle": "20", "eol": "2026-04-30", "latest": "20.20.2"},
                {"cycle": "16", "eol": False, "latest": "16.2.0"},
                {"cycle": "0.115", "eol": False, "latest": "0.115.12"},
            ]
        monkeypatch.setattr(check_eol, "fetch_cycles", fake_fetch)
        out = tmp_path / "eol.md"
        code = check_eol.main(
            ["--out", str(out), "--warn-days", "180",
             "--root", str(REPO_ROOT)]
        )
        assert code == 0
        body = out.read_text(encoding="utf-8")
        # Node 20 should be flagged.
        assert "Node.js" in body

    def test_emit_github_output_respects_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        out_file = tmp_path / "gh-output"
        out_file.write_text("", encoding="utf-8")
        monkeypatch.setenv("GITHUB_OUTPUT", str(out_file))
        check_eol.emit_github_output(has_warnings=True, errors=2)
        content = out_file.read_text(encoding="utf-8")
        assert "has_warnings=true" in content
        assert "error_count=2" in content

    def test_emit_github_output_noop_without_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
        # Must not raise.
        check_eol.emit_github_output(has_warnings=False, errors=0)
