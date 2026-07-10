"""OP-2573 U4-H — strict eval-suite manifest loader tests (offline).

Covers the ticket AC (a)-(j): manifest load matrix, verified happy
path, the fail-closed matrix (one bad shard invalidates ALL — assert
the exception, never a partial result), neg-control detection,
suite_sha256 determinism/sensitivity, the private-holdout seam, the
liveness heartbeat, the REAL repo manifest tripwire, dormancy, and the
additive proof that ``load_all()`` never sees the ``.yml`` manifest.

All tmp_path shards are REAL IQQuestion-valid YAML (the shape of
configs/iq_benchmark/firmware-debug.yaml). No skips.
"""
from __future__ import annotations

import hashlib
import time
from pathlib import Path

import pytest
import yaml

from backend.eval_suite_manifest import (
    HOLDOUT_DIR_ENV,
    SuiteEntry,
    SuiteManifest,
    SuiteManifestError,
    VerifiedSuites,
    emit_eval_heartbeat,
    is_heartbeat_stale,
    load_holdout,
    load_manifest,
    load_verified_suites,
    resolve_holdout_dir,
    suite_sha256,
)
from backend.iq_benchmark import IQQuestion, load_all, load_benchmark

BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
REAL_SUITE_DIR = PROJECT_ROOT / "configs" / "iq_benchmark"
REAL_MANIFEST = REAL_SUITE_DIR / "manifest.yml"

GOOD_SHA = "a" * 64


# ━━ fixtures / helpers ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _shard_yaml(n: int = 10, neg_ids: tuple[str, ...] = (),
                name: str = "tmp-shard", salt: str = "") -> str:
    """A REAL IQQuestion-valid shard (firmware-debug.yaml shape)."""
    lines = ["schema_version: 1", f'name: "{name}"', "questions:"]
    for i in range(1, n + 1):
        qid = f"q{i:02d}"
        lines += [
            f"  - id: {qid}",
            f'    prompt: "What is the safe pattern for case {i}{salt}?"',
            '    expected_keywords: ["bounds", "length"]',
        ]
        if qid in neg_ids:
            lines.append("    neg_control: true")
    return "\n".join(lines) + "\n"


def _write_manifest(dir_path: Path, entries: list[dict],
                    schema_version: int = 1,
                    name: str = "manifest.yml") -> Path:
    p = dir_path / name
    p.write_text(
        yaml.safe_dump({"schema_version": schema_version, "suites": entries}),
        encoding="utf-8",
    )
    return p


def _build_suite(dir_path: Path, shards: dict[str, str],
                 min_cases: int = 10, min_neg_controls: int = 0) -> Path:
    """Write shards + a manifest with REAL computed sha256s."""
    entries = []
    for fname, text in shards.items():
        shard = dir_path / fname
        shard.write_text(text, encoding="utf-8")
        entries.append({
            "path": fname,
            "sha256": hashlib.sha256(shard.read_bytes()).hexdigest(),
            "min_cases": min_cases,
            "min_neg_controls": min_neg_controls,
        })
    return _write_manifest(dir_path, entries)


def _entry(path: str = "s.yaml", sha256: str = GOOD_SHA,
           min_cases: int = 1, min_neg_controls: int = 0, **extra) -> dict:
    return {"path": path, "sha256": sha256, "min_cases": min_cases,
            "min_neg_controls": min_neg_controls, **extra}


# ━━ (a) manifest load matrix ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestManifestLoadMatrix:
    def test_good_manifest(self, tmp_path: Path) -> None:
        p = _write_manifest(tmp_path, [_entry("a.yaml"), _entry("b.yaml")])
        m = load_manifest(p)
        assert isinstance(m, SuiteManifest)
        assert m.suites == (
            SuiteEntry("a.yaml", GOOD_SHA, 1, 0),
            SuiteEntry("b.yaml", GOOD_SHA, 1, 0),
        )

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(SuiteManifestError) as ei:
            load_manifest(tmp_path / "nope.yml")
        assert ei.value.reason == "manifest_missing"

    def test_bad_schema_version(self, tmp_path: Path) -> None:
        p = _write_manifest(tmp_path, [_entry()], schema_version=2)
        with pytest.raises(SuiteManifestError) as ei:
            load_manifest(p)
        assert ei.value.reason == "bad_schema_version"

    def test_empty_suites(self, tmp_path: Path) -> None:
        p = _write_manifest(tmp_path, [])
        with pytest.raises(SuiteManifestError) as ei:
            load_manifest(p)
        assert ei.value.reason == "empty_manifest"

    def test_absent_suites_key(self, tmp_path: Path) -> None:
        p = tmp_path / "manifest.yml"
        p.write_text("schema_version: 1\n", encoding="utf-8")
        with pytest.raises(SuiteManifestError) as ei:
            load_manifest(p)
        assert ei.value.reason == "empty_manifest"

    @pytest.mark.parametrize("bad_sha", [
        "xyz", "A" * 64, "a" * 63, "a" * 65, "g" * 64, ""
    ])
    def test_bad_sha_format(self, tmp_path: Path, bad_sha: str) -> None:
        p = _write_manifest(tmp_path, [_entry(sha256=bad_sha)])
        with pytest.raises(SuiteManifestError) as ei:
            load_manifest(p)
        assert ei.value.reason == "bad_sha"

    def test_duplicate_path(self, tmp_path: Path) -> None:
        p = _write_manifest(tmp_path, [_entry("dup.yaml"), _entry("dup.yaml")])
        with pytest.raises(SuiteManifestError) as ei:
            load_manifest(p)
        assert ei.value.reason == "duplicate_path"

    @pytest.mark.parametrize("entries", [
        [{"path": "s.yaml", "sha256": GOOD_SHA, "min_cases": 1}],  # key gone
        [{"sha256": GOOD_SHA, "min_cases": 1, "min_neg_controls": 0}],
        [_entry(min_cases="ten")],       # mistyped int
        [_entry(min_neg_controls=True)],  # bool is not an int here
        [_entry(path=7)],                 # mistyped path
        ["not-a-dict"],                   # non-dict entry
    ])
    def test_bad_entry_never_bare_keyerror(
            self, tmp_path: Path, entries: list) -> None:
        p = _write_manifest(tmp_path, entries)
        with pytest.raises(SuiteManifestError) as ei:
            load_manifest(p)
        assert ei.value.reason == "bad_entry"

    def test_non_list_suites_is_bad_entry(self, tmp_path: Path) -> None:
        p = tmp_path / "manifest.yml"
        p.write_text(
            "schema_version: 1\nsuites: not-a-list\n", encoding="utf-8"
        )
        with pytest.raises(SuiteManifestError) as ei:
            load_manifest(p)
        assert ei.value.reason == "bad_entry"

    def test_unparseable_yaml_is_typed(self, tmp_path: Path) -> None:
        p = tmp_path / "manifest.yml"
        p.write_text("schema_version: 1\nsuites: [unclosed", encoding="utf-8")
        with pytest.raises(SuiteManifestError) as ei:
            load_manifest(p)
        assert ei.value.reason == "bad_entry"


# ━━ (b) verified load happy path ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestVerifiedLoadHappyPath:
    def test_two_shards_real_parse(self, tmp_path: Path) -> None:
        manifest = _build_suite(tmp_path, {
            "alpha.yaml": _shard_yaml(10, name="alpha"),
            "beta.yaml": _shard_yaml(12, name="beta"),
        })
        vs = load_verified_suites(manifest_path=manifest, base_dir=tmp_path)
        assert isinstance(vs, VerifiedSuites)
        assert set(vs.questions) == {"alpha.yaml", "beta.yaml"}
        assert len(vs.questions["alpha.yaml"]) == 10
        assert len(vs.questions["beta.yaml"]) == 12
        # The REAL iq_benchmark parse produced the real dataclass.
        q = vs.questions["alpha.yaml"][0]
        assert isinstance(q, IQQuestion)
        assert q.id == "q01"
        assert q.expected_keywords == ["bounds", "length"]
        # Equal to a direct load_benchmark of the same shard.
        direct = load_benchmark(tmp_path / "alpha.yaml")
        assert vs.questions["alpha.yaml"] == direct.questions


# ━━ (c) fail-closed matrix — assert exception, NEVER a partial dict ━━


class TestFailClosed:
    def _suite(self, tmp_path: Path, **kw) -> Path:
        return _build_suite(tmp_path, {
            "good.yaml": _shard_yaml(10, name="good"),
            "other.yaml": _shard_yaml(10, name="other"),
        }, **kw)

    def test_corrupt_one_byte_hash_mismatch_nothing_returned(
            self, tmp_path: Path) -> None:
        manifest = self._suite(tmp_path)
        shard = tmp_path / "other.yaml"
        shard.write_bytes(shard.read_bytes() + b"#\n")  # still valid YAML
        with pytest.raises(SuiteManifestError) as ei:
            load_verified_suites(manifest_path=manifest, base_dir=tmp_path)
        assert ei.value.reason == "hash_mismatch:other.yaml"

    def test_deleted_shard_raises(self, tmp_path: Path) -> None:
        manifest = self._suite(tmp_path)
        (tmp_path / "good.yaml").unlink()
        with pytest.raises(SuiteManifestError) as ei:
            load_verified_suites(manifest_path=manifest, base_dir=tmp_path)
        assert ei.value.reason == "shard_missing:good.yaml"

    def test_below_min_cases_raises(self, tmp_path: Path) -> None:
        manifest = self._suite(tmp_path, min_cases=11)
        with pytest.raises(SuiteManifestError) as ei:
            load_verified_suites(manifest_path=manifest, base_dir=tmp_path)
        assert ei.value.reason == "too_few_cases:good.yaml"

    def test_below_min_neg_controls_raises(self, tmp_path: Path) -> None:
        manifest = self._suite(tmp_path, min_neg_controls=1)
        with pytest.raises(SuiteManifestError) as ei:
            load_verified_suites(manifest_path=manifest, base_dir=tmp_path)
        assert ei.value.reason == "too_few_neg_controls:good.yaml"

    def test_unparseable_shard_hash_matched_raises_typed(
            self, tmp_path: Path) -> None:
        # Hash matches (manifest built AFTER corruption) but the strict
        # iq_benchmark parse fails → still fail-closed, still typed.
        manifest = _build_suite(tmp_path, {
            "bad.yaml": "schema_version: 1\nquestions: []\n",
        }, min_cases=0)
        with pytest.raises(SuiteManifestError) as ei:
            load_verified_suites(manifest_path=manifest, base_dir=tmp_path)
        assert ei.value.reason == "bad_shard:bad.yaml"

    def test_suite_sha256_same_fail_closed_path(self, tmp_path: Path) -> None:
        manifest = self._suite(tmp_path)
        shard = tmp_path / "other.yaml"
        shard.write_bytes(shard.read_bytes() + b"#\n")
        with pytest.raises(SuiteManifestError) as ei:
            suite_sha256(manifest, tmp_path)
        assert ei.value.reason == "hash_mismatch:other.yaml"


# ━━ (d) neg-control detection ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestNegControlDetection:
    def test_two_flagged_exact_set(self, tmp_path: Path) -> None:
        manifest = _build_suite(tmp_path, {
            "neg.yaml": _shard_yaml(10, neg_ids=("q03", "q07"), name="neg"),
            "plain.yaml": _shard_yaml(10, name="plain"),
        }, min_neg_controls=0)
        vs = load_verified_suites(manifest_path=manifest, base_dir=tmp_path)
        assert vs.neg_control_ids("neg.yaml") == frozenset({"q03", "q07"})
        assert vs.neg_control_ids("plain.yaml") == frozenset()

    def test_unknown_path_empty_frozenset(self, tmp_path: Path) -> None:
        manifest = _build_suite(
            tmp_path, {"s.yaml": _shard_yaml(10)}
        )
        vs = load_verified_suites(manifest_path=manifest, base_dir=tmp_path)
        assert vs.neg_control_ids("never-heard-of-it.yaml") == frozenset()

    def test_min_neg_controls_satisfied_by_flags(self, tmp_path: Path) -> None:
        manifest = _build_suite(tmp_path, {
            "neg.yaml": _shard_yaml(10, neg_ids=("q01", "q02"), name="neg"),
        }, min_neg_controls=2)
        vs = load_verified_suites(manifest_path=manifest, base_dir=tmp_path)
        assert len(vs.neg_control_ids("neg.yaml")) == 2


# ━━ (e) suite_sha256 determinism + sensitivity ━━━━━━━━━━━━━━━━━━━━━━━


class TestSuiteSha256:
    def test_deterministic_and_64_hex(self, tmp_path: Path) -> None:
        manifest = _build_suite(tmp_path, {"s.yaml": _shard_yaml(10)})
        first = suite_sha256(manifest, tmp_path)
        second = suite_sha256(manifest, tmp_path)
        assert first == second
        assert len(first) == 64
        assert all(c in "0123456789abcdef" for c in first)

    def test_shard_byte_change_changes_value(self, tmp_path: Path) -> None:
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        ma = _build_suite(a, {"s.yaml": _shard_yaml(10)})
        # One byte of shard content differs; manifest sha recomputed.
        mb = _build_suite(b, {"s.yaml": _shard_yaml(10, salt="!")})
        assert suite_sha256(ma, a) != suite_sha256(mb, b)

    def test_manifest_only_change_changes_value(self, tmp_path: Path) -> None:
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        ma = _build_suite(a, {"s.yaml": _shard_yaml(10)}, min_cases=10)
        mb = _build_suite(b, {"s.yaml": _shard_yaml(10)}, min_cases=9)
        assert (a / "s.yaml").read_bytes() == (b / "s.yaml").read_bytes()
        assert suite_sha256(ma, a) != suite_sha256(mb, b)


# ━━ (f) private-holdout seam ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestHoldoutSeam:
    def test_env_unset_returns_none(self, monkeypatch) -> None:
        monkeypatch.delenv(HOLDOUT_DIR_ENV, raising=False)
        assert resolve_holdout_dir() is None
        assert load_holdout() is None

    def test_env_set_loads_external_dir_strictly(
            self, tmp_path: Path, monkeypatch) -> None:
        _build_suite(tmp_path, {"ho.yaml": _shard_yaml(10, name="ho")})
        monkeypatch.setenv(HOLDOUT_DIR_ENV, str(tmp_path))
        assert resolve_holdout_dir() == tmp_path
        vs = load_holdout()
        assert vs is not None
        assert set(vs.questions) == {"ho.yaml"}
        assert len(vs.questions["ho.yaml"]) == 10

    def test_holdout_verification_is_the_same_strict_path(
            self, tmp_path: Path, monkeypatch) -> None:
        _build_suite(tmp_path, {"ho.yaml": _shard_yaml(10, name="ho")})
        shard = tmp_path / "ho.yaml"
        shard.write_bytes(shard.read_bytes() + b"#\n")
        monkeypatch.setenv(HOLDOUT_DIR_ENV, str(tmp_path))
        with pytest.raises(SuiteManifestError) as ei:
            load_holdout()
        assert ei.value.reason == "hash_mismatch:ho.yaml"

    def test_holdout_missing_manifest_raises(
            self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv(HOLDOUT_DIR_ENV, str(tmp_path))
        with pytest.raises(SuiteManifestError) as ei:
            load_holdout()
        assert ei.value.reason == "manifest_missing"


# ━━ (g) liveness heartbeat ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestHeartbeat:
    def test_emit_sets_gauge_to_now(self, monkeypatch) -> None:
        from backend import metrics

        calls: list[float] = []

        class _FakeGauge:
            def set(self, value: float) -> None:
                calls.append(value)

        # Attribute-style access at call time makes this monkeypatch
        # visible to the emitter (the pinned access pattern).
        monkeypatch.setattr(metrics, "memory_liveness_heartbeat", _FakeGauge())
        before = time.time()
        emit_eval_heartbeat()
        after = time.time()
        assert len(calls) == 1
        assert before <= calls[0] <= after

    def test_emit_on_real_gauge_noop_safe(self) -> None:
        # Phase-S no-op-safe: gauge or _NoOp, .set is callable either
        # way and emit must not raise.
        from backend import metrics

        assert hasattr(metrics, "memory_liveness_heartbeat")
        assert callable(metrics.memory_liveness_heartbeat.set)
        emit_eval_heartbeat()

    def test_stale_is_strictly_greater(self) -> None:
        assert is_heartbeat_stale(100.0, 160.0, 60.0) is False  # boundary
        assert is_heartbeat_stale(100.0, 160.001, 60.0) is True
        assert is_heartbeat_stale(100.0, 120.0, 60.0) is False
        assert is_heartbeat_stale(100.0, 100.0, 0.0) is False
        assert is_heartbeat_stale(100.0, 100.5, 0.0) is True


# ━━ (h) the REAL repo manifest verifies clean (the tripwire) ━━━━━━━━━


class TestRealRepoManifest:
    def test_real_manifest_verifies_clean(self) -> None:
        # If a shard changes later without a manifest bump, THIS test
        # goes red = the tripwire working.
        vs = load_verified_suites(
            manifest_path=REAL_MANIFEST, base_dir=REAL_SUITE_DIR
        )
        assert set(vs.questions) == {
            "firmware-debug.yaml", "holdout-finetune.yaml"
        }
        assert len(vs.questions["firmware-debug.yaml"]) == 10
        assert len(vs.questions["holdout-finetune.yaml"]) == 10
        # Existing shards predate neg-controls: none flagged.
        assert vs.neg_control_ids("firmware-debug.yaml") == frozenset()
        assert vs.neg_control_ids("holdout-finetune.yaml") == frozenset()

    def test_real_suite_sha256_shape(self) -> None:
        value = suite_sha256(REAL_MANIFEST, REAL_SUITE_DIR)
        assert len(value) == 64
        assert all(c in "0123456789abcdef" for c in value)


# ━━ (i) dormant — no caller until U4-F ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDormantShip:
    def test_no_non_test_module_references_the_loader(self) -> None:
        own = {"eval_suite_manifest.py", "memory_promotion_eval.py"}
        offenders: list[str] = []
        for py in BACKEND_ROOT.rglob("*.py"):
            rel = py.relative_to(BACKEND_ROOT)
            parts = rel.parts
            if parts[0] in ("tests", "node_modules") or (
                parts[:2] == ("alembic", "versions")
            ):
                continue
            if len(parts) == 1 and parts[0] in own:
                continue
            if "eval_suite_manifest" in py.read_text(errors="ignore"):
                offenders.append(str(rel))
        assert offenders == [], f"dormant-ship violated by: {offenders}"


# ━━ (j) ADDITIVE proof — load_all() never sees the .yml manifest ━━━━━


class TestAdditiveProof:
    def test_load_all_deep_equals_yaml_glob(self) -> None:
        via_load_all = load_all(REAL_SUITE_DIR)
        via_glob = [
            load_benchmark(p) for p in sorted(REAL_SUITE_DIR.glob("*.yaml"))
        ]
        assert via_load_all == via_glob
        # The .yml manifest is invisible to the legacy glob.
        assert "manifest.yml" not in {
            p.name for p in REAL_SUITE_DIR.glob("*.yaml")
        }
        assert (REAL_SUITE_DIR / "manifest.yml").is_file()
