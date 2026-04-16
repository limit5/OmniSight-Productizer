"""N7 — unit tests for ``scripts/surface_deprecations.py``.

The script runs as the last step of every cell in the N7 multi-version
matrix workflow. Its job is to convert deprecation lines buried in
captured pytest / vitest / tsc logs into:

  * deduped GitHub Actions ``::warning`` annotations (capped at
    ``ANNOTATION_CAP`` so a runaway log can't drown the run page)
  * a markdown table appended to ``$GITHUB_STEP_SUMMARY``

These tests exercise:
  * line-level pattern matching for both Python and Node logs
  * dedup + cap behaviour of the annotation renderer
  * GH workflow-command escaping (``%``, ``\r``, ``\n``)
  * step-summary markdown shape, including the empty-log path
  * CLI entrypoint end-to-end (writing to a fake GITHUB_STEP_SUMMARY)
  * graceful handling of a missing log file
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "surface_deprecations.py"

# The script lives in `scripts/` (not under a package), so splice its
# directory onto sys.path the same way `test_check_eol.py` does.
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import surface_deprecations as sd  # noqa: E402


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

class TestPythonParser:
    def test_extracts_explicit_deprecation_warning(self):
        log = (
            "tests/test_x.py::test_foo PASSED\n"
            "tests/test_y.py::test_bar PASSED\n"
            "/usr/lib/python3.13/site-packages/somepkg/_legacy.py:42: "
            "DeprecationWarning: foo() is deprecated, use bar() instead\n"
        )
        findings = sd.parse_python_log(log)
        assert len(findings) == 1
        assert findings[0].source == "pytest"
        assert findings[0].line_no == 3
        assert "DeprecationWarning" in findings[0].message
        assert "foo() is deprecated" in findings[0].message

    def test_catches_pending_and_future_warning(self):
        log = (
            "x.py:1: PendingDeprecationWarning: scheduled for removal\n"
            "y.py:2: FutureWarning: signature will change in 2.0\n"
            "z.py:3: UserWarning: irrelevant\n"
        )
        findings = sd.parse_python_log(log)
        assert len(findings) == 2
        kinds = {f.line_no for f in findings}
        assert kinds == {1, 2}

    def test_empty_log_yields_no_findings(self):
        assert sd.parse_python_log("") == []

    def test_does_not_match_unrelated_lines(self):
        log = "tests/test_a.py PASSED\nINFO: server started\n"
        assert sd.parse_python_log(log) == []


class TestNodeParser:
    def test_extracts_dep_code_and_word(self):
        log = (
            "(node:1234) [DEP0040] DeprecationWarning: The `punycode` "
            "module is deprecated. Please use a userland alternative.\n"
            "vitest  warn  Deprecated: legacy fake timers\n"
            "RUN  v1.6.0\n"
        )
        findings = sd.parse_node_log(log)
        assert len(findings) == 2
        assert findings[0].line_no == 1
        assert "punycode" in findings[0].message

    def test_drops_known_noise(self):
        log = (
            "node --no-deprecation index.js   # noise\n"
            "import { deprecation_policy } from './x'   # noise\n"
            "(node:99) DeprecationWarning: real one\n"
        )
        findings = sd.parse_node_log(log)
        assert len(findings) == 1
        assert findings[0].line_no == 3

    def test_dispatches_via_parse_log(self):
        py_findings = sd.parse_log("x.py:1: DeprecationWarning: a\n", "python")
        node_findings = sd.parse_log("(node:1) DeprecationWarning: b\n", "node")
        assert len(py_findings) == 1
        assert len(node_findings) == 1

    def test_unknown_kind_raises(self):
        with pytest.raises(ValueError):
            sd.parse_log("anything", "rust")


# ---------------------------------------------------------------------------
# Annotation rendering
# ---------------------------------------------------------------------------

class TestAnnotations:
    def test_dedupes_identical_messages(self):
        # pytest's warning-summary block emits the same warning text
        # on consecutive lines when the same call-site triggered N times
        # across the suite — collapse to a single annotation. Different
        # file:line prefixes are intentionally NOT collapsed because
        # they represent different call sites.
        log = "\n".join(
            "DeprecationWarning: foo() is deprecated, use bar()"
            for _ in range(3)
        )
        findings = sd.parse_python_log(log)
        out = sd.render_annotations(findings, "x.log", "py3.13")
        assert len(out) == 1
        assert out[0].startswith("::warning ")
        assert "[py3.13]" in out[0]

    def test_distinct_call_sites_are_not_collapsed(self):
        # Same warning message at different file:line positions ARE
        # different findings — that's information the operator needs to
        # judge upgrade scope.
        log = "\n".join(
            f"x.py:{i}: DeprecationWarning: foo() is deprecated, use bar()"
            for i in range(1, 4)
        )
        findings = sd.parse_python_log(log)
        out = sd.render_annotations(findings, "x.log", "py3.13")
        assert len(out) == 3

    def test_caps_at_annotation_limit(self):
        findings = [
            sd.Finding(source="pytest", message=f"msg-{i}", line_no=i, raw=f"r{i}")
            for i in range(sd.ANNOTATION_CAP + 5)
        ]
        out = sd.render_annotations(findings, "x.log", "py3.13")
        # ANNOTATION_CAP normal entries plus one trailing "N more" line.
        assert len(out) == sd.ANNOTATION_CAP + 1
        assert "more deprecation" in out[-1]

    def test_escapes_workflow_command_specials(self):
        findings = [
            sd.Finding(
                source="pytest",
                message="100% busted\nsecond line",
                line_no=7,
                raw="raw",
            ),
        ]
        out = sd.render_annotations(findings, "weird path%name.log", "lbl")
        line = out[0]
        # Both the file= field and the message must be GH-escaped.
        assert "weird path%25name.log" in line
        assert "%25" in line     # the % in the message
        assert "%0A" in line     # the newline in the message
        # Line numbers are still passed through cleanly.
        assert "line=7" in line


# ---------------------------------------------------------------------------
# Step summary rendering
# ---------------------------------------------------------------------------

class TestSummary:
    def test_empty_findings_renders_ok_state(self):
        body = sd.render_summary([], "py3.13", "x.log")
        assert "py3.13" in body
        assert "No deprecation warnings detected" in body

    def test_aggregates_by_count(self):
        findings = [
            sd.Finding("pytest", "alpha", 1, "r"),
            sd.Finding("pytest", "alpha", 2, "r"),
            sd.Finding("pytest", "alpha", 3, "r"),
            sd.Finding("pytest", "beta", 4, "r"),
        ]
        body = sd.render_summary(findings, "py3.13", "x.log")
        assert "**4** total occurrence(s)" in body
        assert "**2** unique message(s)" in body
        # alpha should appear before beta (3 vs 1).
        alpha_idx = body.index("alpha")
        beta_idx = body.index("beta")
        assert alpha_idx < beta_idx

    def test_escapes_pipe_in_messages(self):
        findings = [sd.Finding("pytest", "a | b", 1, "r")]
        body = sd.render_summary(findings, "lbl", "x.log")
        assert r"a \| b" in body


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

class TestCLI:
    def test_main_writes_summary_and_returns_zero(self, tmp_path, capsys, monkeypatch):
        log = tmp_path / "pytest.log"
        log.write_text(
            "x.py:1: DeprecationWarning: foo() is deprecated\n"
            "y.py:2: DeprecationWarning: bar() is deprecated\n",
            encoding="utf-8",
        )
        summary = tmp_path / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))

        rc = sd.main(["--log", str(log), "--kind", "python", "--label", "py3.13"])
        assert rc == 0

        # Two unique annotations on stdout.
        out = capsys.readouterr().out
        assert out.count("::warning ") == 2
        assert "[py3.13]" in out

        # Summary file got the markdown table appended.
        body = summary.read_text(encoding="utf-8")
        assert "Deprecation warnings" in body
        assert "py3.13" in body
        assert "foo()" in body and "bar()" in body

    def test_main_handles_missing_log(self, tmp_path, capsys, monkeypatch):
        summary = tmp_path / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))

        rc = sd.main([
            "--log", str(tmp_path / "does-not-exist.log"),
            "--kind", "python",
            "--label", "py3.13",
        ])
        assert rc == 0
        # No annotations, but the summary still records the empty state.
        out = capsys.readouterr().out
        assert "::warning" not in out
        body = summary.read_text(encoding="utf-8")
        assert "No deprecation warnings detected" in body

    def test_main_runs_as_subprocess(self, tmp_path):
        """End-to-end smoke: invoke the script via the same python the
        CI workflow would use, to catch shebang / sys.path / arg parsing
        regressions that an in-process call would miss."""
        import subprocess

        log = tmp_path / "vitest.log"
        log.write_text(
            "(node:42) [DEP0040] DeprecationWarning: punycode is deprecated\n",
            encoding="utf-8",
        )
        env = os.environ.copy()
        env.pop("GITHUB_STEP_SUMMARY", None)
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--log", str(log),
                "--kind", "node",
                "--label", "node22-vitest",
            ],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert "::warning " in result.stdout
        assert "node22-vitest" in result.stdout
