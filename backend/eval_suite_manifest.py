"""OP-2573 U4-H — strict eval-suite manifest loader + private holdout seam.

Why this module exists: ``iq_benchmark.load_all()`` silently weakens the
suite — a missing directory yields ``[]`` and a corrupt shard is
skip-with-log. An attacker (or plain rot) removing/corrupting shards
would make the eval EASIER with no signal. Every U4-F eval MUST load its
suite through this module instead: one bad shard invalidates the whole
load (typed ``SuiteManifestError``), never a partial result, never
skip-and-continue.

Manifest extension note (deliberate): the shipped manifest lives at
``configs/iq_benchmark/manifest.yml`` with a ``.yml`` extension ON
PURPOSE. The legacy ``load_all()`` globs ``*.yaml``; a ``.yaml`` manifest
would be picked up there, fail its strict benchmark parse, and emit one
ERROR log line on every nightly/finetune load. ``.yml`` escapes the glob
— zero noise, zero behavioral delta for existing consumers.

Neg-control semantics: a question flagged ``neg_control: true`` in the
shard YAML carries an injection-shaped payload and the CANDIDATE MUST
FAIL it. Enforcement of that contract is U4-F's executor; this module
only guarantees presence + counts (``min_neg_controls``) and exposes the
flagged ids per shard. ``iq_benchmark.IQQuestion`` has no neg-control
field — the flag is detected here from the raw shard YAML, without
touching iq_benchmark.py.

Purity: file I/O + hashlib + yaml only — no DB, no network, and no clock
reads except ``emit_eval_heartbeat`` (the ONE sanctioned clock read).
"""
from __future__ import annotations

import hashlib
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

from backend.iq_benchmark import IQQuestion, load_benchmark

SUPPORTED_SCHEMA_VERSION = 1
HOLDOUT_DIR_ENV = "OMNISIGHT_EVAL_HOLDOUT_DIR"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class SuiteManifestError(Exception):
    """Typed fail-closed error. ``reason`` is a machine-checkable code,
    optionally suffixed ``:<shard-path>`` for per-shard failures."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class SuiteEntry:
    path: str
    sha256: str
    min_cases: int
    min_neg_controls: int


@dataclass(frozen=True)
class SuiteManifest:
    suites: tuple[SuiteEntry, ...]


@dataclass(frozen=True)
class VerifiedSuites:
    """Result of a fully verified suite load.

    ``questions`` is keyed by manifest-relative shard path. A path never
    appears here unless its shard passed EVERY check — hash, strict
    parse, min_cases, min_neg_controls.
    """

    questions: dict[str, list[IQQuestion]]
    _neg_controls: dict[str, frozenset[str]]

    def neg_control_ids(self, path: str) -> frozenset[str]:
        return self._neg_controls.get(path, frozenset())


def _require_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SuiteManifestError("bad_entry")
    return value


def _coerce_entry(raw: object) -> SuiteEntry:
    if not isinstance(raw, dict):
        raise SuiteManifestError("bad_entry")
    try:
        path = raw["path"]
        sha256 = raw["sha256"]
        min_cases = raw["min_cases"]
        min_neg_controls = raw["min_neg_controls"]
    except KeyError:
        raise SuiteManifestError("bad_entry") from None
    if not isinstance(path, str) or not isinstance(sha256, str):
        raise SuiteManifestError("bad_entry")
    if not _SHA256_RE.match(sha256):
        raise SuiteManifestError("bad_sha")
    return SuiteEntry(
        path=path,
        sha256=sha256,
        min_cases=_require_int(min_cases),
        min_neg_controls=_require_int(min_neg_controls),
    )


def load_manifest(manifest_path: Path) -> SuiteManifest:
    """Load + validate the manifest YAML. Rejects (typed reasons):
    missing file (``manifest_missing``), wrong schema_version
    (``bad_schema_version``), empty suites (``empty_manifest``),
    non-64-hex sha (``bad_sha``), duplicate paths (``duplicate_path``),
    structurally malformed entries (``bad_entry``)."""
    if not manifest_path.is_file():
        raise SuiteManifestError("manifest_missing")
    try:
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        raise SuiteManifestError("bad_entry") from None
    if not isinstance(raw, dict):
        raise SuiteManifestError("bad_entry")
    if raw.get("schema_version") != SUPPORTED_SCHEMA_VERSION:
        raise SuiteManifestError("bad_schema_version")
    suites_raw = raw.get("suites")
    if suites_raw is not None and not isinstance(suites_raw, list):
        raise SuiteManifestError("bad_entry")
    if not suites_raw:
        raise SuiteManifestError("empty_manifest")
    entries = tuple(_coerce_entry(e) for e in suites_raw)
    paths = [e.path for e in entries]
    if len(paths) != len(set(paths)):
        raise SuiteManifestError("duplicate_path")
    return SuiteManifest(suites=entries)


def _raw_neg_control_ids(shard_bytes: bytes) -> frozenset[str]:
    raw = yaml.safe_load(shard_bytes.decode("utf-8")) or {}
    flagged: set[str] = set()
    for q in raw.get("questions") or []:
        if isinstance(q, dict) and q.get("neg_control") is True:
            flagged.add(str(q["id"]))
    return frozenset(flagged)


def _verify(
    manifest: SuiteManifest, base_dir: Path
) -> tuple[VerifiedSuites, list[bytes]]:
    """Verify EVERY manifest entry, fail-closed. Returns the verified
    suites plus each shard's bytes in manifest order (for
    ``suite_sha256``). Any failure raises — never a partial result."""
    questions: dict[str, list[IQQuestion]] = {}
    neg_controls: dict[str, frozenset[str]] = {}
    ordered_bytes: list[bytes] = []
    for entry in manifest.suites:
        shard_path = base_dir / entry.path
        if not shard_path.is_file():
            raise SuiteManifestError(f"shard_missing:{entry.path}")
        shard_bytes = shard_path.read_bytes()
        if hashlib.sha256(shard_bytes).hexdigest() != entry.sha256:
            raise SuiteManifestError(f"hash_mismatch:{entry.path}")
        try:
            benchmark = load_benchmark(shard_path)
        except Exception as exc:
            raise SuiteManifestError(f"bad_shard:{entry.path}") from exc
        if len(benchmark.questions) < entry.min_cases:
            raise SuiteManifestError(f"too_few_cases:{entry.path}")
        flagged = _raw_neg_control_ids(shard_bytes)
        if len(flagged) < entry.min_neg_controls:
            raise SuiteManifestError(f"too_few_neg_controls:{entry.path}")
        questions[entry.path] = benchmark.questions
        neg_controls[entry.path] = flagged
        ordered_bytes.append(shard_bytes)
    return (
        VerifiedSuites(questions=questions, _neg_controls=neg_controls),
        ordered_bytes,
    )


def load_verified_suites(
    *, manifest_path: Path, base_dir: Path
) -> VerifiedSuites:
    """Strict, fail-closed suite load: every manifest entry's shard must
    exist, hash-match the manifest sha256, parse via the strict
    ``iq_benchmark.load_benchmark``, and satisfy ``min_cases`` /
    ``min_neg_controls``. ONE BAD SHARD INVALIDATES ALL."""
    manifest = load_manifest(manifest_path)
    verified, _ = _verify(manifest, base_dir)
    return verified


def suite_sha256(manifest_path: Path, base_dir: Path) -> str:
    """Deterministic identity of the verified suite: sha256 over the
    canonical concatenation of manifest bytes + each verified shard's
    bytes in manifest order. This is the ``suite_sha256`` value U4-F
    writes to the eval-run row. Raises on any verification failure —
    the same fail-closed path as ``load_verified_suites``."""
    manifest = load_manifest(manifest_path)
    _, ordered_bytes = _verify(manifest, base_dir)
    digest = hashlib.sha256()
    digest.update(manifest_path.read_bytes())
    for shard_bytes in ordered_bytes:
        digest.update(shard_bytes)
    return digest.hexdigest()


def resolve_holdout_dir() -> Path | None:
    """Private-holdout seam: directory from ``OMNISIGHT_EVAL_HOLDOUT_DIR``,
    or None when unset. The holdout lives OUTSIDE the repo by design —
    an in-repo suite is an answer key."""
    raw = os.environ.get(HOLDOUT_DIR_ENV)
    return Path(raw) if raw else None


def load_holdout(*, manifest_name: str = "manifest.yml") -> VerifiedSuites | None:
    """Load the private holdout suite with the SAME strict verification,
    from the external dir named by ``OMNISIGHT_EVAL_HOLDOUT_DIR``.

    Returns None when the env var is unset. Callers MUST treat None as
    ``holdout_unavailable`` — a LOUD terminal outcome for any eval
    claiming holdout coverage, never a silent pass-through."""
    holdout_dir = resolve_holdout_dir()
    if holdout_dir is None:
        return None
    return load_verified_suites(
        manifest_path=holdout_dir / manifest_name, base_dir=holdout_dir
    )


def emit_eval_heartbeat() -> None:
    """Set the eval liveness heartbeat gauge to the current unix time —
    the ONE sanctioned clock read in this module. The gauge is accessed
    attribute-style at call time (metrics rebinds globals on re-init and
    tests monkeypatch the attribute). External absence-alert wiring is
    operator/U4-J work — this ships the emitter, not the activation."""
    from backend import metrics

    metrics.memory_liveness_heartbeat.set(time.time())


def is_heartbeat_stale(last_ts: float, now: float, slo_seconds: float) -> bool:
    """Pure staleness predicate: strictly greater than the SLO window."""
    return (now - last_ts) > slo_seconds
