#!/usr/bin/env python3
"""OP-1771 - generate the skill-pack maturity matrix.

The audit deliberately separates planner parseability from real capability.
``planner_parseable`` reuses the embedded planner's existing ``tasks.yaml``
load path; maturity is then derived from static scaffold/backend signals.
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = REPO_ROOT / "configs" / "skills"
BACKEND_DIR = REPO_ROOT / "backend"
DEFAULT_OUTPUT = REPO_ROOT / "docs" / "product" / "skill-pack-maturity-matrix.md"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.embedded_planner import (  # noqa: E402
    _load_tasks_yaml,
    _template_to_task,
    reload_tasks_cache,
)

# The exception family the embedded planner raises when handed a pack whose
# template schema it cannot consume. _template_to_task hard-derefs task_id +
# expected_output (KeyError); broader types cover malformed templates.
_SCHEMA_MISMATCH_EXCEPTIONS = (KeyError, AttributeError, TypeError, ValueError)


SIGNAL_RE = re.compile(
    r"\b(NotImplementedError|stub|synthetic|placeholder|XOR|return\s+True|simulated)\b",
    re.IGNORECASE,
)
SIMULATED_RE = re.compile(r"\b(synthetic|simulated)\b", re.IGNORECASE)
STUB_RE = re.compile(
    r"\b(NotImplementedError|stub|placeholder|XOR|return\s+True)\b",
    re.IGNORECASE,
)
SOURCE_EXTENSIONS = frozenset({
    ".c",
    ".cc",
    ".cpp",
    ".go",
    ".h",
    ".hpp",
    ".java",
    ".js",
    ".jsx",
    ".kt",
    ".m",
    ".mm",
    ".py",
    ".rs",
    ".swift",
    ".ts",
    ".tsx",
})

BACKEND_ALIASES: dict[str, tuple[str, ...]] = {
    "_embedded_base": (),
    "barcode_scanner": ("barcode_scanner.py",),
    "compliance-audit": ("compliance_harness.py",),
    "connectivity": ("connectivity.py",),
    "depth_sensing": ("depth_sensing.py", "machine_vision.py"),
    "enterprise_web": ("enterprise_scaffolder.py",),
    "imaging": ("imaging_pipeline.py", "machine_vision.py"),
    "ipcam": ("ipcam_rtsp_server.py",),
    "ota": ("ota_framework.py",),
    "payment": ("payment_compliance.py",),
    "printing": ("printing.py",),
    "security": ("security_stack.py", "security_hardening.py"),
    "sensor_fusion": ("sensor_fusion.py",),
    "skill-android": ("android_scaffolder.py",),
    "skill-astro": ("astro_scaffolder.py",),
    "skill-desktop-tauri": ("tauri_scaffolder.py",),
    "skill-fastapi": ("fastapi_scaffolder.py",),
    "skill-flutter": ("flutter_scaffolder.py",),
    "skill-go-service": ("go_service_scaffolder.py",),
    "skill-ios": ("ios_scaffolder.py",),
    "skill-nextjs": ("nextjs_scaffolder.py",),
    "skill-nuxt": ("nuxt_scaffolder.py",),
    "skill-rn": ("rn_scaffolder.py",),
    "skill-rust-cli": ("rust_cli_scaffolder.py",),
    "skill-spring-boot": ("spring_boot_scaffolder.py",),
    "telemetry": ("telemetry_backend.py",),
    "uvc": ("uvc_gadget.py",),
}


@dataclass(frozen=True)
class SignalHit:
    path: Path
    line: int
    token: str


@dataclass(frozen=True)
class SkillMaturityRow:
    pack: str
    has_tasks_yaml: bool
    planner_parseable: bool
    parse_source: str
    has_scaffolds: bool
    backend_modules: tuple[Path, ...]
    signal_hits: tuple[SignalHit, ...]
    maturity: str
    notes: str


def _utc_timestamp() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iter_skill_dirs(skills_dir: Path) -> Iterable[Path]:
    for child in sorted(skills_dir.iterdir()):
        if child.is_dir():
            yield child


def _iter_source_files(root: Path) -> Iterable[Path]:
    if root.is_file():
        if root.suffix in SOURCE_EXTENSIONS:
            yield root
        return
    if not root.exists():
        return
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix in SOURCE_EXTENSIONS:
            yield path


def _normalise(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.removeprefix("skill-").lower())


def _matching_backend_modules(pack: str) -> tuple[Path, ...]:
    if pack in BACKEND_ALIASES:
        return tuple(
            BACKEND_DIR / filename
            for filename in BACKEND_ALIASES[pack]
            if (BACKEND_DIR / filename).exists()
        )
    explicit = [
        BACKEND_DIR / filename
        for filename in BACKEND_ALIASES.get(pack, ())
        if (BACKEND_DIR / filename).exists()
    ]
    if explicit:
        return tuple(explicit)

    pack_norm = _normalise(pack)
    matches: list[Path] = []
    for path in sorted(BACKEND_DIR.glob("*.py")):
        stem_norm = _normalise(path.stem)
        if pack_norm and (
            pack_norm in stem_norm
            or (len(stem_norm) >= 4 and stem_norm in pack_norm)
        ):
            matches.append(path)
    return tuple(matches)


def _scan_signals(paths: Iterable[Path]) -> tuple[SignalHit, ...]:
    hits: list[SignalHit] = []
    seen: set[Path] = set()
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line_no, line in enumerate(lines, start=1):
            match = SIGNAL_RE.search(line)
            if match:
                hits.append(SignalHit(path=path, line=line_no, token=match.group(1)))
    return tuple(hits)


def _planner_parseable(pack: str, has_tasks_yaml: bool) -> tuple[bool, str]:
    reload_tasks_cache()
    try:
        tasks = _load_tasks_yaml(pack)
    except Exception as exc:
        return False, f"load-error: {type(exc).__name__}"
    # Loading the YAML is NOT enough: the real planner consumes each template via
    # _template_to_task (and plan_embedded_product), which hard-derefs task_id +
    # expected_output. A pack that loads but uses the id/name/artifacts schema
    # KeyErrors there, so it is NOT planner-parseable even though the file loads.
    try:
        for tmpl in tasks:
            _template_to_task(tmpl)
    except _SCHEMA_MISMATCH_EXCEPTIONS as exc:
        return False, f"schema-mismatch: {type(exc).__name__}"
    return True, "tasks.yaml" if has_tasks_yaml else "_embedded_base fallback"


def _classify_maturity(
    *,
    has_tasks_yaml: bool,
    planner_parseable: bool,
    has_scaffolds: bool,
    has_hil: bool,
    signal_hits: tuple[SignalHit, ...],
) -> str:
    signal_text = " ".join(hit.token for hit in signal_hits)
    has_stub_signal = bool(STUB_RE.search(signal_text))
    has_simulated_signal = bool(SIMULATED_RE.search(signal_text))

    if not has_tasks_yaml and not has_scaffolds:
        return "doc-only"
    if has_stub_signal:
        return "stubbed"
    if has_simulated_signal:
        return "simulated"
    if has_scaffolds and has_hil:
        return "hardware-backed"
    if has_scaffolds:
        return "scaffold-only"
    if planner_parseable:
        return "parseable"
    return "doc-only"


def _rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def audit_skill_pack(skill_dir: Path) -> SkillMaturityRow:
    pack = skill_dir.name
    has_tasks_yaml = (skill_dir / "tasks.yaml").exists()
    scaffold_sources = tuple(_iter_source_files(skill_dir / "scaffolds"))
    has_scaffolds = bool(scaffold_sources)
    has_hil = (skill_dir / "hil").is_dir()
    planner_parseable, parse_source = _planner_parseable(pack, has_tasks_yaml)
    backend_modules = _matching_backend_modules(pack)
    signal_roots: list[Path] = []
    if has_scaffolds:
        signal_roots.append(skill_dir / "scaffolds")
    signal_roots.extend(backend_modules)
    signal_hits = _scan_signals(
        path
        for root in signal_roots
        for path in _iter_source_files(root)
    )
    maturity = _classify_maturity(
        has_tasks_yaml=has_tasks_yaml,
        planner_parseable=planner_parseable,
        has_scaffolds=has_scaffolds,
        has_hil=has_hil,
        signal_hits=signal_hits,
    )
    notes = _notes(signal_hits, backend_modules)
    return SkillMaturityRow(
        pack=pack,
        has_tasks_yaml=has_tasks_yaml,
        planner_parseable=planner_parseable,
        parse_source=parse_source,
        has_scaffolds=has_scaffolds,
        backend_modules=backend_modules,
        signal_hits=signal_hits,
        maturity=maturity,
        notes=notes,
    )


def _notes(
    signal_hits: tuple[SignalHit, ...],
    backend_modules: tuple[Path, ...],
) -> str:
    if signal_hits:
        preview = ", ".join(
            f"{_rel(hit.path)}:{hit.line} ({hit.token})"
            for hit in signal_hits[:4]
        )
        if len(signal_hits) > 4:
            preview += f", +{len(signal_hits) - 4} more"
        return preview
    if backend_modules:
        return "backend: " + ", ".join(_rel(path) for path in backend_modules)
    return ""


def discover_rows(skills_dir: Path = SKILLS_DIR) -> list[SkillMaturityRow]:
    return [audit_skill_pack(skill_dir) for skill_dir in _iter_skill_dirs(skills_dir)]


def _cell(value: object) -> str:
    text = str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


def render_markdown(rows: list[SkillMaturityRow]) -> str:
    lines = [
        "# Skill Pack Maturity Matrix",
        "",
        f"Generated: {_utc_timestamp()}",
        "",
        "Generated by `scripts/skill_pack_maturity_audit.py` from `configs/skills/*` plus matching `backend/*.py` static-signal scans.",
        "",
        "This matrix distinguishes planner parseability from real capability. A pack can parse through the embedded planner and still be `stubbed`, `simulated`, or `scaffold-only`; no row is labelled production-grade.",
        "",
        "Signal scan terms: `NotImplementedError`, `stub`, `synthetic`, `placeholder`, `XOR`, `return True`, `simulated`.",
        "",
        "| skill pack | has_tasks_yaml | planner_parseable | parse_source | scaffolds | maturity | backend_modules | signal evidence |",
        "|---|---:|---:|---|---:|---|---|---|",
    ]
    for row in rows:
        backend_modules = ", ".join(_rel(path) for path in row.backend_modules)
        lines.append(
            "| "
            + " | ".join(
                [
                    _cell(row.pack),
                    _yes_no(row.has_tasks_yaml),
                    _yes_no(row.planner_parseable),
                    _cell(row.parse_source),
                    _yes_no(row.has_scaffolds),
                    _cell(row.maturity),
                    _cell(backend_modules),
                    _cell(row.notes),
                ]
            )
            + " |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate docs/product/skill-pack-maturity-matrix.md."
    )
    parser.add_argument("--skills-dir", type=Path, default=SKILLS_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    rows = discover_rows(args.skills_dir)
    markdown = render_markdown(rows)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(markdown, encoding="utf-8")
    sys.stdout.write(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
