"""N5 — unit tests for the nightly upgrade-preview rendering script.

Covers:
  * version classification (`classify_bump`) on all the corner cases
    that surface in real pip / pnpm output (major, minor, patch,
    pre-1.0 series, unparsable strings, leading 'v').
  * pip / pnpm `--outdated` JSON parsers (empty, malformed, both
    pnpm-9 shapes — top-level dict and `{"packages": ...}` envelope).
  * watchlist behaviour — strategic packages (langchain, next, …)
    get flagged even when the SemVer bump alone wouldn't qualify.
  * issue-body renderer (`render_issue_body`) shape + truncation
    behaviour (diff line cap, log tail, over-budget body drops the
    diffs but keeps the rest).
  * CLI entrypoint via subprocess against a temp output path.

The script is deliberately stdlib-only so these tests run in any
Python 3.12 environment without extras.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "upgrade_preview.py"

# Make the script importable — it lives outside `backend/` so the
# regular package import path doesn't reach it.
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import upgrade_preview as up  # noqa: E402  (deliberate post-sys.path insert)


# ─────────────────────────────────────────────────────────────────────
# classify_bump
# ─────────────────────────────────────────────────────────────────────


class TestClassifyBump:
    @pytest.mark.parametrize(
        "current,latest,expected_kind,expected_breaking",
        [
            # Major: leading int change
            ("1.0.0", "2.0.0", "major", True),
            ("1.5.7", "2.0.0", "major", True),
            ("v1.0.0", "v2.0.0", "major", True),  # leading 'v' tolerated
            # Minor on stable: not breaking
            ("1.0.0", "1.1.0", "minor", False),
            ("2.5.7", "2.6.0", "minor", False),
            # Patch: never breaking
            ("1.0.0", "1.0.1", "patch", False),
            ("1.0.0", "1.0.99", "patch", False),
            # Same version: patch + not breaking (no-op)
            ("1.0.0", "1.0.0", "patch", False),
            # 0.x minor: SemVer convention treats as breaking
            ("0.1.0", "0.2.0", "minor", True),
            ("0.5.7", "0.6.0", "minor", True),
            # 0.x patch: not breaking
            ("0.1.0", "0.1.1", "patch", False),
            # Unparsable: unknown + breaking (safer default)
            ("foo", "bar", "unknown", True),
            ("", "1.0.0", "unknown", True),
        ],
    )
    def test_table(self, current, latest, expected_kind, expected_breaking):
        kind, breaking = up.classify_bump(current, latest)
        assert kind == expected_kind, f"kind: {current}->{latest}"
        assert breaking == expected_breaking, f"breaking: {current}->{latest}"


# ─────────────────────────────────────────────────────────────────────
# parse_pip_outdated
# ─────────────────────────────────────────────────────────────────────


class TestParsePipOutdated:
    def test_empty_string(self):
        assert up.parse_pip_outdated("") == []

    def test_empty_array(self):
        assert up.parse_pip_outdated("[]") == []

    def test_malformed_json(self):
        # Must not raise — preview is best-effort.
        assert up.parse_pip_outdated("{not valid json") == []

    def test_basic_entry(self):
        items = up.parse_pip_outdated(json.dumps([
            {"name": "requests", "version": "2.28.0",
             "latest_version": "2.31.0", "latest_filetype": "wheel"},
        ]))
        assert len(items) == 1
        assert items[0].name == "requests"
        assert items[0].current == "2.28.0"
        assert items[0].latest == "2.31.0"
        assert items[0].bump == "minor"
        assert items[0].breaking is False
        assert items[0].extra == "wheel"

    def test_drops_entries_missing_required_fields(self):
        items = up.parse_pip_outdated(json.dumps([
            {"name": "good", "version": "1.0.0", "latest_version": "1.0.1"},
            {"name": "missing-latest", "version": "1.0.0"},
            {"version": "1.0.0", "latest_version": "1.0.1"},  # missing name
        ]))
        names = [i.name for i in items]
        assert names == ["good"]

    def test_watchlist_promotes_to_breaking(self):
        # langchain-* is on the watchlist — even a patch bump should be
        # flagged so an operator gives it a manual look.
        items = up.parse_pip_outdated(json.dumps([
            {"name": "langchain-core", "version": "0.3.74",
             "latest_version": "0.3.75", "latest_filetype": "wheel"},
        ]))
        assert items[0].breaking is True

    def test_sort_breaking_first(self):
        items = up.parse_pip_outdated(json.dumps([
            {"name": "zzz-safe", "version": "1.0.0", "latest_version": "1.0.1"},
            {"name": "aaa-breaking", "version": "1.0.0", "latest_version": "2.0.0"},
        ]))
        # breaking first, then alphabetical within each group
        assert items[0].name == "aaa-breaking"
        assert items[1].name == "zzz-safe"


# ─────────────────────────────────────────────────────────────────────
# parse_pnpm_outdated
# ─────────────────────────────────────────────────────────────────────


class TestParsePnpmOutdated:
    def test_empty(self):
        assert up.parse_pnpm_outdated("") == []
        assert up.parse_pnpm_outdated("{}") == []

    def test_top_level_dict_shape(self):
        items = up.parse_pnpm_outdated(json.dumps({
            "@radix-ui/react-dialog": {
                "current": "1.1.0", "wanted": "1.1.5", "latest": "1.2.0",
                "dependencyType": "dependencies",
            },
        }))
        assert len(items) == 1
        assert items[0].name == "@radix-ui/react-dialog"
        assert items[0].breaking is True  # @radix-ui watchlisted

    def test_packages_envelope_shape(self):
        # pnpm 9 with --long sometimes wraps in {"packages": {...}}.
        items = up.parse_pnpm_outdated(json.dumps({
            "packages": {
                "lodash": {"current": "4.17.20", "latest": "4.17.21",
                           "dependencyType": "dependencies"},
            },
        }))
        assert len(items) == 1
        assert items[0].name == "lodash"
        assert items[0].bump == "patch"
        assert items[0].breaking is False

    def test_uses_latest_not_wanted(self):
        # The preview asks "what would Renovate try?" — Renovate uses
        # latest, not the semver-range-respecting wanted.
        items = up.parse_pnpm_outdated(json.dumps({
            "foo": {"current": "1.0.0", "wanted": "1.5.0", "latest": "2.0.0",
                    "dependencyType": "dependencies"},
        }))
        assert items[0].latest == "2.0.0"
        assert items[0].bump == "major"

    def test_malformed(self):
        assert up.parse_pnpm_outdated("not json") == []

    def test_skips_non_dict_entries(self):
        items = up.parse_pnpm_outdated(json.dumps({
            "good": {"current": "1.0.0", "latest": "1.0.1"},
            "bad": "not-a-dict",
        }))
        assert [i.name for i in items] == ["good"]


# ─────────────────────────────────────────────────────────────────────
# render_issue_body
# ─────────────────────────────────────────────────────────────────────


class TestRenderIssueBody:
    def test_empty_report_renders_cleanly(self):
        body = up.render_issue_body(up.Report(), date_str="2026-04-16")
        assert "# Nightly Dependency Upgrade Preview" in body
        assert "2026-04-16" in body
        # Sections present even when empty
        assert "## Summary" in body
        assert "## Suspected breaking" in body
        assert "## pip outdated (0)" in body
        assert "## pnpm outdated (0)" in body
        assert "_None detected._" in body
        assert "_No outdated packages" in body

    def test_summary_table_reflects_step_outcomes(self):
        report = up.Report(
            pytest_status="success",
            playwright_status="failure",
            pip_install_status="success",
            npm_install_status="skipped",
        )
        body = up.render_issue_body(report)
        assert "✅ success" in body
        assert "❌ failure" in body
        assert "⚪ skipped" in body

    def test_breaking_items_listed(self):
        report = up.Report(
            pip_outdated=[
                up.OutdatedItem("pydantic", "1.10.0", "2.5.0", "major", True),
                up.OutdatedItem("requests", "2.28.0", "2.31.0", "minor", False),
            ],
            npm_outdated=[
                up.OutdatedItem("next", "15.0.0", "16.2.0", "major", True),
            ],
        )
        body = up.render_issue_body(report)
        # 2 breaking
        assert "Suspected breaking (2)" in body
        # Both flagged with the fire emoji
        assert "🔥 **pydantic**" in body
        assert "🔥 **next**" in body
        # Non-breaking pkg appears in outdated table but not in
        # breaking section.
        assert "requests" in body

    def test_diff_truncation(self):
        long_diff = "\n".join(f"+ line {i}" for i in range(500))
        report = up.Report(pip_diff=long_diff)
        body = up.render_issue_body(report)
        assert "truncated" in body
        # Should not embed all 500 lines
        assert body.count("+ line ") <= up.DIFF_LINES_CAP + 5

    def test_pytest_log_tail(self):
        long_log = "\n".join(f"PASS test_{i}" for i in range(200))
        report = up.Report(pytest_log=long_log, pytest_status="success")
        body = up.render_issue_body(report)
        # First lines NOT included; last lines included
        assert "PASS test_0" not in body
        assert "PASS test_199" in body

    def test_run_url_rendered(self):
        body = up.render_issue_body(up.Report(run_url="https://example/runs/42"),
                                    run_id="42")
        assert "42" in body
        assert "https://example/runs/42" in body

    def test_oversized_body_drops_diffs(self):
        # Oversized: ~80 KiB of diff text guarantees we trip the budget.
        huge = "x" * 80_000
        report = up.Report(pip_diff=huge, npm_diff=huge)
        body = up.render_issue_body(report)
        assert len(body.encode("utf-8")) <= up.ISSUE_BODY_MAX
        assert "Diff omitted (issue size budget)" in body
        # Other sections still rendered.
        assert "## Summary" in body


# ─────────────────────────────────────────────────────────────────────
# CLI smoke test
# ─────────────────────────────────────────────────────────────────────


class TestCli:
    def test_renders_to_disk(self, tmp_path: Path):
        pip_json = tmp_path / "pip.json"
        pip_json.write_text(json.dumps([
            {"name": "requests", "version": "2.28.0",
             "latest_version": "2.31.0", "latest_filetype": "wheel"},
        ]), encoding="utf-8")
        out = tmp_path / "issue.md"
        result = subprocess.run(
            [
                sys.executable, str(SCRIPT),
                "--pip-outdated", str(pip_json),
                "--out", str(out),
                "--date", "2026-04-16",
                "--pytest-status", "success",
                "--playwright-status", "skipped",
                "--pip-install-status", "success",
                "--pnpm-install-status", "success",
            ],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stderr
        body = out.read_text(encoding="utf-8")
        assert "2026-04-16" in body
        assert "requests" in body

    def test_missing_inputs_are_tolerated(self, tmp_path: Path):
        # All optional inputs missing — script should still render.
        out = tmp_path / "issue.md"
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--out", str(out), "--date", "2026-04-16"],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stderr
        body = out.read_text(encoding="utf-8")
        assert "Suspected breaking (0)" in body
