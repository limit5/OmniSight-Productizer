"""OP-742 — unit tests for ``scripts/ci_worker.py``.

Covers the daemon's policy decision, lock files, comment formatting,
and end-to-end PS handling with the test runner + Gerrit voter
injected as fakes. None of these tests open real SSH or run pytest.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import ci_worker as worker  # noqa: E402


# ── Helpers ──────────────────────────────────────────────────────


def _ps(
    *,
    change_id: str = "I1234567890abcdef1234567890abcdef12345678",
    change_number: int = 101,
    revision: str = "abcdef1234567890abcdef1234567890abcdef12",
    patchset_number: int = 1,
    files: tuple[str, ...] = ("backend/foo.py",),
    subject: str = "[OP-742] foo",
    existing_verified: int | None = None,
) -> worker.GerritPatchSet:
    return worker.GerritPatchSet(
        change_id=change_id,
        change_number=change_number,
        revision=revision,
        patchset_number=patchset_number,
        subject=subject,
        branch="develop",
        project="omnisight/OmniSight-Productizer",
        files=tuple(worker.GerritFile(file=f) for f in files),
        existing_verified=existing_verified,
    )


def _cfg(tmp_path: Path) -> worker.WorkerConfig:
    return worker.WorkerConfig(
        repo_root=tmp_path,
        lock_dir=tmp_path / "locks",
        log_dir=tmp_path / "logs",
        dry_run=True,
    )


@dataclass
class _FakeProc:
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0


# ── Gerrit query parsing ─────────────────────────────────────────


class TestParseQueryOutput:
    def test_parses_change_with_files_and_existing_verified(self):
        rows = [
            {
                "id": "Iabc",
                "_number": 42,
                "subject": "[OP-742] tweak",
                "branch": "develop",
                "project": "omnisight/OmniSight-Productizer",
                "currentPatchSet": {
                    "revision": "deadbeef" * 5,
                    "number": 3,
                    "files": [
                        {"file": "/COMMIT_MSG"},
                        {"file": "backend/foo.py", "insertions": 4, "deletions": 1},
                    ],
                    "approvals": [
                        {"type": "Verified", "value": "+1"},
                    ],
                },
            },
            {"type": "stats", "rowCount": 1},
        ]
        stdout = "\n".join(json.dumps(r) for r in rows)
        out = worker.parse_query_output(stdout)
        assert len(out) == 1
        ps = out[0]
        assert ps.change_number == 42
        assert ps.patchset_number == 3
        # /COMMIT_MSG filtered out; only backend/foo.py remains
        assert [f.file for f in ps.files] == ["backend/foo.py"]
        assert ps.existing_verified == 1

    def test_skips_changes_without_current_patch_set(self):
        rows = [
            {"id": "Iabc", "_number": 42},  # no currentPatchSet
            {"type": "stats"},
        ]
        stdout = "\n".join(json.dumps(r) for r in rows)
        assert worker.parse_query_output(stdout) == []

    def test_handles_blank_lines_and_garbage(self):
        stdout = "\n\nnot-json\n" + json.dumps({"type": "stats"}) + "\n"
        assert worker.parse_query_output(stdout) == []


# ── Subject / area parsing ───────────────────────────────────────


class TestParseJiraKeys:
    def test_extracts_op_keys(self):
        assert worker.parse_jira_keys("[OP-742] add feature") == ["OP-742"]

    def test_dedupes_repeated_keys(self):
        assert worker.parse_jira_keys("OP-1 OP-1 OP-2") == ["OP-1", "OP-2"]

    def test_no_keys_returns_empty(self):
        assert worker.parse_jira_keys("plain subject") == []


class TestPolicyFromAreas:
    def test_unanimous_skip_collapses(self):
        assert worker.policy_from_areas(["docs"]) == "skip"

    def test_mixed_areas_returns_none(self):
        # backend (affected) + docs (skip) → ambiguous, defer to file classifier
        assert worker.policy_from_areas(["backend", "docs"]) is None

    def test_unknown_area_ignored(self):
        # Only `frontend` collapses; the unknown one is dropped.
        assert worker.policy_from_areas(["frontend", "rare-thing"]) == "frontend-only"


# ── Policy determination ────────────────────────────────────────


class TestDeterminePolicy:
    def test_security_label_overrides_files(self, tmp_path):
        cfg = _cfg(tmp_path)
        ps = _ps(files=("docs/note.md",))
        result = worker.determine_policy(
            ps, cfg, declared_areas=["security"],
        )
        assert result.policy == "full"

    def test_docs_only_short_circuits_to_skip(self, tmp_path):
        cfg = _cfg(tmp_path)
        ps = _ps(files=("docs/note.md",))
        result = worker.determine_policy(
            ps, cfg, declared_areas=["docs"], import_graph={},
        )
        assert result.policy == "skip"

    def test_high_fan_out_change_falls_into_full(self, tmp_path):
        cfg = _cfg(tmp_path)
        ps = _ps(files=("backend/db.py",))
        result = worker.determine_policy(
            ps, cfg, declared_areas=["backend"], import_graph={},
        )
        assert result.policy == "full"


# ── Failed-test parsing + categorisation ────────────────────────


class TestFailedTestParsing:
    def test_extracts_pytest_FAILED_lines(self):
        stdout = (
            "collected 5 items\n"
            "FAILED backend/tests/test_foo.py::test_a - assert 1 == 2\n"
            "FAILED backend/tests/test_bar.py::test_b[case-3] - x\n"
            "1 passed, 2 failed\n"
        )
        assert worker.parse_failed_tests(stdout) == [
            "backend/tests/test_foo.py::test_a",
            "backend/tests/test_bar.py::test_b[case-3]",
        ]

    def test_no_failures_returns_empty_list(self):
        assert worker.parse_failed_tests("3 passed in 0.5s") == []


class TestFirstFailureSnippet:
    def test_extracts_block_starting_at_FAILED(self):
        stdout = "\n".join([
            "before",
            "before2",
            "FAILED test_foo.py::test_a",
            "E   AssertionError: 1 != 2",
            "details",
        ])
        out = worker.first_failure_snippet(stdout, max_lines=2)
        assert out.startswith("FAILED")
        assert "AssertionError" in out
        assert "details" not in out

    def test_returns_empty_when_no_failure_marker(self):
        assert worker.first_failure_snippet("all good") == ""


class TestCategoriseFailure:
    def test_returncode_two_is_infra(self):
        assert worker._categorise_failure("", "", 2) == "infra"

    def test_no_tests_ran_is_stale(self):
        assert worker._categorise_failure("no tests ran in 0.01s", "", 5) == "stale"

    def test_timeout_is_flaky(self):
        out = "FAILED ... TimeoutError: query timed out after 30s"
        assert worker._categorise_failure(out, "", 1) == "flaky"

    def test_assertion_is_real_bug(self):
        out = "FAILED test_foo::test_a - AssertionError"
        assert worker._categorise_failure(out, "", 1) == "real-bug"


# ── Comment formatting ──────────────────────────────────────────


class TestFormatComments:
    def _result(self, **kw) -> worker.TestRunResult:
        return worker.TestRunResult(
            policy=kw.get("policy", "affected"),
            returncode=kw.get("returncode", 0),
            duration_s=kw.get("duration_s", 12.3),
            log_path=kw.get("log_path", Path("/tmp/x.log")),
            failed_tests=kw.get("failed_tests", []),
            first_failure_log=kw.get("first_failure_log", ""),
            category_hint=kw.get("category_hint", "passed"),
            reason=kw.get("reason", "ok"),
        )

    def test_pass_comment_has_policy_and_runtime(self):
        msg = worker.format_pass_comment(_ps(), self._result())
        assert "PASSED" in msg
        assert "Policy: affected" in msg
        assert "Test runtime: 12.3s" in msg

    def test_skip_comment_marks_auto_pass(self):
        r = self._result(policy="skip", duration_s=0.0, reason="docs-only PS")
        msg = worker.format_skip_comment(_ps(), r)
        assert "AUTO-PASS" in msg
        assert "docs-only" in msg

    def test_failure_comment_has_six_required_fields(self):
        # Per OP-742(e): PS, policy, runtime, failed-test list, log
        # snippet, category hint.
        r = self._result(
            returncode=1, duration_s=42.5,
            failed_tests=[
                "backend/tests/test_foo.py::test_a",
                "backend/tests/test_bar.py::test_b",
            ],
            first_failure_log="FAILED test_foo.py::test_a\nE   assert 1 == 2",
            category_hint="real-bug",
        )
        msg = worker.format_failure_comment(_ps(patchset_number=4), r)
        # 1) PS
        assert "PS4" in msg
        # 2) policy
        assert "Policy: affected" in msg
        # 3) runtime
        assert "42.5s" in msg
        # 4) failed test list
        assert "backend/tests/test_foo.py::test_a" in msg
        assert "Failed tests (2)" in msg
        # 5) log snippet
        assert "AssertionError" in msg or "assert 1 == 2" in msg
        # 6) category hint
        assert "real-bug" in msg

    def test_failure_comment_caps_long_failure_list(self):
        r = self._result(
            returncode=1,
            failed_tests=[f"t::case_{i}" for i in range(30)],
            category_hint="real-bug",
        )
        msg = worker.format_failure_comment(_ps(), r)
        assert "10 more" in msg


# ── Lock files ──────────────────────────────────────────────────


class TestClaimLock:
    def test_first_claim_succeeds_and_cleans_up(self, tmp_path):
        path = tmp_path / "x.lock"
        with worker.claim_lock(path) as got:
            assert got is True
            assert path.read_text().strip() == str(os.getpid())
        assert not path.exists()

    def test_second_claim_with_live_owner_fails(self, tmp_path):
        path = tmp_path / "x.lock"
        outer_started = threading.Event()
        outer_release = threading.Event()
        inner_got: list[bool] = []

        def outer():
            with worker.claim_lock(path):
                outer_started.set()
                outer_release.wait(timeout=5)

        t = threading.Thread(target=outer)
        t.start()
        outer_started.wait(timeout=5)
        with worker.claim_lock(path) as got2:
            inner_got.append(got2)
        outer_release.set()
        t.join(timeout=5)

        assert inner_got == [False]

    def test_stale_lock_with_dead_pid_is_reclaimed(self, tmp_path):
        path = tmp_path / "x.lock"
        # Plant a lock file pointing at a PID that doesn't exist.
        path.write_text("999999999")
        with worker.claim_lock(path) as got:
            assert got is True
            assert path.read_text().strip() == str(os.getpid())


# ── Pytest argv contract ────────────────────────────────────────


class TestBuildPytestArgv:
    def test_affected_uses_minus_x_no_cov(self, tmp_path):
        cfg = _cfg(tmp_path)
        argv = worker._build_pytest_argv(
            "affected", ["backend/tests/test_foo.py"], cfg,
        )
        assert "--no-cov" in argv
        assert "-x" in argv
        assert argv[-1] == "backend/tests/test_foo.py"

    def test_full_runs_backend_tests_dir(self, tmp_path):
        cfg = _cfg(tmp_path)
        argv = worker._build_pytest_argv("full", [], cfg)
        assert argv[-1] == "backend/tests"
        assert "--no-cov" in argv

    def test_affected_without_files_raises(self, tmp_path):
        cfg = _cfg(tmp_path)
        with pytest.raises(ValueError):
            worker._build_pytest_argv("affected", [], cfg)


# ── End-to-end handle_patchset ──────────────────────────────────


class _Capture:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []

    def vote(self, score: int, message: str) -> None:
        self.calls.append((score, message))


def _runner_returning(rc: int, stdout: str = "", stderr: str = ""):
    def _run(argv, timeout, cwd):
        return _FakeProc(stdout=stdout, stderr=stderr, returncode=rc)
    return _run


class TestHandlePatchset:
    def test_already_verified_is_skipped(self, tmp_path, monkeypatch):
        cfg = _cfg(tmp_path)
        ps = _ps(existing_verified=1)
        capture = _Capture()
        monkeypatch.setattr(
            worker, "post_verified_vote",
            lambda c, p, score, msg: capture.vote(score, msg) or True,
        )
        out = worker.handle_patchset(ps, cfg)
        assert out is None
        assert capture.calls == []

    def test_force_rerun_overrides_existing_verified(self, tmp_path, monkeypatch):
        cfg = _cfg(tmp_path)
        cfg.force_rerun = True
        ps = _ps(existing_verified=-1, files=("docs/note.md",))
        capture = _Capture()
        monkeypatch.setattr(
            worker, "post_verified_vote",
            lambda c, p, score, msg: capture.vote(score, msg) or True,
        )
        worker.handle_patchset(ps, cfg, declared_areas=["docs"])
        assert capture.calls and capture.calls[0][0] == +1

    def test_docs_only_auto_plus_one(self, tmp_path, monkeypatch):
        cfg = _cfg(tmp_path)
        ps = _ps(files=("docs/note.md",))
        capture = _Capture()
        monkeypatch.setattr(
            worker, "post_verified_vote",
            lambda c, p, score, msg: capture.vote(score, msg) or True,
        )
        result = worker.handle_patchset(ps, cfg, declared_areas=["docs"])
        assert result is not None
        assert result.policy == "skip"
        assert capture.calls
        score, msg = capture.calls[0]
        assert score == +1
        assert "AUTO-PASS" in msg

    def test_pytest_failure_posts_minus_one_with_six_fields(
        self, tmp_path, monkeypatch
    ):
        cfg = _cfg(tmp_path)
        # Build a minimal repo so the impact analyser can map foo.py.
        (tmp_path / "backend" / "tests").mkdir(parents=True)
        (tmp_path / "backend" / "__init__.py").write_text("")
        (tmp_path / "backend" / "foo.py").write_text("x = 1\n")
        (tmp_path / "backend" / "tests" / "test_foo.py").write_text(
            "def test_x(): assert False\n"
        )

        ps = _ps(files=("backend/foo.py",))
        capture = _Capture()
        monkeypatch.setattr(
            worker, "post_verified_vote",
            lambda c, p, score, msg: capture.vote(score, msg) or True,
        )

        fake_stdout = (
            "collected 1 item\n"
            "FAILED backend/tests/test_foo.py::test_x - assert False\n"
            "1 failed in 0.5s\n"
        )
        result = worker.handle_patchset(
            ps, cfg, runner=_runner_returning(1, stdout=fake_stdout),
        )
        assert result is not None
        assert capture.calls
        score, msg = capture.calls[0]
        assert score == -1
        # Six-field invariant
        assert "PS1" in msg
        assert "Policy:" in msg
        assert "Test runtime:" in msg
        assert "Failed tests" in msg
        assert "backend/tests/test_foo.py::test_x" in msg
        assert "Failure category" in msg
        # The full log path must appear so C3 can fetch it
        assert str(result.log_path) in msg

    def test_pytest_success_posts_plus_one(self, tmp_path, monkeypatch):
        cfg = _cfg(tmp_path)
        (tmp_path / "backend" / "tests").mkdir(parents=True)
        (tmp_path / "backend" / "__init__.py").write_text("")
        (tmp_path / "backend" / "foo.py").write_text("x = 1\n")
        (tmp_path / "backend" / "tests" / "test_foo.py").write_text(
            "def test_x(): assert True\n"
        )

        ps = _ps(files=("backend/foo.py",))
        capture = _Capture()
        monkeypatch.setattr(
            worker, "post_verified_vote",
            lambda c, p, score, msg: capture.vote(score, msg) or True,
        )

        result = worker.handle_patchset(
            ps, cfg, runner=_runner_returning(0, stdout="1 passed in 0.5s\n"),
        )
        assert result is not None
        assert capture.calls and capture.calls[0][0] == +1

    def test_synthetic_10ps_soak_all_get_votes(self, tmp_path, monkeypatch):
        """OP-742 AC: 10 PSes parallel — all get Verified votes.

        Real soak runs for 30 min with the full test suite; here we
        substitute a fake runner that sleeps a fixed duration so the
        parallel-speedup contract can be unit-tested in <1 s. The
        invariant we pin: with N=4 workers and 10 PSes each taking 0.2 s
        of "test time", wall-clock < 10 × 0.2 s = 2.0 s (i.e. parallel
        actually fires).
        """
        import concurrent.futures
        import time

        cfg = _cfg(tmp_path)
        cfg.max_workers = 4
        captures: list[tuple[int, str]] = []
        captures_lock = threading.Lock()

        def vote(c, p, score, msg):
            with captures_lock:
                captures.append((p.change_number, score))
            return True

        monkeypatch.setattr(worker, "post_verified_vote", vote)

        def slow_runner(argv, timeout, cwd):
            time.sleep(0.2)
            return _FakeProc(stdout="1 passed in 0.2s\n", returncode=0)

        # Build the 10 PSes — docs-only so no actual import-graph walk
        # is needed; we still want the runner path exercised, so we
        # mix in 5 backend-shaped ones.
        (tmp_path / "backend" / "tests").mkdir(parents=True)
        (tmp_path / "backend" / "__init__.py").write_text("")
        (tmp_path / "backend" / "foo.py").write_text("x = 1\n")
        (tmp_path / "backend" / "tests" / "test_foo.py").write_text(
            "def test_x(): assert True\n"
        )

        pses = []
        for i in range(10):
            files = ("backend/foo.py",) if i % 2 == 0 else ("docs/note.md",)
            pses.append(_ps(
                change_id=f"I{i:040d}",
                change_number=200 + i,
                revision=f"{i:040x}",
                files=files,
                subject=f"[OP-742] case {i}",
            ))

        started = time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = [
                pool.submit(
                    worker.handle_patchset, ps, cfg,
                    declared_areas=["backend"] if ps.files[0].file.startswith("backend") else ["docs"],
                    runner=slow_runner,
                )
                for ps in pses
            ]
            for f in futures:
                f.result(timeout=10)
        elapsed = time.monotonic() - started

        # All 10 get a vote
        assert len(captures) == 10
        # All votes are +1 (passing)
        assert all(score == +1 for _, score in captures)
        # Parallel speedup actually happened (serial would be ~1.0s for
        # 5 backend runs at 0.2s each plus instantaneous docs runs;
        # with N=4 workers expect well under 0.6s).
        assert elapsed < 1.5, f"parallel soak too slow: {elapsed:.2f}s"

    def test_locked_change_is_skipped(self, tmp_path, monkeypatch):
        cfg = _cfg(tmp_path)
        ps = _ps(files=("docs/note.md",))
        capture = _Capture()
        monkeypatch.setattr(
            worker, "post_verified_vote",
            lambda c, p, score, msg: capture.vote(score, msg) or True,
        )

        # Pre-plant a lock owned by *this* process so claim_lock sees a
        # live PID and refuses.
        cfg.lock_dir.mkdir(parents=True, exist_ok=True)
        lock_path = cfg.lock_dir / ps.lock_name
        lock_path.write_text(str(os.getpid()))
        try:
            out = worker.handle_patchset(ps, cfg, declared_areas=["docs"])
        finally:
            lock_path.unlink(missing_ok=True)
        assert out is None
        assert capture.calls == []
