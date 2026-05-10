"""OP-829 static-analysis pre-flight gate."""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from backend.agents.tool_dispatcher import StructuredToolError

log = logging.getLogger(__name__)

PY_EXT = frozenset({".py"})
TS_EXT = frozenset({".ts", ".tsx", ".js", ".jsx"})
DEFAULT_MAX_LINT_ROUNDS = 3
COUNTER_KEY = "lint_round_counter"
PARTIAL_KEY = "lint_partial"
Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class LintDiagnostic:
    file: str
    line: int
    col: int
    code: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "line": self.line,
            "col": self.col,
            "code": self.code,
            "message": self.message,
        }


@dataclass(frozen=True)
class StaticAnalysisResult:
    status: str
    diagnostics: tuple[LintDiagnostic, ...] = ()
    warnings: tuple[str, ...] = ()
    lint_round_counter: int = 0
    lint_partial: bool = False

    @property
    def is_clean(self) -> bool:
        return not self.diagnostics

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "diagnostics": [d.to_dict() for d in self.diagnostics],
            "warnings": list(self.warnings),
            "lint_round_counter": self.lint_round_counter,
            "lint_partial": self.lint_partial,
        }

    def to_model_feedback(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)


def run_static_analysis(
    file_paths: list[str | Path],
    *,
    worktree_root: str | Path,
    progress_path: str | Path | None = None,
    max_rounds: int = DEFAULT_MAX_LINT_ROUNDS,
    runner: Runner = subprocess.run,
) -> StaticAnalysisResult:
    root = Path(worktree_root).resolve()
    targets = _targets(file_paths, root)
    counter = _read_counter(progress_path)
    if not targets:
        return StaticAnalysisResult(status="clean", lint_round_counter=counter)

    warnings: list[str] = []
    diagnostics: list[LintDiagnostic] = []
    py_files = [p for p in targets if p.suffix in PY_EXT]
    ts_files = [p for p in targets if p.suffix in TS_EXT]
    if py_files:
        diagnostics += _run("ruff", ["ruff", "check", "--output-format", "json", *map(str, py_files)], root, runner, _parse_ruff, warnings)
        diagnostics += _run("mypy", ["mypy", "--ignore-missing-imports", "--show-column-numbers", "--no-error-summary", *map(str, py_files)], root, runner, _parse_mypy, warnings)
    if ts_files:
        diagnostics += _run("eslint", ["eslint", "-f", "json", *map(str, ts_files)], root, runner, _parse_eslint, warnings)
        diagnostics += _run("tsc", ["tsc", "--noEmit", "--pretty", "false"], root, runner, lambda cp: _parse_tsc(cp.stdout + "\n" + cp.stderr), warnings)

    if not diagnostics:
        return StaticAnalysisResult(
            status="clean",
            warnings=tuple(warnings),
            lint_round_counter=counter,
        )
    counter += 1
    lint_partial = counter >= max_rounds
    if progress_path is not None:
        _write_progress(Path(progress_path), counter, lint_partial)
    diagnostics.sort(key=lambda d: (d.file, d.line, d.col, d.code, d.message))
    return StaticAnalysisResult(
        status="lint_partial" if lint_partial else "dirty",
        diagnostics=tuple(diagnostics),
        warnings=tuple(warnings),
        lint_round_counter=counter,
        lint_partial=lint_partial,
    )


def wrap_text_editor_with_static_analysis(
    text_editor: Callable[[dict[str, Any]], str],
    *,
    worktree_root: str | Path,
    progress_path: str | Path | None = None,
    runner: Runner = subprocess.run,
) -> Callable[[dict[str, Any]], str]:
    def _wrapped(payload: dict[str, Any]) -> str:
        output = text_editor(payload)
        if payload.get("command") not in {"create", "str_replace", "insert"}:
            return output
        path = payload.get("path")
        if not isinstance(path, str) or not path:
            return output
        result = run_static_analysis(
            [path],
            worktree_root=worktree_root,
            progress_path=progress_path,
            runner=runner,
        )
        if result.is_clean and not result.warnings:
            return output
        return f"{output}\n\nstatic_analysis_gate:\n{result.to_model_feedback()}"

    return _wrapped


def _targets(paths: list[str | Path], root: Path) -> list[Path]:
    out = []
    for raw in paths:
        p = Path(raw).expanduser()
        resolved = (p if p.is_absolute() else root / p).resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise StructuredToolError(
                "sandbox_boundary_violation",
                f"lint_target_outside_worktree: {resolved} is outside {root}",
            ) from exc
        if resolved.suffix in PY_EXT | TS_EXT:
            out.append(resolved)
    return out


def _run(
    binary: str,
    cmd: list[str],
    root: Path,
    runner: Runner,
    parser: Callable[[subprocess.CompletedProcess[str]], list[LintDiagnostic]],
    warnings: list[str],
) -> list[LintDiagnostic]:
    if shutil.which(binary) is None:
        warning = f"linter_binary_missing: {binary}"
        warnings.append(warning)
        log.warning(warning)
        return []
    try:
        completed = runner(cmd, cwd=root, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        warning = f"linter_crash: {binary}: {type(exc).__name__}: {exc}"
        warnings.append(warning)
        log.warning(warning)
        return []
    parsed = parser(completed)
    if completed.returncode and not parsed:
        blob = f"{completed.stdout}\n{completed.stderr}".lower()
        config = "config" in blob and any(s in blob for s in ("conflict", "failed to load", "could not find", "invalid"))
        warning = (
            f"linter_config_conflict: {binary}; using defaults"
            if config
            else f"linter_unavailable: {binary} exited {completed.returncode}"
        )
        warnings.append(warning)
        log.warning(warning)
    return parsed


def _parse_ruff(cp: subprocess.CompletedProcess[str]) -> list[LintDiagnostic]:
    try:
        rows = json.loads(cp.stdout or "[]")
    except json.JSONDecodeError:
        return []
    return [
        LintDiagnostic(
            file=str(row.get("filename") or ""),
            line=int((row.get("location") or {}).get("row") or 1),
            col=int((row.get("location") or {}).get("column") or 1),
            code=str(row.get("code") or "ruff"),
            message=str(row.get("message") or ""),
        )
        for row in rows
        if isinstance(row, dict)
    ]


_MYPY_RE = re.compile(r"^(.*?):(\d+)(?::(\d+))?:\s*(error|note|warning):\s*(.*?)(?:\s*\[([^\]]+)])?$")
_TSC_RE = re.compile(r"^(.+?)\((\d+),(\d+)\):\s*error\s*(TS\d+):\s*(.*)$")


def _parse_mypy(cp: subprocess.CompletedProcess[str]) -> list[LintDiagnostic]:
    out = []
    for line in cp.stdout.splitlines():
        match = _MYPY_RE.match(line)
        if match and match.group(4) == "error":
            out.append(LintDiagnostic(match.group(1), int(match.group(2)), int(match.group(3) or 1), match.group(6) or "mypy", match.group(5)))
    return out


def _parse_eslint(cp: subprocess.CompletedProcess[str]) -> list[LintDiagnostic]:
    try:
        rows = json.loads(cp.stdout or "[]")
    except json.JSONDecodeError:
        return []
    out = []
    for row in rows if isinstance(rows, list) else []:
        for msg in row.get("messages") or []:
            out.append(LintDiagnostic(str(row.get("filePath") or ""), int(msg.get("line") or 1), int(msg.get("column") or 1), str(msg.get("ruleId") or msg.get("fatal") or "eslint"), str(msg.get("message") or "")))
    return out


def _parse_tsc(output: str) -> list[LintDiagnostic]:
    out = []
    for line in output.splitlines():
        match = _TSC_RE.match(line)
        if match:
            out.append(LintDiagnostic(match.group(1), int(match.group(2)), int(match.group(3)), match.group(4), match.group(5)))
    return out


def _read_counter(progress_path: str | Path | None) -> int:
    if progress_path is None:
        return 0
    try:
        for line in Path(progress_path).read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{COUNTER_KEY}="):
                return max(0, int(line.split("=", 1)[1]))
    except (OSError, ValueError):
        return 0
    return 0


def _write_progress(progress_path: Path, counter: int, partial: bool) -> None:
    try:
        lines = progress_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    values = {COUNTER_KEY: str(counter), PARTIAL_KEY: "true" if partial else "false"}
    seen: set[str] = set()
    updated = []
    for line in lines:
        key = line.split("=", 1)[0]
        updated.append(f"{key}={values[key]}" if key in values else line)
        seen.add(key)
    updated.extend(f"{key}={value}" for key, value in values.items() if key not in seen)
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    progress_path.write_text("\n".join(updated) + "\n", encoding="utf-8")


__all__ = [
    "DEFAULT_MAX_LINT_ROUNDS",
    "LintDiagnostic",
    "StaticAnalysisResult",
    "run_static_analysis",
    "wrap_text_editor_with_static_analysis",
]
