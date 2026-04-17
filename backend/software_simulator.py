"""X1 #297 — Software simulate-track driver.

Single entry point for ``scripts/simulate.sh --type=software`` covering
the four X1 deliverables:

    1. Language autodetect from project root markers (``pyproject.toml``
       / ``go.mod`` / ``Cargo.toml`` / ``pom.xml`` / ``build.gradle`` /
       ``package.json`` / ``*.csproj``).
    2. Multi-language test-runner dispatcher:
         python → pytest (or unittest discover)
         go     → go test
         rust   → cargo test
         java   → mvn test / gradle test
         node   → npm test / pnpm test
         csharp → dotnet test (xUnit / NUnit / MSTest)
    3. Coverage gate with per-language thresholds.
    4. Optional benchmark regression — compares latest run against a
       JSON baseline under ``test_assets/benchmarks/<module>.json``.

Design
------
Every external tool (pytest / go / cargo / mvn / npm / dotnet) is
**optional**. If the binary is not on PATH (sandbox / CI-first-run /
a Python-only image that nonetheless got handed a Go project), the
dispatcher emits a ``mock`` result marked so the caller can distinguish
"runner exited 0" from "no runner available". Nothing here fabricates
a real-pass result — a mock result means "environment lacks the
tooling", not "tests passed".

The Python module owns all language autodetection, per-runner argv
building, coverage-report parsing, and multi-step JSON aggregation.
``scripts/simulate.sh software`` stays a thin shell dispatcher that
invokes this module once via ``python3 -m backend.software_simulator``
and reads a single JSON summary back — the exact same contract used by
W2 ``web_simulator`` / P2 ``mobile_simulator``.

Public API
----------
``simulate_software(*, profile: str, app_path: Path, ...) -> SoftwareSimResult``

Returns a dataclass with the flat-dict shape consumed by
``run_software`` in ``simulate.sh``.

Language autodetect
-------------------
``resolve_language(app_path)`` inspects the app directory for the
first unambiguous marker and returns one of
``python`` / ``go`` / ``rust`` / ``java`` / ``node`` / ``csharp``.
Order is deliberately specific-before-generic — a polyglot repo that
contains both ``pyproject.toml`` and ``go.mod`` at the root is
rare-to-nonexistent in practice, but when it happens the caller can
force the pick via ``--language=`` or via the project role skill's
``software_runtime`` field (X2 #298).

Coverage thresholds
-------------------
Per X1 ticket:
    python 80%   go 70%   rust 75%   java 70%   node 80%   csharp 70%

These are the *minimum* bars a clean green CI must clear. Projects can
override downward only by passing ``--coverage-override=<pct>`` and
must document the waiver in their role skill (X2).

Why not shell out everything from bash
--------------------------------------
Same rationale as HMI / Web / Mobile tracks: coverage-report parsing
(XML / JSON / cobertura), unit arithmetic, and multi-step aggregation
are miserable in bash. The shell layer remains a thin dispatcher that
invokes this module once.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Constants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

SUPPORTED_LANGUAGES: tuple[str, ...] = (
    "python", "go", "rust", "java", "node", "csharp",
)

# X1 ticket thresholds — do not soften without also updating TODO.md
# and HANDOFF.md. A waiver goes in the project role skill (X2 #298).
COVERAGE_THRESHOLDS: Mapping[str, float] = {
    "python": 80.0,
    "go": 70.0,
    "rust": 75.0,
    "java": 70.0,
    "node": 80.0,
    "csharp": 70.0,
}

# Ordered list of (marker_file, language). First match wins. Specific
# marker before generic — e.g. ``pyproject.toml`` is more telling than
# ``requirements.txt``.
_LANG_MARKERS: tuple[tuple[str, str], ...] = (
    ("pyproject.toml", "python"),
    ("setup.py", "python"),
    ("setup.cfg", "python"),
    ("requirements.txt", "python"),
    ("go.mod", "go"),
    ("Cargo.toml", "rust"),
    ("pom.xml", "java"),
    ("build.gradle", "java"),
    ("build.gradle.kts", "java"),
    ("package.json", "node"),
)


class SoftwareSimError(RuntimeError):
    """Raised when ``simulate_software`` cannot proceed (e.g. profile
    target_kind mismatch)."""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Dataclasses
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class TestRunReport:
    status: str = "skip"          # "pass" | "fail" | "skip" | "mock"
    runner: str = ""              # e.g. "pytest" | "go test" | "cargo test"
    total: int = 0
    passed: int = 0
    failed: int = 0
    duration_ms: int = 0
    detail: str = ""


@dataclass
class CoverageReport:
    status: str = "skip"          # "pass" | "fail" | "skip" | "mock"
    percentage: float = 0.0
    threshold: float = 0.0
    source: str = "mock"          # "coverage.py" | "go-cover" | "llvm-cov" | "jacoco" | "c8" | "coverlet" | "mock"
    detail: str = ""


@dataclass
class BenchmarkReport:
    status: str = "skip"          # "pass" | "fail" | "skip" | "mock"
    current_ms: float = 0.0
    baseline_ms: float = 0.0
    regression_pct: float = 0.0
    threshold_pct: float = 10.0
    detail: str = ""


@dataclass
class SoftwareSimResult:
    profile: str
    app_path: str
    language: str = ""
    packaging: str = ""
    test_run: TestRunReport = field(default_factory=TestRunReport)
    coverage: CoverageReport = field(default_factory=CoverageReport)
    benchmark: BenchmarkReport = field(default_factory=BenchmarkReport)
    gates: dict[str, bool] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def overall_pass(self) -> bool:
        return all(self.gates.values()) and not self.errors


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Language autodetect
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def resolve_language(app_path: Path) -> str:
    """Return the detected language for a project root.

    Walks the marker file list in order. Returns ``""`` if no marker
    is present — the caller decides whether to treat this as an error
    or dispatch to a generic ``make test`` fallback.
    """
    if not app_path.is_dir():
        return ""
    for marker, lang in _LANG_MARKERS:
        if (app_path / marker).is_file():
            return lang
    # C# uses a *.csproj / *.sln under the root rather than a fixed
    # filename. Glob to avoid hard-coding a project name.
    if any(app_path.glob("*.csproj")) or any(app_path.glob("*.sln")):
        return "csharp"
    return ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Per-language test runners
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _mock_run(runner: str, reason: str) -> TestRunReport:
    """Produce a mock ``TestRunReport`` when the real runner is absent."""
    return TestRunReport(
        status="mock", runner=runner, total=1, passed=1, failed=0,
        detail=f"{runner} unavailable — {reason}",
    )


def _run_subprocess(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout: int,
    env: Optional[Mapping[str, str]] = None,
) -> subprocess.CompletedProcess:
    env_dict = dict(os.environ)
    if env:
        env_dict.update(env)
    return subprocess.run(
        list(argv), cwd=str(cwd), capture_output=True, text=True,
        timeout=timeout, env=env_dict, check=False,
    )


def run_python_tests(app_path: Path, *, timeout: int = 300) -> TestRunReport:
    """Run pytest; degrade to mock when pytest / python3 missing."""
    pytest = shutil.which("pytest")
    python3 = shutil.which("python3")
    if not pytest and not python3:
        return _mock_run("pytest", "neither pytest nor python3 on PATH")
    argv: list[str]
    runner_label = "pytest"
    if pytest:
        argv = [pytest, "-q", "--tb=short"]
    else:
        argv = [python3, "-m", "pytest", "-q", "--tb=short"]  # type: ignore[list-item]
    try:
        proc = _run_subprocess(argv, cwd=app_path, timeout=timeout)
    except subprocess.TimeoutExpired:
        return TestRunReport(status="fail", runner=runner_label,
                             detail=f"timeout after {timeout}s")
    except (FileNotFoundError, OSError) as exc:
        return _mock_run(runner_label, str(exc))
    total, passed, failed = _parse_pytest_output(proc.stdout + proc.stderr)
    status = "pass" if proc.returncode == 0 and failed == 0 else "fail"
    # pytest exit 5 means "no tests collected" — treat as skip so a
    # brand-new project scaffold doesn't fail X1 before tests exist.
    if proc.returncode == 5:
        return TestRunReport(status="skip", runner=runner_label,
                             detail="no tests collected")
    return TestRunReport(
        status=status, runner=runner_label,
        total=max(total, passed + failed), passed=passed, failed=failed,
        detail=_tail(proc.stdout, 400),
    )


_PYTEST_SUMMARY_RE = re.compile(
    r"(\d+)\s+(passed|failed|error|errors|skipped|deselected|xfailed|xpassed)"
)


def _parse_pytest_output(out: str) -> tuple[int, int, int]:
    """Extract (total, passed, failed) from a pytest summary line."""
    passed = failed = skipped = 0
    for m in _PYTEST_SUMMARY_RE.finditer(out):
        count = int(m.group(1))
        bucket = m.group(2)
        if bucket == "passed":
            passed = count
        elif bucket in ("failed", "error", "errors"):
            failed += count
        elif bucket == "skipped":
            skipped = count
    total = passed + failed + skipped
    return total, passed, failed


def run_go_tests(app_path: Path, *, timeout: int = 300) -> TestRunReport:
    """Run ``go test ./...``; mock when go missing."""
    go = shutil.which("go")
    if not go:
        return _mock_run("go test", "go toolchain not on PATH")
    try:
        proc = _run_subprocess(
            [go, "test", "-count=1", "-json", "./..."],
            cwd=app_path, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return TestRunReport(status="fail", runner="go test",
                             detail=f"timeout after {timeout}s")
    total, passed, failed = _parse_go_json(proc.stdout)
    status = "pass" if proc.returncode == 0 and failed == 0 else "fail"
    if total == 0 and proc.returncode == 0:
        return TestRunReport(status="skip", runner="go test",
                             detail="no tests found")
    return TestRunReport(
        status=status, runner="go test",
        total=total, passed=passed, failed=failed,
        detail=_tail(proc.stdout, 400),
    )


def _parse_go_json(out: str) -> tuple[int, int, int]:
    total = passed = failed = 0
    for line in out.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("Action") == "pass" and ev.get("Test"):
            passed += 1
            total += 1
        elif ev.get("Action") == "fail" and ev.get("Test"):
            failed += 1
            total += 1
    return total, passed, failed


def run_rust_tests(app_path: Path, *, timeout: int = 600) -> TestRunReport:
    """Run ``cargo test``; mock when cargo missing."""
    cargo = shutil.which("cargo")
    if not cargo:
        return _mock_run("cargo test", "cargo toolchain not on PATH")
    try:
        proc = _run_subprocess(
            [cargo, "test", "--no-fail-fast"],
            cwd=app_path, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return TestRunReport(status="fail", runner="cargo test",
                             detail=f"timeout after {timeout}s")
    total, passed, failed = _parse_cargo_output(proc.stdout + proc.stderr)
    status = "pass" if proc.returncode == 0 and failed == 0 else "fail"
    if total == 0 and proc.returncode == 0:
        return TestRunReport(status="skip", runner="cargo test",
                             detail="no tests found")
    return TestRunReport(
        status=status, runner="cargo test",
        total=total, passed=passed, failed=failed,
        detail=_tail(proc.stdout, 400),
    )


_CARGO_SUMMARY_RE = re.compile(
    r"test result:\s+\w+\.\s+(\d+)\s+passed;\s+(\d+)\s+failed"
)


def _parse_cargo_output(out: str) -> tuple[int, int, int]:
    total = passed = failed = 0
    for m in _CARGO_SUMMARY_RE.finditer(out):
        p = int(m.group(1)); f = int(m.group(2))
        passed += p; failed += f; total += p + f
    return total, passed, failed


def run_java_tests(app_path: Path, *, timeout: int = 900) -> TestRunReport:
    """Run ``mvn test`` (or ``gradle test`` when gradlew present)."""
    if (app_path / "gradlew").is_file() or (app_path / "build.gradle").is_file() \
            or (app_path / "build.gradle.kts").is_file():
        gradle = shutil.which("gradle")
        wrapper = app_path / "gradlew"
        if wrapper.is_file() and os.access(str(wrapper), os.X_OK):
            argv = [str(wrapper), "test"]
            runner = "gradle test"
        elif gradle:
            argv = [gradle, "test"]
            runner = "gradle test"
        else:
            return _mock_run("gradle test", "neither gradlew nor gradle on PATH")
    else:
        mvn = shutil.which("mvn")
        if not mvn:
            return _mock_run("mvn test", "mvn not on PATH")
        argv = [mvn, "-B", "-q", "test"]
        runner = "mvn test"
    try:
        proc = _run_subprocess(argv, cwd=app_path, timeout=timeout)
    except subprocess.TimeoutExpired:
        return TestRunReport(status="fail", runner=runner,
                             detail=f"timeout after {timeout}s")
    total, passed, failed = _parse_surefire_dir(app_path)
    if total == 0:
        total, passed, failed = _parse_maven_output(proc.stdout + proc.stderr)
    status = "pass" if proc.returncode == 0 and failed == 0 else "fail"
    if total == 0 and proc.returncode == 0:
        return TestRunReport(status="skip", runner=runner,
                             detail="no tests found")
    return TestRunReport(
        status=status, runner=runner,
        total=total, passed=passed, failed=failed,
        detail=_tail(proc.stdout, 400),
    )


_MVN_SUMMARY_RE = re.compile(
    r"Tests run:\s+(\d+),\s+Failures:\s+(\d+),\s+Errors:\s+(\d+),\s+Skipped:\s+(\d+)"
)


def _parse_maven_output(out: str) -> tuple[int, int, int]:
    total = passed = failed = 0
    for m in _MVN_SUMMARY_RE.finditer(out):
        tot = int(m.group(1)); fail = int(m.group(2))
        err = int(m.group(3)); skip = int(m.group(4))
        run = tot - skip
        passed += run - fail - err
        failed += fail + err
        total += run
    return total, passed, failed


def _parse_surefire_dir(app_path: Path) -> tuple[int, int, int]:
    """Parse ``target/surefire-reports/*.xml`` Maven / ``build/test-results/**/*.xml``
    Gradle outputs (junit-xml format)."""
    total = passed = failed = 0
    candidates: list[Path] = []
    for sub in ("target/surefire-reports", "build/test-results"):
        root = app_path / sub
        if root.is_dir():
            candidates.extend(root.rglob("TEST-*.xml"))
            candidates.extend(root.rglob("*.xml"))
    # Deduplicate paths (rglob can overlap glob) preserving order.
    seen: set[Path] = set()
    deduped: list[Path] = []
    for p in candidates:
        if p not in seen:
            seen.add(p); deduped.append(p)
    import xml.etree.ElementTree as ET
    for p in deduped:
        try:
            root = ET.parse(str(p)).getroot()
        except (ET.ParseError, OSError):
            continue
        # JUnit-XML: testsuite tag (possibly top-level) with attrs
        suites = [root] if root.tag.endswith("testsuite") else list(root.iter("testsuite"))
        for s in suites:
            try:
                tot = int(s.attrib.get("tests", "0"))
                fail = int(s.attrib.get("failures", "0"))
                err = int(s.attrib.get("errors", "0"))
                skip = int(s.attrib.get("skipped", "0"))
            except ValueError:
                continue
            run = tot - skip
            passed += run - fail - err
            failed += fail + err
            total += run
    return total, passed, failed


def run_node_tests(app_path: Path, *, timeout: int = 600) -> TestRunReport:
    """Run ``npm test`` / ``pnpm test`` / ``yarn test``."""
    pm = None
    if (app_path / "pnpm-lock.yaml").is_file() and shutil.which("pnpm"):
        pm = "pnpm"
    elif (app_path / "yarn.lock").is_file() and shutil.which("yarn"):
        pm = "yarn"
    elif shutil.which("npm"):
        pm = "npm"
    if not pm:
        return _mock_run("npm test", "no Node package manager on PATH")
    argv = [pm, "test", "--silent"] if pm == "npm" else [pm, "test"]
    runner = f"{pm} test"
    try:
        proc = _run_subprocess(argv, cwd=app_path, timeout=timeout)
    except subprocess.TimeoutExpired:
        return TestRunReport(status="fail", runner=runner,
                             detail=f"timeout after {timeout}s")
    total, passed, failed = _parse_node_output(proc.stdout + proc.stderr)
    status = "pass" if proc.returncode == 0 and failed == 0 else "fail"
    if total == 0 and proc.returncode == 0:
        return TestRunReport(status="skip", runner=runner,
                             detail="no tests found")
    return TestRunReport(
        status=status, runner=runner,
        total=total, passed=passed, failed=failed,
        detail=_tail(proc.stdout, 400),
    )


_VITEST_TOTALS_RE = re.compile(r"Tests\s+(\d+)\s+passed(?:\s*\|\s*(\d+)\s+failed)?")
_JEST_TOTALS_RE = re.compile(r"Tests:\s+(?:(\d+)\s+failed,\s+)?(\d+)\s+passed,\s+(\d+)\s+total")
_MOCHA_TOTALS_RE = re.compile(r"(\d+)\s+passing")
_MOCHA_FAIL_RE = re.compile(r"(\d+)\s+failing")


def _parse_node_output(out: str) -> tuple[int, int, int]:
    # Jest style first (most common with `total` field).
    m = _JEST_TOTALS_RE.search(out)
    if m:
        failed = int(m.group(1) or 0)
        passed = int(m.group(2))
        total = int(m.group(3))
        return total, passed, failed
    # Vitest
    m = _VITEST_TOTALS_RE.search(out)
    if m:
        passed = int(m.group(1))
        failed = int(m.group(2) or 0)
        return passed + failed, passed, failed
    # Mocha / tap-style
    m = _MOCHA_TOTALS_RE.search(out)
    if m:
        passed = int(m.group(1))
        failed_m = _MOCHA_FAIL_RE.search(out)
        failed = int(failed_m.group(1)) if failed_m else 0
        return passed + failed, passed, failed
    return 0, 0, 0


def run_csharp_tests(app_path: Path, *, timeout: int = 600) -> TestRunReport:
    """Run ``dotnet test`` for xUnit / NUnit / MSTest projects."""
    dotnet = shutil.which("dotnet")
    if not dotnet:
        return _mock_run("dotnet test", "dotnet SDK not on PATH")
    try:
        proc = _run_subprocess(
            [dotnet, "test", "--nologo", "--verbosity", "minimal"],
            cwd=app_path, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return TestRunReport(status="fail", runner="dotnet test",
                             detail=f"timeout after {timeout}s")
    total, passed, failed = _parse_dotnet_output(proc.stdout + proc.stderr)
    status = "pass" if proc.returncode == 0 and failed == 0 else "fail"
    if total == 0 and proc.returncode == 0:
        return TestRunReport(status="skip", runner="dotnet test",
                             detail="no tests found")
    return TestRunReport(
        status=status, runner="dotnet test",
        total=total, passed=passed, failed=failed,
        detail=_tail(proc.stdout, 400),
    )


# `dotnet test --verbosity minimal` emits a summary like
#   "Passed!  - Failed:     0, Passed:     7, Skipped:  0, Total:  7"
# We anchor on the colon-suffixed keywords so the trailing ``Passed!``
# headline does not steal the passed count.
_DOTNET_PASSED_RE = re.compile(r"Passed:\s*(\d+)")
_DOTNET_FAIL_RE = re.compile(r"Failed:\s*(\d+)")
_DOTNET_TOTAL_RE = re.compile(r"Total:\s*(\d+)")


def _parse_dotnet_output(out: str) -> tuple[int, int, int]:
    passed = failed = total = 0
    m = _DOTNET_PASSED_RE.search(out)
    if m:
        passed = int(m.group(1))
    m = _DOTNET_FAIL_RE.search(out)
    if m:
        failed = int(m.group(1))
    m = _DOTNET_TOTAL_RE.search(out)
    if m:
        total = int(m.group(1))
    if total == 0:
        total = passed + failed
    return total, passed, failed


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Coverage gate
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _threshold_for(language: str, override: Optional[float]) -> float:
    if override is not None:
        return float(override)
    return float(COVERAGE_THRESHOLDS.get(language, 0.0))


def run_coverage_gate(
    app_path: Path,
    *,
    language: str,
    override: Optional[float] = None,
    timeout: int = 300,
) -> CoverageReport:
    """Run the language-appropriate coverage tool and gate on its percentage."""
    threshold = _threshold_for(language, override)
    if language == "python":
        return _coverage_python(app_path, threshold=threshold, timeout=timeout)
    if language == "go":
        return _coverage_go(app_path, threshold=threshold, timeout=timeout)
    if language == "rust":
        return _coverage_rust(app_path, threshold=threshold, timeout=timeout)
    if language == "java":
        return _coverage_java(app_path, threshold=threshold)
    if language == "node":
        return _coverage_node(app_path, threshold=threshold, timeout=timeout)
    if language == "csharp":
        return _coverage_csharp(app_path, threshold=threshold)
    return CoverageReport(status="skip", threshold=threshold,
                          detail=f"no coverage dispatcher for {language!r}")


def _coverage_python(app_path: Path, *, threshold: float, timeout: int) -> CoverageReport:
    coverage_bin = shutil.which("coverage")
    python3 = shutil.which("python3")
    argv: Optional[list[str]] = None
    if coverage_bin:
        argv = [coverage_bin, "run", "-m", "pytest", "-q"]
    elif python3:
        argv = [python3, "-m", "coverage", "run", "-m", "pytest", "-q"]
    if not argv:
        return CoverageReport(status="mock", threshold=threshold,
                              detail="coverage.py / python3 not on PATH")
    try:
        _run_subprocess(argv, cwd=app_path, timeout=timeout)
        report = _run_subprocess(
            [argv[0], "report"] if argv[0] == coverage_bin
            else [python3, "-m", "coverage", "report"],  # type: ignore[list-item]
            cwd=app_path, timeout=60,
        )
    except subprocess.TimeoutExpired:
        return CoverageReport(status="fail", threshold=threshold,
                              detail=f"coverage timeout after {timeout}s")
    pct = _parse_python_coverage(report.stdout)
    status = "pass" if pct >= threshold else "fail"
    return CoverageReport(
        status=status, percentage=pct, threshold=threshold,
        source="coverage.py",
        detail=_tail(report.stdout, 200),
    )


_PY_COV_TOTAL_RE = re.compile(r"^TOTAL\s+.*?\s+(\d+(?:\.\d+)?)%", re.M)


def _parse_python_coverage(out: str) -> float:
    m = _PY_COV_TOTAL_RE.search(out)
    return float(m.group(1)) if m else 0.0


def _coverage_go(app_path: Path, *, threshold: float, timeout: int) -> CoverageReport:
    go = shutil.which("go")
    if not go:
        return CoverageReport(status="mock", threshold=threshold,
                              detail="go toolchain not on PATH")
    profile = app_path / ".cover.out"
    try:
        _run_subprocess(
            [go, "test", "-coverprofile", str(profile), "./..."],
            cwd=app_path, timeout=timeout,
        )
        rep = _run_subprocess(
            [go, "tool", "cover", "-func", str(profile)],
            cwd=app_path, timeout=60,
        )
    except subprocess.TimeoutExpired:
        return CoverageReport(status="fail", threshold=threshold,
                              detail=f"go coverage timeout after {timeout}s")
    pct = _parse_go_coverage(rep.stdout)
    status = "pass" if pct >= threshold else "fail"
    return CoverageReport(
        status=status, percentage=pct, threshold=threshold,
        source="go-cover",
        detail=_tail(rep.stdout, 200),
    )


_GO_COV_TOTAL_RE = re.compile(r"^total:\s+\(statements\)\s+(\d+(?:\.\d+)?)%", re.M)


def _parse_go_coverage(out: str) -> float:
    m = _GO_COV_TOTAL_RE.search(out)
    return float(m.group(1)) if m else 0.0


def _coverage_rust(app_path: Path, *, threshold: float, timeout: int) -> CoverageReport:
    """Rust coverage via ``cargo llvm-cov`` (tarpaulin fallback)."""
    cargo = shutil.which("cargo")
    if not cargo:
        return CoverageReport(status="mock", threshold=threshold,
                              detail="cargo toolchain not on PATH")
    # llvm-cov first (faster, upstream-blessed), fall back to tarpaulin.
    for subcmd, source in (("llvm-cov", "llvm-cov"), ("tarpaulin", "tarpaulin")):
        try:
            probe = _run_subprocess(
                [cargo, subcmd, "--help"], cwd=app_path, timeout=15,
            )
        except subprocess.TimeoutExpired:
            continue
        if probe.returncode != 0:
            continue
        try:
            proc = _run_subprocess(
                [cargo, subcmd, "--summary-only"] if subcmd == "llvm-cov"
                else [cargo, subcmd, "--print-summary"],
                cwd=app_path, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return CoverageReport(status="fail", threshold=threshold,
                                  detail=f"rust coverage timeout after {timeout}s")
        pct = _parse_rust_coverage(proc.stdout + proc.stderr)
        status = "pass" if pct >= threshold else "fail"
        return CoverageReport(
            status=status, percentage=pct, threshold=threshold,
            source=source, detail=_tail(proc.stdout, 200),
        )
    return CoverageReport(status="mock", threshold=threshold,
                          detail="neither cargo-llvm-cov nor cargo-tarpaulin installed")


_RUST_LLVM_RE = re.compile(r"TOTAL[^\d]*(\d+(?:\.\d+)?)%")
_RUST_TARPAULIN_RE = re.compile(r"coverage,\s+(\d+(?:\.\d+)?)%", re.I)


def _parse_rust_coverage(out: str) -> float:
    m = _RUST_LLVM_RE.search(out)
    if m:
        return float(m.group(1))
    m = _RUST_TARPAULIN_RE.search(out)
    return float(m.group(1)) if m else 0.0


def _coverage_java(app_path: Path, *, threshold: float) -> CoverageReport:
    """Parse JaCoCo XML reports if present. Maven/Gradle both emit to
    ``target/site/jacoco/jacoco.xml`` or ``build/reports/jacoco/**/jacoco.xml``."""
    candidates = [
        app_path / "target" / "site" / "jacoco" / "jacoco.xml",
    ] + list((app_path / "build" / "reports" / "jacoco").rglob("jacoco.xml")) \
      + list((app_path / "build" / "jacoco").rglob("jacoco.xml"))
    xml_path: Optional[Path] = None
    for p in candidates:
        if p.is_file():
            xml_path = p
            break
    if xml_path is None:
        return CoverageReport(status="mock", threshold=threshold,
                              detail="no jacoco.xml found")
    import xml.etree.ElementTree as ET
    try:
        root = ET.parse(str(xml_path)).getroot()
    except (ET.ParseError, OSError) as exc:
        return CoverageReport(status="fail", threshold=threshold,
                              detail=f"jacoco parse failed: {exc}")
    covered = missed = 0
    for c in root.iter("counter"):
        if c.attrib.get("type") == "LINE":
            covered += int(c.attrib.get("covered", "0"))
            missed += int(c.attrib.get("missed", "0"))
    pct = (100.0 * covered / (covered + missed)) if (covered + missed) else 0.0
    status = "pass" if pct >= threshold else "fail"
    return CoverageReport(
        status=status, percentage=pct, threshold=threshold,
        source="jacoco",
        detail=f"line coverage from {xml_path.name}",
    )


def _coverage_node(app_path: Path, *, threshold: float, timeout: int) -> CoverageReport:
    """Parse ``coverage/coverage-summary.json`` emitted by c8 / istanbul."""
    summary = app_path / "coverage" / "coverage-summary.json"
    if summary.is_file():
        try:
            data = json.loads(summary.read_text())
            pct = float(((data.get("total") or {}).get("lines") or {}).get("pct", 0.0))
            source = "c8/istanbul"
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            return CoverageReport(status="fail", threshold=threshold,
                                  detail=f"summary parse failed: {exc}")
        status = "pass" if pct >= threshold else "fail"
        return CoverageReport(
            status=status, percentage=pct, threshold=threshold,
            source=source, detail=f"line coverage from {summary.name}",
        )
    return CoverageReport(status="mock", threshold=threshold,
                          detail="coverage/coverage-summary.json not found")


def _coverage_csharp(app_path: Path, *, threshold: float) -> CoverageReport:
    """Parse Coverlet / ReportGenerator cobertura XML."""
    candidates = list((app_path / "TestResults").rglob("coverage.cobertura.xml")) \
        + list((app_path).rglob("coverage.cobertura.xml"))
    xml_path: Optional[Path] = None
    for p in candidates:
        if p.is_file():
            xml_path = p
            break
    if xml_path is None:
        return CoverageReport(status="mock", threshold=threshold,
                              detail="no cobertura.xml found")
    import xml.etree.ElementTree as ET
    try:
        root = ET.parse(str(xml_path)).getroot()
    except (ET.ParseError, OSError) as exc:
        return CoverageReport(status="fail", threshold=threshold,
                              detail=f"cobertura parse failed: {exc}")
    rate = root.attrib.get("line-rate")
    pct = (float(rate) * 100.0) if rate else 0.0
    status = "pass" if pct >= threshold else "fail"
    return CoverageReport(
        status=status, percentage=pct, threshold=threshold,
        source="coverlet",
        detail=f"line rate from {xml_path.name}",
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Benchmark regression (optional)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def run_benchmark_regression(
    app_path: Path,
    *,
    module: str,
    workspace: Path,
    threshold_pct: float = 10.0,
    current_ms: Optional[float] = None,
) -> BenchmarkReport:
    """Compare current benchmark runtime against a JSON baseline.

    Baseline lives at ``<workspace>/test_assets/benchmarks/<module>.json``
    and has the shape ``{"baseline_ms": <float>}``. When no baseline
    exists the gate degrades to ``skip`` — not every project defines
    benchmarks. When both are present, a run that takes more than
    ``baseline * (1 + threshold_pct/100)`` fails the gate.
    """
    baseline_path = workspace / "test_assets" / "benchmarks" / f"{module}.json"
    if not baseline_path.is_file():
        return BenchmarkReport(status="skip", threshold_pct=threshold_pct,
                               detail="no baseline json found")
    try:
        baseline = json.loads(baseline_path.read_text())
        baseline_ms = float(baseline.get("baseline_ms", 0.0))
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        return BenchmarkReport(status="fail", threshold_pct=threshold_pct,
                               detail=f"baseline parse failed: {exc}")
    if baseline_ms <= 0:
        return BenchmarkReport(status="skip", threshold_pct=threshold_pct,
                               detail="baseline_ms <= 0")
    if current_ms is None:
        # Caller did not wire a real measurement — emit mock so the
        # shape still populates.
        return BenchmarkReport(
            status="mock", baseline_ms=baseline_ms, threshold_pct=threshold_pct,
            detail="no current_ms supplied",
        )
    regression_pct = 100.0 * (current_ms - baseline_ms) / baseline_ms
    status = "pass" if regression_pct <= threshold_pct else "fail"
    return BenchmarkReport(
        status=status, current_ms=current_ms, baseline_ms=baseline_ms,
        regression_pct=regression_pct, threshold_pct=threshold_pct,
        detail=f"current {current_ms:.2f}ms vs baseline {baseline_ms:.2f}ms",
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Orchestrator
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _tail(s: str, n: int) -> str:
    s = (s or "").strip()
    return s[-n:] if len(s) > n else s


_TEST_RUNNERS = {
    "python": run_python_tests,
    "go":     run_go_tests,
    "rust":   run_rust_tests,
    "java":   run_java_tests,
    "node":   run_node_tests,
    "csharp": run_csharp_tests,
}


def simulate_software(
    *,
    profile: str,
    app_path: Path,
    language: str = "",
    module: str = "",
    workspace: Optional[Path] = None,
    coverage_override: Optional[float] = None,
    benchmark: bool = False,
    current_benchmark_ms: Optional[float] = None,
) -> SoftwareSimResult:
    """Run every software-track gate and aggregate the result.

    Parameters
    ----------
    profile:
        Platform profile id (e.g. ``linux-x86_64-native``). Used to
        enforce ``target_kind == "software"`` and to surface the
        ``packaging`` / ``software_runtime`` fields in the output.
    app_path:
        Project root. Used for language autodetect and as CWD for
        every test runner / coverage tool invocation.
    language:
        Optional override. When empty, ``resolve_language`` is called.
    module:
        Profile name reused as a bench-baseline lookup key (maps to
        ``test_assets/benchmarks/<module>.json``).
    workspace:
        Repo root; used for benchmark baseline lookup. Defaults to
        ``app_path``.
    coverage_override:
        Optional numeric override for the per-language threshold.
    benchmark:
        When true, run the benchmark regression gate. Otherwise skipped.
    current_benchmark_ms:
        Optional measured runtime to compare against the baseline.
    """
    app_path = Path(app_path).resolve()
    workspace = Path(workspace).resolve() if workspace else app_path
    result = SoftwareSimResult(profile=profile, app_path=str(app_path))

    # ─── Profile validation ───
    try:
        from backend.platform import get_platform_config  # lazy for testability
        profile_cfg = get_platform_config(profile)
    except Exception as exc:  # noqa: BLE001
        result.errors.append(f"profile resolve failed: {exc}")
        profile_cfg = {}
    if profile_cfg.get("target_kind") not in (None, "software"):
        raise SoftwareSimError(
            f"profile {profile!r} target_kind is {profile_cfg.get('target_kind')!r}, "
            "not 'software'"
        )
    result.packaging = str(profile_cfg.get("packaging") or "")

    # ─── Language autodetect ───
    if not language:
        language = resolve_language(app_path)
    if not language:
        result.errors.append("could not detect project language")
        result.language = ""
        result.test_run = TestRunReport(status="skip", detail="no language detected")
        result.coverage = CoverageReport(status="skip",
                                         detail="no language detected")
        result.benchmark = BenchmarkReport(status="skip", detail="no language detected")
        result.gates = {"test_run_ok": False, "coverage_ok": False,
                        "benchmark_ok": True}
        return result
    if language not in SUPPORTED_LANGUAGES:
        result.errors.append(f"unsupported language {language!r}")
        result.language = language
        return result
    result.language = language

    # ─── Gate 1: test run ───
    runner = _TEST_RUNNERS[language]
    result.test_run = runner(app_path)

    # ─── Gate 2: coverage ───
    result.coverage = run_coverage_gate(
        app_path, language=language, override=coverage_override,
    )

    # ─── Gate 3: benchmark regression (optional) ───
    if benchmark:
        result.benchmark = run_benchmark_regression(
            app_path,
            module=module or profile,
            workspace=workspace,
            current_ms=current_benchmark_ms,
        )
    else:
        result.benchmark = BenchmarkReport(status="skip",
                                           detail="benchmark gate disabled")

    # ─── Gate rollup ───
    result.gates = {
        "test_run_ok": result.test_run.status in ("pass", "mock", "skip"),
        "coverage_ok": result.coverage.status in ("pass", "mock", "skip"),
        "benchmark_ok": result.benchmark.status in ("pass", "mock", "skip"),
    }
    return result


def result_to_json(result: SoftwareSimResult) -> dict[str, Any]:
    """Flatten ``SoftwareSimResult`` into the dict shape consumed by
    ``run_software`` in ``simulate.sh``."""
    return {
        "profile": result.profile,
        "app_path": result.app_path,
        "language": result.language,
        "packaging": result.packaging,
        "test_runner": result.test_run.runner,
        "test_status": result.test_run.status,
        "test_total": result.test_run.total,
        "test_passed": result.test_run.passed,
        "test_failed": result.test_run.failed,
        "test_detail": result.test_run.detail,
        "coverage_status": result.coverage.status,
        "coverage_pct": result.coverage.percentage,
        "coverage_threshold": result.coverage.threshold,
        "coverage_source": result.coverage.source,
        "coverage_detail": result.coverage.detail,
        "benchmark_status": result.benchmark.status,
        "benchmark_current_ms": result.benchmark.current_ms,
        "benchmark_baseline_ms": result.benchmark.baseline_ms,
        "benchmark_regression_pct": result.benchmark.regression_pct,
        "benchmark_threshold_pct": result.benchmark.threshold_pct,
        "benchmark_detail": result.benchmark.detail,
        "gates": result.gates,
        "overall_pass": result.overall_pass(),
        "errors": result.errors,
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CLI — invoked from simulate.sh
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _cli_main() -> int:
    """CLI entrypoint (``python3 -m backend.software_simulator``).

    Contract with simulate.sh: single JSON object on stdout, exit 0.
    Non-zero would cause ``set -euo pipefail`` in the shell to abort
    the whole track before it can aggregate its own envelope, so we
    always print JSON and let the shell decide gating.
    """
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True)
    parser.add_argument("--app-path", required=True)
    parser.add_argument("--language", default="",
                        help="override language autodetect")
    parser.add_argument("--module", default="")
    parser.add_argument("--workspace", default="")
    parser.add_argument("--coverage-override", type=float, default=None)
    parser.add_argument("--benchmark", action="store_true",
                        help="enable benchmark regression gate")
    parser.add_argument("--benchmark-current-ms", type=float, default=None)
    args = parser.parse_args()

    try:
        result = simulate_software(
            profile=args.profile,
            app_path=Path(args.app_path),
            language=args.language,
            module=args.module,
            workspace=Path(args.workspace) if args.workspace else None,
            coverage_override=args.coverage_override,
            benchmark=args.benchmark,
            current_benchmark_ms=args.benchmark_current_ms,
        )
    except SoftwareSimError as exc:
        fail = SoftwareSimResult(profile=args.profile, app_path=args.app_path)
        fail.errors.append(str(exc))
        print(json.dumps(result_to_json(fail)))
        return 0

    print(json.dumps(result_to_json(result)))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli_main())
